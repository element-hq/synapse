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

//! The `users` section: one row per local user, with counts of everything
//! sync has to load for them. Every per-user count is a grouped subquery so
//! the planner can hash each table once instead of probing it per user.

use anyhow::Result;
use tracing::info;

use super::Ctx;
use crate::db::{explain, stream_rows};
use crate::model::UserRow;
use crate::out::{self, SectionWriter};

const SQL: &str = "
SELECT u.name, u.creation_ts, coalesce(u.is_guest, 0) <> 0, u.appservice_id IS NOT NULL,
       coalesce(u.deactivated, 0) <> 0, coalesce(u.admin, 0) <> 0, coalesce(u.shadow_banned, false),
       coalesce(u.locked, false), coalesce(u.suspended, false), u.user_type,
       us.joined_rooms,
       coalesce(d.n, 0), d.last_seen,
       coalesce(dk.n, 0), coalesce(otk.n, 0), coalesce(fb.n, 0), coalesce(csk.n, 0),
       ad.types,
       coalesce(rt.n, 0), coalesce(pr.n, 0), coalesce(f.n, 0), coalesce(ig.n, 0), coalesce(p.n, 0),
       ps.state, ps.last_active_ts
FROM users u
LEFT JOIN user_stats_current us ON us.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n, max(last_seen) AS last_seen FROM devices WHERE NOT coalesce(hidden, false) GROUP BY user_id) d ON d.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n FROM e2e_device_keys_json GROUP BY user_id) dk ON dk.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n FROM e2e_one_time_keys_json GROUP BY user_id) otk ON otk.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n FROM e2e_fallback_keys_json GROUP BY user_id) fb ON fb.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n FROM e2e_cross_signing_keys GROUP BY user_id) csk ON csk.user_id = u.name
LEFT JOIN (SELECT user_id, string_agg(account_data_type, E'\\n') AS types FROM account_data GROUP BY user_id) ad ON ad.user_id = u.name
LEFT JOIN (SELECT user_id, count(*) AS n FROM room_tags GROUP BY user_id) rt ON rt.user_id = u.name
LEFT JOIN (SELECT user_name, count(*) AS n FROM push_rules GROUP BY user_name) pr ON pr.user_name = u.name
LEFT JOIN (SELECT coalesce(full_user_id, '@' || user_id || ':' || $1::text) AS user_id, count(*) AS n FROM user_filters GROUP BY 1) f ON f.user_id = u.name
LEFT JOIN (SELECT ignorer_user_id AS user_id, count(*) AS n FROM ignored_users GROUP BY ignorer_user_id) ig ON ig.user_id = u.name
LEFT JOIN (SELECT user_name, count(*) AS n FROM pushers GROUP BY user_name) p ON p.user_name = u.name
LEFT JOIN presence_stream ps ON ps.user_id = u.name
ORDER BY u.name
";

pub async fn run(ctx: &mut Ctx) -> Result<u64> {
    if ctx.args.resume && out::section_done(&ctx.out, "users") {
        return load(ctx).await;
    }
    let client = ctx.pool.get().await?;
    // `user_filters.user_id` is a bare localpart; `full_user_id` is only
    // filled for rows written since it was added.
    let server = ctx.anon.local_server().to_owned();
    if ctx.args.explain {
        info!("users plan:\n{}", explain(&client, SQL, &[&server]).await?);
    }
    let mut w = SectionWriter::create(&ctx.out, "users")?;
    let mut users = std::mem::take(&mut ctx.users);
    stream_rows(&client, SQL, &[&server], |row| {
        let name: String = row.get(0);
        let idx = users.insert(&name);
        let creation_s: Option<i64> = row.get(1);
        let types: Option<String> = row.get(17);
        let r = UserRow {
            idx,
            id: ctx.anon.user(&name),
            created_h: ctx.hours_ago(creation_s.map(|s| s * 1000)),
            last_seen_h: ctx.hours_ago(row.get(12)),
            guest: row.get(2),
            appservice: row.get(3),
            deactivated: row.get(4),
            admin: row.get(5),
            shadow_banned: row.get(6),
            locked: row.get(7),
            suspended: row.get(8),
            user_type: row.get(9),
            joined_rooms: row.get(10),
            devices: row.get(11),
            device_keys: row.get(13),
            one_time_keys: row.get(14),
            fallback_keys: row.get(15),
            cross_signing_keys: row.get(16),
            account_data: types
                .map(|t| t.lines().map(|x| ctx.anon.type_name(x)).collect())
                .unwrap_or_default(),
            room_tags: row.get(18),
            push_rules: row.get(19),
            filters: row.get(20),
            ignored_users: row.get(21),
            pushers: row.get(22),
            presence: row.get(23),
            last_active_h: ctx.hours_ago(row.get(24)),
        };
        w.write(&r)
    })
    .await?;
    ctx.users = users;
    w.finish()
}

async fn load(ctx: &mut Ctx) -> Result<u64> {
    let mut by_pseudonym = std::collections::HashMap::new();
    for row in out::read_section::<UserRow>(&ctx.out, "users")? {
        let r = row?;
        by_pseudonym.insert(r.id, r.idx);
    }
    let client = ctx.pool.get().await?;
    let mut missing = 0u64;
    stream_rows(&client, "SELECT name FROM users", &[], |row| {
        let name: String = row.get(0);
        match by_pseudonym.get(&ctx.anon.user(&name)) {
            Some(&idx) => ctx.users.insert_at(name, idx),
            None => missing += 1,
        }
        Ok(())
    })
    .await?;
    if missing > 0 {
        tracing::warn!(
            missing,
            "users appeared since the earlier run; they are left out of resumed sections"
        );
    }
    let n = by_pseudonym.len() as u64;
    if ctx.users.len() == 0 && n > 0 {
        anyhow::bail!(
            "no user pseudonym from users.jsonl.zst matched the database; wrong --salt-file?"
        );
    }
    info!(users = n, "loaded user index from earlier run");
    Ok(n)
}
