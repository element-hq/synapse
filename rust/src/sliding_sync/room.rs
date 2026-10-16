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

//! The decisions `get_room_sync_data` makes for one room, apart from its
//! reads: where its updates start, its live events, its stripped state,
//! name, avatar and heroes, its bump stamp, and the config the connection
//! records for it.

use std::collections::BTreeMap;

use pyo3::{exceptions::PyValueError, prelude::*, types::PyModule};
use serde_json::{json, Map, Value};

use crate::sliding_sync::config::{
    config_from_py, to_test_tuple, RequiredStateMap, RoomSyncConfig,
};
use crate::sliding_sync::connection::HaveSentRoom;
use crate::types::stream_token::{persisted_after, RoomStreamToken};

/// `SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES`: the event types that move a
/// room up the list.
pub const BUMP_EVENT_TYPES: [&str; 7] = [
    "m.room.create",
    "m.room.message",
    "m.room.encrypted",
    "m.sticker",
    "m.call.invite",
    "m.poll.start",
    "m.beacon_info",
];

/// `_timeline_bounds`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimelineBounds {
    /// The lower bound of the room's updates, `None` for the whole room.
    pub from_bound: Option<RoomStreamToken>,
    /// Whether the room is sent in full.
    pub initial: bool,
    /// Whether the timeline ignores `from_bound`, as the timeline limit
    /// grew (`unstable_expanded_timeline`).
    pub ignore_timeline_bound: bool,
}

/// `_timeline_bounds`: where the room's updates start, from what the
/// connection sent before. `from_room_key` is the `pos` token's room key,
/// and `skip_previous_bounds` sends the room as if never sent, when newly
/// joined or state reset out of it.
pub fn timeline_bounds(
    room_status: &HaveSentRoom<RoomStreamToken>,
    from_room_key: Option<&RoomStreamToken>,
    skip_previous_bounds: bool,
    prev_room_sync_config: Option<&RoomSyncConfig>,
    room_sync_config: &RoomSyncConfig,
) -> TimelineBounds {
    let mut bounds = TimelineBounds {
        from_bound: None,
        initial: true,
        ignore_timeline_bound: false,
    };
    let Some(from_room_key) = from_room_key else {
        return bounds;
    };
    if skip_previous_bounds {
        return bounds;
    }
    match room_status {
        HaveSentRoom::Live => {
            bounds.from_bound = Some(from_room_key.clone());
            bounds.initial = false;
        }
        HaveSentRoom::Previously(last_token) => {
            bounds.from_bound = Some(last_token.clone());
            bounds.initial = false;
        }
        HaveSentRoom::Never => {}
    }
    if let Some(prev) = prev_room_sync_config {
        if prev.timeline_limit < room_sync_config.timeline_limit {
            bounds.ignore_timeline_bound = true;
        }
    }
    bounds
}

/// `num_live`: how many of the timeline's last events are after the `pos`
/// token, given each event's `(instance_name, stream_ordering)` in
/// timeline order. Zero on an initial request.
pub fn num_live<'a>(
    positions: impl DoubleEndedIterator<Item = (&'a str, i64)>,
    from_room_key: Option<&RoomStreamToken>,
) -> i64 {
    let Some(from_room_key) = from_room_key else {
        return 0;
    };
    positions
        .rev()
        .take_while(|(instance_name, stream)| {
            persisted_after(instance_name, *stream, from_room_key)
        })
        .count() as i64
}

/// `strip_event`: the parts of a state event stripped state carries.
pub fn strip_event(event_type: &str, state_key: &str, content: &Value, sender: &str) -> Value {
    json!({
        "type": event_type,
        "state_key": state_key,
        "content": content,
        "sender": sender,
    })
}

/// `_stripped_state`: the stripped state of an invite or knock, from the
/// event's `membership` and `unsigned`, then `stripped_event`, the event
/// itself stripped. A non-list `invite_room_state`/`knock_room_state` is
/// ignored.
pub fn stripped_state(
    membership: Option<&str>,
    unsigned: &Map<String, Value>,
    stripped_event: Value,
) -> Vec<Value> {
    let key = match membership {
        Some("invite") => Some("invite_room_state"),
        Some("knock") => Some("knock_room_state"),
        _ => None,
    };
    let mut stripped = match key.and_then(|key| unsigned.get(key)) {
        Some(Value::Array(state)) => state.clone(),
        _ => Vec::new(),
    };
    stripped.push(stripped_event);
    stripped
}

