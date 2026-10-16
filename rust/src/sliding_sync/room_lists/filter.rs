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

//! Which of a user's rooms a sync covers, and a list's filters.

use std::collections::{HashMap, HashSet};

use pyo3::{exceptions::PyValueError, prelude::*, types::PyModule};

use crate::sliding_sync::request::Filters;
use crate::sliding_sync::room_lists::{room_from_py, PyRoomForUser, RoomForUser};

/// A filter Python does not implement either: `spaces` and
/// `room_name_like` raise `NotImplementedError` there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotImplementedFilter(pub &'static str);

impl std::fmt::Display for NotImplementedFilter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "the {} filter is not implemented", self.0)
    }
}

impl std::error::Error for NotImplementedFilter {}

/// `filter_membership_for_sync`: whether the sync covers the room. Every
/// membership but a leave does, and a leave only when it is newly left or
/// a kick. A leave with no sender is a state reset.
pub fn filter_membership_for_sync(user_id: &str, room: &RoomForUser, newly_left: bool) -> bool {
    &*room.membership != "leave"
        || newly_left
        || room
            .sender
            .as_deref()
            .is_some_and(|sender| sender != user_id)
}

/// `filter_rooms_using_tables`: the rooms of `rooms` that pass `filters`,
/// in their order, plus any the user was state reset out of that the
/// connection has sent before (`sent_before` is whether it has, from the
/// previous connection state). `room_tags` maps a room to the user's tags
/// on it, and is only read for the tag filters.
pub fn filter_rooms_using_tables<'a>(
    rooms: &[&'a RoomForUser],
    filters: &Filters,
    dm_room_ids: &HashSet<String>,
    room_tags: &HashMap<String, HashSet<String>>,
    sent_before: impl Fn(&str) -> bool,
) -> Result<Vec<&'a RoomForUser>, NotImplementedFilter> {
    if filters.spaces.is_some() {
        return Err(NotImplementedFilter("spaces"));
    }
    if filters.room_name_like.is_some() {
        return Err(NotImplementedFilter("room_name_like"));
    }

    let no_tags = HashSet::new();
    let passes = |room: &RoomForUser| -> bool {
        if let Some(is_dm) = filters.is_dm {
            if dm_room_ids.contains(&*room.room_id) != is_dm {
                return false;
            }
        }
        if let Some(is_encrypted) = filters.is_encrypted {
            // An unknown encryption status fails either way.
            if !room.has_known_state || room.is_encrypted != is_encrypted {
                return false;
            }
        }
        if let Some(is_invite) = filters.is_invite {
            if (&*room.membership == "invite") != is_invite {
                return false;
            }
        }
        if filters.room_types.is_some() || filters.not_room_types.is_some() {
            if !room.has_known_state {
                return false;
            }
            let room_type = room.room_type.as_deref();
            if let Some(room_types) = &filters.room_types {
                if !room_types.iter().any(|t| t.as_deref() == room_type) {
                    return false;
                }
            }
            if let Some(not_room_types) = &filters.not_room_types {
                if not_room_types.iter().any(|t| t.as_deref() == room_type) {
                    return false;
                }
            }
        }
        if filters.tags.is_some() || filters.not_tags.is_some() {
            let tags = room_tags.get(&*room.room_id).unwrap_or(&no_tags);
            if let Some(wanted) = &filters.tags {
                if !wanted.iter().any(|tag| tags.contains(tag)) {
                    return false;
                }
            }
            if let Some(unwanted) = &filters.not_tags {
                if unwanted.iter().any(|tag| tags.contains(tag)) {
                    return false;
                }
            }
        }
        true
    };

    Ok(rooms
        .iter()
        .copied()
        .filter(|room| {
            passes(room)
                // State reset out of a room the client knows: it must hear
                // of the reset whatever the filters.
                || (room.event_id.is_none() && sent_before(&room.room_id))
        })
        .collect())
}

/// `filter_membership_for_sync` through the Rust implementation.
#[pyfunction]
fn filter_membership_for_sync_for_test(
    user_id: &str,
    room: PyRoomForUser,
    newly_left: bool,
) -> bool {
    filter_membership_for_sync(user_id, &room_from_py(room), newly_left)
}

/// `filter_rooms_using_tables` through the Rust implementation, with
/// `filters` as JSON. Returns the room IDs that pass, or raises
/// `NotImplementedError`.
#[pyfunction]
fn filter_rooms_for_test(
    rooms: Vec<PyRoomForUser>,
    filters: &str,
    dm_room_ids: HashSet<String>,
    room_tags: HashMap<String, HashSet<String>>,
    sent_before: HashSet<String>,
) -> PyResult<Vec<String>> {
    let filters: Filters =
        serde_json::from_str(filters).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let rooms: Vec<RoomForUser> = rooms.into_iter().map(room_from_py).collect();
    let rooms: Vec<&RoomForUser> = rooms.iter().collect();
    let filtered = filter_rooms_using_tables(&rooms, &filters, &dm_room_ids, &room_tags, |r| {
        sent_before.contains(r)
    })
    .map_err(|e| pyo3::exceptions::PyNotImplementedError::new_err(e.to_string()))?;
    Ok(filtered
        .iter()
        .map(|room| room.room_id.to_string())
        .collect())
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(filter_membership_for_sync_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(filter_rooms_for_test, m)?)?;
    Ok(())
}
