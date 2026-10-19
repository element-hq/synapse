/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! A [`Querier`] over the native driver's pool, outside any transaction.

use anyhow::Context;
use async_trait::async_trait;
use tokio_postgres::{types::ToSql, Row};

use crate::database::postgres::pool::{ConnectionPool, PooledConnection};
use crate::logging::opentracing::{db_query_span, traced_query};
use crate::storage::events::Querier;

/// Runs each statement on its own, in autocommit, on one connection checked
/// out of the pool, which goes back when this is dropped.
///
/// The connection is checked out up front, before the caller claims any
/// event loads in the event cache. A load claimed first and then left
/// waiting for a connection could stall until the checkout times out: the
/// Rust readers that join it hold pooled connections while they wait, and
/// enough of them leave none for the load.
///
/// Traced as a `db.connection` span from the checkout until the drop, with a
/// `db.query` child per statement.
pub struct PoolQuerier {
    conn: PooledConnection,
    span: tracing::Span,
}

impl PoolQuerier {
    pub async fn checkout(pool: &ConnectionPool) -> anyhow::Result<Self> {
        let waiting = std::time::Instant::now();
        let conn = pool
            .get()
            .await
            .map_err(|e| anyhow::anyhow!("checking out a database connection: {e}"))?;
        // The wait goes on the caller's span, as on the write path: Python's
        // `db.connection` starts once the connection is in hand.
        tracing::info!(
            message = "checked out",
            wait_ms = waiting.elapsed().as_secs_f64() * 1000.0
        );
        // Never entered, so it lasts until it closes: when the querier and
        // the queries it parents are done.
        let span = tracing::info_span!("db.connection");
        Ok(PoolQuerier { conn, span })
    }
}

#[async_trait]
impl Querier for PoolQuerier {
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>> {
        let span = db_query_span(Some(&self.span), sql);
        traced_query(span, async {
            // A statement missing from the cache costs a prepare round trip.
            let statement = match self.conn.cached(sql) {
                Some(statement) => statement,
                None => {
                    tracing::Span::current().record("db.prepared", true);
                    self.conn
                        .prepare_cached(sql, false)
                        .await
                        .with_context(|| format!("preparing {}", sql.trim()))?
                }
            };
            self.conn
                .query(&statement, params)
                .await
                .with_context(|| format!("running {}", sql.trim()))
        })
        .await
    }
}

#[cfg(test)]
pub(crate) mod tests {
    //! Against a real server: these run when `SYNAPSE_TEST_POSTGRES_DSN` is
    //! set (e.g. to `host=postgres user=postgres password=postgres`), and
    //! otherwise do nothing.

    use tokio::runtime::Runtime;

    use super::*;
    use crate::database::postgres::pool::{create_pool, PoolConfig};
    use crate::logging::opentracing::layer::testing::{tag, traced};
    use crate::logging::opentracing::thrift::TagValue;
    use crate::logging::opentracing::SamplerConfig;

    pub(crate) fn test_pool() -> Option<(Runtime, ConnectionPool)> {
        let Ok(dsn) = std::env::var("SYNAPSE_TEST_POSTGRES_DSN") else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return None;
        };
        // Current-thread, so that spans are only entered on the thread whose
        // subscriber `traced` sets.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let pool = create_pool(
            &dsn,
            rt.handle().clone(),
            PoolConfig {
                max_size: 2,
                ..PoolConfig::default()
            },
        )
        .unwrap();
        Some((rt, pool))
    }

    /// A `db.connection` span from checkout to drop, with a `db.query` per
    /// statement saying whether it needed a prepare.
    #[test]
    fn traced_queries() {
        let Some((rt, pool)) = test_pool() else {
            return;
        };
        let (_, spans) = traced(SamplerConfig::Const(true), || {
            rt.block_on(async {
                let querier = PoolQuerier::checkout(&pool).await.unwrap();
                // A statement no other test prepares, then again cached.
                let sql = "SELECT 1 AS pool_querier_traced\n  WHERE true";
                querier.query(sql, &[]).await.unwrap();
                querier.query(sql, &[]).await.unwrap();
                querier
                    .query("SELECT nonsense FROM", &[])
                    .await
                    .unwrap_err();
            })
        });

        let [first, second, failed, connection] = &spans[..] else {
            panic!("expected four spans: {spans:#?}")
        };
        assert_eq!(connection.operation, "db.connection");
        for query in [first, second, failed] {
            assert_eq!(query.operation, "db.query");
            assert_eq!(query.parent_id, connection.span_id);
        }
        assert_eq!(
            tag(first, "db.statement"),
            Some(&TagValue::Str(
                "SELECT 1 AS pool_querier_traced WHERE true".into()
            ))
        );
        assert_eq!(tag(first, "db.prepared"), Some(&TagValue::Bool(true)));
        assert_eq!(tag(second, "db.prepared"), None);
        // Starts once the connection is in hand, and lasts until the querier
        // is dropped, after its queries.
        assert!(connection.start <= first.start);
        assert!(
            connection.start + connection.duration >= failed.start + failed.duration,
            "{connection:?} ends before {failed:?}"
        );
        assert_eq!(tag(failed, "error"), Some(&TagValue::Bool(true)));
        assert_eq!(tag(first, "error"), None);
    }
}
