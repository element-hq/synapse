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

use std::any::Any;
use std::future::Future;

use futures::future::BoxFuture;
use futures::FutureExt;

pub mod python_db_pool;

/// A type-erased `run_interaction` callback.
///
/// This is the dyn-compatible form of the `func` passed to
/// [`DatabasePoolExt::run_interaction`].
///
/// The ergonomic [`DatabasePoolExt::run_interaction`] handles the boxing and downcasts
/// the result back to `R` for the caller.
///
/// It may be invoked multiple times under certain failure modes (serialization
/// and deadlock errors), so it is `Fn` rather than `FnOnce`.
pub type ErasedInteraction =
    Box<dyn for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, ErasedResult> + Send>;

/// The type-erased *result* of an [`ErasedInteraction`]
/// [`DatabasePool::run_interaction_erased`].
pub type ErasedResult = anyhow::Result<Box<dyn Any + Send>>;

/// A database connection pool.
///
/// Held behind a trait object (e.g. `Box<dyn DatabasePool>`) as it can be backed by
/// either the Python-backed pool (in Synapse, see [`python_db_pool`]) or a Rust native
/// `tokio-postgres` pool (expected to be used in `synapse-rust-apps`).
///
/// To keep the trait dyn-compatible, we have to specify a type-erased
/// [`run_interaction_erased`](Self::run_interaction_erased) version; callers should
/// prefer the ergonomic, generic [`run_interaction`](DatabasePoolExt::run_interaction).
///
/// `Send + Sync` so it can be stored in a `#[pyclass]` and shared across threads.
#[async_trait::async_trait]
pub trait DatabasePool: Send + Sync {
    /// Starts a transaction on the database and runs the given (type-erased)
    /// `func`, returning its boxed result.
    ///
    /// Implementors implement this; callers should prefer
    /// [`DatabasePoolExt::run_interaction`], which boxes up the result and
    /// downcasts it back to the concrete type for you.
    async fn run_interaction_erased(
        &self,
        name: &'static str,
        func: ErasedInteraction,
    ) -> ErasedResult;
}

/// Ergonomic, strongly-typed access to a [`DatabasePool`].
pub trait DatabasePoolExt: DatabasePool {
    /// Starts a transaction on the database and runs the given function,
    /// returning its result.
    ///
    /// `name` should be a descriptive identifier for logging/metrics
    ///
    /// `func` may be called multiple times under certain failure modes (like
    /// serialization and deadlock errors), so it is `Fn` rather than `FnOnce`.
    ///
    /// `func` is async but you should only call `.await` on [`Transaction`] methods.
    /// This is a minor cosmetic flaw but seems fine, as you don't want to be doing any
    /// unnecessary waiting in your transaction anyway.
    ///
    /// Usage:
    /// ```rust
    /// db_pool
    /// .run_interaction(|txn| {
    ///     async move {
    ///         /* do stuff with txn */
    ///     }
    ///     .boxed()
    /// })
    /// ```
    //
    // Ideally, this method signature would be slightly different to allow downstream
    // usage to look like the following (simpler) but because we allow the work to
    // happen on other threads, the `Future` needs to be `Send`; As of 2026-06-22, the
    // `AsyncFn` trait has no stable way to express that "the future this async closure
    // produces is `Send`". The intended fix is probably return-type-notation
    // (https://github.com/rust-lang/rust/issues/109417).
    // ```
    // db_pool.run_interaction("description", async move |txn| {
    //     /* do stuff with txn */
    // })
    // ```
    //
    // Refs:
    //  - [RFC 3668: Async closures](https://github.com/rust-lang/rfcs/pull/3668)
    //  - [RFC 3654: Return Type Notation](https://github.com/rust-lang/rfcs/pull/3654)
    //  - [Tracking Issue for return type notation](https://github.com/rust-lang/rust/issues/109417)
    fn run_interaction<R, F>(
        &self,
        name: &'static str,
        func: F,
    ) -> impl Future<Output = anyhow::Result<R>> + Send
    where
        R: Send + 'static,
        F: for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, anyhow::Result<R>>
            + Send
            + 'static,
    {
        // Erase the concrete return type `R` into `Box<dyn Any>` so we can call
        // through the dyn-compatible `run_interaction_erased`.
        let erased: ErasedInteraction = Box::new(move |txn| {
            let fut = func(txn);
            async move { Ok(Box::new(fut.await?) as Box<dyn Any + Send>) }.boxed()
        });

        async move {
            let boxed = self.run_interaction_erased(name, erased).await?;
            Ok(*boxed.downcast::<R>().expect(
                "run_interaction return type mismatch (this is a Synapse programming error)",
            ))
        }
    }
}

