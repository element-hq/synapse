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
use crate::storage::events::Querier;

/// Runs each statement on its own, in autocommit, on one connection checked
/// out of the pool, which goes back when this is dropped.
///
/// The connection is checked out up front, before the caller claims any
/// event loads in the event cache. A load claimed first and then left
/// waiting for a connection could stall until the checkout times out: the
/// Rust readers that join it hold pooled connections while they wait, and
/// enough of them leave none for the load.
pub struct PoolQuerier {
    conn: PooledConnection,
}

impl PoolQuerier {
    pub async fn checkout(pool: &ConnectionPool) -> anyhow::Result<Self> {
        let conn = pool
            .get()
            .await
            .map_err(|e| anyhow::anyhow!("checking out a database connection: {e}"))?;
        Ok(PoolQuerier { conn })
    }
}

#[async_trait]
impl Querier for PoolQuerier {
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>> {
        let statement = self
            .conn
            .prepare_cached(sql, false)
            .await
            .with_context(|| format!("preparing {}", sql.trim()))?;
        self.conn
            .query(&statement, params)
            .await
            .with_context(|| format!("running {}", sql.trim()))
    }
}
