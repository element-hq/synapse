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

//! The `rooms` section. Defines the room index: every room that appears in
//! `local_current_membership`, i.e. that some local user has ever had a
//! membership in. Rooms known only through federation with no local member
//! are irrelevant to sync and are dropped.

use anyhow::Result;
use tracing::info;

use super::Ctx;
use crate::db::{explain, stream_rows};
use crate::model::RoomRow;
use crate::out::{self, SectionWriter};

const SQL: &str = "
SELECT r.room_id, r.room_version, coalesce(r.is_public, false), r.creator,
       rsc.current_state_events, rsc.joined_members, rsc.invited_members, rsc.left_members,
       rsc.banned_members, rsc.knocked_members, rsc.local_users_in_room,
       coalesce(rss.name, '') <> '', coalesce(rss.topic, '') <> '', coalesce(rss.avatar, '') <> '',
       rss.join_rules, rss.history_visibility, rss.encryption, rss.guest_access, rss.is_federatable, rss.room_type,
       ssj.event_stream_ordering, ssj.bump_stamp, ssj.tombstone_successor_room_id IS NOT NULL,
       coalesce(th.n, 0), coalesce(al.n, 0),
       ce.origin_server_ts
FROM rooms r
WHERE_CLAUSE
LEFT JOIN room_stats_current rsc ON rsc.room_id = r.room_id
LEFT JOIN room_stats_state rss ON rss.room_id = r.room_id
LEFT JOIN sliding_sync_joined_rooms ssj ON ssj.room_id = r.room_id
LEFT JOIN (SELECT room_id, count(*) AS n FROM threads GROUP BY room_id) th ON th.room_id = r.room_id
LEFT JOIN (SELECT room_id, count(*) AS n FROM room_aliases GROUP BY room_id) al ON al.room_id = r.room_id
LEFT JOIN LATERAL (
    SELECT e.origin_server_ts
    FROM current_state_events cse
    JOIN events e ON e.event_id = cse.event_id
    WHERE cse.room_id = r.room_id AND cse.type = 'm.room.create' AND cse.state_key = ''
    LIMIT 1
) ce ON true
ORDER BY r.room_id
";

fn sql() -> String {
    SQL.replace(
        "WHERE_CLAUSE",
        "JOIN (SELECT DISTINCT room_id FROM local_current_membership) lcm ON lcm.room_id = r.room_id",
    )
}

pub async fn run(ctx: &mut Ctx) -> Result<u64> {
    if ctx.args.resume && out::section_done(&ctx.out, "rooms") {
        return load(ctx).await;
    }
    let client = ctx.pool.get().await?;
    if ctx.args.explain {
        info!("rooms plan:\n{}", explain(&client, &sql(), &[]).await?);
    }
    let mut w = SectionWriter::create(&ctx.out, "rooms")?;
    let mut rooms = std::mem::take(&mut ctx.rooms);
    let mut raw = Vec::new();
    let mut last_so = Vec::new();
    let mut state_events = Vec::new();
    stream_rows(&client, &sql(), &[], |row| {
        let room_id: String = row.get(0);
        let idx = rooms.insert(&room_id);
        debug_assert_eq!(idx as usize, raw.len());
        let creator: Option<String> = row.get(3);
        let so: Option<i64> = row.get(20);
        let bump: Option<i64> = row.get(21);
        let r = RoomRow {
            idx,
            id: ctx.anon.room(&room_id),
            version: row.get(1),
            public: row.get(2),
            creator_local: creator
                .as_deref()
                .map(|c| ctx.anon.is_local(c))
                .unwrap_or(false),
            created_h: ctx.hours_ago(row.get(25)),
            state_events: row.get(4),
            joined: row.get(5),
            invited: row.get(6),
            left: row.get(7),
            banned: row.get(8),
            knocked: row.get(9),
            local_users: row.get(10),
            has_name: row.get(11),
            has_topic: row.get(12),
            has_avatar: row.get(13),
            aliases: row.get(24),
            join_rules: row.get(14),
            history_visibility: row.get(15),
            encryption: row.get(16),
            guest_access: row.get(17),
            federatable: row.get(18),
            room_type: row
                .get::<_, Option<String>>(19)
                .map(|t| ctx.anon.type_name(&t)),
            last_so: so,
            bump_lag: match (so, bump) {
                (Some(s), Some(b)) => Some(s - b),
                _ => None,
            },
            tombstoned: row.get(22),
            threads: row.get(23),
        };
        raw.push(room_id);
        last_so.push(so);
        state_events.push(r.state_events.unwrap_or(0) as i64);
        w.write(&r)
    })
    .await?;
    ctx.rooms = rooms;
    ctx.room_raw = raw;
    ctx.room_last_so = last_so;
    ctx.room_state_events = state_events;
    w.finish()
}

/// On resume: rebuild the in-memory index from the finished section. The
/// pseudonym of each raw room ID from the database is looked up in the file.
async fn load(ctx: &mut Ctx) -> Result<u64> {
    let mut by_pseudonym = std::collections::HashMap::new();
    let mut last_so = Vec::new();
    let mut state_events = Vec::new();
    for row in out::read_section::<RoomRow>(&ctx.out, "rooms")? {
        let r = row?;
        if r.idx as usize != last_so.len() {
            anyhow::bail!("rooms section is not in index order; cannot resume");
        }
        by_pseudonym.insert(r.id, r.idx);
        last_so.push(r.last_so);
        state_events.push(r.state_events.unwrap_or(0) as i64);
    }
    let mut raw = vec![String::new(); by_pseudonym.len()];
    let client = ctx.pool.get().await?;
    let mut missing = 0u64;
    stream_rows(
        &client,
        "SELECT DISTINCT room_id FROM local_current_membership",
        &[],
        |row| {
            let room_id: String = row.get(0);
            match by_pseudonym.get(&ctx.anon.room(&room_id)) {
                Some(&idx) => {
                    ctx.rooms.insert_at(room_id.clone(), idx);
                    raw[idx as usize] = room_id;
                }
                None => missing += 1,
            }
            Ok(())
        },
    )
    .await?;
    if missing > 0 {
        tracing::warn!(
            missing,
            "rooms appeared since the earlier run; they are left out of resumed sections"
        );
    }
    let n = by_pseudonym.len() as u64;
    if ctx.rooms.len() == 0 && n > 0 {
        anyhow::bail!(
            "no room pseudonym from rooms.jsonl.zst matched the database; wrong --salt-file?"
        );
    }
    ctx.room_raw = raw;
    ctx.room_last_so = last_so;
    ctx.room_state_events = state_events;
    info!(rooms = n, "loaded room index from earlier run");
    Ok(n)
}
