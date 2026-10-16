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

//! The room lists: which rooms a request is interested in, filtered,
//! sorted and cut to the requested ranges
//! (`synapse.handlers.sliding_sync.room_lists`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use pyo3::{
    exceptions::{PyKeyError, PyValueError},
    prelude::*,
    types::PyModule,
};

use crate::sliding_sync::config::{config_from_py, to_test_tuple, RoomSyncConfig};
use crate::sliding_sync::connection::{HaveSentRoom, PerConnectionState, RoomStatusMap};
use crate::storage::sync_store::PersistedEventPosition;
use crate::types::stream_token::RoomStreamToken;

pub mod filter;
pub mod lists;
pub mod sort;

/// A user's membership in a room at the `to_token`, as the sliding sync
/// tables record it: `RoomsForUserSlidingSync`, which also stands for
/// `RoomsForUser` and `RoomsForUserStateReset` where only their fields are
/// read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoomForUser {
    pub room_id: Arc<str>,
    /// `None` after a state reset.
    pub sender: Option<Arc<str>>,
    pub membership: Arc<str>,
    /// `None` after a state reset.
    pub event_id: Option<Arc<str>>,
    pub event_pos: PersistedEventPosition,
    pub room_version_id: Arc<str>,
    pub has_known_state: bool,
    pub room_type: Option<Arc<str>>,
    pub is_encrypted: bool,
}

/// A [`RoomForUser`] from the test form `(room_id, sender, membership,
/// event_id, stream, has_known_state, room_type, is_encrypted)`.
pub(crate) type PyRoomForUser = (
    String,
    Option<String>,
    String,
    Option<String>,
    i64,
    bool,
    Option<String>,
    bool,
);

pub(crate) fn room_from_py(room: PyRoomForUser) -> RoomForUser {
    let (room_id, sender, membership, event_id, stream, has_known_state, room_type, is_encrypted) =
        room;
    RoomForUser {
        room_id: Arc::from(room_id),
        sender: sender.map(Arc::from),
        membership: Arc::from(membership),
        event_id: event_id.map(Arc::from),
        event_pos: PersistedEventPosition {
            instance_name: Arc::from("master"),
            stream,
        },
        room_version_id: Arc::from("10"),
        has_known_state,
        room_type: room_type.map(Arc::from),
        is_encrypted,
    }
}

type PyConfig = (i64, BTreeMap<String, Vec<String>>);

/// `sort_rooms` through the Rust implementation, with the stream change
/// cache as `earliest_cache_position` and `cache` (room to its max position
/// of last change), and the database as `recheck` (room to the stream of
/// its last event before the `to_token`) and `joined_positions`. Returns
/// the sorted room IDs. Raises `KeyError` as Python does.
#[pyfunction]
#[pyo3(signature = (rooms, earliest_cache_position, cache, to_stream, limit, recheck, joined_positions))]
fn sort_rooms_for_test(
    rooms: Vec<PyRoomForUser>,
    earliest_cache_position: i64,
    cache: HashMap<String, i64>,
    to_stream: i64,
    limit: Option<usize>,
    recheck: HashMap<String, Option<i64>>,
    joined_positions: HashMap<String, i64>,
) -> PyResult<Vec<String>> {
    let rooms: Vec<RoomForUser> = rooms.into_iter().map(room_from_py).collect();
    let rooms: Vec<&RoomForUser> = rooms.iter().collect();
    let mut plan = sort::SortPlan::new(&rooms, earliest_cache_position, |room_id| {
        cache.get(room_id).copied()
    });
    for room_id in plan.rooms_to_recheck(limit, to_stream) {
        let result = recheck
            .get(&*room_id)
            .ok_or_else(|| PyValueError::new_err(format!("no recheck for {room_id}")))?;
        plan.apply_recheck(&room_id, *result);
    }
    let sorted = match plan.early_result(&rooms, limit) {
        Some(sorted) => sorted,
        None => {
            let joined: HashMap<Arc<str>, i64> = sort::SortPlan::joined_rooms(&rooms)
                .into_iter()
                .filter_map(|room_id| {
                    joined_positions
                        .get(&*room_id)
                        .map(|position| (room_id, *position))
                })
                .collect();
            plan.sort_all(&rooms, &joined)
                .map_err(|e| PyKeyError::new_err(e.to_string()))?
        }
    };
    Ok(sorted.iter().map(|room| room.room_id.to_string()).collect())
}

