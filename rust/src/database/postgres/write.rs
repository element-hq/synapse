/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 */

//! Write transactions on the native pool, for Rust code that writes many
//! rows to several tables in one transaction.
//!
//! [`run_write_interaction`] is the Rust `runInteraction`: it opens a
//! `REPEATABLE READ` transaction (Python's default isolation), runs the
//! closure, commits, and retries the whole thing on a serialization failure,
//! a deadlock, a cancelled statement or a lost connection, up to five
//! attempts, as
//! `DatabasePool.new_transaction` does.
//!
//! Inside, [`WriteTxn::insert_many`] and [`WriteTxn::upsert_many`] write a
//! batch of rows in one statement however many there are, by passing each
//! column as an array and selecting from `UNNEST` of them. Statements that
//! need a per-row sub-select go through [`WriteTxn::execute_batch`], which
//! pipelines them on the connection.
//!
//! Round trips are what a write transaction mostly waits on, so it sends as
//! much as it can together:
//!
//! - The `BEGIN` goes out with the first statement.
//! - A write whose result nobody reads can be queued with one of the
//!   `*_later` methods. Queued writes go out, in order, in the same round
//!   trip as the next statement that is not queued, or as the `COMMIT`. A
//!   failure surfaces there, reported as the queued statement's own.
//! - Statements not yet prepared on the connection are prepared together.

use std::{
    cell::RefCell,
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicBool, AtomicU8, Ordering},
        Arc, Mutex,
    },
    task::Poll,
    time::{Duration, Instant},
};

use async_trait::async_trait;
use futures::{
    future::{join, maybe_done, poll_fn, BoxFuture},
    FutureExt,
};
use log::warn;
use tokio::runtime::Handle;
use tokio_postgres::{types::ToSql, Row};

use super::pool::{ConnectionPool, PooledConnection};
use crate::storage::events::Querier;

/// Python's `MAX_NUMBER_OF_ATTEMPTS`.
pub const MAX_ATTEMPTS: u32 = 5;

/// Values of [`WriteTxn::failed`].
const NOT_FAILED: u8 = 0;
const FAILED: u8 = 1;
const FAILED_RETRYABLE: u8 = 2;

/// Why a write failed.
#[derive(Debug)]
pub enum WriteError {
    /// A constraint was violated (SQLSTATE class `23`). Callers that expect
    /// one, such as a conflicting insert, can tell it apart.
    Constraint {
        /// The SQLSTATE, such as `23505` for a unique violation.
        sqlstate: String,
        message: String,
    },
    /// A serialization failure (`40001`) or deadlock (`40P01`): the
    /// transaction can be run again.
    Conflict(String),
    /// The connection was lost: the transaction can be run again on another.
    ConnectionLost(String),
    /// A statement was cancelled (`57014`), for example by
    /// `statement_timeout`. Run again, as Python does.
    Cancelled(String),
    /// Anything else.
    Other(anyhow::Error),
}

/// How [`WriteError::from_pg`] classifies a database error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PgErrorKind {
    Conflict,
    Cancelled,
    Constraint,
    ConnectionLost,
    Other,
}

impl PgErrorKind {
    fn of(error: &tokio_postgres::Error) -> Self {
        match error.code().map(|code| code.code()) {
            Some("40001" | "40P01") => PgErrorKind::Conflict,
            Some("57014") => PgErrorKind::Cancelled,
            Some(code) if code.starts_with("23") => PgErrorKind::Constraint,
            Some(code) if ["08", "57", "58"].iter().any(|c| code.starts_with(c)) => {
                PgErrorKind::ConnectionLost
            }
            None if error.is_closed() => PgErrorKind::ConnectionLost,
            _ => PgErrorKind::Other,
        }
    }

    fn is_retryable(self) -> bool {
        matches!(
            self,
            PgErrorKind::Conflict | PgErrorKind::Cancelled | PgErrorKind::ConnectionLost
        )
    }
}

impl WriteError {
    fn from_pg(context: &str, error: tokio_postgres::Error) -> Self {
        let message = match error.as_db_error() {
            Some(db) => format!("{context}: {} ({})", db.message(), db.code().code()),
            None => format!("{context}: {error}"),
        };
        match PgErrorKind::of(&error) {
            PgErrorKind::Conflict => WriteError::Conflict(message),
            PgErrorKind::Cancelled => WriteError::Cancelled(message),
            PgErrorKind::Constraint => WriteError::Constraint {
                sqlstate: error
                    .code()
                    .map(|code| code.code().to_owned())
                    .unwrap_or_default(),
                message,
            },
            PgErrorKind::ConnectionLost => WriteError::ConnectionLost(message),
            PgErrorKind::Other => {
                WriteError::Other(anyhow::Error::new(error).context(context.to_owned()))
            }
        }
    }

    /// Whether running the transaction again may succeed.
    fn is_retryable(&self) -> bool {
        matches!(
            self,
            WriteError::Conflict(_) | WriteError::ConnectionLost(_) | WriteError::Cancelled(_)
        )
    }
}

/// Whether running again the work that failed with `error` may succeed: a
/// cause in its chain is a database error of a kind [`run_write_interaction`]
/// retries, such as a lost connection. For work outside a write transaction,
/// such as a group of reads, which Python runs in a `runInteraction` that
/// retries them.
///
/// A [`WriteError`] in the chain does not count: it comes from a write
/// interaction that has already run its own retries.
pub fn is_retryable_error(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        if let Some(error) = cause.downcast_ref::<tokio_postgres::Error>() {
            PgErrorKind::of(error).is_retryable()
        } else if let Some(deadpool::managed::PoolError::Backend(error)) =
            cause.downcast_ref::<deadpool::managed::PoolError<tokio_postgres::Error>>()
        {
            PgErrorKind::of(error).is_retryable()
        } else {
            false
        }
    })
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::Constraint { message, .. } => write!(f, "constraint violation: {message}"),
            WriteError::Conflict(message) => write!(f, "transaction conflict: {message}"),
            WriteError::ConnectionLost(message) => write!(f, "connection lost: {message}"),
            WriteError::Cancelled(message) => write!(f, "statement cancelled: {message}"),
            WriteError::Other(error) => write!(f, "{error:#}"),
        }
    }
}

impl std::error::Error for WriteError {}

impl From<anyhow::Error> for WriteError {
    /// A [`WriteError`] that went through `anyhow` on its way out, as one
    /// from a [`Querier`] call does, comes back as itself, so that a
    /// conflict is still retried. Anything else is [`WriteError::Other`].
    fn from(error: anyhow::Error) -> Self {
        match error.downcast_ref::<WriteError>() {
            Some(WriteError::Other(_)) | None => WriteError::Other(error),
            Some(_) => error
                .downcast::<WriteError>()
                .expect("checked by downcast_ref"),
        }
    }
}

/// One column of a batch for [`WriteTxn::insert_many`]: its name, the
/// Postgres type of its elements (`text`, `bigint`, `boolean`, ...), and
/// the values, one per row.
pub struct Column<'a> {
    name: &'a str,
    sql_type: &'a str,
    values: &'a (dyn ToSql + Sync),
    rows: usize,
}

