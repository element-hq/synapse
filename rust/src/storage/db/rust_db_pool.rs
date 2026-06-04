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


// TODO: remove. This is just here to make sure our `DatabasePool`/`Transaction`
// interfaces are compatible with `tokio-postgres`.

use anyhow::Context;
use bb8_postgres::{tokio_postgres::Row, PostgresConnectionManager};
use postgres_native_tls::MakeTlsConnector;

use crate::storage::db::{DatabasePool, Transaction};

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
pub struct RustDatabasePool {
    db_pool: bb8::Pool<PostgresConnectionManager<MakeTlsConnector>>,
}

impl DatabasePool for RustDatabasePool {
    async fn get_transaction(&self, description: &str) -> dyn Transaction {
        let mut conn = self
            .db_pool
            .get()
            // .instrument(tracing::info_span!("acquire database connection"))
            .await
            .context("Failed to acquire database connection")?;

        let txn = conn
            .transaction()
            // .instrument(tracing::info_span!("start transaction"))
            .await
            .context("Failed to start transaction")?;

        // TODO: Set isolation level
        txn
    }
}

struct TokioPostgresTransaction<'a> {
    txn: bb8_postgres::tokio_postgres::Transaction<'a>,
}

impl Transaction for TokioPostgresTransaction<'_> {
    async fn query(&self, sql: &str, args: &[&str]) -> () {
        todo!("TODO");

        let rows = self.txn.query(sql, args).await?;

        rows
    }

    async fn commit(&self) -> () {
        self.txn
            .commit()
            // .instrument(tracing::info_span!("commit transaction"))
            .await
            .context("Failed to commit transaction")?;
    }
}
