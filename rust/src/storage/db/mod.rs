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

#[async_trait::async_trait]
pub trait DatabasePool {
    /// TODO
    async fn get_connection(&self) -> Result<Box<dyn DatabaseConnection>, anyhow::Error>;
}

/// A `tokio_postgres` Connection looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait DatabaseConnection {
    /// TODO
    ///
    /// Arguments:
    /// description of the transaction, for logging and metrics
    async fn get_transaction(
        &self,
        description: &str,
    ) -> Result<Box<dyn Transaction>, anyhow::Error>;
}

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
#[async_trait::async_trait]
pub trait Transaction {
    async fn query(&self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error>;
    async fn commit(self) -> Result<(), anyhow::Error>;
}

pub type Row = Vec<String>;
