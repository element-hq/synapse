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

use std::sync::Arc;

use pyo3::{prelude::*, types::PyModule};

use crate::storage::sync_store::PersistedEventPosition;

pub mod filter;

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

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    filter::register_test_functions(m)?;
    Ok(())
}
