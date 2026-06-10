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

pub mod python_db_pool;
pub mod rust_db_pool;

/// A single database row, represented as the textual value of each column.
///
/// This is intentionally a lossy, engine-agnostic representation: it is the
/// lowest common denominator that both the Python (`LoggingTransaction`) and
/// native `tokio-postgres` backends can produce. Callers are responsible for
/// parsing the strings into richer types as needed.
pub type Row = Vec<String>;

/// The type-erased result of a `run_interaction` callback.
///
/// We box the result as `dyn Any` so that the [`DatabasePool`] trait can stay
/// object-safe (and therefore usable as `Box<dyn DatabasePool>`) while still
/// allowing callbacks to return an arbitrary `R`. The ergonomic, generic
/// [`DatabasePoolExt::run_interaction`] downcasts this back to the concrete
/// type for the caller.
pub type AnyResult = anyhow::Result<Box<dyn Any + Send>>;

/// A type-erased `run_interaction` callback.
///
/// The callback is given a [`Transaction`] and returns a boxed future
/// resolving to a type-erased result. It may be invoked multiple times under
/// certain failure modes (serialization and deadlock errors), so it is `Fn`
/// rather than `FnOnce`.
pub type InteractionFn =
    Box<dyn for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, AnyResult> + Send>;

/// A database connection pool.
///
/// We use a `Box<dyn DatabasePool>` so the same code can run against either the
/// Python-backed pool (in Synapse, see [`python_db_pool`]) or a native
/// `tokio-postgres` pool (in `synapse-rust-apps`, see [`rust_db_pool`]). To keep
/// the trait object-safe, the only required method is the type-erased
/// [`Self::run_interaction_erased`]; prefer the generic
/// [`DatabasePoolExt::run_interaction`] at call sites.
///
/// `Send + Sync` so it can be stored in a `#[pyclass]` and shared across threads.
#[async_trait::async_trait]
pub trait DatabasePool: Send + Sync {
    /// Starts a transaction on the database and runs the given (type-erased)
    /// function.
    ///
    /// The given `func` may be called multiple times under certain failure
    /// modes (like serialization and deadlock errors).
    async fn run_interaction_erased(
        &self,
        name: &'static str,
        func: InteractionFn,
    ) -> AnyResult;
}

/// Ergonomic, generic extension to [`DatabasePool`].
///
/// This is automatically implemented for every `DatabasePool` (including
/// `dyn DatabasePool`) via the blanket impl below, and provides the typed
/// `run_interaction` that callers actually use. It lives in a separate trait
/// (rather than on `DatabasePool` directly) because a generic method would make
/// `DatabasePool` no longer object-safe.
pub trait DatabasePoolExt: DatabasePool {
    /// Starts a transaction on the database and runs the given function,
    /// returning its result.
    ///
    /// The given `func` may be called multiple times under certain failure
    /// modes (like serialization and deadlock errors).
    fn run_interaction<R, F>(
        &self,
        name: &'static str,
        func: F,
    ) -> impl Future<Output = anyhow::Result<R>> + Send
    where
        R: Send + 'static,
        F: for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, anyhow::Result<R>>
            + Send
            + Sync
            + 'static,
    {
        // Erase the concrete return type `R` into `Box<dyn Any>` so we can call
        // through the object-safe `run_interaction_erased`.
        let erased: InteractionFn = Box::new(move |txn| {
            let fut = func(txn);
            Box::pin(async move {
                let value = fut.await?;
                Ok(Box::new(value) as Box<dyn Any + Send>)
            })
        });

        async move {
            let boxed = self.run_interaction_erased(name, erased).await?;
            Ok(*boxed
                .downcast::<R>()
                .expect("run_interaction return type mismatch (this is a Synapse programming error)"))
        }
    }
}

impl<T: DatabasePool + ?Sized> DatabasePoolExt for T {}

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait Transaction: Send {
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error>;
}
