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

//! The servlet's `encode_rooms` for the rooms Rust computed, so that their
//! events never become Python objects.

use std::collections::HashMap;

use pyo3::prelude::*;
use pythonize::pythonize;
use serde_json::{json, Map, Value};

use crate::events::json_object::JsonObject;
use crate::events::serialize::{
    serialize_event, BundleRef, SerializeEventConfig, SerializeLookups,
};
use crate::events::Event;
use crate::sliding_sync::result::RoomResult;
use crate::sliding_sync::room_entry::RoomEntry;
use crate::sync::relations::Bundle;

/// The lookups of one room: the bundles and redactions Rust loaded for it,
/// and the module callbacks' unsigned additions.
struct EntryLookups<'a> {
    entry: &'a RoomEntry,
    unsigned_additions: &'a HashMap<String, JsonObject>,
}

impl SerializeLookups for EntryLookups<'_> {
    fn redaction(&self, event_id: &str) -> Option<&Event> {
        self.entry.redactions.get(event_id)
    }

    fn bundle(&self, event_id: &str) -> Option<BundleRef<'_>> {
        self.entry
            .bundles
            .as_ref()?
            .get(event_id)
            .map(Bundle::as_ref)
    }

    fn unsigned_additions(&self, event_id: &str) -> Option<&JsonObject> {
        self.unsigned_additions.get(event_id)
    }
}

/// `encode_rooms` for one room.
pub fn encode_room(
    entry: &RoomEntry,
    time_now_ms: i64,
    config: &SerializeEventConfig,
    unsigned_additions: &HashMap<String, JsonObject>,
) -> PyResult<Value> {
    let lookups = EntryLookups {
        entry,
        unsigned_additions,
    };

    // Notifications are only done properly by clients, so these are
    // placeholders.
    let mut room = Map::new();
    room.insert("notification_count".to_owned(), json!(0));
    room.insert("highlight_count".to_owned(), json!(0));
    if let Some(bump_stamp) = entry.bump_stamp {
        room.insert("bump_stamp".to_owned(), json!(bump_stamp));
    }
    if let Some(joined_count) = entry.joined_count {
        room.insert("joined_count".to_owned(), json!(joined_count));
    }
    if let Some(invited_count) = entry.invited_count {
        room.insert("invited_count".to_owned(), json!(invited_count));
    }
    if let Some(name) = entry.name.as_deref().filter(|name| !name.is_empty()) {
        room.insert("name".to_owned(), json!(name));
    }
    if let Some(avatar) = entry.avatar.as_deref().filter(|avatar| !avatar.is_empty()) {
        room.insert("avatar".to_owned(), json!(avatar));
    }
    if !entry.heroes.is_empty() {
        let heroes: Vec<Value> = entry
            .heroes
            .iter()
            .map(|hero| {
                let mut serialized = Map::new();
                serialized.insert("user_id".to_owned(), json!(hero.user_id));
                // The spec spells it "displayname".
                if let Some(display_name) = &hero.display_name {
                    serialized.insert("displayname".to_owned(), json!(display_name));
                }
                if let Some(avatar_url) = &hero.avatar_url {
                    serialized.insert("avatar_url".to_owned(), json!(avatar_url));
                }
                Value::Object(serialized)
            })
            .collect();
        room.insert("heroes".to_owned(), Value::Array(heroes));
    }
    // Only sent when true, to save bandwidth.
    if entry.initial {
        room.insert("initial".to_owned(), json!(true));
    }
    if entry.unstable_expanded_timeline {
        room.insert("unstable_expanded_timeline".to_owned(), json!(true));
    }
    if !entry.required_state.is_empty() {
        let state = entry
            .required_state
            .iter()
            .map(|event| {
                serialize_event(event, time_now_ms, config, None, false, &lookups)
                    .map(Value::Object)
            })
            .collect::<PyResult<Vec<_>>>()?;
        room.insert("required_state".to_owned(), Value::Array(state));
    }
    if !entry.timeline.is_empty() {
        let with_bundles = entry.bundles.is_some();
        let timeline = entry
            .timeline
            .iter()
            .map(|filtered| {
                serialize_event(
                    &filtered.event,
                    time_now_ms,
                    config,
                    Some(&filtered.membership),
                    with_bundles,
                    &lookups,
                )
                .map(Value::Object)
            })
            .collect::<PyResult<Vec<_>>>()?;
        room.insert("timeline".to_owned(), Value::Array(timeline));
    }
    if let Some(limited) = entry.limited {
        room.insert("limited".to_owned(), json!(limited));
    }
    if let Some(prev_batch) = &entry.prev_batch {
        room.insert("prev_batch".to_owned(), json!(prev_batch));
    }
    if let Some(num_live) = entry.num_live {
        room.insert("num_live".to_owned(), json!(num_live));
    }
    // Absent on rooms that are not DMs.
    if entry.is_dm {
        room.insert("is_dm".to_owned(), json!(true));
    }
    if !entry.stripped_state.is_empty() {
        room.insert(
            "invite_state".to_owned(),
            Value::Array(entry.stripped_state.clone()),
        );
    }
    Ok(Value::Object(room))
}

/// `encode_rooms` for `rooms`, in order. `unsigned_additions` are the
/// module callbacks' additions to the rooms' events, by event ID.
#[pyfunction]
#[pyo3(signature = (rooms, time_now_ms, config, *, unsigned_additions = None))]
pub fn encode_rooms<'py>(
    py: Python<'py>,
    rooms: Vec<Bound<'py, RoomResult>>,
    time_now_ms: i64,
    config: &SerializeEventConfig,
    unsigned_additions: Option<HashMap<String, JsonObject>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let unsigned_additions = unsigned_additions.unwrap_or_default();
    rooms
        .iter()
        .map(|room| {
            let encoded = encode_room(&room.get().entry, time_now_ms, config, &unsigned_additions)?;
            Ok(pythonize(py, &encoded)?)
        })
        .collect()
}
