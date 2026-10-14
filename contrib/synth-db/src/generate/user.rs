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

//! Per-user rows: the account, devices and tokens, end-to-end keys,
//! account data, push rules, filters, presence.

use anyhow::Result;
use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use serde_json::json;

use super::sink::F;
use super::sinks::{put, Sinks};
use super::text::b64ish;
use super::{Counters, GenCtx};
use crate::model::UserRow;

/// The access token for a user's n-th device. Deterministic, so a load
/// generator needs no login step.
pub fn access_token(uidx: u32, device: u32) -> String {
    format!("synt_u{uidx}_d{device}")
}

pub fn device_id(uidx: u32, device: u32) -> String {
    format!("SYNTH{uidx}D{device}")
}

pub async fn emit(
    ctx: &GenCtx,
    counters: &mut Counters,
    sinks: &mut Sinks,
    uidx: u32,
    user: &UserRow,
    dms: &[(String, String)],
) -> Result<()> {
    let mut rng = StdRng::seed_from_u64(ctx.args.seed ^ ((uidx as u64) << 8) ^ 7);
    let user_id = ctx.user_id(uidx);
    let localpart = format!("u{uidx}");
    let now = ctx.timeline.now_ms;
    let created_ts = now - user.created_h.unwrap_or(24 * 365) * 3_600_000;
    let last_seen = user.last_seen_h.map(|h| now - h * 3_600_000);

    put(
        &mut sinks.users,
        &[
            F::S(&user_id),
            F::S(&ctx.password_hash),
            F::I(created_ts / 1000),
            F::I(user.admin as i64),
            F::I(user.guest as i64),
            if user.appservice {
                F::S("synth-appservice")
            } else {
                F::Null
            },
            user.user_type
                .as_deref()
                .filter(|t| ["support", "bot"].contains(t))
                .map(F::S)
                .unwrap_or(F::Null),
            F::I(user.deactivated as i64),
            F::B(user.shadow_banned),
            F::B(true),
            F::B(user.locked),
            F::B(user.suspended),
        ],
    )
    .await?;
    let display_name = format!("User {uidx}");
    put(
        &mut sinks.profiles,
        &[
            F::S(&localpart),
            F::S(&display_name),
            F::Null,
            F::S(&user_id),
        ],
    )
    .await?;
    put(
        &mut sinks.user_directory,
        &[F::S(&user_id), F::Null, F::S(&display_name), F::Null],
    )
    .await?;
    put(
        &mut sinks.user_stats_current,
        &[
            F::S(&user_id),
            F::I(user.joined_rooms.unwrap_or(0)),
            F::I(ctx.timeline.max_so),
        ],
    )
    .await?;

    // Devices, tokens and keys. Keys are spread over the first devices.
    let devices = user.devices.max(0) as u32;
    let mut with_keys = user.device_keys.clamp(0, devices as i64) as u32;
    if with_keys == 0 && devices > 0 && (user.one_time_keys > 0 || user.fallback_keys > 0) {
        // Keys without a device key row happen (old uploads); keep the
        // one-time keys on the first device so their count survives.
        with_keys = 1;
    }
    let mut otks_left = user.one_time_keys.max(0);
    let mut fallbacks_left = user.fallback_keys.max(0);
    for d in 0..devices {
        let did = device_id(uidx, d);
        counters.token_id += 1;
        put(
            &mut sinks.devices,
            &[
                F::S(&user_id),
                F::S(&did),
                F::S("synth device"),
                last_seen.map(F::I).unwrap_or(F::Null),
                F::S("127.0.0.1"),
                F::S("synth-db/0.1"),
                F::B(false),
            ],
        )
        .await?;
        let token = access_token(uidx, d);
        put(
            &mut sinks.access_tokens,
            &[
                F::I(counters.token_id),
                F::S(&user_id),
                F::S(&did),
                F::S(&token),
                F::Null,
                F::Null,
                F::I(now),
                F::Null,
                F::B(false),
            ],
        )
        .await?;
        if d < with_keys {
            let key_json = json!({
                "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
                "device_id": did,
                "keys": { format!("curve25519:{did}"): b64ish(&mut rng, 43), format!("ed25519:{did}"): b64ish(&mut rng, 43) },
                "signatures": { user_id.clone(): { format!("ed25519:{did}"): b64ish(&mut rng, 86) } },
                "user_id": user_id,
            })
            .to_string();
            put(
                &mut sinks.e2e_device_keys_json,
                &[
                    F::S(&user_id),
                    F::S(&did),
                    F::I(created_ts),
                    F::S(&key_json),
                ],
            )
            .await?;
            let share = (otks_left + (with_keys - d) as i64 - 1) / (with_keys - d) as i64;
            for k in 0..share.min(otks_left) {
                let key_id = format!("AAAA{}", b64ish(&mut rng, 6));
                let kj = json!({ "key": b64ish(&mut rng, 43), "signatures": { user_id.clone(): { format!("ed25519:{did}"): b64ish(&mut rng, 86) } } }).to_string();
                put(
                    &mut sinks.e2e_one_time_keys_json,
                    &[
                        F::S(&user_id),
                        F::S(&did),
                        F::S("signed_curve25519"),
                        F::S(&key_id),
                        F::I(created_ts + k),
                        F::S(&kj),
                    ],
                )
                .await?;
            }
            otks_left -= share.min(otks_left);
            if fallbacks_left > 0 {
                let kj = json!({ "key": b64ish(&mut rng, 43), "fallback": true, "signatures": {} })
                    .to_string();
                put(
                    &mut sinks.e2e_fallback_keys_json,
                    &[
                        F::S(&user_id),
                        F::S(&did),
                        F::S("signed_curve25519"),
                        F::S("AAAAfallback"),
                        F::S(&kj),
                        F::B(false),
                    ],
                )
                .await?;
                fallbacks_left -= 1;
            }
        }
    }
    for (i, keytype) in ["master", "self_signing", "user_signing"]
        .iter()
        .enumerate()
    {
        if (i as i64) >= user.cross_signing_keys {
            break;
        }
        counters.cross_signing += 1;
        let pub_key = b64ish(&mut rng, 43);
        let keydata = json!({ "user_id": user_id, "usage": [keytype], "keys": { format!("ed25519:{pub_key}"): pub_key } }).to_string();
        put(
            &mut sinks.e2e_cross_signing_keys,
            &[
                F::S(&user_id),
                F::S(keytype),
                F::S(&keydata),
                F::I(counters.cross_signing),
                F::S("master"),
            ],
        )
        .await?;
    }

    // Global account data. Push rules live in their own table.
    let mut ignored: Vec<String> = Vec::new();
    for i in 0..user.ignored_users.max(0) {
        let other = ctx.user_id(rng.gen_range(0..ctx.users_total.max(1)));
        put(&mut sinks.ignored_users, &[F::S(&user_id), F::S(&other)]).await?;
        ignored.push(other);
        let _ = i;
    }
    let mut types: Vec<String> = user
        .account_data
        .iter()
        .filter(|t| *t != "m.push_rules")
        .cloned()
        .collect();
    if !ignored.is_empty() && !types.iter().any(|t| t == "m.ignored_user_list") {
        types.push("m.ignored_user_list".into());
    }
    if !dms.is_empty() && !types.iter().any(|t| t == "m.direct") {
        types.push("m.direct".into());
    }
    for t in &types {
        let content = match t.as_str() {
            "m.direct" => {
                let mut map = serde_json::Map::new();
                for (other, room) in dms {
                    map.entry(other.clone())
                        .or_insert_with(|| json!([]))
                        .as_array_mut()
                        .unwrap()
                        .push(json!(room));
                }
                serde_json::Value::Object(map).to_string()
            }
            "m.ignored_user_list" => {
                json!({ "ignored_users": ignored.iter().map(|u| (u.clone(), json!({}))).collect::<serde_json::Map<_, _>>() }).to_string()
            }
            _ => {
                let len = rng.gen_range(0..200);
                json!({ "synth": ctx.vocab.text(&mut rng, len) }).to_string()
            }
        };
        counters.account_data += 1;
        put(
            &mut sinks.account_data,
            &[
                F::S(&user_id),
                F::S(t),
                F::I(counters.account_data),
                F::S(&content),
                F::S("master"),
            ],
        )
        .await?;
    }

    for i in 0..user.push_rules.max(0) {
        counters.push_rule_id += 1;
        let rule_id = format!("synth.rule{i}");
        let conditions = json!([{ "kind": "event_match", "key": "content.body", "pattern": format!("synth{i}") }]).to_string();
        put(
            &mut sinks.push_rules,
            &[
                F::I(counters.push_rule_id),
                F::S(&user_id),
                F::S(&rule_id),
                F::I(5),
                F::I(i),
                F::S(&conditions),
                F::S("[\"notify\"]"),
            ],
        )
        .await?;
    }
    for i in 0..user.filters.max(0) {
        let filter = json!({ "room": { "timeline": { "limit": 20 }, "state": { "lazy_load_members": true } } }).to_string();
        put(
            &mut sinks.user_filters,
            &[
                F::S(&localpart),
                F::I(i),
                F::Bytes(filter.as_bytes()),
                F::S(&user_id),
            ],
        )
        .await?;
    }
    if let Some(state) = user
        .presence
        .as_deref()
        .filter(|s| ["online", "offline", "unavailable", "busy"].contains(s))
    {
        counters.presence += 1;
        let last_active = now - user.last_active_h.unwrap_or(0) * 3_600_000;
        put(
            &mut sinks.presence_stream,
            &[
                F::I(counters.presence),
                F::S(&user_id),
                F::S(state),
                F::I(last_active),
                F::I(last_active),
                F::I(last_active),
                F::Null,
                F::B(state == "online"),
                F::S("master"),
            ],
        )
        .await?;
    }
    Ok(())
}
