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

pub mod python_db_pool;
pub mod rust_db_pool;

// Using `Send + Sync` traits so this can stored in the `#[pyclass]` just fine
#[async_trait::async_trait]
pub trait DatabasePool: Send + Sync {
    /// Starts a transaction on the database and runs a given function
    ///
    /// The given `func` may be called multiple times under certain failure modes (like
    /// serialization and deadlock errors).
    fn run_interaction<'txn, R, F>(
        &'txn self,
        name: &'static str,
        func: F,
    ) -> impl Future<Output = anyhow::Result<R>> + 'txn
    where
        R: Send + Sync + 'static,
        // TODO: Can we use `&'f mut dyn Txn` here?
        F: for<'f> Fn(&'f mut dyn Transaction<'f>) -> BoxFuture<'f, anyhow::Result<R>>
            + Send
            + 'static;
}

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait Transaction {
    async fn query(&self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error>;
    async fn commit(self) -> Result<(), anyhow::Error>;
}

pub type Row = Vec<String>;
