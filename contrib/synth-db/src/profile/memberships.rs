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

//! The `memberships` section: the local-user/room graph, one row per
//! `local_current_membership` entry, decorated with the per-(user, room)
//! state sync has to load: notification counts, receipts, room account data
//! and tags.

use anyhow::Result;
use tracing::{info, warn};

use super::Ctx;
use crate::db::{explain, stream_rows};
use crate::model::MembershipRow;
use crate::out::SectionWriter;

const SQL: &str = "
SELECT lcm.user_id, lcm.room_id, lcm.membership, lcm.event_stream_ordering,
       eps.notif_count, eps.unread_count,
       coalesce(rad.n, 0), coalesce(rt.n, 0),
       rr.so, rp.so
FROM local_current_membership lcm
LEFT JOIN event_push_summary eps
       ON eps.user_id = lcm.user_id AND eps.room_id = lcm.room_id AND eps.thread_id = 'main'
LEFT JOIN (SELECT user_id, room_id, count(*) AS n FROM room_account_data GROUP BY 1, 2) rad
       ON rad.user_id = lcm.user_id AND rad.room_id = lcm.room_id
LEFT JOIN (SELECT user_id, room_id, count(*) AS n FROM room_tags GROUP BY 1, 2) rt
       ON rt.user_id = lcm.user_id AND rt.room_id = lcm.room_id
LEFT JOIN (SELECT user_id, room_id, max(event_stream_ordering) AS so FROM receipts_linearized
           WHERE receipt_type = 'm.read' AND thread_id IS NULL GROUP BY 1, 2) rr
       ON rr.user_id = lcm.user_id AND rr.room_id = lcm.room_id
LEFT JOIN (SELECT user_id, room_id, max(event_stream_ordering) AS so FROM receipts_linearized
           WHERE receipt_type = 'm.read.private' AND thread_id IS NULL GROUP BY 1, 2) rp
       ON rp.user_id = lcm.user_id AND rp.room_id = lcm.room_id
";

pub async fn run(ctx: &Ctx) -> Result<u64> {
    let client = ctx.pool.get().await?;
    if ctx.args.explain {
        info!("memberships plan:\n{}", explain(&client, SQL, &[]).await?);
    }
    let mut w = SectionWriter::create(&ctx.out, "memberships")?;
    let mut unknown = 0u64;
    stream_rows(&client, SQL, &[], |row| {
        let user_id: String = row.get(0);
        let room_id: String = row.get(1);
        let (Some(u), Some(r)) = (ctx.users.get(&user_id), ctx.rooms.get(&room_id)) else {
            unknown += 1;
            return Ok(());
        };
        let last_so = ctx.room_last_so[r as usize];
        let lag = |so: Option<i64>| match (last_so, so) {
            (Some(l), Some(s)) => Some(l - s),
            _ => None,
        };
        let row = MembershipRow {
            u,
            r,
            m: row.get(2),
            so: row.get(3),
            notif: row.get(4),
            unread: row.get(5),
            rad: row.get(6),
            tags: row.get(7),
            read_lag: lag(row.get(8)),
            private_read_lag: lag(row.get(9)),
        };
        w.write(&row)
    })
    .await?;
    if unknown > 0 {
        warn!(unknown, "membership rows referenced a user or room not in the index (changed since the index was taken)");
    }
    w.finish()
}
