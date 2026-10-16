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

//! A list's ranges to its `SYNC` operations, the room configs the lists
//! and room subscriptions combine into, and which of those rooms may have
//! updates to send (`_filter_relevant_rooms_to_send`).

use std::collections::HashSet;
use std::sync::Arc;

use indexmap::IndexMap;

use crate::sliding_sync::config::RoomSyncConfig;
use crate::sliding_sync::connection::{HaveSentRoom, PerConnectionState};

/// `MINIMUM_NOT_USED_AGE_EXPIRY`, in milliseconds: a connection with too
/// many rooms to update is only reset after this long unused.
pub const MINIMUM_NOT_USED_AGE_EXPIRY_MS: i64 = 60 * 60 * 1000;

/// `NUM_ROOMS_THRESHOLD`: more rooms with updates than this, on a
/// connection unused for [`MINIMUM_NOT_USED_AGE_EXPIRY_MS`], resets it.
/// Python passes its value per request, which tests patch.
pub const NUM_ROOMS_THRESHOLD: usize = 100;

/// The rooms a request covers, to their combined configs, in the order
/// the lists and room subscriptions add them.
pub type RelevantRoomMap = IndexMap<Arc<str>, RoomSyncConfig>;

/// `SlidingWindowList.Operation`: always a `SYNC`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncOp {
    pub range: (u64, u64),
    pub room_ids: Vec<Arc<str>>,
}

/// Whether a list with these `ranges` and `count` rooms needs sorting:
/// not when one range covers every room from the start, and not without
/// ranges.
pub fn needs_sort(ranges: &[(u64, u64)], count: usize) -> bool {
    match ranges {
        [] => false,
        [(0, end)] => (*end as u128) < (count as u128).saturating_sub(1),
        _ => true,
    }
}

/// The number of top rooms the ranges need sorted: the end of the last
/// one, inclusive.
pub fn sort_limit(ranges: &[(u64, u64)]) -> usize {
    ranges
        .iter()
        .map(|(_, end)| {
            usize::try_from(*end)
                .unwrap_or(usize::MAX)
                .saturating_add(1)
        })
        .max()
        .unwrap_or(0)
}

/// `_sync_list_ops`: the `SYNC` operations of a list's `ranges` over its
/// sorted rooms, adding each room in a range to `relevant_room_map` with
/// the list's config combined with the room's existing one.
///
/// As in Python, once a room combines with an existing config, the rooms
/// after it in the list take the combined config too.
pub fn sync_list_ops(
    ranges: &[(u64, u64)],
    sorted_room_ids: &[Arc<str>],
    room_sync_config: &RoomSyncConfig,
    relevant_room_map: &mut RelevantRoomMap,
) -> Vec<SyncOp> {
    let mut room_sync_config = room_sync_config.clone();
    let mut ops = Vec::with_capacity(ranges.len());
    for &(start, end) in ranges {
        // An end before the start takes no rooms, as in Python.
        let max_num_rooms = if end < start {
            0
        } else {
            (end - start).saturating_add(1)
        };
        let skip = usize::try_from(start).unwrap_or(usize::MAX);
        let mut room_ids = Vec::new();
        for room_id in sorted_room_ids.iter().skip(skip) {
            if room_ids.len() as u64 >= max_num_rooms {
                break;
            }
            if let Some(existing) = relevant_room_map.get(room_id) {
                room_sync_config = existing.combine(&room_sync_config);
            }
            relevant_room_map.insert(Arc::clone(room_id), room_sync_config.clone());
            room_ids.push(Arc::clone(room_id));
        }
        ops.push(SyncOp {
            range: (start, end),
            room_ids,
        });
    }
    ops
}

/// Add a room subscription's room to `relevant_room_map`, combined with
/// the room's existing config.
pub fn add_room_subscription(
    relevant_room_map: &mut RelevantRoomMap,
    room_id: &Arc<str>,
    room_sync_config: RoomSyncConfig,
) {
    let config = match relevant_room_map.get(room_id) {
        Some(existing) => existing.combine(&room_sync_config),
        None => room_sync_config,
    };
    relevant_room_map.insert(Arc::clone(room_id), config);
}

