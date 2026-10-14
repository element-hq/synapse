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

//! Row types for every profile section. These are the on-disk format:
//! one JSON object per line in `<section>.jsonl.zst`. Field names are kept
//! short because the bulk sections have millions of rows.
//!
//! Conventions:
//! - `idx` / `r` / `u` are dense integer indexes into the `rooms` and
//!   `users` sections.
//! - `*_h` fields are hours before the profile was taken (`meta.now_ms`).
//! - `*_so` fields are raw stream orderings. They are plain integers with no
//!   identifying content and let the generator reproduce relative recency.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::db::TableEstimate;

#[derive(Debug, Serialize, Deserialize)]
pub struct Meta {
    pub tool_version: String,
    /// Unix millis at which the profile was taken.
    pub now_ms: i64,
    pub window_days: i64,
    /// Start of the rates window, unix millis.
    pub window_start_ms: i64,
    pub server_version: String,
    pub schema_version: Option<i64>,
    pub sample_rooms: usize,
    pub sample_events: u64,
    pub event_sample_percent: f32,
    pub tables: HashMap<String, TableEstimate>,
    pub sections: HashMap<String, SectionStats>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SectionStats {
    pub rows: u64,
    pub seconds: f64,
}

/// One row per room that at least one local user has ever been a member of.
#[derive(Debug, Serialize, Deserialize)]
pub struct RoomRow {
    pub idx: u32,
    pub id: String,
    pub version: Option<String>,
    /// Listed in the public room directory.
    pub public: bool,
    pub creator_local: bool,
    /// Hours ago the room was created (from the create event).
    pub created_h: Option<i64>,
    /// From `room_stats_current`; absent when stats were never computed.
    pub state_events: Option<i32>,
    pub joined: Option<i32>,
    pub invited: Option<i32>,
    pub left: Option<i32>,
    pub banned: Option<i32>,
    pub knocked: Option<i32>,
    pub local_users: Option<i32>,
    pub has_name: bool,
    pub has_topic: bool,
    pub has_avatar: bool,
    pub aliases: i64,
    pub join_rules: Option<String>,
    pub history_visibility: Option<String>,
    pub encryption: Option<String>,
    pub guest_access: Option<String>,
    pub federatable: Option<bool>,
    pub room_type: Option<String>,
    /// Stream ordering of the latest event, from `sliding_sync_joined_rooms`;
    /// only present while some local user is joined.
    pub last_so: Option<i64>,
    /// `last_so - bump_stamp`: how far the last "bump" event lags the tail.
    pub bump_lag: Option<i64>,
    pub tombstoned: bool,
    pub threads: i64,
}

/// One row per local user.
#[derive(Debug, Serialize, Deserialize)]
pub struct UserRow {
    pub idx: u32,
    pub id: String,
    pub created_h: Option<i64>,
    pub last_seen_h: Option<i64>,
    pub guest: bool,
    pub appservice: bool,
    pub deactivated: bool,
    pub admin: bool,
    pub shadow_banned: bool,
    pub locked: bool,
    pub suspended: bool,
    pub user_type: Option<String>,
    pub joined_rooms: Option<i64>,
    pub devices: i64,
    pub device_keys: i64,
    pub one_time_keys: i64,
    pub fallback_keys: i64,
    pub cross_signing_keys: i64,
    /// Global account data types present for this user (spec types verbatim,
    /// others hashed).
    pub account_data: Vec<String>,
    pub room_tags: i64,
    pub push_rules: i64,
    pub filters: i64,
    pub ignored_users: i64,
    pub pushers: i64,
    pub presence: Option<String>,
    pub last_active_h: Option<i64>,
}

/// One row per (local user, room) in `local_current_membership`.
#[derive(Debug, Serialize, Deserialize)]
pub struct MembershipRow {
    pub u: u32,
    pub r: u32,
    /// join / invite / leave / ban / knock
    pub m: String,
    /// Stream ordering of the membership event.
    pub so: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notif: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unread: Option<i64>,
    /// Number of room account data entries.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub rad: i64,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub tags: i64,
    /// `room.last_so - receipt stream ordering` for the public read receipt,
    /// i.e. how many stream positions behind the tail the user has read to.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub read_lag: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub private_read_lag: Option<i64>,
}

fn is_zero(n: &i64) -> bool {
    *n == 0
}

/// Per-room detail gathered for a sample of rooms only.
#[derive(Debug, Serialize, Deserialize)]
pub struct RoomDetailRow {
    pub r: u32,
    /// Non-member current state, by event type.
    pub state_types: Vec<(String, i64)>,
    /// Current member events by (server pseudonym, membership).
    pub members: Vec<(String, String, i64)>,
}

/// Log2 histogram of `length(event_json.json)` for one (type, is_state).
#[derive(Debug, Serialize, Deserialize)]
pub struct EventSizeRow {
    pub t: String,
    pub state: bool,
    pub count: u64,
    pub bytes: u64,
    /// `buckets[b]` counts events whose size has log2 bucket `b`
    /// (0 for 0 bytes, else floor(log2(n)) + 1).
    pub buckets: Vec<u64>,
}

/// Event rates for one room over the window.
#[derive(Debug, Serialize, Deserialize, Default)]
pub struct RateRow {
    pub r: u32,
    /// `days[d]` is the number of events dated in window day `d`
    /// (`meta.window_start_ms + d * 86400000`).
    pub days: Vec<u64>,
    /// (bucket, sender_is_local, count). Bucket is `m.room.message`,
    /// `m.room.encrypted`, `state` or `other`.
    pub by: Vec<(String, bool, u64)>,
    pub senders_local: u64,
    pub senders_remote: u64,
}

/// Server-wide rates: there is exactly one row in `rates_global`.
#[derive(Debug, Serialize, Deserialize, Default)]
pub struct RatesGlobalRow {
    /// (hour index from window start, bucket, sender_is_local, count).
    pub hours: Vec<(i32, String, bool, u64)>,
    /// (day index from window start, distinct users, distinct (user, device)).
    pub daily_active: Vec<(i32, i64, i64)>,
    /// Events in the window that were sent to rooms with no local member and
    /// therefore do not appear in `rates`.
    pub events_in_unprofiled_rooms: u64,
}
