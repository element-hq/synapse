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

//! `sort_rooms`: a list's rooms by the stream ordering of the last event
//! the user can see in each, latest first.
//!
//! Python's method reads the events stream change cache and the database
//! as it goes. Here [`SortPlan`] is the decision logic, and the caller
//! fetches what it asks for between the steps:
//!
//! 1. [`SortPlan::new`] reads the cached positions.
//! 2. With a `limit`, [`SortPlan::rooms_to_recheck`] are cached positions
//!    past the `to_token`, whose last event before it the caller looks up
//!    and passes to [`SortPlan::apply_recheck`].
//! 3. [`SortPlan::early_result`] is the answer if the cache covered the
//!    top `limit` rooms.
//! 4. Otherwise the caller fetches the last event positions of
//!    [`SortPlan::joined_rooms`] and [`SortPlan::sort_all`] sorts.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::anyhow;

use crate::sliding_sync::room_lists::RoomForUser;

/// The positions [`sort_rooms`](self) has decided on so far.
#[derive(Debug, Clone)]
pub struct SortPlan {
    /// The earliest position the events stream change cache knows.
    earliest_cache_position: i64,
    /// The last activity of each room the user is not joined to: their
    /// membership event, as they see nothing after it.
    last_activity: HashMap<Arc<str>, i64>,
    /// The positions known to be in the stream change cache's range.
    cached_positions: HashMap<Arc<str>, i64>,
}

impl SortPlan {
    /// Read the positions the cache gives. `max_pos_of_last_change` is
    /// the cache's `get_max_pos_of_last_change`.
    pub fn new(
        rooms: &[&RoomForUser],
        earliest_cache_position: i64,
        max_pos_of_last_change: impl Fn(&str) -> Option<i64>,
    ) -> Self {
        let mut last_activity = HashMap::new();
        let mut cached_positions = HashMap::new();
        for room in rooms {
            if &*room.membership == "join" {
                if let Some(position) = max_pos_of_last_change(&room.room_id) {
                    cached_positions.insert(Arc::clone(&room.room_id), position);
                }
            } else {
                // FIXME, as in Python: invited or knocking users might see
                // further, depending on the history visibility.
                let stream = room.event_pos.stream;
                last_activity.insert(Arc::clone(&room.room_id), stream);
                if stream > earliest_cache_position {
                    cached_positions.insert(Arc::clone(&room.room_id), stream);
                }
            }
        }
        SortPlan {
            earliest_cache_position,
            last_activity,
            cached_positions,
        }
    }

    /// Whether the cached positions cover the top `limit` rooms.
    fn enough_cached(&self, limit: Option<usize>) -> bool {
        limit.is_some_and(|limit| self.cached_positions.len() >= limit)
    }

    /// The rooms whose cached position is past `to_stream`, which need
    /// their last event before the `to_token` looked up. Only with a
    /// `limit` the cache could answer.
    pub fn rooms_to_recheck(&self, limit: Option<usize>, to_stream: i64) -> Vec<Arc<str>> {
        if !self.enough_cached(limit) {
            return Vec::new();
        }
        self.cached_positions
            .iter()
            .filter(|(_, position)| **position > to_stream)
            .map(|(room_id, _)| Arc::clone(room_id))
            .collect()
    }

    /// Record a recheck: `last_event_stream` is the stream ordering of the
    /// room's last event before the `to_token`, if any. A position outside
    /// the cache's range drops the room from the cached positions.
    pub fn apply_recheck(&mut self, room_id: &str, last_event_stream: Option<i64>) {
        match last_event_stream {
            Some(stream) if stream > self.earliest_cache_position => {
                self.cached_positions.insert(Arc::from(room_id), stream);
            }
            _ => {
                self.cached_positions.remove(room_id);
            }
        }
    }

    /// The top rooms from the cache alone, if it covers `limit` of them:
    /// the rooms with a cached position, latest first, ties in `rooms`
    /// order.
    pub fn early_result<'a>(
        &self,
        rooms: &[&'a RoomForUser],
        limit: Option<usize>,
    ) -> Option<Vec<&'a RoomForUser>> {
        if !self.enough_cached(limit) {
            return None;
        }
        let mut sorted: Vec<&RoomForUser> = rooms
            .iter()
            .filter(|room| self.cached_positions.contains_key(&room.room_id))
            .copied()
            .collect();
        sorted.sort_by_key(|room| std::cmp::Reverse(self.cached_positions[&room.room_id]));
        Some(sorted)
    }

    /// The joined rooms whose last event position the full sort needs.
    pub fn joined_rooms(rooms: &[&RoomForUser]) -> Vec<Arc<str>> {
        rooms
            .iter()
            .filter(|room| &*room.membership == "join")
            .map(|room| Arc::clone(&room.room_id))
            .collect()
    }

    /// Sort every room, given `joined_positions`, the last event position
    /// before the `to_token` of each joined room. Fails for a joined room
    /// without one, where Python raises `KeyError`.
    pub fn sort_all<'a>(
        mut self,
        rooms: &[&'a RoomForUser],
        joined_positions: &HashMap<Arc<str>, i64>,
    ) -> anyhow::Result<Vec<&'a RoomForUser>> {
        self.last_activity.extend(
            joined_positions
                .iter()
                .map(|(room_id, position)| (Arc::clone(room_id), *position)),
        );
        let mut keyed = Vec::with_capacity(rooms.len());
        for room in rooms {
            let position = self
                .last_activity
                .get(&room.room_id)
                .copied()
                .ok_or_else(|| anyhow!("no last event position for {}", room.room_id))?;
            keyed.push((position, *room));
        }
        keyed.sort_by_key(|(position, _)| std::cmp::Reverse(*position));
        Ok(keyed.into_iter().map(|(_, room)| room).collect())
    }
}