/// A string field of an event's content: `None` when missing or not a
/// string, as the Python's `isinstance(value, str)` checks.
pub fn string_field<'a>(content: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    content.get(key).and_then(Value::as_str)
}

/// Whether the room needs heroes: it has no name (an empty one counts as
/// none), and is sent in full or its membership changed.
pub fn needs_heroes(room_name: Option<&str>, initial: bool, membership_changed: bool) -> bool {
    room_name.is_none_or(str::is_empty) && (initial || membership_changed)
}

/// `SlidingSyncResult.RoomResult.StrippedHero`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrippedHero {
    pub user_id: String,
    pub display_name: Option<String>,
    pub avatar_url: Option<String>,
}

/// The heroes of `hero_user_ids` that `member_content` finds a membership
/// event for, with its display name and avatar.
pub fn assemble_heroes<'a>(
    hero_user_ids: &[&str],
    member_content: impl Fn(&str) -> Option<&'a Map<String, Value>>,
) -> Vec<StrippedHero> {
    hero_user_ids
        .iter()
        .filter_map(|user_id| {
            let content = member_content(user_id)?;
            Some(StrippedHero {
                user_id: (*user_id).to_owned(),
                display_name: string_field(content, "displayname").map(str::to_owned),
                avatar_url: string_field(content, "avatar_url").map(str::to_owned),
            })
        })
        .collect()
}

/// The first half of `_get_bump_stamp`: the stream ordering of the
/// timeline's last bump event, given each event's `(type,
/// stream_ordering)` in timeline order. A backfilled event's negative
/// stream ordering is skipped.
pub fn timeline_bump_stamp<'a>(
    timeline: impl DoubleEndedIterator<Item = (&'a str, i64)>,
) -> Option<i64> {
    timeline
        .rev()
        .find(|(event_type, stream)| BUMP_EVENT_TYPES.contains(event_type) && *stream > 0)
        .map(|(_, stream)| stream)
}

/// What `_get_bump_stamp` does with the room's `bump_stamp` from the
/// sliding sync tables.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoreBumpStamp {
    /// The answer.
    Done(Option<i64>),
    /// Look up the room's last bump event before the `to_token`, and pass
    /// its stream ordering to [`bump_stamp_from_last_event`].
    LookupLastEvent,
}

/// The second half of `_get_bump_stamp`, outside the timeline:
/// `latest_room_bump_stamp` is the tables' `bump_stamp`, and
/// `min_to_token_position` the `to_token`'s minimum stream position.
pub fn store_bump_stamp(
    latest_room_bump_stamp: Option<i64>,
    have_finished_bg_jobs: bool,
    min_to_token_position: i64,
) -> StoreBumpStamp {
    match latest_room_bump_stamp {
        None if have_finished_bg_jobs => StoreBumpStamp::Done(None),
        // Only a bump stamp wholly before the token can be trusted.
        Some(stamp) if stamp < min_to_token_position => {
            StoreBumpStamp::Done(Some(stamp).filter(|stamp| *stamp > 0))
        }
        _ => StoreBumpStamp::LookupLastEvent,
    }
}

/// The bump stamp from the room's last bump event before the `to_token`.
pub fn bump_stamp_from_last_event(last_event_stream: Option<i64>) -> Option<i64> {
    last_event_stream.filter(|stream| *stream > 0)
}

/// `always_return_bump_stamp`: whether the response must carry a bump
/// stamp even when it is unchanged. `limited` is `None` with no timeline.
pub fn always_return_bump_stamp(membership: &str, limited: Option<bool>, initial: bool) -> bool {
    membership != "join" || limited != Some(false) || initial
}