/// `_sync_list_ops` through the Rust implementation, the relevant room map
/// as a list of `(room_id, config)`. Returns `(ops, relevant_room_map)`,
/// each op as `(range, room_ids)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn sync_list_ops_for_test(
    ranges: Vec<(u64, u64)>,
    sorted_room_ids: Vec<String>,
    room_sync_config: PyConfig,
    relevant_room_map: Vec<(String, PyConfig)>,
) -> (Vec<((u64, u64), Vec<String>)>, Vec<(String, PyConfig)>) {
    let mut relevant: lists::RelevantRoomMap = relevant_room_map
        .into_iter()
        .map(|(room_id, (limit, map))| (Arc::from(room_id), config_from_py(limit, map)))
        .collect();
    let sorted: Vec<Arc<str>> = sorted_room_ids.into_iter().map(Arc::from).collect();
    let ops = lists::sync_list_ops(
        &ranges,
        &sorted,
        &config_from_py(room_sync_config.0, room_sync_config.1),
        &mut relevant,
    );
    (
        ops.into_iter()
            .map(|op| {
                (
                    op.range,
                    op.room_ids.iter().map(|r| r.to_string()).collect(),
                )
            })
            .collect(),
        relevant
            .iter()
            .map(|(room_id, config)| (room_id.to_string(), to_test_tuple(config)))
            .collect(),
    )
}

/// The decision of `_filter_relevant_rooms_to_send` through the Rust
/// implementation, each room's status as `(flag, token)` with a token an
/// int. Returns the room IDs to send, or raises `ValueError` for an
/// unknown position.
#[pyfunction]
fn rooms_to_send_for_test(
    relevant_room_map: Vec<(String, PyConfig)>,
    room_configs: HashMap<String, PyConfig>,
    statuses: HashMap<String, (String, Option<i64>)>,
    last_used_ts: Option<i64>,
    rooms_that_have_updates: HashSet<String>,
    now_ms: i64,
) -> PyResult<Vec<String>> {
    let relevant: lists::RelevantRoomMap = relevant_room_map
        .into_iter()
        .map(|(room_id, (limit, map))| (Arc::from(room_id), config_from_py(limit, map)))
        .collect();
    let mut rooms = HashMap::new();
    for (room_id, (flag, token)) in statuses {
        let status = match (flag.as_str(), token) {
            ("never", _) => HaveSentRoom::Never,
            ("live", _) => HaveSentRoom::Live,
            (_, Some(token)) => HaveSentRoom::Previously(RoomStreamToken::live(token)),
            _ => return Err(PyValueError::new_err("bad status")),
        };
        rooms.insert(room_id, status);
    }
    let room_configs: HashMap<String, RoomSyncConfig> = room_configs
        .into_iter()
        .map(|(room_id, (limit, map))| (room_id, config_from_py(limit, map)))
        .collect();
    let state: PerConnectionState<i64> = PerConnectionState {
        last_used_ts,
        rooms: RoomStatusMap::new(rooms),
        room_configs: Arc::new(room_configs),
        ..Default::default()
    };
    let updates: HashSet<Arc<str>> = rooms_that_have_updates.into_iter().map(Arc::from).collect();
    lists::rooms_to_send(&relevant, &state, &updates, now_ms)
        .map(|rooms| rooms.iter().map(|r| r.to_string()).collect())
        .map_err(|_| PyValueError::new_err("unknown position"))
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    filter::register_test_functions(m)?;
    m.add_function(wrap_pyfunction!(sort_rooms_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(sync_list_ops_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(rooms_to_send_for_test, m)?)?;
    Ok(())
}