impl<'a> Column<'a> {
    pub fn new<T: ToSql + Sync>(name: &'a str, sql_type: &'a str, values: &'a Vec<T>) -> Self
    where
        Vec<T>: ToSql + Sync,
    {
        Column {
            name,
            sql_type,
            values,
            rows: values.len(),
        }
    }
}

/// A parameter of a queued statement, which owns its value.
pub type OwnedParam = Box<dyn ToSql + Sync + Send>;

/// One column of a batch for [`WriteTxn::insert_many_later`] and
/// [`WriteTxn::upsert_many_later`]: like [`Column`], but owning its values,
/// as the statement is sent after the call returns.
pub struct OwnedColumn {
    name: &'static str,
    sql_type: &'static str,
    values: OwnedParam,
    rows: usize,
}

impl OwnedColumn {
    pub fn new<T>(name: &'static str, sql_type: &'static str, values: Vec<T>) -> Self
    where
        Vec<T>: ToSql + Sync + Send + 'static,
    {
        OwnedColumn {
            name,
            sql_type,
            rows: values.len(),
            values: Box::new(values),
        }
    }
}

impl OwnedColumn {
    /// A column of copies of `values`, for values borrowed from data that
    /// does not outlive the call, such as `&str`s of an event.
    pub fn copy<T: ToOwnedSql>(name: &'static str, sql_type: &'static str, values: &[T]) -> Self
    where
        Vec<T::Owned>: ToSql + Sync + Send + 'static,
    {
        OwnedColumn::new(name, sql_type, values.iter().map(T::to_owned_sql).collect())
    }
}

/// A value that can be copied into one a queued statement can own.
pub trait ToOwnedSql {
    type Owned;
    fn to_owned_sql(&self) -> Self::Owned;
}

impl ToOwnedSql for str {
    type Owned = String;
    fn to_owned_sql(&self) -> String {
        self.to_owned()
    }
}

impl<T: ToOwnedSql + ?Sized> ToOwnedSql for &T {
    type Owned = T::Owned;
    fn to_owned_sql(&self) -> T::Owned {
        (**self).to_owned_sql()
    }
}

impl<T: ToOwnedSql> ToOwnedSql for Vec<T> {
    type Owned = Vec<T::Owned>;
    fn to_owned_sql(&self) -> Vec<T::Owned> {
        self.iter().map(T::to_owned_sql).collect()
    }
}

impl<T: ToOwnedSql> ToOwnedSql for [T] {
    type Owned = Vec<T::Owned>;
    fn to_owned_sql(&self) -> Vec<T::Owned> {
        self.iter().map(T::to_owned_sql).collect()
    }
}

impl ToOwnedSql for Option<&str> {
    type Owned = Option<String>;
    fn to_owned_sql(&self) -> Option<String> {
        self.map(str::to_owned)
    }
}

impl ToOwnedSql for String {
    type Owned = String;
    fn to_owned_sql(&self) -> String {
        self.clone()
    }
}

impl ToOwnedSql for Option<String> {
    type Owned = Option<String>;
    fn to_owned_sql(&self) -> Option<String> {
        self.clone()
    }
}

macro_rules! copy_to_owned_sql {
    ($($t:ty),*) => {
        $(impl ToOwnedSql for $t {
            type Owned = $t;
            fn to_owned_sql(&self) -> $t {
                *self
            }
        })*
    };
}

copy_to_owned_sql!(i32, i64, bool, Option<i32>, Option<i64>, Option<bool>);

/// The parameters of a queued statement, copied from borrowed values: for
/// [`WriteTxn::execute_later`].
#[macro_export]
macro_rules! owned_params {
    ($($param:expr),* $(,)?) => {
        vec![$(Box::new(
            $crate::database::postgres::write::ToOwnedSql::to_owned_sql(&$param),
        ) as $crate::database::postgres::write::OwnedParam),*]
    };
}

/// How to turn the error of a queued statement into the one its caller
/// wants, e.g. a constraint violation into a domain error.
pub type MapError = fn(WriteError) -> WriteError;

/// A statement queued by one of the `*_later` methods.
struct Queued {
    /// Shared by the rows of an [`WriteTxn::execute_batch_later`].
    sql: Arc<str>,
    params: Vec<OwnedParam>,
    map_error: Option<MapError>,
}

/// An open transaction. See the module docs. Rolled back if dropped before
/// [`WriteTxn::commit`].
///
/// Its statement methods must not run concurrently: a statement could then
/// be sent before writes queued ahead of it.
pub struct WriteTxn {
    conn: Option<PooledConnection>,
    in_transaction: bool,
    /// The `BEGIN` has not been sent yet. The first statement sends it in
    /// front of its own first request, so that they share a round trip.
    begin_pending: AtomicBool,
    /// The `BEGIN` failed, so the statement sent behind it may have run
    /// outside a transaction: the connection is discarded, not reused.
    begin_failed: AtomicBool,
    /// Writes whose results nobody reads, not yet sent. They go out, in
    /// order, ahead of the next statement that is not queued, or of the
    /// `COMMIT`, in the same round trip. Never locked across an await.
    queued: Mutex<Vec<Queued>>,
    /// A statement failed, so Postgres has aborted the transaction. A
    /// `COMMIT` would then roll back without an error, so the closure must
    /// not be able to report success after catching the error. Holds one of
    /// the `FAILED_*` values, for the first failure.
    failed: AtomicU8,
}

impl WriteTxn {
    /// Start a transaction on `conn`. Nothing is sent until the first
    /// statement, which sends the `BEGIN` with it.
    fn begin(conn: PooledConnection) -> WriteTxn {
        WriteTxn {
            conn: Some(conn),
            // Set before anything is sent: if we are dropped after the BEGIN
            // goes out but before it returns, the transaction is open and
            // must be rolled back.
            in_transaction: true,
            begin_pending: AtomicBool::new(true),
            begin_failed: AtomicBool::new(false),
            queued: Mutex::new(Vec::new()),
            failed: AtomicU8::new(NOT_FAILED),
        }
    }

    /// The `BEGIN`, if no statement has sent it yet. The caller must send it
    /// in front of its statement.
    fn take_begin(&self) -> Option<BoxFuture<'_, Result<(), tokio_postgres::Error>>> {
        self.begin_pending.swap(false, Ordering::Relaxed).then(|| {
            self.conn()
                .batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                .boxed()
        })
    }

    fn conn(&self) -> &PooledConnection {
        self.conn.as_ref().expect("held until dropped")
    }

    fn take_queued(&self) -> Vec<Queued> {
        std::mem::take(&mut *self.queued.lock().expect("not poisoned"))
    }

    /// The error for a statement that could not be sent, such as one whose
    /// parameters do not encode. The transaction must not be committed after
    /// it.
    fn client_failed(&self, context: &str, error: anyhow::Error) -> WriteError {
        let _ =
            self.failed
                .compare_exchange(NOT_FAILED, FAILED, Ordering::Relaxed, Ordering::Relaxed);
        WriteError::Other(error.context(context.to_owned()))
    }

    /// The error for a failed statement, noting that the transaction is now
    /// aborted.
    fn statement_failed(&self, context: &str, error: tokio_postgres::Error) -> WriteError {
        let error = WriteError::from_pg(context, error);
        let failed = if error.is_retryable() {
            FAILED_RETRYABLE
        } else {
            FAILED
        };
        // Keep the first: later statements fail only because it aborted the
        // transaction.
        let _ =
            self.failed
                .compare_exchange(NOT_FAILED, failed, Ordering::Relaxed, Ordering::Relaxed);
        error
    }

