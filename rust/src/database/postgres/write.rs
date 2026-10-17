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

use std::{
    fmt::Write as _,
    sync::atomic::{AtomicU8, Ordering},
};

use async_trait::async_trait;
use futures::{future::try_join_all, future::BoxFuture};
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

/// An open transaction. See the module docs. Rolled back if dropped before
/// [`WriteTxn::commit`].
pub struct WriteTxn {
    conn: Option<PooledConnection>,
    in_transaction: bool,
    /// A statement failed, so Postgres has aborted the transaction. A
    /// `COMMIT` would then roll back without an error, so the closure must
    /// not be able to report success after catching the error. Holds one of
    /// the `FAILED_*` values, for the first failure.
    failed: AtomicU8,
}

impl WriteTxn {
    async fn begin(conn: PooledConnection) -> Result<WriteTxn, WriteError> {
        // Set before awaiting: if we are dropped after BEGIN is sent but
        // before it returns, the transaction is open and must be rolled back.
        let txn = WriteTxn {
            conn: Some(conn),
            in_transaction: true,
            failed: AtomicU8::new(NOT_FAILED),
        };
        txn.conn()
            .batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            .await
            .map_err(|e| WriteError::from_pg("opening a transaction", e))?;
        Ok(txn)
    }

    fn conn(&self) -> &PooledConnection {
        self.conn.as_ref().expect("held until dropped")
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

    /// Run a statement that returns rows.
    pub async fn query(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<Vec<Row>, WriteError> {
        let statement = self.prepare(sql).await?;
        self.conn()
            .query(&statement, params)
            .await
            .map_err(|e| self.statement_failed(&describe(sql), e))
    }

    /// Run a statement, returning the number of rows it changed.
    pub async fn execute(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> Result<u64, WriteError> {
        let statement = self.prepare(sql).await?;
        self.conn()
            .execute(&statement, params)
            .await
            .map_err(|e| self.statement_failed(&describe(sql), e))
    }

    /// Run one statement once per set of parameters, pipelined on the
    /// connection, returning the total number of rows changed. For the
    /// statements that need a sub-select per row; the rest can use
    /// [`WriteTxn::insert_many`].
    pub async fn execute_batch(
        &self,
        sql: &str,
        rows: &[Vec<&(dyn ToSql + Sync)>],
    ) -> Result<u64, WriteError> {
        if rows.is_empty() {
            return Ok(0);
        }
        let statement = self.prepare(sql).await?;
        let conn = self.conn();
        let counts = try_join_all(rows.iter().map(|params| conn.execute(&statement, params)))
            .await
            .map_err(|e| self.statement_failed(&describe(sql), e))?;
        Ok(counts.into_iter().sum())
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
        let Some(rows) = batch_rows(table, columns)? else {
            return Ok(0);
        };
        debug_assert!(rows > 0);
        let mut sql = unnest_insert(table, columns);
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
        let Some(_) = batch_rows_of(table, &columns)? else {
            return Ok(0);
        };
        let mut sql = unnest_insert_of(table, &columns);
        let key_names: Vec<&str> = keys.iter().map(|c| c.name).collect();
        if values.is_empty() {
            let _ = write!(sql, " ON CONFLICT ({}) DO NOTHING", key_names.join(", "));
        } else {
            let updates: Vec<String> = values
                .iter()
                .map(|c| format!("{0} = EXCLUDED.{0}", c.name))
                .collect();
            let _ = write!(
                sql,
                " ON CONFLICT ({}) DO UPDATE SET {}",
                key_names.join(", "),
                updates.join(", ")
            );
        }
        let params: Vec<&(dyn ToSql + Sync)> = columns.iter().map(|c| c.values).collect();
        self.execute(&sql, &params).await
    }

    async fn prepare(&self, sql: &str) -> Result<tokio_postgres::Statement, WriteError> {
        self.conn()
            .prepare_cached(sql, false)
            .await
            .map_err(|e| self.statement_failed(&format!("preparing {}", describe(sql)), e))
    }

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
        self.conn()
            .batch_execute("COMMIT")
            .await
            .map_err(|e| WriteError::from_pg("committing", e))?;
        self.in_transaction = false;
        Ok(())
    }

    /// Roll back now, rather than on a background task when dropped, so a
    /// retry does not race the rollback for the rows.
    async fn rollback(mut self) {
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
        if !self.in_transaction {
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
    let mut attempt = 1;
    loop {
        let result = run_once(pool, commit, &mut f).await;
        match result {
            Err(error) if error.is_retryable() && attempt < MAX_ATTEMPTS => {
                warn!("[TXN RETRY] {{{name}}} {error} {attempt}/{MAX_ATTEMPTS}");
                attempt += 1;
            }
            result => return result,
        }
    }
}

async fn run_once<'env, T, F>(
    pool: &ConnectionPool,
    commit: bool,
    f: &mut F,
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
    let txn = WriteTxn::begin(conn).await?;
    match f(&txn, &&()).await {
        Ok(value) if commit => {
            txn.commit().await?;
            Ok(value)
        }
        Ok(value) => {
            txn.rollback().await;
            Ok(value)
        }
        Err(error) => {
            txn.rollback().await;
            Err(error)
        }
    }
}

/// The batch's row count, or `None` for an empty batch. All columns must
/// have one value per row.
fn batch_rows(table: &str, columns: &[Column<'_>]) -> Result<Option<usize>, WriteError> {
    batch_rows_of(table, &columns.iter().collect::<Vec<_>>())
}

fn batch_rows_of(table: &str, columns: &[&Column<'_>]) -> Result<Option<usize>, WriteError> {
    let Some(first) = columns.first() else {
        return Err(anyhow::anyhow!("a batch for {table} with no columns").into());
    };
    if let Some(column) = columns.iter().find(|c| c.rows != first.rows) {
        return Err(anyhow::anyhow!(
            "a batch for {table} with {} values of {} and {} of {}",
            first.rows,
            first.name,
            column.rows,
            column.name
        )
        .into());
    }
    Ok((first.rows > 0).then_some(first.rows))
}

/// `INSERT INTO table (a, b) SELECT * FROM UNNEST($1::text[], $2::bigint[])`.
fn unnest_insert(table: &str, columns: &[Column<'_>]) -> String {
    unnest_insert_of(table, &columns.iter().collect::<Vec<_>>())
}

fn unnest_insert_of(table: &str, columns: &[&Column<'_>]) -> String {
    let names: Vec<&str> = columns.iter().map(|c| c.name).collect();
    let arrays: Vec<String> = columns
        .iter()
        .enumerate()
        .map(|(i, c)| format!("${}::{}[]", i + 1, c.sql_type))
        .collect();
    format!(
        "INSERT INTO {table} ({}) SELECT * FROM UNNEST({})",
        names.join(", "),
        arrays.join(", ")
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
        assert!(batch_rows("t", &columns).is_err());
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
