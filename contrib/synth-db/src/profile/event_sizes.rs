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

//! The `event_sizes` section: a log2 histogram of serialised event size per
//! (type, is_state), from a block sample of `event_json`.

use std::collections::HashMap;

use anyhow::Result;
use tracing::info;

use super::Ctx;
use crate::anon::log2_bucket;
use crate::db::{explain, stream_rows};
use crate::model::EventSizeRow;
use crate::out::SectionWriter;

const SQL: &str = "
SELECT e.type, e.state_key IS NOT NULL, length(ej.json)
FROM event_json ej TABLESAMPLE SYSTEM ($1::real)
JOIN events e ON e.event_id = ej.event_id
";

pub async fn run(ctx: &Ctx) -> Result<u64> {
    let client = ctx.pool.get().await?;
    let pct = ctx.event_sample_percent;
    if ctx.args.explain {
        info!(
            "event_sizes plan:\n{}",
            explain(&client, SQL, &[&pct]).await?
        );
    }
    info!(percent = pct, "sampling event_json");
    let mut acc: HashMap<(String, bool), EventSizeRow> = HashMap::new();
    let sampled = stream_rows(&client, SQL, &[&pct], |row| {
        let t: String = row.get(0);
        let state: bool = row.get(1);
        let len = row.get::<_, i32>(2).max(0) as u64;
        let key = (ctx.anon.type_name(&t), state);
        let e = acc.entry(key.clone()).or_insert_with(|| EventSizeRow {
            t: key.0,
            state,
            count: 0,
            bytes: 0,
            buckets: Vec::new(),
        });
        let b = log2_bucket(len);
        if e.buckets.len() <= b {
            e.buckets.resize(b + 1, 0);
        }
        e.buckets[b] += 1;
        e.count += 1;
        e.bytes += len;
        Ok(())
    })
    .await?;
    info!(sampled, types = acc.len(), "event sizes sampled");
    let mut rows: Vec<_> = acc.into_values().collect();
    rows.sort_by_key(|r| std::cmp::Reverse(r.count));
    let mut w = SectionWriter::create(&ctx.out, "event_sizes")?;
    for r in &rows {
        w.write(r)?;
    }
    w.finish()
}