    /// Send the queued writes, then the request `last` builds for the
    /// statement `sql` (prepared, if there is one), all in one round trip,
    /// with the `BEGIN` in front if it is still to be sent. Statements not
    /// yet prepared on this connection are prepared first, together, in one
    /// more round trip.
    ///
    /// Requests go on the wire in this order, as tokio-postgres sends each
    /// when its future is first polled and [`join_in_order`] polls them in
    /// order. A failure is reported as the first one on the wire: the
    /// requests after it fail only because it aborted the transaction.
    async fn send<'s, T, F>(
        &'s self,
        sql: Option<&str>,
        last: impl FnOnce(&'s PooledConnection, Option<tokio_postgres::Statement>) -> F,
        context: impl FnOnce() -> String,
    ) -> Result<T, WriteError>
    where
        F: Future<Output = Result<T, tokio_postgres::Error>> + 's,
    {
        let conn = self.conn();
        let queued = self.take_queued();
        let mut begin = self.take_begin();

        let sqls: Vec<&str> = queued.iter().map(|q| &*q.sql).chain(sql).collect();
        // Each distinct statement once: a batch queues one row per statement.
        let mut by_sql: HashMap<&str, Option<tokio_postgres::Statement>> = HashMap::new();
        for sql in &sqls {
            by_sql.entry(sql).or_insert_with(|| conn.cached(sql));
        }

        let mut missing: Vec<&str> = by_sql
            .iter()
            .filter(|(_, statement)| statement.is_none())
            .map(|(sql, _)| *sql)
            .collect();
        missing.sort_unstable();
        if !missing.is_empty() {
            let prepares = join_in_order(missing.iter().map(|sql| conn.prepare_cached(sql, false)));
            let prepared = match begin.take() {
                None => prepares.await,
                Some(begin) => {
                    let (begun, prepared) = join(begin, prepares).await;
                    if let Err(e) = begun {
                        self.begin_failed.store(true, Ordering::Relaxed);
                        return Err(self.statement_failed("opening a transaction", e));
                    }
                    prepared
                }
            };
            for (sql, result) in missing.iter().zip(prepared) {
                let statement = result.map_err(|e| {
                    self.statement_failed(&format!("preparing {}", describe(sql)), e)
                })?;
                by_sql.insert(sql, Some(statement));
            }
        }

        let mut statements = sqls
            .iter()
            .map(|sql| by_sql[sql].clone().expect("cached or prepared above"));
        let queued_statements: Vec<tokio_postgres::Statement> =
            statements.by_ref().take(queued.len()).collect();
        let last_statement = statements.next();

        // tokio-postgres encodes a request's parameters when it is first
        // polled, and if that fails it sends nothing, while the requests
        // after it still go out, `COMMIT` included. So check that every
        // queued write's parameters encode before sending any of them.
        for (q, statement) in queued.iter().zip(&queued_statements) {
            if let Err(error) = check_params(statement, &q.params) {
                return Err(self.client_failed(&describe(&q.sql), error));
            }
        }

        let writes = join_in_order(queued.iter().zip(&queued_statements).map(|(q, statement)| {
            conn.execute_raw(
                statement,
                q.params.iter().map(|p| p.as_ref() as &(dyn ToSql + Sync)),
            )
        }));
        let requests = join(writes, last(conn, last_statement));
        let (written, result) = match begin {
            None => requests.await,
            Some(begin) => {
                let (begun, results) = join(begin, requests).await;
                if let Err(e) = begun {
                    self.begin_failed.store(true, Ordering::Relaxed);
                    return Err(self.statement_failed("opening a transaction", e));
                }
                results
            }
        };

        for (q, outcome) in queued.iter().zip(written) {
            if let Err(e) = outcome {
                let error = self.statement_failed(&describe(&q.sql), e);
                return Err(match q.map_error {
                    Some(map_error) => map_error(error),
                    None => error,
                });
            }
        }
        result.map_err(|e| self.statement_failed(&context(), e))
    }

