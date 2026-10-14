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

//! The `room_detail` section: for a sample of rooms, the non-member current
//! state by type and the member events by (server, membership). Both come
//! from indexed per-room lookups on `current_state_events`, so the cost
//! scales with the sample, not the table.

use anyhow::Result;
use rand::seq::SliceRandom;
use tracing::info;

use super::Ctx;
use crate::model::RoomDetailRow;
use crate::out::SectionWriter;

const STATE_SQL: &str = "
SELECT type, count(*) FROM current_state_events
WHERE room_id = $1 AND type <> 'm.room.member'
GROUP BY type
";

const MEMBERS_SQL: &str = "
SELECT substr(state_key, position(':' in state_key) + 1), coalesce(membership, ''), count(*)
FROM current_state_events
WHERE room_id = $1 AND type = 'm.room.member'
GROUP BY 1, 2
";

/// Pick the sample: the largest quarter by current state size (these
/// dominate sync cost and are too rare to hit randomly) plus a uniform
/// random draw from the rest.
pub fn pick_sample(ctx: &Ctx) -> Vec<u32> {
    let n = ctx.args.sample_rooms.min(ctx.room_raw.len());
    if n == 0 {
        return Vec::new();
    }
    let mut by_size: Vec<u32> = (0..ctx.room_raw.len() as u32).collect();
    by_size.sort_by_key(|&i| std::cmp::Reverse(ctx.room_state_events[i as usize]));
    let top = n / 4;
    let mut sample: Vec<u32> = by_size[..top].to_vec();
    let mut rest = by_size[top..].to_vec();
    rest.shuffle(&mut rand::thread_rng());
    sample.extend_from_slice(&rest[..(n - top).min(rest.len())]);
    sample.sort_unstable();
    sample
}

pub async fn run(ctx: &Ctx) -> Result<u64> {
    let sample = pick_sample(ctx);
    info!(rooms = sample.len(), "sampling room detail");
    let client = ctx.pool.get().await?;
    let state_stmt = client.prepare(STATE_SQL).await?;
    let members_stmt = client.prepare(MEMBERS_SQL).await?;
    let mut w = SectionWriter::create(&ctx.out, "room_detail")?;
    for r in sample {
        let raw = &ctx.room_raw[r as usize];
        let state_types = client
            .query(&state_stmt, &[raw])
            .await?
            .into_iter()
            .map(|row| (ctx.anon.type_name(row.get(0)), row.get::<_, i64>(1)))
            .collect();
        let members = client
            .query(&members_stmt, &[raw])
            .await?
            .into_iter()
            .map(|row| {
                (
                    ctx.anon.server(row.get(0)),
                    row.get::<_, String>(1),
                    row.get::<_, i64>(2),
                )
            })
            .collect();
        w.write(&RoomDetailRow {
            r,
            state_types,
            members,
        })?;
    }
    w.finish()
}