/// `SlidingSyncUnknownPosition`: the connection is reset, as it has too
/// many rooms to update or its position is not one the store knows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownPosition;

impl std::fmt::Display for UnknownPosition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("unknown sliding sync connection position")
    }
}

impl std::error::Error for UnknownPosition {}

/// The rooms of an incremental sync that may have updates, the decision
/// half of `_filter_relevant_rooms_to_send`: rooms whose config expanded,
/// rooms never sent or with updates left over, and
/// `rooms_that_have_updates`, the rooms the events stream has new events
/// in since the `from_token`. Their order is `relevant_room_map`'s.
pub fn rooms_to_send<R: Clone>(
    relevant_room_map: &RelevantRoomMap,
    previous_state: &PerConnectionState<R>,
    rooms_that_have_updates: &HashSet<Arc<str>>,
    num_rooms_threshold: usize,
    now_ms: i64,
) -> Result<Vec<Arc<str>>, UnknownPosition> {
    let mut rooms_should_send: HashSet<&Arc<str>> = HashSet::new();
    for (room_id, room_config) in relevant_room_map {
        if let Some(prev) = previous_state.room_configs.get(&**room_id) {
            // Timeline limit increases and required state additions.
            if prev.combine(room_config) != *prev {
                rooms_should_send.insert(room_id);
                continue;
            }
        }
        match previous_state.rooms.have_sent_room(room_id) {
            HaveSentRoom::Never | HaveSentRoom::Previously(_) => {
                rooms_should_send.insert(room_id);
            }
            // Everything up to the `from_token` was sent.
            HaveSentRoom::Live => {}
        }
    }

    // A client with lots to catch up on does better starting over with a
    // small range, unless it synced recently and would loop.
    if rooms_that_have_updates.len() > num_rooms_threshold {
        if let Some(last_used_ts) = previous_state.last_used_ts {
            if now_ms - last_used_ts > MINIMUM_NOT_USED_AGE_EXPIRY_MS {
                return Err(UnknownPosition);
            }
        }
    }

    Ok(relevant_room_map
        .keys()
        .filter(|room_id| {
            rooms_should_send.contains(room_id) || rooms_that_have_updates.contains(*room_id)
        })
        .cloned()
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(limit: i64) -> RoomSyncConfig {
        RoomSyncConfig {
            timeline_limit: limit,
            ..Default::default()
        }
    }

    #[test]
    fn full_range_needs_no_sort() {
        assert!(!needs_sort(&[], 5));
        assert!(!needs_sort(&[(0, 4)], 5));
        assert!(!needs_sort(&[(0, 0)], 0));
        assert!(!needs_sort(&[(0, 10)], 5));
        assert!(needs_sort(&[(0, 3)], 5));
        assert!(needs_sort(&[(1, 10)], 5));
        assert!(needs_sort(&[(0, 10), (0, 10)], 5));
        assert_eq!(sort_limit(&[(0, 3), (5, 9)]), 10);
    }

    #[test]
    fn combined_config_carries_on_down_the_list() {
        let rooms: Vec<Arc<str>> = ["!a", "!b", "!c"].into_iter().map(Arc::from).collect();
        let mut relevant = RelevantRoomMap::new();
        relevant.insert(Arc::from("!b"), config(10));
        let ops = sync_list_ops(&[(0, 1), (2, 5)], &rooms, &config(1), &mut relevant);
        assert_eq!(ops[0].room_ids, rooms[..2].to_vec());
        assert_eq!(ops[1].room_ids, rooms[2..].to_vec());
        assert_eq!(relevant["!a"].timeline_limit, 1);
        assert_eq!(relevant["!b"].timeline_limit, 10);
        // Python's leak: `!c` takes `!b`'s combined config.
        assert_eq!(relevant["!c"].timeline_limit, 10);
    }
}