/// The bump stamp the room's entry carries: `new_bump_stamp` from
/// `_get_bump_stamp` (for joined rooms), else the membership event's
/// position if one must be returned, and never negative.
pub fn final_bump_stamp(
    new_bump_stamp: Option<i64>,
    always_return: bool,
    membership_stream: i64,
) -> Option<i64> {
    let bump_stamp = match new_bump_stamp {
        Some(stamp) => Some(stamp),
        None if always_return => Some(membership_stream),
        None => None,
    };
    // A negative stream ordering cannot be compared with positive ones. It
    // only happens for a membership in a room left, deleted and rejoined.
    bump_stamp.map(|stamp| stamp.max(0))
}

/// `_room_config_to_persist`: the config the connection records for the
/// room after sending it, or `None` to keep the stored one. An empty
/// `changed_required_state_map` records the request's map, but counts as a
/// change, as in Python.
pub fn room_config_to_persist(
    room_sync_config: &RoomSyncConfig,
    prev_room_sync_config: Option<&RoomSyncConfig>,
    changed_required_state_map: Option<&RequiredStateMap>,
    ignore_timeline_bound: bool,
    limited: Option<bool>,
) -> Option<RoomSyncConfig> {
    let required_state_map = match changed_required_state_map {
        Some(changed) if !changed.is_empty() => changed.clone(),
        _ => room_sync_config.required_state_map.clone(),
    };
    let config = RoomSyncConfig {
        timeline_limit: room_sync_config.timeline_limit,
        required_state_map,
    };
    if ignore_timeline_bound {
        return Some(config);
    }
    let Some(prev) = prev_room_sync_config else {
        return Some(config);
    };
    // A smaller limit is only recorded when the timeline was limited: the
    // client then lacks the events a later larger limit would send.
    if limited == Some(true) && prev.timeline_limit > room_sync_config.timeline_limit {
        return Some(config);
    }
    if changed_required_state_map.is_some() {
        return Some(config);
    }
    None
}

type PyConfig = (i64, BTreeMap<String, Vec<String>>);

/// `_timeline_bounds` through the Rust implementation, the status as
/// `(flag, token)` and tokens as live stream positions. Returns
/// `(from_bound, initial, ignore_timeline_bound)`.
#[pyfunction]
fn timeline_bounds_for_test(
    room_status: (String, Option<i64>),
    from_stream: Option<i64>,
    skip_previous_bounds: bool,
    prev_room_sync_config: Option<PyConfig>,
    room_sync_config: PyConfig,
) -> PyResult<(Option<i64>, bool, bool)> {
    let status = match (room_status.0.as_str(), room_status.1) {
        ("never", _) => HaveSentRoom::Never,
        ("live", _) => HaveSentRoom::Live,
        ("previously", Some(token)) => HaveSentRoom::Previously(RoomStreamToken::live(token)),
        _ => return Err(PyValueError::new_err("bad status")),
    };
    let from = from_stream.map(RoomStreamToken::live);
    let prev = prev_room_sync_config.map(|(limit, map)| config_from_py(limit, map));
    let bounds = timeline_bounds(
        &status,
        from.as_ref(),
        skip_previous_bounds,
        prev.as_ref(),
        &config_from_py(room_sync_config.0, room_sync_config.1),
    );
    Ok((
        bounds.from_bound.map(|token| token.stream),
        bounds.initial,
        bounds.ignore_timeline_bound,
    ))
}

/// `_stripped_state` through the Rust implementation, with the event as
/// JSON. Returns the stripped state as JSON.
#[pyfunction]
fn stripped_state_for_test(event: &str) -> PyResult<String> {
    let event: Value =
        serde_json::from_str(event).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let empty = Map::new();
    let content = event
        .get("content")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let membership = content.get("membership").and_then(Value::as_str);
    let unsigned = event
        .get("unsigned")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let field = |key: &str| event.get(key).and_then(Value::as_str).unwrap_or_default();
    let stripped = strip_event(field("type"), field("state_key"), &content, field("sender"));
    Ok(Value::Array(stripped_state(membership, unsigned, stripped)).to_string())
}

