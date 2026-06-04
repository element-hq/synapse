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

use crate::db::python_db_pool::DatabasePool as PythonDatabasePool;

/// A [`tokio_postgres::Transaction`] looking thing that we can use on the Rust side to
/// interact with the database
pub trait Transaction {
    pub fn query(&self, sql: &str, args: &[&str]) -> None {
        todo!("TODO");
    }
}

/// Database access backed by the database pool we already have in the Python side of Synapse
struct SynapsePythonTransaction {
    db_pool: PythonDatabasePool,
}

impl Transaction for SynapsePythonTransaction {
    fn query(&self, sql: &str, args: &[&str]) -> None {
        todo!("TODO");
    }
}

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
// struct TokioPostgresTransaction {
//     db_pool: bb8::Pool<PostgresConnectionManager<MakeTlsConnector>>,
// }

// impl Transaction for TokioPostgresTransaction {
//     fn query(&self, sql: &str, args: &[&str]) -> None {
//         todo!("TODO");

//         // TODO: Set isolation level

//         let mut conn = self
//             .db_pool
//             .get()
//             .instrument(tracing::info_span!("acquire database connection"))
//             .await
//             .map_err(|e| {
//                 sentry::capture_error(&e);
//                 tracing::error!(
//                     error = e.to_string(),
//                     "Failed to acquire database connection"
//                 );
//                 anyhow::anyhow!("Failed to acquire database connection: {e}")
//             })?;

//         let txn = conn
//             .transaction()
//             .instrument(tracing::info_span!("start transaction"))
//             .await
//             .context("Failed to start transaction")?;

//         let rows = txn.query(sql, args).await?;

//         rows
//     }
// }
