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

use crate::storage::db::{
    DatabasePool, DbRow, DbValue, ErasedInteraction, ErasedResult, Transaction,
};

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
pub struct RustDatabasePool {
    db_pool: bb8::Pool<PostgresConnectionManager<MakeTlsConnector>>,
}

#[async_trait::async_trait]
impl DatabasePool for RustDatabasePool {
    async fn run_interaction_erased(
        &self,
        _name: &'static str,
        func: ErasedInteraction,
    ) -> ErasedResult {
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
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<DbRow>, anyhow::Error> {
        // Synapse SQL uses `?` placeholders; `tokio-postgres` uses `$1`, `$2`, ...
        let sql = convert_param_style(sql);

        let params: Vec<&(dyn ToSql + Sync)> =
            args.iter().map(|arg| arg as &(dyn ToSql + Sync)).collect();
        let rows = self
            .txn
            .query(&sql, &params)
            .await
            .context("Failed to run query")?;

        rows.iter().map(tokio_row_to_row).collect()
    }
}

/// Convert a native [`tokio_postgres::Row`] into our engine-agnostic [`Row`].
fn tokio_row_to_row(row: &tokio_postgres::Row) -> Result<DbRow, anyhow::Error> {
    (0..row.len())
        .map(|index| tokio_cell_to_value(row, index))
        .collect()
}

/// Convert a single cell of a [`tokio_postgres::Row`] into a [`DbValue`].
///
/// Dispatch on the column's Postgres type, leaning on `tokio-postgres`'s own
/// `FromSql` impls to read the cell. Everything is read as `Option<_>` so a SQL
/// `NULL` becomes `DbValue::Null`.
fn tokio_cell_to_value(row: &tokio_postgres::Row, index: usize) -> Result<DbValue, anyhow::Error> {
    if let Ok(value) = row.try_get::<_, Option<bool>>(index) {
        Ok(value.map_or(DbValue::Null, DbValue::Bool))
    } else if let Ok(value) = row.try_get::<_, Option<i64>>(index) {
        Ok(value.map_or(DbValue::Null, DbValue::Int))
    } else if let Ok(value) = row.try_get::<_, Option<f64>>(index) {
        Ok(value.map_or(DbValue::Null, DbValue::Float))
    } else if let Ok(value) = row.try_get::<_, Option<String>>(index) {
        Ok(value.map_or(DbValue::Null, DbValue::Text))
    } else {
        let ty = row.columns()[index].type_();
        anyhow::bail!(
            "Unsupported `tokio-postgres` type {} encountered when trying to convert it \
            to our generic database `DbValue` type. You probably just need to implement it in `tokio_cell_to_value(...)`.",
            ty
        )
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
