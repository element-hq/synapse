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
use bb8_postgres::tokio_postgres::{
    self,
    types::{ToSql, Type},
    IsolationLevel,
};
use bb8_postgres::PostgresConnectionManager;
use futures::future::BoxFuture;
use postgres_native_tls::MakeTlsConnector;

use crate::storage::db::{DatabasePool, Row, Transaction, Value};

/// Native Rust database access backed by `tokio-postgres` (for use in synapse-rust-apps)
pub struct RustDatabasePool {
    db_pool: bb8::Pool<PostgresConnectionManager<MakeTlsConnector>>,
}

impl DatabasePool for RustDatabasePool {
    async fn run_interaction<R, F>(&self, _name: &'static str, func: F) -> anyhow::Result<R>
    where
        R: Send + 'static,
        F: for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, anyhow::Result<R>>
            + Send
            + Sync
            + 'static,
    {
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
    async fn query(
        &mut self,
        sql: &str,
        args: &[&str],
    ) -> Result<Vec<Box<dyn Row>>, anyhow::Error> {
        // Synapse SQL uses `?` placeholders; `tokio-postgres` uses `$1`, `$2`, ...
        let sql = convert_param_style(sql);

        let params: Vec<&(dyn ToSql + Sync)> =
            args.iter().map(|arg| arg as &(dyn ToSql + Sync)).collect();
        let rows = self
            .txn
            .query(&sql, &params)
            .await
            .context("Failed to run query")?;

        let out = rows
            .into_iter()
            .map(|row| Box::new(TokioPostgresRow { row }) as Box<dyn Row>)
            .collect();

        Ok(out)
    }
}

/// A [`Row`] backed by a native [`tokio_postgres::Row`].
#[derive(Debug)]
struct TokioPostgresRow {
    row: tokio_postgres::Row,
}

impl Row for TokioPostgresRow {
    fn len(&self) -> usize {
        self.row.len()
    }

    fn value(&self, index: usize) -> Result<Value, anyhow::Error> {
        let column = self.row.columns().get(index).ok_or_else(|| {
            anyhow::anyhow!(
                "tried to get column {index} but the row only has {} column(s)",
                self.row.len()
            )
        })?;

        // Dispatch on the column's Postgres type, leaning on `tokio-postgres`'s
        // own `FromSql` impls to read the cell. Everything is read as
        // `Option<_>` so a SQL `NULL` becomes `Value::Null`.
        let ty = column.type_();
        let value = if *ty == Type::BOOL {
            self.row
                .try_get::<_, Option<bool>>(index)?
                .map_or(Value::Null, Value::Bool)
        } else if *ty == Type::INT2 {
            self.row
                .try_get::<_, Option<i16>>(index)?
                .map_or(Value::Null, |v| Value::Int(v.into()))
        } else if *ty == Type::INT4 {
            self.row
                .try_get::<_, Option<i32>>(index)?
                .map_or(Value::Null, |v| Value::Int(v.into()))
        } else if *ty == Type::INT8 {
            self.row
                .try_get::<_, Option<i64>>(index)?
                .map_or(Value::Null, Value::Int)
        } else if *ty == Type::FLOAT4 {
            self.row
                .try_get::<_, Option<f32>>(index)?
                .map_or(Value::Null, |v| Value::Float(v.into()))
        } else if *ty == Type::FLOAT8 {
            self.row
                .try_get::<_, Option<f64>>(index)?
                .map_or(Value::Null, Value::Float)
        } else if *ty == Type::TEXT
            || *ty == Type::VARCHAR
            || *ty == Type::BPCHAR
            || *ty == Type::NAME
        {
            self.row
                .try_get::<_, Option<String>>(index)?
                .map_or(Value::Null, Value::Text)
        } else if *ty == Type::BYTEA {
            self.row
                .try_get::<_, Option<Vec<u8>>>(index)?
                .map_or(Value::Null, Value::Bytes)
        } else {
            anyhow::bail!("unsupported Postgres column type `{ty}` at column {index}");
        };

        Ok(value)
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