    /// Run a statement that returns rows, after the queued writes.
    pub async fn query(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<Row>, WriteError> {
        self.send(
            Some(sql),
            |conn, statement| async move {
                conn.query(&statement.expect("has a statement"), params)
                    .await
            },
            || describe(sql),
        )
        .await
    }

    /// Run a statement, after the queued writes, returning the number of rows
    /// it changed.
    pub async fn execute(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<u64, WriteError> {
        self.send(
            Some(sql),
            |conn, statement| async move {
                conn.execute(&statement.expect("has a statement"), params)
                    .await
            },
            || describe(sql),
        )
        .await
    }

    /// Run one statement once per set of parameters, pipelined on the
    /// connection after the queued writes, returning the total number of
    /// rows changed. For the statements that need a sub-select per row; the
    /// rest can use [`WriteTxn::insert_many`].
    pub async fn execute_batch(
        &self,
        sql: &str,
        rows: &[Vec<&(dyn ToSql + Sync)>],
    ) -> Result<u64, WriteError> {
        if rows.is_empty() {
            return Ok(0);
        }
        self.send(
            Some(sql),
            |conn, statement| async move {
                let statement = statement.expect("has a statement");
                let counts =
                    join_in_order(rows.iter().map(|params| conn.execute(&statement, params))).await;
                counts.into_iter().sum::<Result<u64, _>>()
            },
            || describe(sql),
        )
        .await
    }

    /// Send the queued writes now, and wait for them.
    pub async fn flush(&self) -> Result<(), WriteError> {
        if self.queued.lock().expect("not poisoned").is_empty() {
            return Ok(());
        }
        self.send(None, |_, _| async { Ok(()) }, String::new).await
    }

    /// Queue a write whose result nobody reads. It goes out ahead of the
    /// next statement that is not queued, or of the `COMMIT`, and a failure
    /// surfaces there.
    ///
    /// Queued statements are all prepared before the first of them runs, so
    /// one must not depend on a schema change an earlier queued one makes.
    pub fn execute_later(&self, sql: impl Into<String>, params: Vec<OwnedParam>) {
        self.queue(Arc::from(sql.into()), params, None);
    }

    /// As [`WriteTxn::execute_later`], with `map_error` applied to the
    /// statement's error.
    pub fn execute_later_mapping_error(
        &self,
        sql: impl Into<String>,
        params: Vec<OwnedParam>,
        map_error: MapError,
    ) {
        self.queue(Arc::from(sql.into()), params, Some(map_error));
    }

    /// As [`WriteTxn::execute_batch`], queued as [`WriteTxn::execute_later`]:
    /// the statement once per row of parameters.
    pub fn execute_batch_later(&self, sql: &str, rows: Vec<Vec<OwnedParam>>) {
        let sql: Arc<str> = Arc::from(sql);
        for params in rows {
            self.queue(Arc::clone(&sql), params, None);
        }
    }

    fn queue(&self, sql: Arc<str>, params: Vec<OwnedParam>, map_error: Option<MapError>) {
        self.queued.lock().expect("not poisoned").push(Queued {
            sql,
            params,
            map_error,
        });
    }

    /// As [`WriteTxn::insert_many`], queued as [`WriteTxn::execute_later`].
    pub fn insert_many_later(
        &self,
        table: &str,
        columns: Vec<OwnedColumn>,
        on_conflict: Option<&str>,
    ) -> Result<(), WriteError> {
        let shapes: Vec<(&str, &str, usize)> = columns
            .iter()
            .map(|c| (c.name, c.sql_type, c.rows))
            .collect();
        if check_batch(table, &shapes)?.is_none() {
            return Ok(());
        }
        let mut sql = unnest_insert_sql(table, &shapes);
        if let Some(on_conflict) = on_conflict {
            sql.push(' ');
            sql.push_str(on_conflict);
        }
        self.execute_later(sql, columns.into_iter().map(|c| c.values).collect());
        Ok(())
    }

    /// As [`WriteTxn::upsert_many`], queued as [`WriteTxn::execute_later`].
    pub fn upsert_many_later(
        &self,
        table: &str,
        keys: Vec<OwnedColumn>,
        values: Vec<OwnedColumn>,
    ) -> Result<(), WriteError> {
        let shapes: Vec<(&str, &str, usize)> = keys
            .iter()
            .chain(&values)
            .map(|c| (c.name, c.sql_type, c.rows))
            .collect();
        if check_batch(table, &shapes)?.is_none() {
            return Ok(());
        }
        let key_names: Vec<&str> = keys.iter().map(|c| c.name).collect();
        let value_names: Vec<&str> = values.iter().map(|c| c.name).collect();
        let sql = unnest_insert_sql(table, &shapes) + &on_conflict_update(&key_names, &value_names);
        self.execute_later(
            sql,
            keys.into_iter().chain(values).map(|c| c.values).collect(),
        );
        Ok(())
    }

    /// Insert a batch of rows in one statement, returning the number
    /// inserted. Does nothing for an empty batch. With `on_conflict`, that
    /// clause is added, for example `ON CONFLICT DO NOTHING`.
    pub async fn insert_many(
        &self,
        table: &str,
        columns: &[Column<'_>],
        on_conflict: Option<&str>,
    ) -> Result<u64, WriteError> {
        let shapes: Vec<(&str, &str, usize)> = columns
            .iter()
            .map(|c| (c.name, c.sql_type, c.rows))
            .collect();
        if check_batch(table, &shapes)?.is_none() {
            return Ok(0);
        }
        let mut sql = unnest_insert_sql(table, &shapes);
        if let Some(on_conflict) = on_conflict {
            sql.push(' ');
            sql.push_str(on_conflict);
        }
        let params: Vec<&(dyn ToSql + Sync)> = columns.iter().map(|c| c.values).collect();
        self.execute(&sql, &params).await
    }

    /// Insert a batch of rows, or update the `values` columns of the rows
    /// whose `keys` columns match, in one statement. With no `values`
    /// columns, existing rows are left alone. With `values`, two rows of the
    /// batch with the same key fail the statement, as in Python's
    /// `simple_upsert_many_txn`. Without, the first of them is kept.
    pub async fn upsert_many(
        &self,
        table: &str,
        keys: &[Column<'_>],
        values: &[Column<'_>],
    ) -> Result<u64, WriteError> {
        let columns: Vec<&Column<'_>> = keys.iter().chain(values).collect();
        let shapes: Vec<(&str, &str, usize)> = columns
            .iter()
            .map(|c| (c.name, c.sql_type, c.rows))
            .collect();
        if check_batch(table, &shapes)?.is_none() {
            return Ok(0);
        }
        let key_names: Vec<&str> = keys.iter().map(|c| c.name).collect();
        let value_names: Vec<&str> = values.iter().map(|c| c.name).collect();
        let sql = unnest_insert_sql(table, &shapes) + &on_conflict_update(&key_names, &value_names);
        let params: Vec<&(dyn ToSql + Sync)> = columns.iter().map(|c| c.values).collect();
        self.execute(&sql, &params).await
    }

    /// Send the queued writes and commit, in one round trip.
    async fn commit(mut self) -> Result<(), WriteError> {
        match self.failed.load(Ordering::Relaxed) {
            NOT_FAILED => {}
            // The closure caught an error that a retry may get past, so
            // retry rather than fail for good.
            FAILED_RETRYABLE => {
                self.rollback().await;
                return Err(WriteError::Conflict(
                    "a statement failed with a retryable error, so the transaction was rolled back"
                        .to_owned(),
                ));
            }
            _ => {
                self.rollback().await;
                return Err(WriteError::Other(anyhow::anyhow!(
                    "a statement failed, so the transaction was rolled back"
                )));
            }
        }
        if self.begin_pending.load(Ordering::Relaxed)
            && self.queued.lock().expect("not poisoned").is_empty()
        {
            // No statement ran, so there is no transaction to commit.
            self.in_transaction = false;
            return Ok(());
        }
        self.send(
            None,
            |conn, _| conn.batch_execute("COMMIT"),
            || "committing".to_owned(),
        )
        .await?;
        self.in_transaction = false;
        Ok(())
    }

    /// Roll back now, rather than on a background task when dropped, so a
    /// retry does not race the rollback for the rows. Queued writes are
    /// dropped unsent.
    async fn rollback(mut self) {
        self.take_queued();
        if self.begin_pending.load(Ordering::Relaxed) {
            // No statement ran, so there is no transaction to roll back.
            self.in_transaction = false;
            return;
        }
        if self.begin_failed.load(Ordering::Relaxed) {
            // Dropping discards the connection.
            return;
        }
        if let Err(e) = self.conn().batch_execute("ROLLBACK").await {
            warn!("Failed to roll back a write transaction: {e}");
            // Leave `in_transaction` set: dropping discards the connection
            // rather than returning it to the pool.
            if let Some(conn) = self.conn.take() {
                drop(PooledConnection::take(conn));
            }
            return;
        }
        self.in_transaction = false;
    }
}

/// Check that `params` encode as `statement`'s parameter types, as
/// tokio-postgres would when sending it.
fn check_params(
    statement: &tokio_postgres::Statement,
    params: &[OwnedParam],
) -> anyhow::Result<()> {
    let types = statement.params();
    if types.len() != params.len() {
        anyhow::bail!(
            "statement takes {} parameters but {} were given",
            types.len(),
            params.len()
        );
    }
    let mut buf = bytes::BytesMut::new();
    for (i, (param, ty)) in params.iter().zip(types).enumerate() {
        buf.clear();
        param
            .to_sql_checked(ty, &mut buf)
            .map_err(|e| anyhow::anyhow!("error serializing parameter {i}: {e}"))?;
    }
    Ok(())
}

/// Await `futures` together, polling every one still pending, in order, on
/// each wake-up. tokio-postgres sends a request when its future is first
/// polled, so the requests go out in the order given. (`join_all` and
/// `try_join_all` do not promise an order for long lists.)
pub(crate) fn join_in_order<F: Future>(
    futures: impl IntoIterator<Item = F>,
) -> impl Future<Output = Vec<F::Output>> {
    let mut futures: Vec<_> = futures
        .into_iter()
        .map(|future| maybe_done(Box::pin(future)))
        .collect();
    poll_fn(move |cx| {
        let mut done = true;
        for future in futures.iter_mut() {
            if Pin::new(future).poll(cx).is_pending() {
                done = false;
            }
        }
        if !done {
            return Poll::Pending;
        }
        Poll::Ready(
            futures
                .iter_mut()
                .map(|future| {
                    Pin::new(future)
                        .take_output()
                        .expect("polled to completion above")
                })
                .collect(),
        )
    })
}

/// For the read helpers shared with the read-only paths, such as the chain
/// cover reads in `persist::reader`.
#[async_trait]
impl Querier for WriteTxn {
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>> {
        WriteTxn::query(self, sql, params)
            .await
            .map_err(anyhow::Error::new)
    }
}

impl Drop for WriteTxn {
    fn drop(&mut self) {
        let Some(conn) = self.conn.take() else { return };
        if self.begin_failed.load(Ordering::Relaxed) {
            // The session state is unknown: discard the connection.
            drop(PooledConnection::take(conn));
            return;
        }
        if !self.in_transaction || self.begin_pending.load(Ordering::Relaxed) {
            return;
        }
        // Roll back before the connection goes back to the pool, so the next
        // user doesn't find it inside our transaction. Without a runtime to
        // do that on, the connection is dropped rather than returned.
        match Handle::try_current() {
            Ok(handle) => {
                handle.spawn(async move {
                    if let Err(e) = conn.batch_execute("ROLLBACK").await {
                        warn!("Failed to roll back a write transaction: {e}");
                    }
                    drop(conn);
                });
            }
            Err(_) => {
                drop(PooledConnection::take(conn));
            }
        }
    }
}

/// Run `f` in a write transaction and commit it, running it again from the
/// start on a serialization failure, a deadlock or a lost connection, up to
/// five attempts in all. `name` labels the warnings. See the module docs.
///
/// `f` must leave no trace outside the database before it returns, or reset
/// what it left at its start, since a retry runs it again.
///
/// `f` takes a second argument it ignores: its type, `&'t &'env ()`, tells
/// the compiler that the borrows `f` captures (of lifetime `'env`) outlive
/// the transaction (`'t`), so the future may use them. Call it as
/// `run_write_interaction(pool, name, |txn, _| async move { .. }.boxed())`.
pub async fn run_write_interaction<'env, T, F>(
    pool: &ConnectionPool,
    name: &str,
    f: F,
) -> Result<T, WriteError>
where
    F: for<'t> FnMut(&'t WriteTxn, &'t &'env ()) -> BoxFuture<'t, Result<T, WriteError>>,
{
    run_interaction(pool, name, true, f).await
}

/// As [`run_write_interaction`], but rolls the transaction back even when
/// `f` succeeds. For a dry run whose writes must not be seen.
pub async fn run_rolled_back_interaction<'env, T, F>(
    pool: &ConnectionPool,
    name: &str,
    f: F,
) -> Result<T, WriteError>
where
    F: for<'t> FnMut(&'t WriteTxn, &'t &'env ()) -> BoxFuture<'t, Result<T, WriteError>>,
{
    run_interaction(pool, name, false, f).await
}

async fn run_interaction<'env, T, F>(
    pool: &ConnectionPool,
    name: &str,
    commit: bool,
    mut f: F,
) -> Result<T, WriteError>
where
    F: for<'t> FnMut(&'t WriteTxn, &'t &'env ()) -> BoxFuture<'t, Result<T, WriteError>>,
{
    // As `new_transaction`, from when each attempt has its connection: the
    // wait for one is not the transaction's.
    let mut elapsed = Duration::ZERO;
    let mut attempt = 1;
    let result = loop {
        let result = run_once(pool, commit, &mut f, &mut elapsed).await;
        match result {
            Err(error) if error.is_retryable() && attempt < MAX_ATTEMPTS => {
                warn!("[TXN RETRY] {{{name}}} {error} {attempt}/{MAX_ATTEMPTS}");
                attempt += 1;
            }
            result => break result,
        }
    };
    record_txn_timing(name, elapsed);
    result
}

