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

//! The `rates` and `rates_global` sections: event counts over the trailing
//! window, per room per day by type bucket and sender locality, plus a
//! server-wide hourly curve and distinct senders per room.
//!
//! The window is `origin_server_ts` in `[now - window, now)`, read through
//! the `events_ts (origin_server_ts, stream_ordering)` index. One range scan
//! feeds all three groupings via `GROUPING SETS`, so the window is read
//! exactly once. Backfilled events (negative stream ordering) are history
//! pulled in on join, not traffic, and are left out.

use std::collections::{HashMap, HashSet};

use anyhow::Result;
use tracing::info;

use super::Ctx;
use crate::db::{explain, stream_rows};
use crate::model::{RateRow, RatesGlobalRow};
use crate::out::SectionWriter;

/// `GROUPING(room_id, sender, day, hour)` bit masks for each grouping set.
const SET_ROOM_DAY: i32 = 0b0101;
const SET_HOUR: i32 = 0b1110;
const SET_ROOM_SENDER: i32 = 0b0011;

const SQL: &str = "
SELECT GROUPING(room_id, sender, day, hour)::int, room_id, sender, bucket, local, day, hour, count(*)
FROM (
    SELECT room_id, sender,
           CASE WHEN state_key IS NOT NULL THEN 'state'
                WHEN type IN ('m.room.message', 'm.room.encrypted') THEN type
                ELSE 'other' END AS bucket,
           sender LIKE ('%:' || $3::text) AS local,
           ((origin_server_ts - $1::bigint) / 86400000)::int AS day,
           ((origin_server_ts - $1::bigint) / 3600000)::int AS hour
    FROM events
    WHERE origin_server_ts >= $1::bigint AND origin_server_ts < $2::bigint
      AND stream_ordering > 0 AND NOT outlier AND rejection_reason IS NULL
) e
GROUP BY GROUPING SETS ((room_id, bucket, local, day), (hour, bucket, local), (room_id, sender))
";

const DAILY_ACTIVE_SQL: &str = "
SELECT ((timestamp - $1::bigint) / 86400000)::int AS day, count(DISTINCT user_id), count(*)
FROM user_daily_visits
WHERE timestamp >= $1::bigint
GROUP BY 1 ORDER BY 1
";

pub async fn run(ctx: &Ctx) -> Result<u64> {
    let client = ctx.pool.get().await?;
    let start = ctx.window_start_ms;
    let days = ctx.args.window_days as i32;
    let local = ctx.anon.local_server().to_owned();

    let end = ctx.now_ms;
    let params: [&(dyn tokio_postgres::types::ToSql + Sync); 3] = [&start, &end, &local];
    if ctx.args.explain {
        info!("rates plan:\n{}", explain(&client, SQL, &params).await?);
    }

    let mut per_room: HashMap<u32, RateRow> = HashMap::new();
    let mut senders: HashMap<u32, HashSet<String>> = HashMap::new();
    let mut global = RatesGlobalRow::default();
    let n_days = days as usize;

    let scanned = stream_rows(&client, SQL, &params, |row| {
        let set: i32 = row.get(0);
        let count = row.get::<_, i64>(7).max(0) as u64;
        match set {
            SET_HOUR => {
                let hour: i32 = row.get(6);
                let bucket: String = row.get(3);
                let local: bool = row.get(4);
                global.hours.push((hour, bucket, local, count));
            }
            SET_ROOM_DAY | SET_ROOM_SENDER => {
                let room_id: String = row.get(1);
                let Some(r) = ctx.rooms.get(&room_id) else {
                    if set == SET_ROOM_DAY {
                        global.events_in_unprofiled_rooms += count;
                    }
                    return Ok(());
                };
                if set == SET_ROOM_SENDER {
                    let sender: Option<String> = row.get(2);
                    if let Some(s) = sender {
                        senders.entry(r).or_default().insert(s);
                    }
                    return Ok(());
                }
                let day: i32 = row.get(5);
                let bucket: String = row.get(3);
                let local: bool = row.get(4);
                let e = per_room.entry(r).or_insert_with(|| RateRow {
                    r,
                    days: vec![0; n_days],
                    ..Default::default()
                });
                e.days[day as usize] += count;
                match e
                    .by
                    .iter_mut()
                    .find(|(b, l, _)| *b == bucket && *l == local)
                {
                    Some(slot) => slot.2 += count,
                    None => e.by.push((bucket, local, count)),
                }
            }
            other => anyhow::bail!("unexpected grouping set mask {other}"),
        }
        Ok(())
    })
    .await?;
    info!(scanned, rooms = per_room.len(), "rates aggregated");

    for (r, set) in senders {
        let e = per_room.entry(r).or_insert_with(|| RateRow {
            r,
            days: vec![0; n_days],
            ..Default::default()
        });
        for s in set {
            if ctx.anon.is_local(&s) {
                e.senders_local += 1;
            } else {
                e.senders_remote += 1;
            }
        }
    }

    global.hours.sort();
    let active = client.query(DAILY_ACTIVE_SQL, &[&start]).await?;
    global.daily_active = active
        .iter()
        .map(|row| {
            (
                row.get::<_, i32>(0),
                row.get::<_, i64>(1),
                row.get::<_, i64>(2),
            )
        })
        .collect();

    let mut rows: Vec<_> = per_room.into_values().collect();
    rows.sort_by_key(|r| r.r);
    let mut w = SectionWriter::create(&ctx.out, "rates")?;
    for r in &rows {
        w.write(r)?;
    }
    let n = w.finish()?;
    let mut g = SectionWriter::create(&ctx.out, "rates_global")?;
    g.write(&global)?;
    g.finish()?;
    Ok(n)
}
