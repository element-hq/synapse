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

/// A database connection pool.
///
/// Code is written against this trait so the same store can run against either
/// the Python-backed pool (in Synapse, see [`python_db_pool`]) or a native
/// `tokio-postgres` pool (in `synapse-rust-apps`, see [`rust_db_pool`]). The pool
/// type is fixed within any given binary, so callers are generic over the pool
/// (e.g. `Store<P: DatabasePool>`) rather than using dynamic dispatch.
///
/// `Send + Sync` so it can be stored in a `#[pyclass]` and shared across threads.
pub trait DatabasePool: Send + Sync {
    /// Starts a transaction on the database and runs the given function,
    /// returning its result.
    ///
    /// `func` may be called multiple times under certain failure modes (like
    /// serialization and deadlock errors), so it is `Fn` rather than `FnOnce`.
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
            + 'static;
}

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait Transaction: Send {
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error>;
}