/// What each write interaction run in a [`with_txn_timings`] scope took,
/// as `(name, seconds)`, in the order they finished.
pub type TxnTimings = Vec<(String, f64)>;

tokio::task_local! {
    static TXN_TIMINGS: RefCell<TxnTimings>;
}

/// Run `future`, and return with its output what each write interaction
/// it ran took, whether it succeeded or not, retries included and the wait
/// for a connection left out, as `DatabasePool.new_transaction` times
/// them. A caller whose future fails must still report them. Python records them where it
/// records its own (`sql_txn_count`, `sql_txn_duration` and the log
/// context's transaction counts), which Rust cannot reach without the GIL.
/// Interactions run on other tasks are not seen.
pub async fn with_txn_timings<F: Future>(future: F) -> (F::Output, TxnTimings) {
    TXN_TIMINGS
        .scope(RefCell::new(Vec::new()), async {
            let output = future.await;
            let timings = TXN_TIMINGS.with(|timings| timings.take());
            (output, timings)
        })
        .await
}

/// Record that an interaction run outside [`run_write_interaction`] took
/// `elapsed`, if a [`with_txn_timings`] scope is listening.
pub fn record_txn_timing(name: &str, elapsed: Duration) {
    let _ = TXN_TIMINGS.try_with(|timings| {
        timings
            .borrow_mut()
            .push((name.to_owned(), elapsed.as_secs_f64()))
    });
}

/// One attempt, adding its time from when it has a connection to
/// `elapsed`.
async fn run_once<'env, T, F>(
    pool: &ConnectionPool,
    commit: bool,
    f: &mut F,
    elapsed: &mut Duration,
) -> Result<T, WriteError>
where
    F: for<'t> FnMut(&'t WriteTxn, &'t &'env ()) -> BoxFuture<'t, Result<T, WriteError>>,
{
    let conn = pool.get().await.map_err(|e| match e {
        deadpool::managed::PoolError::Backend(e) => {
            WriteError::from_pg("checking out a database connection", e)
        }
        e => WriteError::Other(anyhow::anyhow!("checking out a database connection: {e}")),
    })?;
    let start = Instant::now();
    let result = attempt(conn, commit, f).await;
    *elapsed += start.elapsed();
    result
}

async fn attempt<'env, T, F>(
    conn: PooledConnection,
    commit: bool,
    f: &mut F,
) -> Result<T, WriteError>
where
    F: for<'t> FnMut(&'t WriteTxn, &'t &'env ()) -> BoxFuture<'t, Result<T, WriteError>>,
{
    let txn = WriteTxn::begin(conn);
    match f(&txn, &&()).await {
        Ok(value) if commit => {
            txn.commit().await?;
            Ok(value)
        }
        Ok(value) => {
            // A dry run still checks its queued writes.
            let flushed = txn.flush().await;
            txn.rollback().await;
            flushed.map(|()| value)
        }
        Err(error) => {
            txn.rollback().await;
            Err(error)
        }
    }
}

/// The batch's row count, or `None` for an empty batch. All columns must
/// have one value per row.
/// The batch's row count, given each column's `(name, type, rows)`, or
/// `None` for an empty batch. All columns must have the same number of rows.
fn check_batch(table: &str, columns: &[(&str, &str, usize)]) -> Result<Option<usize>, WriteError> {
    let Some(&(first_name, _, first_rows)) = columns.first() else {
        return Err(anyhow::anyhow!("a batch for {table} with no columns").into());
    };
    if let Some(&(name, _, rows)) = columns.iter().find(|c| c.2 != first_rows) {
        return Err(anyhow::anyhow!(
            "a batch for {table} with {first_rows} values of {first_name} and {rows} of {name}"
        )
        .into());
    }
    Ok((first_rows > 0).then_some(first_rows))
}