/// `_get_bump_stamp` through the Rust implementation, the timeline as
/// `(type, stream_ordering)`, the store as `latest_room_bump_stamp`,
/// `have_finished_bg_jobs` and `last_event_stream`.
#[pyfunction]
fn get_bump_stamp_for_test(
    timeline: Vec<(String, i64)>,
    check_outside_timeline: bool,
    latest_room_bump_stamp: Option<i64>,
    have_finished_bg_jobs: bool,
    to_stream: i64,
    last_event_stream: Option<i64>,
) -> Option<i64> {
    if let Some(stamp) = timeline_bump_stamp(timeline.iter().map(|(t, s)| (t.as_str(), *s))) {
        return Some(stamp);
    }
    if !check_outside_timeline {
        return None;
    }
    match store_bump_stamp(latest_room_bump_stamp, have_finished_bg_jobs, to_stream) {
        StoreBumpStamp::Done(stamp) => stamp,
        StoreBumpStamp::LookupLastEvent => bump_stamp_from_last_event(last_event_stream),
    }
}

/// `_room_config_to_persist` through the Rust implementation.
#[pyfunction]
fn room_config_to_persist_for_test(
    room_sync_config: PyConfig,
    prev_room_sync_config: Option<PyConfig>,
    changed_required_state_map: Option<BTreeMap<String, Vec<String>>>,
    ignore_timeline_bound: bool,
    limited: Option<bool>,
) -> Option<PyConfig> {
    let changed = changed_required_state_map.map(|map| config_from_py(0, map).required_state_map);
    let prev = prev_room_sync_config.map(|(limit, map)| config_from_py(limit, map));
    room_config_to_persist(
        &config_from_py(room_sync_config.0, room_sync_config.1),
        prev.as_ref(),
        changed.as_ref(),
        ignore_timeline_bound,
        limited,
    )
    .map(|config| to_test_tuple(&config))
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(timeline_bounds_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(stripped_state_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(get_bump_stamp_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(room_config_to_persist_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_events_counted_from_the_end() {
        let from = RoomStreamToken::live(5);
        let positions = [
            ("master", 3),
            ("master", 6),
            ("master", 4),
            ("master", 7),
            ("master", 8),
        ];
        assert_eq!(num_live(positions.iter().copied(), Some(&from)), 2);
        assert_eq!(num_live(positions.iter().copied(), None), 0);
    }

    #[test]
    fn names_and_heroes() {
        let content = json!({"name": "x", "url": 3, "displayname": "Alice"});
        let content = content.as_object().unwrap();
        assert_eq!(string_field(content, "name"), Some("x"));
        assert_eq!(string_field(content, "url"), None);
        assert!(needs_heroes(Some(""), true, false));
        assert!(!needs_heroes(Some("x"), true, true));
        assert!(!needs_heroes(None, false, false));

        let heroes = assemble_heroes(&["@a:x", "@b:x"], |user_id| {
            (user_id == "@a:x").then_some(content)
        });
        assert_eq!(
            heroes,
            vec![StrippedHero {
                user_id: "@a:x".to_owned(),
                display_name: Some("Alice".to_owned()),
                avatar_url: None,
            }]
        );
    }

    #[test]
    fn bump_stamps() {
        let timeline = [
            ("m.room.message", 4),
            ("m.room.member", 9),
            ("m.room.message", -2),
        ];
        assert_eq!(timeline_bump_stamp(timeline.iter().copied()), Some(4));
        assert!(always_return_bump_stamp("leave", Some(false), false));
        assert!(always_return_bump_stamp("join", None, false));
        assert!(!always_return_bump_stamp("join", Some(false), false));
        assert_eq!(final_bump_stamp(None, true, -5), Some(0));
        assert_eq!(final_bump_stamp(None, false, 5), None);
        assert_eq!(final_bump_stamp(Some(7), false, 5), Some(7));
    }

    #[test]
    fn stripped_state_from_unsigned() {
        let unsigned =
            json!({"invite_room_state": [{"type": "m.room.name"}], "knock_room_state": 3});
        let unsigned = unsigned.as_object().unwrap();
        let me = json!({"type": "m.room.member"});
        assert_eq!(
            stripped_state(Some("invite"), unsigned, me.clone()).len(),
            2
        );
        assert_eq!(
            stripped_state(Some("knock"), unsigned, me.clone()),
            vec![me]
        );
    }
}