/// Blanket-implemented for every [`DatabasePool`] so
/// [`run_interaction`](DatabasePoolExt::run_interaction) is always available
impl<T: DatabasePool + ?Sized> DatabasePoolExt for T {}

/// A transaction to interact with the database
///
/// Based on the ergonomics of  [`tokio_postgres::Transaction`]
#[async_trait::async_trait]
pub trait Transaction: Send {
    /// Run a database query, returning a list of resulting rows.
    ///
    /// We expect the `sql` query should use `?` placeholders for the `args`. Downstream
    /// implementations should string-replace `?` as necessary.
    //
    // `async` as this  is representing a round-trip between the app and database
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<DbRow>, anyhow::Error>;
}

/// A single backend-agnostic value within a [`DbRow`].
///
/// Each pool maps the values its database driver hands back into this common
/// set, so callers can work with one representation regardless of engine.
#[derive(Debug, Clone, PartialEq)]
pub enum DbValue {
    /// A SQL `NULL`.
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
}

/// A row of data returned from the database by a query.
///
/// Each pool converts the cells its database driver hands back into the
/// engine-agnostic [`DbValue`] representation, so a row is simply a list of them.
/// Values are pulled out by their numeric index with [`DbRowExt::try_get`].
pub type DbRow = Vec<DbValue>;

/// Extension methods for reading typed values out of a [`DbRow`].
///
/// Based on [`tokio_postgres::Row`]'s `try_get`: [`try_get`](Self::try_get)
/// converts the [`DbValue`] at a given index into the requested type via
/// [`FromDbValue`] (our analogue of `tokio-postgres`'s `FromSql`).
pub trait DbRowExt {
    /// Deserializes a value from the row, specified by its numeric index,
    /// returning an error if the index is out of bounds or the value cannot be
    /// converted into `T`.
    fn try_get<T: FromDbValue>(&self, index: usize) -> Result<T, anyhow::Error>;
}

impl DbRowExt for DbRow {
    fn try_get<T: FromDbValue>(&self, index: usize) -> Result<T, anyhow::Error> {
        let value = self.get(index).cloned().ok_or_else(|| {
            anyhow::anyhow!(
                "tried to get column {index} but the row only has {} column(s)",
                self.len()
            )
        })?;

        T::from_value(value)
    }
}

/// Converts a backend-agnostic [`DbValue`] into a concrete Rust type, analogous to
/// `tokio-postgres`'s `FromSql`.
pub trait FromDbValue: Sized {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error>;
}

impl FromDbValue for bool {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Bool(b) => Ok(b),
            // SQLite has no native boolean type and stores them as integers.
            DbValue::Int(i) => match i {
                0 => Ok(false),
                1 => Ok(true),
                _ => anyhow::bail!("cannot read DbValue::Int({i}) as bool"),
            },
            other => anyhow::bail!("cannot read {other:?} as bool"),
        }
    }
}

impl FromDbValue for i64 {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Int(i) => Ok(i),
            other => anyhow::bail!("cannot read {other:?} as i64"),
        }
    }
}

impl FromDbValue for f64 {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Float(f) => Ok(f),
            other => anyhow::bail!("cannot read {other:?} as f64"),
        }
    }
}

impl FromDbValue for String {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Text(s) => Ok(s),
            other => anyhow::bail!("cannot read {other:?} as String"),
        }
    }
}

impl<T: FromDbValue> FromDbValue for Option<T> {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Null => Ok(None),
            other => Ok(Some(T::from_value(other)?)),
        }
    }
}