/// `INSERT INTO table (a, b) SELECT * FROM UNNEST($1::text[], $2::bigint[])`,
/// given each column's `(name, type, rows)`.
fn unnest_insert_sql(table: &str, columns: &[(&str, &str, usize)]) -> String {
    let names: Vec<&str> = columns.iter().map(|c| c.0).collect();
    let arrays: Vec<String> = columns
        .iter()
        .enumerate()
        .map(|(i, c)| format!("${}::{}[]", i + 1, c.1))
        .collect();
    format!(
        "INSERT INTO {table} ({}) SELECT * FROM UNNEST({})",
        names.join(", "),
        arrays.join(", ")
    )
}

/// The `ON CONFLICT` clause of an upsert: do nothing without `values`,
/// otherwise update them from the new row.
fn on_conflict_update(keys: &[&str], values: &[&str]) -> String {
    if values.is_empty() {
        return format!(" ON CONFLICT ({}) DO NOTHING", keys.join(", "));
    }
    let updates: Vec<String> = values
        .iter()
        .map(|name| format!("{name} = EXCLUDED.{name}"))
        .collect();
    format!(
        " ON CONFLICT ({}) DO UPDATE SET {}",
        keys.join(", "),
        updates.join(", ")
    )
}

/// The start of a statement, for error messages.
fn describe(sql: &str) -> String {
    sql.split_whitespace().take(6).collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    //! Against a real server: these run when `SYNAPSE_TEST_POSTGRES_DSN` is
    //! set (e.g. to `host=postgres user=postgres password=postgres`), and
    //! otherwise do nothing.

    use std::sync::{
        atomic::{AtomicU32, Ordering},
        Arc,
    };

    use anyhow::Context;
    use futures::FutureExt;
    use tokio::runtime::Runtime;

    use super::*;
    use crate::database::postgres::pool::{create_pool, PoolConfig};

    fn setup() -> Option<(Runtime, ConnectionPool, String)> {
        let Ok(dsn) = std::env::var("SYNAPSE_TEST_POSTGRES_DSN") else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return None;
        };
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 3,
                ..PoolConfig::default()
            },
        )
        .unwrap();

        // A table of its own per test, as tests run in parallel.
        let table = format!(
            "write_txn_test_{}",
            ulid::Ulid::generate().to_string().to_lowercase()
        );
        rt.block_on(async {
            pool.get()
                .await
                .unwrap()
                .batch_execute(&format!(
                    "CREATE TABLE {table} (
                        id TEXT PRIMARY KEY,
                        n BIGINT NOT NULL,
                        flag BOOLEAN,
                        note TEXT
                    )"
                ))
                .await
                .unwrap();
        });
        Some((rt, pool, table))
    }

    fn teardown(rt: &Runtime, pool: &ConnectionPool, table: &str) {
        rt.block_on(async {
            pool.get()
                .await
                .unwrap()
                .batch_execute(&format!("DROP TABLE {table}"))
                .await
                .unwrap();
        });
    }

    async fn rows(
        pool: &ConnectionPool,
        table: &str,
    ) -> Vec<(String, i64, Option<bool>, Option<String>)> {
        pool.get()
            .await
            .unwrap()
            .query(
                &format!("SELECT id, n, flag, note FROM {table} ORDER BY id"),
                &[],
            )
            .await
            .unwrap()
            .iter()
            .map(|row| (row.get(0), row.get(1), row.get(2), row.get(3)))
            .collect()
    }

    /// Queued writes go out ahead of the next statement, which sees them,
    /// and with the `COMMIT` when nothing follows them.
    #[test]
    fn queued_writes_are_sent_in_order() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let seen = run_write_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    txn.insert_many_later(
                        &table,
                        vec![
                            OwnedColumn::new("id", "text", vec!["a".to_owned(), "b".to_owned()]),
                            OwnedColumn::new("n", "bigint", vec![1i64, 2]),
                        ],
                        None,
                    )?;
                    txn.execute_later(
                        format!("UPDATE {table} SET n = n + 10 WHERE id = $1"),
                        vec![Box::new("a".to_owned())],
                    );
                    let rows = txn
                        .query(&format!("SELECT id, n FROM {table} ORDER BY id"), &[])
                        .await?;
                    // Only queued: sent with the COMMIT.
                    txn.upsert_many_later(
                        &table,
                        vec![OwnedColumn::new("id", "text", vec!["c".to_owned()])],
                        vec![OwnedColumn::new("n", "bigint", vec![3i64])],
                    )?;
                    Ok(rows
                        .iter()
                        .map(|row| (row.get::<_, String>(0), row.get::<_, i64>(1)))
                        .collect::<Vec<_>>())
                }
                .boxed()
            })
            .await
            .unwrap();
            assert_eq!(seen, vec![("a".to_owned(), 11), ("b".to_owned(), 2)]);
            assert_eq!(
                rows(&pool, &table).await,
                vec![
                    ("a".to_owned(), 11, None, None),
                    ("b".to_owned(), 2, None, None),
                    ("c".to_owned(), 3, None, None),
                ]
            );
        });
        teardown(&rt, &pool, &table);
    }

    /// A queued write that fails surfaces at the next statement, as itself
    /// (the next statement only fails because it aborted the transaction),
    /// mapped as asked. Nothing is committed.
    #[test]
    fn queued_write_failure_surfaces_at_the_next_statement() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let error = run_write_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    txn.execute_later(
                        format!("INSERT INTO {table} (id, n) VALUES ('a', 1)"),
                        vec![],
                    );
                    txn.execute_later_mapping_error(
                        format!("INSERT INTO {table} (id, n) VALUES ('a', 2)"),
                        vec![],
                        |error| match error {
                            WriteError::Constraint { .. } => {
                                WriteError::Other(anyhow::anyhow!("mapped duplicate"))
                            }
                            other => other,
                        },
                    );
                    txn.query(&format!("SELECT id FROM {table}"), &[]).await?;
                    Ok(())
                }
                .boxed()
            })
            .await
            .unwrap_err();
            assert!(
                format!("{error}").contains("mapped duplicate"),
                "unexpected error: {error}"
            );
            assert_eq!(rows(&pool, &table).await, vec![]);
        });
        teardown(&rt, &pool, &table);
    }

    /// A queued write whose parameters do not encode stops everything queued
    /// with it from being sent, the `COMMIT` included, so nothing is
    /// committed.
    #[test]
    fn queued_write_that_does_not_encode_commits_nothing() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let result: Result<(), WriteError> = run_write_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    for (id, n) in [("x", "1"), ("y", "2"), ("z", "3")] {
                        let id: OwnedParam = if id == "y" {
                            // `id` is text: an integer does not encode.
                            Box::new(5i64)
                        } else {
                            Box::new(id.to_owned())
                        };
                        txn.execute_later(
                            format!("INSERT INTO {table} (id, n) VALUES ($1, {n})"),
                            vec![id],
                        );
                    }
                    Ok(())
                }
                .boxed()
            })
            .await;
            let error = result.unwrap_err();
            assert!(
                format!("{error:#}").contains("error serializing parameter 0"),
                "unexpected error: {error:#}"
            );
            assert_eq!(rows(&pool, &table).await, vec![]);
        });
        teardown(&rt, &pool, &table);
    }

    /// A dry run still sends its queued writes, so their failures surface.
    #[test]
    fn dry_run_checks_queued_writes() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let result = run_rolled_back_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    txn.execute_later(
                        format!("INSERT INTO {table} (id, n) VALUES ('a', NULL)"),
                        vec![],
                    );
                    Ok(())
                }
                .boxed()
            })
            .await;
            assert!(
                matches!(result, Err(WriteError::Constraint { .. })),
                "unexpected result: {result:?}"
            );
        });
        teardown(&rt, &pool, &table);
    }

    /// When the closure fails, its queued writes are dropped unsent.
    #[test]
    fn queued_writes_are_dropped_on_failure() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let result: Result<(), WriteError> = run_write_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    txn.execute_later(
                        format!("INSERT INTO {table} (id, n) VALUES ('a', 1)"),
                        vec![],
                    );
                    Err(WriteError::Other(anyhow::anyhow!("closure failed")))
                }
                .boxed()
            })
            .await;
            assert!(result.is_err());
            assert_eq!(rows(&pool, &table).await, vec![]);
        });
        teardown(&rt, &pool, &table);
    }

    /// The `BEGIN` goes out with the first statement, so that statement
    /// runs inside the transaction, whether it is prepared afresh or comes
    /// from the cache.
    #[test]
    fn first_statement_runs_in_the_transaction() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            for _ in 0..6 {
                let isolation = run_write_interaction(&pool, "test", |txn, _| {
                    async move {
                        let rows = txn
                            .query("SELECT current_setting('transaction_isolation')", &[])
                            .await?;
                        Ok(rows[0].get::<_, String>(0))
                    }
                    .boxed()
                })
                .await
                .unwrap();
                assert_eq!(isolation, "repeatable read");
            }
        });
        teardown(&rt, &pool, &table);
    }

    /// A transaction that runs no statement sends neither `BEGIN` nor
    /// `COMMIT`, and leaves the connection usable.
    #[test]
    fn empty_transaction_sends_nothing() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            run_write_interaction(&pool, "test", |_txn, _| async move { Ok(()) }.boxed())
                .await
                .unwrap();
            let count = run_write_interaction(&pool, "test", |txn, _| {
                let table = table.clone();
                async move {
                    let rows = txn
                        .query(&format!("SELECT COUNT(*) FROM {table}"), &[])
                        .await?;
                    Ok(rows[0].get::<_, i64>(0))
                }
                .boxed()
            })
            .await
            .unwrap();
            assert_eq!(count, 0);
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn insert_and_upsert_many() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let ids = vec!["a".to_string(), "b".to_string()];
            let ns = vec![1i64, 2];
            let flags = vec![Some(true), None];
            let notes: Vec<Option<String>> = vec![None, Some("x".into())];
            run_write_interaction(&pool, "insert", |txn, _| {
                let table = &table;
                let (ids, ns, flags, notes) = (&ids, &ns, &flags, &notes);
                async move {
                    txn.insert_many(
                        table,
                        &[
                            Column::new("id", "text", ids),
                            Column::new("n", "bigint", ns),
                            Column::new("flag", "boolean", flags),
                            Column::new("note", "text", notes),
                        ],
                        None,
                    )
                    .await
                }
                .boxed()
            })
            .await
            .unwrap();

            // Update one row, add another; an empty batch does nothing.
            let ids = vec!["b".to_string(), "c".to_string()];
            let ns = vec![20i64, 30];
            let empty: Vec<String> = Vec::new();
            run_write_interaction(&pool, "upsert", |txn, _| {
                let table = &table;
                let (ids, ns, empty) = (&ids, &ns, &empty);
                async move {
                    txn.upsert_many(
                        table,
                        &[Column::new("id", "text", ids)],
                        &[Column::new("n", "bigint", ns)],
                    )
                    .await?;
                    let inserted = txn
                        .insert_many(table, &[Column::new("id", "text", empty)], None)
                        .await?;
                    assert_eq!(inserted, 0);
                    Ok(())
                }
                .boxed()
            })
            .await
            .unwrap();

            assert_eq!(
                rows(&pool, &table).await,
                vec![
                    ("a".into(), 1, Some(true), None),
                    ("b".into(), 20, None, Some("x".into())),
                    ("c".into(), 30, None, None),
                ]
            );
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn mismatched_columns_are_refused() {
        let ids = vec!["a".to_string()];
        let ns = vec![1i64, 2];
        let columns = [
            Column::new("id", "text", &ids),
            Column::new("n", "bigint", &ns),
        ];
        let shapes: Vec<(&str, &str, usize)> = columns
            .iter()
            .map(|c| (c.name, c.sql_type, c.rows))
            .collect();
        assert!(check_batch("t", &shapes).is_err());
    }

    #[test]
    fn constraint_violation_is_typed_and_rolls_back() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let ids = vec!["a".to_string(), "a".to_string()];
            let ns = vec![1i64, 2];
            let result = run_write_interaction(&pool, "duplicate", |txn, _| {
                let table = &table;
                let (ids, ns) = (&ids, &ns);
                async move {
                    txn.execute_batch(
                        &format!("INSERT INTO {table} (id, n) VALUES ($1, $2)"),
                        &[vec![&ids[0], &ns[0]]],
                    )
                    .await?;
                    txn.insert_many(
                        table,
                        &[
                            Column::new("id", "text", ids),
                            Column::new("n", "bigint", ns),
                        ],
                        None,
                    )
                    .await
                }
                .boxed()
            })
            .await;
            match result {
                Err(WriteError::Constraint { sqlstate, .. }) => assert_eq!(sqlstate, "23505"),
                other => panic!("expected a constraint violation, got {other:?}"),
            }
            // The first insert was rolled back with the rest.
            assert!(rows(&pool, &table).await.is_empty());
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn caught_statement_error_does_not_commit() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let result = run_write_interaction(&pool, "caught", |txn, _| {
                let table = &table;
                async move {
                    txn.execute(&format!("INSERT INTO {table} (id, n) VALUES ('a', 1)"), &[])
                        .await?;
                    // Postgres aborts the transaction here, so ignoring the
                    // error must not let the first insert look committed.
                    let duplicate = txn
                        .execute(&format!("INSERT INTO {table} (id, n) VALUES ('a', 2)"), &[])
                        .await;
                    assert!(matches!(duplicate, Err(WriteError::Constraint { .. })));
                    Ok(())
                }
                .boxed()
            })
            .await;
            assert!(matches!(result, Err(WriteError::Other(_))), "{result:?}");
            assert!(rows(&pool, &table).await.is_empty());
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn serialization_failure_is_retried() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            pool.get()
                .await
                .unwrap()
                .batch_execute(&format!("INSERT INTO {table} (id, n) VALUES ('a', 0)"))
                .await
                .unwrap();

            // On the first two attempts, another transaction updates the row
            // after ours has taken its snapshot, so our update fails with a
            // serialization failure.
            let attempts = Arc::new(AtomicU32::new(0));
            let result = run_write_interaction(&pool, "conflict", |txn, _| {
                let (pool, table, attempts) = (&pool, &table, Arc::clone(&attempts));
                async move {
                    let attempt = attempts.fetch_add(1, Ordering::SeqCst) + 1;
                    txn.query(&format!("SELECT n FROM {table} WHERE id = 'a'"), &[])
                        .await?;
                    if attempt <= 2 {
                        pool.get()
                            .await
                            .unwrap()
                            .batch_execute(&format!("UPDATE {table} SET n = n + 10 WHERE id = 'a'"))
                            .await
                            .unwrap();
                    }
                    txn.execute(&format!("UPDATE {table} SET n = n + 1 WHERE id = 'a'"), &[])
                        .await?;
                    Ok(attempt)
                }
                .boxed()
            })
            .await
            .unwrap();

            assert_eq!(result, 3);
            assert_eq!(rows(&pool, &table).await[0].1, 21);
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn write_errors_survive_anyhow() {
        let through = |error: WriteError| -> WriteError {
            let error: anyhow::Result<()> = Err(anyhow::Error::new(error));
            error.context("reading the chain links").unwrap_err().into()
        };
        assert!(matches!(
            through(WriteError::Conflict("simulated".into())),
            WriteError::Conflict(_)
        ));
        assert!(matches!(
            through(WriteError::Constraint {
                sqlstate: "23505".into(),
                message: "simulated".into()
            }),
            WriteError::Constraint { sqlstate, .. } if sqlstate == "23505"
        ));
        assert!(matches!(
            through(WriteError::ConnectionLost("simulated".into())),
            WriteError::ConnectionLost(_)
        ));
        assert!(matches!(
            through(WriteError::Cancelled("simulated".into())),
            WriteError::Cancelled(_)
        ));
        // Anything else keeps its context.
        let WriteError::Other(error) = through(WriteError::Other(anyhow::anyhow!("boom"))) else {
            panic!("expected Other");
        };
        assert_eq!(format!("{error:#}"), "reading the chain links: boom");
    }

    #[test]
    fn retryable_errors_are_found_in_the_chain() {
        // A write interaction has already retried its own errors.
        let wrapped = |error: WriteError| anyhow::Error::new(error).context("reading");
        assert!(!is_retryable_error(&wrapped(WriteError::Conflict(
            "x".into()
        ))));
        assert!(!is_retryable_error(&wrapped(WriteError::Cancelled(
            "x".into()
        ))));
        assert!(!is_retryable_error(&anyhow::anyhow!("x")));

        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let conn = pool.get().await.unwrap();
            conn.batch_execute("SET statement_timeout = 1")
                .await
                .unwrap();
            let cancelled = conn.query("SELECT pg_sleep(1)", &[]).await.unwrap_err();
            conn.batch_execute("RESET statement_timeout").await.unwrap();
            let cancelled = anyhow::Error::new(cancelled).context("running a query");
            assert!(is_retryable_error(&cancelled), "{cancelled:#}");

            let error = conn.query("SELECT 1/0", &[]).await.unwrap_err();
            assert!(!is_retryable_error(&anyhow::Error::new(error)));
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn serialization_failure_through_querier_is_retried() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            pool.get()
                .await
                .unwrap()
                .batch_execute(&format!("INSERT INTO {table} (id, n) VALUES ('a', 0)"))
                .await
                .unwrap();

            // As in `serialization_failure_is_retried`, with the statements
            // going through `Querier` and `anyhow`.
            let attempts = Arc::new(AtomicU32::new(0));
            let result = run_write_interaction(&pool, "conflict", |txn, _| {
                let (pool, table, attempts) = (&pool, &table, Arc::clone(&attempts));
                async move {
                    let querier: &dyn Querier = txn;
                    let attempt = attempts.fetch_add(1, Ordering::SeqCst) + 1;
                    querier
                        .query(&format!("SELECT n FROM {table} WHERE id = 'a'"), &[])
                        .await?;
                    if attempt == 1 {
                        pool.get()
                            .await
                            .unwrap()
                            .batch_execute(&format!("UPDATE {table} SET n = n + 10 WHERE id = 'a'"))
                            .await
                            .unwrap();
                    }
                    querier
                        .query(
                            &format!("UPDATE {table} SET n = n + 1 WHERE id = 'a' RETURNING n"),
                            &[],
                        )
                        .await
                        .context("bumping n")?;
                    Ok(attempt)
                }
                .boxed()
            })
            .await
            .unwrap();

            assert_eq!(result, 2);
            assert_eq!(rows(&pool, &table).await[0].1, 11);
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn caught_serialization_failure_is_retried() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            pool.get()
                .await
                .unwrap()
                .batch_execute(&format!("INSERT INTO {table} (id, n) VALUES ('a', 0)"))
                .await
                .unwrap();

            // The first attempt hits a serialization failure and swallows
            // it. The commit is refused, and as the error was retryable the
            // whole interaction runs again.
            let attempts = Arc::new(AtomicU32::new(0));
            let result = run_write_interaction(&pool, "caught conflict", |txn, _| {
                let (pool, table, attempts) = (&pool, &table, Arc::clone(&attempts));
                async move {
                    let attempt = attempts.fetch_add(1, Ordering::SeqCst) + 1;
                    txn.query(&format!("SELECT n FROM {table} WHERE id = 'a'"), &[])
                        .await?;
                    if attempt == 1 {
                        pool.get()
                            .await
                            .unwrap()
                            .batch_execute(&format!("UPDATE {table} SET n = n + 10 WHERE id = 'a'"))
                            .await
                            .unwrap();
                    }
                    let update = txn
                        .execute(&format!("UPDATE {table} SET n = n + 1 WHERE id = 'a'"), &[])
                        .await;
                    if attempt == 1 {
                        assert!(matches!(update, Err(WriteError::Conflict(_))), "{update:?}");
                    } else {
                        update?;
                    }
                    Ok(attempt)
                }
                .boxed()
            })
            .await
            .unwrap();

            assert_eq!(result, 2);
            assert_eq!(rows(&pool, &table).await[0].1, 11);
        });
        teardown(&rt, &pool, &table);
    }

    #[test]
    fn retries_stop_after_five_attempts() {
        let Some((rt, pool, table)) = setup() else {
            return;
        };
        rt.block_on(async {
            let attempts = AtomicU32::new(0);
            let result: Result<(), _> = run_write_interaction(&pool, "always", |_txn, _| {
                attempts.fetch_add(1, Ordering::SeqCst);
                async { Err(WriteError::Conflict("simulated".into())) }.boxed()
            })
            .await;
            assert!(matches!(result, Err(WriteError::Conflict(_))));
            assert_eq!(attempts.load(Ordering::SeqCst), MAX_ATTEMPTS);

            // Other errors are not retried.
            attempts.store(0, Ordering::SeqCst);
            let result: Result<(), _> = run_write_interaction(&pool, "other", |_txn, _| {
                attempts.fetch_add(1, Ordering::SeqCst);
                async { Err(anyhow::anyhow!("boom").into()) }.boxed()
            })
            .await;
            assert!(matches!(result, Err(WriteError::Other(_))));
            assert_eq!(attempts.load(Ordering::SeqCst), 1);
        });
        teardown(&rt, &pool, &table);
    }
}
