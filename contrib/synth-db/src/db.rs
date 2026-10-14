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

//! Connection pool and streaming query helpers.

use std::collections::HashMap;
use std::str::FromStr;

use anyhow::{Context, Result};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use futures_util::{pin_mut, TryStreamExt};
use tokio_postgres::types::ToSql;
use tokio_postgres::{Client, NoTls, Row};

pub struct DbOptions {
    pub url: String,
    pub concurrency: usize,
    pub statement_timeout_secs: u64,
    pub work_mem: String,
}

pub fn make_pool(opts: &DbOptions) -> Result<Pool> {
    let mut config = tokio_postgres::Config::from_str(&opts.url).context("parsing database URL")?;
    // Session-level safety: nothing this tool does should ever write, and a
    // runaway query should give up rather than sit on the primary for hours.
    config.options(format!(
        "-c default_transaction_read_only=on -c statement_timeout={}s -c work_mem={} -c application_name=synth-db",
        opts.statement_timeout_secs, opts.work_mem
    ));
    let mgr = Manager::from_config(
        config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );
    Ok(Pool::builder(mgr).max_size(opts.concurrency).build()?)
}

/// Run `sql` and feed every row to `f` as it arrives, without buffering the
/// whole result set in memory.
pub async fn stream_rows<F>(
    client: &Client,
    sql: &str,
    params: &[&(dyn ToSql + Sync)],
    mut f: F,
) -> Result<u64>
where
    F: FnMut(Row) -> Result<()>,
{
    let stmt = client.prepare(sql).await.context("preparing statement")?;
    let stream = client
        .query_raw(&stmt, params.iter().copied())
        .await
        .context("executing query")?;
    pin_mut!(stream);
    let mut n = 0u64;
    while let Some(row) = stream.try_next().await.context("reading row")? {
        f(row)?;
        n += 1;
    }
    Ok(n)
}

/// `EXPLAIN` a query and return the plan text, for `--explain` runs.
pub async fn explain(client: &Client, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Result<String> {
    let explain_sql = format!("EXPLAIN {sql}");
    let rows = client.query(&explain_sql, params).await?;
    Ok(rows
        .iter()
        .map(|r| r.get::<_, String>(0))
        .collect::<Vec<_>>()
        .join("\n"))
}

/// Approximate row counts for every table in the public schema, from the
/// planner statistics. Free, and good enough for calibration.
pub async fn table_estimates(client: &Client) -> Result<HashMap<String, TableEstimate>> {
    let rows = client
        .query(
            "SELECT c.relname, c.reltuples::bigint, pg_total_relation_size(c.oid)
             FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind = 'r' AND n.nspname = 'public'",
            &[],
        )
        .await?;
    Ok(rows
        .into_iter()
        .map(|r| {
            (
                r.get::<_, String>(0),
                TableEstimate {
                    rows: r.get::<_, i64>(1).max(0),
                    bytes: r.get::<_, i64>(2).max(0),
                },
            )
        })
        .collect())
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TableEstimate {
    pub rows: i64,
    pub bytes: i64,
}
