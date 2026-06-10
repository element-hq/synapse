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
use bb8_postgres::tokio_postgres::{self, types::ToSql, IsolationLevel};
use bb8_postgres::PostgresConnectionManager;
use postgres_native_tls::MakeTlsConnector;

use crate::storage::db::{AnyResult, DatabasePool, InteractionFn, Row, Transaction};

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
pub struct RustDatabasePool {
    db_pool: bb8::Pool<PostgresConnectionManager<MakeTlsConnector>>,
}

#[async_trait::async_trait]
impl DatabasePool for RustDatabasePool {
    async fn run_interaction_erased(&self, _name: &'static str, func: InteractionFn) -> AnyResult {
        // Like Synapse's `runInteraction`, retry the whole transaction on
        // serialization/deadlock errors (which can happen under repeatable-read).
        loop {
            let mut conn = self
                .db_pool
                .get()
                .await
                .context("Failed to acquire database connection")?;

            // Repeatable-read isolation level (like Synapse).
            let txn = conn
                .build_transaction()
                .isolation_level(IsolationLevel::RepeatableRead)
                .start()
                .await
                .context("Failed to start transaction")?;

            let mut wrapper = TokioPostgresTransaction { txn };
            match func(&mut wrapper).await {
                Ok(value) => {
                    wrapper
                        .txn
                        .commit()
                        .await
                        .context("Failed to commit transaction")?;
                    return Ok(value);
                }
                Err(err) => {
                    // The transaction is rolled back implicitly when dropped, but
                    // be explicit about it before deciding whether to retry.
                    let _ = wrapper.txn.rollback().await;
                    if is_retryable(&err) {
                        continue;
                    }
                    return Err(err);
                }
            }
        }
    }
}

/// Whether a failed transaction should be retried (serialization/deadlock errors).
fn is_retryable(err: &anyhow::Error) -> bool {
    err.downcast_ref::<tokio_postgres::Error>()
        .and_then(|e| e.code())
        .map(|code| {
            *code == tokio_postgres::error::SqlState::T_R_SERIALIZATION_FAILURE
                || *code == tokio_postgres::error::SqlState::T_R_DEADLOCK_DETECTED
        })
        .unwrap_or(false)
}

struct TokioPostgresTransaction<'a> {
    txn: tokio_postgres::Transaction<'a>,
}

#[async_trait::async_trait]
impl Transaction for TokioPostgresTransaction<'_> {
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error> {
        // Synapse SQL uses `?` placeholders; `tokio-postgres` uses `$1`, `$2`, ...
        let sql = convert_param_style(sql);

        let params: Vec<&(dyn ToSql + Sync)> =
            args.iter().map(|arg| arg as &(dyn ToSql + Sync)).collect();
        let rows = self
            .txn
            .query(&sql, &params)
            .await
            .context("Failed to run query")?;

        let mut out: Vec<Row> = Vec::with_capacity(rows.len());
        for row in rows {
            let mut cells: Row = Vec::with_capacity(row.len());
            for i in 0..row.len() {
                // Best-effort textual extraction to match the engine-agnostic
                // `Row` type. A real implementation would map column types
                // properly; this only exists to prove the interface fits.
                let value: String = row.try_get(i).unwrap_or_default();
                cells.push(value);
            }
            out.push(cells);
        }

        Ok(out)
    }
}

/// Convert `?`-style placeholders into `tokio-postgres`'s `$1`, `$2`, ... style.
fn convert_param_style(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len());
    let mut n = 0;
    for ch in sql.chars() {
        if ch == '?' {
            n += 1;
            out.push('$');
            out.push_str(&n.to_string());
        } else {
            out.push(ch);
        }
    }
    out
}
