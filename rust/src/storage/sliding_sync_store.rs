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

//! What the sliding sync handler reads from the database, and the two
//! transactions that load and store a connection's state.
//!
//! [`SlidingSyncReader`] is a [`SyncReader`] with the sliding sync queries
//! on top, so one reader serves a request's v2 reads (timelines, state,
//! events) and its sliding sync ones, counting both in one set of stats.
//! Each query is a port of the store method it is named after, and
//! `tests/storage/test_sliding_sync_store.py` runs both against the same
//! database.
//!
//! Only the path Python takes once the sliding sync background updates
//! have finished is ported: the request falls back to Python as a whole
//! before then.
//!
//! The reads that consult Python's in-memory stream change caches take
//! what they need from them as arguments, read once per request: whether
//! the user's membership may have changed, and which rooms have new
//! events. The caches that mirror a Python `@cached` function are in
//! [`StoreCaches`](super::caches::StoreCaches).

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    ops::Deref,
    sync::Arc,
};

use anyhow::Context;
use futures::{future::BoxFuture, FutureExt};
use indexmap::IndexMap;
use log::warn;
use pyo3::{prelude::*, types::PyTuple};
use tokio::runtime::Handle;
use tokio_postgres::{error::SqlState, Row};

use crate::caches::python::PyCacheKey;
use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::room_versions::is_known_room_version;
use crate::sliding_sync::config::{RequiredStateMap, RoomSyncConfig};
use crate::sliding_sync::connection::{HaveSentRoom, RoomLazyMembershipChanges};
use crate::sliding_sync::room_lists::{lists::UnknownPosition, RoomForUser};
use crate::sliding_sync::SlidingSyncHandler;
use crate::state::StateKey;
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::{
    anyhow_to_py, Borrowed, Interner, PersistedEventPosition, SyncReader, SyncStore,
    SELECT_MANY_BATCH_SIZE,
};
use crate::types::state_filter::StateFilter;
use crate::types::stream_token::{filter_results_by_stream, RoomStreamToken};

/// How often a connection's `last_used_ts` is rewritten when it is used,
/// in milliseconds: `UPDATE_INTERVAL_LAST_USED_TS`.
pub const UPDATE_INTERVAL_LAST_USED_TS_MS: i64 = 5 * 60 * 1000;

/// How many times a write transaction is tried, as `runInteraction` tries
/// one that fails with a serialization error or a deadlock.
const MAX_TRANSACTION_ATTEMPTS: usize = 5;

/// The rooms of a user's membership snapshots, by room ID, in the order
/// the query returns them, as Python's dict keeps it: a list that covers
/// every room is not sorted, so its rooms come in this order.
pub type SnapshotRooms = IndexMap<Arc<str>, RoomForUser>;

/// A user's tags: room ID to tag to the tag's content.
pub type UserTags = HashMap<Arc<str>, HashMap<String, serde_json::Value>>;

/// `get_room_type`'s answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoomType {
    /// `ROOM_UNKNOWN_SENTINEL`: the server has no current state for the
    /// room, or no create event in it.
    Unknown,
    /// The create event's `type`, `None` without one. A `type` that is not
    /// a string reads as none, as the sliding sync tables record it.
    Known(Option<Arc<str>>),
}

/// `RoomsForUserStateReset`: a meaningful change to the user's membership
/// of a room, from `get_sliding_sync_membership_changes`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MembershipChange {
    pub room_id: Arc<str>,
    /// `None` after a state reset.
    pub sender: Option<Arc<str>>,
    pub membership: Arc<str>,
    /// `None` after a state reset.
    pub event_id: Option<Arc<str>>,
    pub event_pos: PersistedEventPosition,
    pub room_version_id: Option<Arc<str>>,
}

/// `CurrentStateDeltaMembership`: a change to the user's membership in a
/// room's current state, and the membership it replaced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeltaMembershipChange {
    pub room_id: Arc<str>,
    /// `None` when the server left the room or a state reset removed the
    /// user.
    pub event_id: Option<Arc<str>>,
    pub event_pos: PersistedEventPosition,
    pub membership: Arc<str>,
    pub sender: Option<Arc<str>>,
    pub prev_event_id: Option<Arc<str>>,
    pub prev_event_pos: Option<PersistedEventPosition>,
    pub prev_membership: Option<Arc<str>>,
    pub prev_sender: Option<Arc<str>>,
}

/// `PerConnectionStateDB`: a connection's state as stored, with each
/// stream's tokens as their strings. Loading it gives the whole state at a
/// position. Storing one stores the request's updates.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct StoredConnectionState {
    /// When the connection was last used, before this load.
    pub last_used_ts: Option<i64>,
    pub rooms: HashMap<String, HaveSentRoom<String>>,
    pub receipts: HashMap<String, HaveSentRoom<String>>,
    pub account_data: HashMap<String, HaveSentRoom<String>>,
    pub room_configs: HashMap<String, RoomSyncConfig>,
}

impl StoredConnectionState {
    /// `len()` of the `PerConnectionState` it becomes: what the memo of
    /// loaded states weighs it as.
    pub fn len(&self) -> usize {
        self.rooms.len() + self.receipts.len() + self.account_data.len() + self.room_configs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The updates of one request to a connection's state, for
/// [`SlidingSyncReader::persist_connection_state`]: each stream's status
/// updates with their tokens as strings, the room configs set, and the
/// rooms' lazy-loaded memberships.
#[derive(Debug, Clone, Default)]
pub struct ConnectionStateUpdates {
    pub rooms: HashMap<String, HaveSentRoom<String>>,
    pub receipts: HashMap<String, HaveSentRoom<String>>,
    pub account_data: HashMap<String, HaveSentRoom<String>>,
    pub room_configs: HashMap<String, RoomSyncConfig>,
    pub room_lazy_membership: HashMap<String, RoomLazyMembershipChanges>,
}

/// The key of a loaded connection state: `(user_id, device_id, conn_id,
/// connection_position)`, the arguments of
/// `get_and_clear_connection_positions`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ConnectionPositionKey {
    pub user_id: Arc<str>,
    pub device_id: Arc<str>,
    pub conn_id: Arc<str>,
    pub connection_position: i64,
}

impl PyCacheKey for ConnectionPositionKey {
    fn from_py_key(key: &Bound<'_, PyTuple>) -> Option<Self> {
        let (user_id, device_id, conn_id, connection_position): (String, String, String, i64) =
            key.extract().ok()?;
        Some(ConnectionPositionKey {
            user_id: Arc::from(user_id),
            device_id: Arc::from(device_id),
            conn_id: Arc::from(conn_id),
            connection_position,
        })
    }
}

impl SyncStore {
    /// A sliding sync reader for one request, over `pool`.
    pub fn sliding_sync_reader(&self, pool: ConnectionPool) -> SlidingSyncReader<'_> {
        SlidingSyncReader {
            reader: self.reader(pool),
        }
    }
}

/// The queries of one sliding sync request: the v2 reader's, through
/// `Deref`, and the sliding sync ones. See the module docs.
pub struct SlidingSyncReader<'a> {
    reader: SyncReader<'a>,
}

impl<'a> Deref for SlidingSyncReader<'a> {
    type Target = SyncReader<'a>;

    fn deref(&self) -> &SyncReader<'a> {
        &self.reader
    }
}

/// The columns of a membership snapshot, as `RoomsForUserSlidingSync`
/// reads them, less the room type and encryption, which each query takes
/// from its own tables.
const SNAPSHOT_COLUMNS: &str = "m.room_id, m.sender, m.membership, m.membership_event_id,
    r.room_version, m.event_instance_name, m.event_stream_ordering, m.has_known_state";

/// A [`RoomForUser`] from a row of [`SNAPSHOT_COLUMNS`], the room type and
/// whether the room is encrypted.
fn snapshot_from_row(row: &Row, interner: &mut Interner) -> anyhow::Result<RoomForUser> {
    let room_id: String = row.try_get(0)?;
    let sender: String = row.try_get(1)?;
    let membership: String = row.try_get(2)?;
    let event_id: String = row.try_get(3)?;
    let room_version: Option<String> = row.try_get(4)?;
    let instance_name: String = row.try_get(5)?;
    let stream: i64 = row.try_get(6)?;
    let has_known_state: bool = row.try_get(7)?;
    let room_type: Option<String> = row.try_get(8)?;
    let is_encrypted: bool = row.try_get(9)?;
    Ok(RoomForUser {
        room_id: Arc::from(room_id),
        sender: Some(Arc::from(sender)),
        membership: interner.intern(&membership),
        event_id: Some(Arc::from(event_id)),
        event_pos: PersistedEventPosition {
            instance_name: interner.intern(&instance_name),
            stream,
        },
        room_version_id: room_version.map(|version| interner.intern(&version)),
        has_known_state,
        room_type: room_type.map(|room_type| interner.intern(&room_type)),
        is_encrypted,
    })
}

/// Whether a snapshot's room version is one this server knows. A room
/// without a version is not.
fn has_known_version(room: &RoomForUser) -> bool {
    room.room_version_id
        .as_deref()
        .is_some_and(is_known_room_version)
}

/// A position from a nullable instance name: a null one is a row from
/// before sharded persisters, which Python reads as `"master"`.
fn position(
    instance_name: Option<String>,
    stream: i64,
    interner: &mut Interner,
) -> PersistedEventPosition {
    PersistedEventPosition {
        instance_name: interner.intern(instance_name.as_deref().unwrap_or("master")),
        stream,
    }
}

/// Fail on a historic token, as the Python asserts.
fn assert_live(token: &RoomStreamToken) -> anyhow::Result<()> {
    anyhow::ensure!(!token.is_historic(), "expected a live token, got {token:?}");
    Ok(())
}

/// Whether `e` is a serialization failure or a deadlock, which
/// `runInteraction` retries.
fn is_retryable(e: &anyhow::Error) -> bool {
    e.chain().any(|cause| {
        cause
            .downcast_ref::<tokio_postgres::Error>()
            .and_then(|e| e.code())
            .is_some_and(|code| {
                *code == SqlState::T_R_SERIALIZATION_FAILURE
                    || *code == SqlState::T_R_DEADLOCK_DETECTED
            })
    })
}

/// A room status as `sliding_sync_connection_streams` stores it:
/// `HaveSentRoomFlag`'s value and the token.
fn status_to_row(status: &HaveSentRoom<String>) -> (&'static str, Option<&str>) {
    match status {
        HaveSentRoom::Never => ("never", None),
        HaveSentRoom::Live => ("live", None),
        HaveSentRoom::Previously(token) => ("previously", Some(token)),
    }
}

/// A room status from its row. A `previously` without a token is an error,
/// where Python would carry the `None` on until something read it.
fn status_from_row(
    status: &str,
    last_token: Option<String>,
) -> anyhow::Result<HaveSentRoom<String>> {
    match (status, last_token) {
        ("never", _) => Ok(HaveSentRoom::Never),
        ("live", _) => Ok(HaveSentRoom::Live),
        ("previously", Some(token)) => Ok(HaveSentRoom::Previously(token)),
        (status, token) => anyhow::bail!("invalid room status {status:?} with token {token:?}"),
    }
}

/// A room's required state as `sliding_sync_connection_required_state`
/// stores it: the sorted `[type, state_key]` pairs, encoded as Python's
/// `json_encoder` does, so the same state from either side dedups to one
/// row.
pub fn encode_required_state(map: &RequiredStateMap) -> String {
    let mut out = String::from("[");
    let mut first = true;
    // The map and its sets are sorted, so this is the order of Python's
    // `sorted` over the pairs.
    for (event_type, state_keys) in map {
        for state_key in state_keys {
            if !first {
                out.push(',');
            }
            first = false;
            out.push('[');
            crate::state::v2::push_py_json_str(&mut out, event_type);
            out.push(',');
            crate::state::v2::push_py_json_str(&mut out, state_key);
            out.push(']');
        }
    }
    out.push(']');
    out
}

/// The reverse of [`encode_required_state`].
fn decode_required_state(json: &str) -> anyhow::Result<RequiredStateMap> {
    let pairs: Vec<(String, String)> =
        serde_json::from_str(json).with_context(|| format!("required state {json}"))?;
    let mut map: RequiredStateMap = BTreeMap::new();
    for (event_type, state_key) in pairs {
        map.entry(event_type).or_default().insert(state_key);
    }
    Ok(map)
}

impl<'a> SlidingSyncReader<'a> {
    // Membership snapshots.

    /// `get_sliding_sync_rooms_for_user_from_membership_snapshots`: the
    /// user's rooms from their membership snapshots, less forgotten rooms,
    /// rooms they left themselves, and rooms of unknown versions.
    pub async fn rooms_from_snapshots(&self, user_id: &str) -> anyhow::Result<Arc<SnapshotRooms>> {
        let key: Arc<str> = Arc::from(user_id);
        self.store()
            .caches()
            .sliding_sync_rooms_for_user
            .get_or_load(Arc::clone(&key), || async {
                let sql = format!(
                    "SELECT {SNAPSHOT_COLUMNS},
                        COALESCE(j.room_type, m.room_type),
                        COALESCE(j.is_encrypted, m.is_encrypted)
                    FROM sliding_sync_membership_snapshots AS m
                    INNER JOIN rooms AS r USING (room_id)
                    LEFT JOIN sliding_sync_joined_rooms AS j
                        ON (j.room_id = m.room_id AND m.membership = 'join')
                    WHERE user_id = $1
                        AND m.forgotten = 0
                        AND (m.membership != 'leave' OR m.user_id != m.sender)"
                );
                let rows = self.query_one_shot(&sql, &[&&*key]).await?;
                let mut interner = Interner::default();
                let mut rooms = SnapshotRooms::with_capacity(rows.len());
                for row in &rows {
                    let room = snapshot_from_row(row, &mut interner)?;
                    // Rooms of versions we don't know shouldn't go down sync,
                    // and their metadata may be broken.
                    if has_known_version(&room) {
                        rooms.insert(Arc::clone(&room.room_id), room);
                    }
                }
                Ok(Arc::new(rooms))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_sliding_sync_self_leave_rooms_after_to_token`: the rooms the
    /// user left themselves after `to_token`, from their snapshots, which
    /// may have been relevant before the leave.
    pub async fn self_leave_rooms_after(
        &self,
        user_id: &str,
        to_token: &RoomStreamToken,
    ) -> anyhow::Result<SnapshotRooms> {
        // The token's instance map covers more of the stream than its
        // minimum: fetch from the minimum and filter.
        let min_to_token_position = to_token.stream;
        let sql = format!(
            "SELECT {SNAPSHOT_COLUMNS}, m.room_type, m.is_encrypted
            FROM sliding_sync_membership_snapshots AS m
            INNER JOIN rooms AS r USING (room_id)
            WHERE user_id = $1
                AND m.forgotten = 0
                AND m.membership = 'leave'
                AND m.user_id = m.sender
                AND (m.event_stream_ordering > $2)"
        );
        let rows = self
            .query_one_shot(&sql, &[&user_id, &min_to_token_position])
            .await?;
        let mut interner = Interner::default();
        let mut rooms = SnapshotRooms::new();
        for row in &rows {
            let room = snapshot_from_row(row, &mut interner)?;
            if !has_known_version(&room) {
                continue;
            }
            // Only the leaves after the token.
            if filter_results_by_stream(
                None,
                Some(to_token),
                Some(&room.event_pos.instance_name),
                room.event_pos.stream,
            )? {
                continue;
            }
            rooms.insert(Arc::clone(&room.room_id), room);
        }
        Ok(rooms)
    }

    /// `get_sliding_sync_room_for_user`: the user's snapshot of the room,
    /// forgotten or not left, whatever its version.
    pub async fn snapshot_room(
        &self,
        user_id: &str,
        room_id: &str,
    ) -> anyhow::Result<Option<RoomForUser>> {
        let sql = format!(
            "SELECT {SNAPSHOT_COLUMNS},
                COALESCE(j.room_type, m.room_type),
                COALESCE(j.is_encrypted, m.is_encrypted)
            FROM sliding_sync_membership_snapshots AS m
            INNER JOIN rooms AS r USING (room_id)
            LEFT JOIN sliding_sync_joined_rooms AS j
                ON (j.room_id = m.room_id AND m.membership = 'join')
            WHERE user_id = $1
                AND m.forgotten = 0
                AND m.room_id = $2"
        );
        let rows = self.query_one_shot(&sql, &[&user_id, &room_id]).await?;
        let mut interner = Interner::default();
        rows.first()
            .map(|row| snapshot_from_row(row, &mut interner))
            .transpose()
    }

    /// `get_sliding_sync_room_for_user_batch`: [`Self::snapshot_room`] for
    /// each of the rooms.
    pub async fn snapshot_rooms(
        &self,
        user_id: &str,
        room_ids: &[Arc<str>],
    ) -> anyhow::Result<SnapshotRooms> {
        let mut rooms = SnapshotRooms::with_capacity(room_ids.len());
        if room_ids.is_empty() {
            return Ok(rooms);
        }
        let sql = format!(
            "SELECT {SNAPSHOT_COLUMNS},
                COALESCE(j.room_type, m.room_type),
                COALESCE(j.is_encrypted, m.is_encrypted)
            FROM sliding_sync_membership_snapshots AS m
            INNER JOIN rooms AS r USING (room_id)
            LEFT JOIN sliding_sync_joined_rooms AS j
                ON (j.room_id = m.room_id AND m.membership = 'join')
            WHERE m.forgotten = 0
                AND m.room_id = ANY($1)
                AND user_id = $2"
        );
        let ids: Vec<&str> = room_ids.iter().map(|id| &**id).collect();
        let rows = self.query_one_shot(&sql, &[&ids, &user_id]).await?;
        let mut interner = Interner::default();
        for row in &rows {
            let room = snapshot_from_row(row, &mut interner)?;
            rooms.insert(Arc::clone(&room.room_id), room);
        }
        Ok(rooms)
    }

    // Membership changes.

    /// `get_sliding_sync_membership_changes`: the meaningful changes to the
    /// user's membership after `from_token` and up to `to_token`, by room,
    /// from the snapshots and, for state resets, the current state deltas.
    /// In stream order of each room's first change.
    ///
    /// `membership_may_have_changed` is
    /// `_membership_stream_cache.has_entity_changed(user_id, from_token.stream)`.
    pub async fn snapshot_membership_changes(
        &self,
        user_id: &str,
        from_token: &RoomStreamToken,
        to_token: &RoomStreamToken,
        excluded_room_ids: &HashSet<Arc<str>>,
        membership_may_have_changed: bool,
    ) -> anyhow::Result<IndexMap<Arc<str>, MembershipChange>> {
        assert_live(from_token)?;
        assert_live(to_token)?;
        let mut changes = IndexMap::new();
        if from_token == to_token || !membership_may_have_changed {
            return Ok(changes);
        }

        // The tokens' instance maps cover more of the stream than their
        // minimums: fetch up to the maximum and filter.
        let min_from_id = from_token.stream;
        let max_to_id = to_token.get_max_stream_pos();
        // The snapshots don't include users state reset out of a room, so
        // the current state deltas are read for those.
        let rows = self
            .query_one_shot(
                "SELECT
                    room_id,
                    membership_event_id,
                    event_instance_name,
                    event_stream_ordering,
                    membership,
                    sender,
                    prev_membership,
                    room_version
                FROM
                (
                    SELECT
                        s.room_id,
                        s.membership_event_id,
                        s.event_instance_name,
                        s.event_stream_ordering,
                        s.membership,
                        s.sender,
                        m_prev.membership AS prev_membership
                    FROM sliding_sync_membership_snapshots as s
                        LEFT JOIN event_edges AS e ON e.event_id = s.membership_event_id
                        LEFT JOIN room_memberships AS m_prev ON m_prev.event_id = e.prev_event_id
                    WHERE s.user_id = $1

                    UNION ALL

                    SELECT
                        s.room_id,
                        e.event_id,
                        s.instance_name,
                        s.stream_id,
                        m.membership,
                        e.sender,
                        m_prev.membership AS prev_membership
                    FROM current_state_delta_stream AS s
                        LEFT JOIN events AS e ON e.event_id = s.event_id
                        LEFT JOIN room_memberships AS m ON m.event_id = s.event_id
                        LEFT JOIN room_memberships AS m_prev ON m_prev.event_id = s.prev_event_id
                    WHERE
                        s.type = 'm.room.member'
                        AND s.state_key = $1
                ) AS c
                INNER JOIN rooms USING (room_id)
                WHERE event_stream_ordering > $2 AND event_stream_ordering <= $3
                ORDER BY event_stream_ordering ASC",
                &[&user_id, &min_from_id, &max_to_id],
            )
            .await?;

        let mut interner = Interner::default();
        for row in &rows {
            let room_id: String = row.try_get(0)?;
            let membership_event_id: Option<String> = row.try_get(1)?;
            let instance_name: Option<String> = row.try_get(2)?;
            let stream: i64 = row.try_get(3)?;
            let membership: Option<String> = row.try_get(4)?;
            let sender: Option<String> = row.try_get(5)?;
            let prev_membership: Option<String> = row.try_get(6)?;
            let room_version: Option<String> = row.try_get(7)?;

            if excluded_room_ids.contains(room_id.as_str()) {
                continue;
            }
            if !filter_results_by_stream(
                Some(from_token),
                Some(to_token),
                instance_name.as_deref(),
                stream,
            )? {
                continue;
            }
            // When the server leaves a room, every piece of current state
            // gets a row with no event, the user's leave included. Don't
            // report a leave twice.
            if membership_event_id.is_none() && prev_membership.as_deref() == Some("leave") {
                continue;
            }
            // Python's "SUSPICIOUS" case, ported as it is: a state reset
            // after a change in the same window is not reported.
            if membership_event_id.is_none() && changes.contains_key(room_id.as_str()) {
                continue;
            }
            // A row without an event is the server leaving or a state
            // reset, which leave the user out of the room.
            let membership = membership.unwrap_or_else(|| "leave".to_owned());
            // The same membership again, such as a display name change.
            if prev_membership.as_deref() == Some(membership.as_str()) {
                continue;
            }
            let room_id: Arc<str> = Arc::from(room_id);
            changes.insert(
                Arc::clone(&room_id),
                MembershipChange {
                    room_id,
                    sender: sender.map(Arc::from),
                    membership: interner.intern(&membership),
                    event_id: membership_event_id.map(Arc::from),
                    event_pos: position(instance_name, stream, &mut interner),
                    room_version_id: room_version.map(|version| interner.intern(&version)),
                },
            );
        }
        Ok(changes)
    }

    /// `get_current_state_delta_membership_changes_for_user`: the changes
    /// to the user's membership in the current state of their rooms after
    /// `from_token` and up to `to_token`, in stream order, with the
    /// membership each replaced.
    ///
    /// `membership_may_have_changed` is as for
    /// [`Self::snapshot_membership_changes`].
    pub async fn membership_delta_changes(
        &self,
        user_id: &str,
        from_token: &RoomStreamToken,
        to_token: &RoomStreamToken,
        excluded_room_ids: &HashSet<Arc<str>>,
        membership_may_have_changed: bool,
    ) -> anyhow::Result<Vec<DeltaMembershipChange>> {
        assert_live(from_token)?;
        assert_live(to_token)?;
        if from_token == to_token || !membership_may_have_changed {
            return Ok(Vec::new());
        }

        let min_from_id = from_token.stream;
        let max_to_id = to_token.get_max_stream_pos();
        // The delta's own position, not its event's: see the Python for why
        // that is enough between two valid tokens.
        let rows = self
            .query_one_shot(
                "SELECT
                    s.room_id,
                    e.event_id,
                    s.instance_name,
                    s.stream_id,
                    m.membership,
                    e.sender,
                    s.prev_event_id,
                    e_prev.instance_name AS prev_instance_name,
                    e_prev.stream_ordering AS prev_stream_ordering,
                    m_prev.membership AS prev_membership,
                    e_prev.sender AS prev_sender
                FROM current_state_delta_stream AS s
                    LEFT JOIN events AS e ON e.event_id = s.event_id
                    LEFT JOIN room_memberships AS m ON m.event_id = s.event_id
                    LEFT JOIN events AS e_prev ON e_prev.event_id = s.prev_event_id
                    LEFT JOIN room_memberships AS m_prev ON m_prev.event_id = s.prev_event_id
                WHERE s.stream_id > $1 AND s.stream_id <= $2
                    AND s.type = 'm.room.member'
                    AND s.state_key = $3
                ORDER BY s.stream_id ASC",
                &[&min_from_id, &max_to_id, &user_id],
            )
            .await?;

        let mut interner = Interner::default();
        let mut changes = Vec::new();
        for row in &rows {
            let room_id: String = row.try_get(0)?;
            let event_id: Option<String> = row.try_get(1)?;
            let instance_name: Option<String> = row.try_get(2)?;
            let stream: i64 = row.try_get(3)?;
            let membership: Option<String> = row.try_get(4)?;
            let sender: Option<String> = row.try_get(5)?;
            let prev_event_id: Option<String> = row.try_get(6)?;
            let prev_instance_name: Option<String> = row.try_get(7)?;
            let prev_stream: Option<i64> = row.try_get(8)?;
            let prev_membership: Option<String> = row.try_get(9)?;
            let prev_sender: Option<String> = row.try_get(10)?;

            if !filter_results_by_stream(
                Some(from_token),
                Some(to_token),
                instance_name.as_deref(),
                stream,
            )? {
                continue;
            }
            // As in `snapshot_membership_changes`: the server's leave after
            // the user's own.
            if event_id.is_none() && prev_membership.as_deref() == Some("leave") {
                continue;
            }
            if excluded_room_ids.contains(room_id.as_str()) {
                continue;
            }
            changes.push(DeltaMembershipChange {
                room_id: Arc::from(room_id),
                event_id: event_id.map(Arc::from),
                event_pos: position(instance_name, stream, &mut interner),
                membership: interner.intern(membership.as_deref().unwrap_or("leave")),
                sender: sender.map(Arc::from),
                prev_event_id: prev_event_id.map(Arc::from),
                prev_event_pos: prev_stream
                    .map(|stream| position(prev_instance_name, stream, &mut interner)),
                prev_membership: prev_membership.map(|membership| interner.intern(&membership)),
                prev_sender: prev_sender.map(Arc::from),
            });
        }
        Ok(changes)
    }

    // Stream positions.

    /// `get_rooms_that_have_updates_since_sliding_sync_table`: which of the
    /// rooms may have new events after `from_token`, false positives
    /// allowed.
    ///
    /// `changed_since` is `_events_stream_cache.get_all_entities_changed`
    /// at `from_token.stream`: the rooms with new events, or `None` if the
    /// cache does not reach back that far, when the table answers.
    pub async fn rooms_with_updates_since(
        &self,
        room_ids: &[Arc<str>],
        from_token: &RoomStreamToken,
        changed_since: Option<&HashSet<Arc<str>>>,
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        if let Some(changed) = changed_since {
            return Ok(room_ids
                .iter()
                .filter(|room_id| changed.contains(*room_id))
                .cloned()
                .collect());
        }
        let mut rooms = HashSet::new();
        if room_ids.is_empty() {
            return Ok(rooms);
        }
        let borrowed = self.borrow().await?;
        for batch in room_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT room_id FROM sliding_sync_joined_rooms
                    WHERE room_id = ANY($1) AND event_stream_ordering > $2",
                    &[&ids, &from_token.stream],
                )
                .await?;
            for row in rows {
                let room_id: String = row.try_get(0)?;
                rooms.insert(Arc::from(room_id));
            }
        }
        Ok(rooms)
    }

    /// `_bulk_get_max_event_pos`: the stream position of the latest event
    /// in each room, capped at `now_token`, the current room key
    /// (`get_room_max_token`), or `None` for a room without a
    /// `sliding_sync_joined_rooms` row. Through the mirror of
    /// `_get_max_event_pos`.
    ///
    /// `last_change_pos` is `_events_stream_cache.get_max_pos_of_last_change`
    /// for the rooms it knows. As in Python, the misses are answered from it
    /// alone, uncapped, when it knows them all. Otherwise every miss goes to
    /// the database, whose answer wins, and its positions stand only for the
    /// rooms without a row: Python's batch loop runs over all the rooms
    /// rather than the ones it didn't know.
    pub async fn max_event_pos(
        &self,
        room_ids: &[Arc<str>],
        now_token: &RoomStreamToken,
        last_change_pos: &HashMap<Arc<str>, i64>,
    ) -> anyhow::Result<HashMap<Arc<str>, Option<i64>>> {
        self.store()
            .caches()
            .max_event_pos
            .get_or_load_many(room_ids, |missing| async move {
                let mut results: HashMap<Arc<str>, Option<i64>> = missing
                    .iter()
                    .map(|room_id| (Arc::clone(room_id), last_change_pos.get(room_id).copied()))
                    .collect();
                if results.values().all(Option::is_some) {
                    return Ok(results);
                }
                let mut recheck = Vec::new();
                {
                    let borrowed = self.borrow().await?;
                    for batch in missing.chunks(SELECT_MANY_BATCH_SIZE) {
                        let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
                        let rows = borrowed
                            .query(
                                "SELECT room_id, event_stream_ordering
                                FROM sliding_sync_joined_rooms
                                WHERE room_id = ANY($1)
                                ORDER BY event_stream_ordering DESC",
                                &[&ids],
                            )
                            .await?;
                        for row in rows {
                            let room_id: String = row.try_get(0)?;
                            let stream: i64 = row.try_get(1)?;
                            let room_id: Arc<str> = Arc::from(room_id);
                            if stream <= now_token.stream {
                                results.insert(room_id, Some(stream));
                            } else {
                                recheck.push(room_id);
                            }
                        }
                    }
                }
                // Rooms whose latest event may be ahead of the token, which
                // is rare, one by one.
                for room_id in recheck {
                    if let Some((_, position)) = self.last_event_before(&room_id, now_token).await?
                    {
                        results.insert(room_id, Some(position.stream));
                    }
                }
                Ok(results)
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `bulk_get_last_event_pos_in_room_before_stream_ordering`: the stream
    /// position of the last event at or before `end_token` in each room
    /// that has one. `now_token` and `last_change_pos` are as for
    /// [`Self::max_event_pos`].
    pub async fn last_event_pos_before(
        &self,
        room_ids: &[Arc<str>],
        end_token: &RoomStreamToken,
        now_token: &RoomStreamToken,
        last_change_pos: &HashMap<Arc<str>, i64>,
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        // Most rooms' latest event is before the token, and cached.
        let uncapped = self
            .max_event_pos(room_ids, now_token, last_change_pos)
            .await?;
        let mut results = HashMap::with_capacity(uncapped.len());
        let mut recheck = Vec::new();
        let min_token = end_token.stream;
        for (room_id, stream) in uncapped {
            // `None` is what the cache holds for a room the load didn't
            // find.
            let Some(stream) = stream else { continue };
            if stream <= min_token {
                results.insert(room_id, stream);
            } else {
                recheck.push(room_id);
            }
        }
        for room_id in recheck {
            if let Some((_, position)) = self.last_event_before(&room_id, end_token).await? {
                results.insert(room_id, position.stream);
            }
        }
        Ok(results)
    }

    // Room metadata.

    /// `get_member_counts`, through its mirror.
    pub async fn cached_member_counts(
        &self,
        room_id: &str,
    ) -> anyhow::Result<Arc<HashMap<String, i64>>> {
        self.store()
            .caches()
            .member_counts
            .get_or_load(Arc::from(room_id), || async {
                Ok(Arc::new(self.member_counts(room_id).await?))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_room_type`: the type in the room's create event, from its
    /// current state.
    pub async fn room_type(&self, room_id: &str) -> anyhow::Result<RoomType> {
        self.store()
            .caches()
            .room_types
            .get_or_load(Arc::from(room_id), || async {
                let filter = StateFilter::from_types([("m.room.create", Some(""))]);
                let state = self.current_state_ids(room_id, &filter).await?;
                let Some(create_id) = state.get(&StateKey::new("m.room.create", "")) else {
                    return Ok(RoomType::Unknown);
                };
                let events = self
                    .events(
                        std::slice::from_ref(create_id),
                        RedactBehaviour::Redact,
                        false,
                        false,
                    )
                    .await?;
                let Some(create) = events.first() else {
                    return Ok(RoomType::Unknown);
                };
                Ok(RoomType::Known(
                    create
                        .content()
                        .get_field("type")
                        .and_then(|room_type| room_type.as_str())
                        .map(Arc::from),
                ))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_room_version_id`: the room's version, or `None` where Python
    /// raises `NotFoundError`.
    pub async fn room_version_id(&self, room_id: &str) -> anyhow::Result<Option<Arc<str>>> {
        /// The load's answer for a room without a version, so that it is not
        /// cached.
        #[derive(Debug)]
        struct NoVersion;
        impl std::fmt::Display for NoVersion {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("no room version")
            }
        }
        impl std::error::Error for NoVersion {}

        let result = self
            .store()
            .caches()
            .room_version_ids
            .get_or_load(Arc::from(room_id), || async {
                let rows = self
                    .query_one_shot(
                        "SELECT room_version FROM rooms WHERE room_id = $1",
                        &[&room_id],
                    )
                    .await?;
                let version: Option<String> = match rows.first() {
                    Some(row) => row.try_get(0)?,
                    None => None,
                };
                version
                    .map(Arc::from)
                    .ok_or_else(|| anyhow::Error::new(NoVersion))
            })
            .await;
        match result {
            Ok(version) => Ok(Some(version)),
            Err(e) if e.inner().is::<NoVersion>() => Ok(None),
            Err(e) => Err(anyhow::Error::new(e)),
        }
    }

    /// The `instance_map` table: each writer's instance ID and name, as
    /// stream tokens name writers by ID. Cached: see
    /// [`Self::forget_instance_map`].
    pub async fn instance_map(&self) -> anyhow::Result<Arc<[(i64, Arc<str>)]>> {
        self.store()
            .caches()
            .instance_map
            .get_or_load((), || async {
                let rows = self
                    .query_one_shot("SELECT instance_id, instance_name FROM instance_map", &[])
                    .await?;
                rows.iter()
                    .map(|row| {
                        let id: i32 = row.try_get(0)?;
                        let name: String = row.try_get(1)?;
                        Ok((i64::from(id), Arc::from(name)))
                    })
                    .collect()
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// Drop the cached `instance_map`, for a caller that found a writer
    /// missing from it. A writer added since the cache was filled is only in
    /// the table.
    pub fn forget_instance_map(&self) {
        drop(self.store().caches().instance_map.invalidate(&()));
    }

    /// `get_partial_rooms`: every room the server joined with partial
    /// state, which is never many.
    pub async fn partial_rooms(&self) -> anyhow::Result<Arc<HashSet<Arc<str>>>> {
        self.store()
            .caches()
            .partial_rooms
            .get_or_load((), || async {
                let rows = self
                    .query_one_shot("SELECT room_id FROM partial_state_rooms", &[])
                    .await?;
                let mut rooms = HashSet::with_capacity(rows.len());
                for row in rows {
                    let room_id: String = row.try_get(0)?;
                    rooms.insert(Arc::from(room_id));
                }
                Ok(Arc::new(rooms))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_tags_for_user`: the user's tags, by room.
    pub async fn tags_for_user(&self, user_id: &str) -> anyhow::Result<Arc<UserTags>> {
        self.store()
            .caches()
            .tags_for_user
            .get_or_load(Arc::from(user_id), || async {
                let rows = self
                    .query_one_shot(
                        "SELECT room_id, tag, content FROM room_tags WHERE user_id = $1",
                        &[&user_id],
                    )
                    .await?;
                let mut tags = UserTags::new();
                for row in rows {
                    let room_id: String = row.try_get(0)?;
                    let tag: String = row.try_get(1)?;
                    let content: String = row.try_get(2)?;
                    let content = serde_json::from_str(&content)
                        .with_context(|| format!("tag {tag} of {user_id} in {room_id}"))?;
                    tags.entry(Arc::from(room_id))
                        .or_default()
                        .insert(tag, content);
                }
                Ok(Arc::new(tags))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_is_encrypted_for_room_at_token`: whether the room was encrypted
    /// at `to_token`, from its current state rolled back by the deltas
    /// since. `now_token` is the current room key.
    pub async fn is_encrypted_at(
        &self,
        room_id: &str,
        to_token: &RoomStreamToken,
        now_token: &RoomStreamToken,
    ) -> anyhow::Result<bool> {
        let filter = StateFilter::from_types([("m.room.encryption", Some(""))]);
        let state = self.current_state_ids(room_id, &filter).await?;
        let mut encryption_event_id = state.get(&StateKey::new("m.room.encryption", "")).cloned();

        // The first change after the token holds the state at it.
        let deltas = self
            .state_deltas_by_stream_id(room_id, Some(to_token), Some(now_token))
            .await?;
        if let Some(delta) = deltas
            .iter()
            .find(|delta| &*delta.event_type == "m.room.encryption")
        {
            match &delta.prev_event_id {
                Some(prev_event_id) => encryption_event_id = Some(Arc::clone(prev_event_id)),
                // No encryption event before the change.
                None => return Ok(false),
            }
        }

        let Some(encryption_event_id) = encryption_event_id else {
            return Ok(false);
        };
        let events = self
            .events(
                std::slice::from_ref(&encryption_event_id),
                RedactBehaviour::Redact,
                false,
                false,
            )
            .await?;
        // Python's `get_event` raises for an event it can't find.
        let event = events
            .first()
            .with_context(|| format!("encryption event {encryption_event_id} not found"))?;
        Ok(event
            .content()
            .get_field("algorithm")
            .is_some_and(|algorithm| !algorithm.is_null()))
    }

    // Connection state.

    /// `get_sliding_sync_connection_lazy_members`: which of the users the
    /// connection sent lazy-loaded memberships for in the room, as of
    /// `connection_position`, to when each was last needed.
    pub async fn connection_lazy_members(
        &self,
        connection_position: i64,
        room_id: &str,
        user_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        let mut members = HashMap::new();
        if user_ids.is_empty() {
            return Ok(members);
        }
        let ids: Vec<&str> = user_ids.iter().map(|id| &**id).collect();
        // The position's connection is found through its row, as the caller
        // doesn't have the connection key. Of the two `connection_position`
        // columns, the members' is the one filtered on.
        let rows = self
            .query_one_shot(
                "SELECT user_id, members.connection_position, last_seen_ts
                FROM sliding_sync_connection_lazy_members AS members
                INNER JOIN sliding_sync_connection_positions AS pos USING (connection_key)
                WHERE pos.connection_position = $1 AND room_id = $2 AND user_id = ANY($3)",
                &[&connection_position, &room_id, &ids],
            )
            .await?;
        for row in rows {
            let user_id: String = row.try_get(0)?;
            let member_position: Option<i64> = row.try_get(1)?;
            let last_seen_ts: i64 = row.try_get(2)?;
            // An entry with a position applies to that fork of the
            // connection only. One without applies to every position.
            if member_position.is_none_or(|position| position == connection_position) {
                members.insert(Arc::from(user_id), last_seen_ts);
            }
        }
        Ok(members)
    }

    /// `get_and_clear_connection_positions`: the connection's state at
    /// `connection_position`, or [`UnknownPosition`] if the position is not
    /// one of this user's device's connection. Loading a position commits
    /// the client to it: the connection's other positions are deleted.
    ///
    /// Memoized by its arguments, as Python's `@cached` is, so a long-poll's
    /// second call and a client's retry don't run the transaction again.
    /// `now_ms` is the time to record as the connection's last use.
    pub async fn load_connection_state(
        &self,
        user_id: &str,
        device_id: &str,
        conn_id: &str,
        connection_position: i64,
        now_ms: i64,
    ) -> anyhow::Result<Arc<StoredConnectionState>> {
        let key = ConnectionPositionKey {
            user_id: Arc::from(user_id),
            device_id: Arc::from(device_id),
            conn_id: Arc::from(conn_id),
            connection_position,
        };
        self.store()
            .caches()
            .connection_states
            .get_or_load(key, || async {
                self.retry_transaction(|| {
                    self.load_connection_state_txn(
                        user_id,
                        device_id,
                        conn_id,
                        connection_position,
                        now_ms,
                    )
                    .boxed()
                })
                .await
                .map(Arc::new)
            })
            .await
            .map_err(|e| match e.inner().downcast_ref::<UnknownPosition>() {
                Some(unknown) => anyhow::Error::new(unknown.clone()),
                None => anyhow::Error::new(e),
            })
    }

    /// Run `txn` until it succeeds, fails with an error other than a
    /// serialization failure or a deadlock, or has been tried
    /// [`MAX_TRANSACTION_ATTEMPTS`] times.
    async fn retry_transaction<'s, T>(
        &'s self,
        txn: impl Fn() -> BoxFuture<'s, anyhow::Result<T>>,
    ) -> anyhow::Result<T> {
        let mut attempt = 1;
        loop {
            match txn().await {
                Err(e) if attempt < MAX_TRANSACTION_ATTEMPTS && is_retryable(&e) => {
                    warn!("Retrying a sliding sync transaction, attempt {attempt}: {e:#}");
                    attempt += 1;
                }
                result => return result,
            }
        }
    }

    async fn load_connection_state_txn(
        &self,
        user_id: &str,
        device_id: &str,
        conn_id: &str,
        connection_position: i64,
        now_ms: i64,
    ) -> anyhow::Result<StoredConnectionState> {
        let txn = self.begin_write().await?;

        // The position is the client's to name, so check it is theirs.
        let rows = txn
            .query(
                "SELECT connection_key, last_used_ts
                FROM sliding_sync_connection_positions
                INNER JOIN sliding_sync_connections USING (connection_key)
                WHERE
                    connection_position = $1
                    AND user_id = $2 AND effective_device_id = $3 AND conn_id = $4",
                &[&connection_position, &user_id, &device_id, &conn_id],
            )
            .await?;
        let Some(row) = rows.first() else {
            return Err(UnknownPosition.into());
        };
        let connection_key: i64 = row.try_get(0)?;
        let last_used_ts: Option<i64> = row.try_get(1)?;

        // Not on every use, to spare the writes.
        if last_used_ts.is_none_or(|ts| now_ms - ts > UPDATE_INTERVAL_LAST_USED_TS_MS) {
            txn.query(
                "UPDATE sliding_sync_connections SET last_used_ts = $1 WHERE connection_key = $2",
                &[&now_ms, &connection_key],
            )
            .await?;
        }

        // The client has the position, so the others can go. What follows
        // relies on it being the only one left.
        txn.query(
            "DELETE FROM sliding_sync_connection_positions
            WHERE connection_key = $1 AND connection_position != $2",
            &[&connection_key, &connection_position],
        )
        .await?;

        // With the forks gone, the position's lazy members hold for every
        // position after it.
        txn.query(
            "UPDATE sliding_sync_connection_lazy_members SET connection_position = NULL
            WHERE connection_key = $1 AND connection_position = $2",
            &[&connection_key, &connection_position],
        )
        .await?;

        let rows = txn
            .query(
                "SELECT required_state_id, required_state
                FROM sliding_sync_connection_required_state
                WHERE connection_key = $1",
                &[&connection_key],
            )
            .await?;
        let mut required_states: HashMap<i64, RequiredStateMap> =
            HashMap::with_capacity(rows.len());
        for row in &rows {
            let required_state_id: i64 = row.try_get(0)?;
            let required_state: String = row.try_get(1)?;
            required_states.insert(required_state_id, decode_required_state(&required_state)?);
        }

        let rows = txn
            .query(
                "SELECT room_id, timeline_limit, required_state_id
                FROM sliding_sync_connection_room_configs
                WHERE connection_position = $1",
                &[&connection_position],
            )
            .await?;
        let mut room_configs = HashMap::with_capacity(rows.len());
        let mut used_required_state_ids = HashSet::new();
        for row in &rows {
            let room_id: String = row.try_get(0)?;
            let timeline_limit: i64 = row.try_get(1)?;
            let required_state_id: i64 = row.try_get(2)?;
            let required_state_map = required_states
                .get(&required_state_id)
                .with_context(|| format!("required state {required_state_id} of {room_id}"))?
                .clone();
            used_required_state_ids.insert(required_state_id);
            room_configs.insert(
                room_id,
                RoomSyncConfig {
                    timeline_limit,
                    required_state_map,
                },
            );
        }

        // The required states no room of the one remaining position uses.
        let unused: Vec<i64> = required_states
            .keys()
            .filter(|id| !used_required_state_ids.contains(*id))
            .copied()
            .collect();
        if !unused.is_empty() {
            txn.query(
                "DELETE FROM sliding_sync_connection_required_state
                WHERE connection_key = $1 AND required_state_id = ANY($2)",
                &[&connection_key, &unused],
            )
            .await?;
        }

        let rows = txn
            .query(
                "SELECT stream, room_id, room_status, last_token
                FROM sliding_sync_connection_streams
                WHERE connection_position = $1",
                &[&connection_position],
            )
            .await?;
        let mut state = StoredConnectionState {
            last_used_ts,
            room_configs,
            ..Default::default()
        };
        for row in &rows {
            let stream: String = row.try_get(0)?;
            let room_id: String = row.try_get(1)?;
            let room_status: String = row.try_get(2)?;
            let last_token: Option<String> = row.try_get(3)?;
            let statuses = match stream.as_str() {
                "rooms" => &mut state.rooms,
                "receipts" => &mut state.receipts,
                "account_data" => &mut state.account_data,
                // Later versions may add streams.
                _ => {
                    warn!("Unrecognized sliding sync stream in DB {stream:?}");
                    continue;
                }
            };
            statuses.insert(
                room_id,
                status_from_row(&room_status, last_token)
                    .with_context(|| format!("stream {stream} of connection {connection_key}"))?,
            );
        }

        txn.commit().await?;
        Ok(state)
    }

    /// `persist_per_connection_state`: store the request's `updates` to
    /// the connection's state at `previous_position` as a new position,
    /// and return it. Without a previous position the connection starts
    /// over, dropping what was stored for it. [`UnknownPosition`] if the
    /// previous position is not this user's device's connection's.
    #[allow(clippy::too_many_arguments)]
    pub async fn persist_connection_state(
        &self,
        user_id: &str,
        device_id: &str,
        conn_id: &str,
        previous_position: Option<i64>,
        updates: &ConnectionStateUpdates,
        now_ms: i64,
    ) -> anyhow::Result<i64> {
        self.retry_transaction(|| {
            self.persist_connection_state_txn(
                user_id,
                device_id,
                conn_id,
                previous_position,
                updates,
                now_ms,
            )
            .boxed()
        })
        .await
    }

    async fn persist_connection_state_txn(
        &self,
        user_id: &str,
        device_id: &str,
        conn_id: &str,
        previous_position: Option<i64>,
        updates: &ConnectionStateUpdates,
        now_ms: i64,
    ) -> anyhow::Result<i64> {
        let txn = self.begin_write().await?;

        let connection_key: i64 = match previous_position {
            Some(previous_position) => {
                // Lock the connection up front. Concurrent upserts of its lazy
                // members deadlocked otherwise. The position is the client's
                // to name, so check it is theirs.
                let rows = txn
                    .query(
                        "SELECT connection_key
                        FROM sliding_sync_connection_positions
                        INNER JOIN sliding_sync_connections USING (connection_key)
                        WHERE
                            connection_position = $1
                            AND user_id = $2 AND effective_device_id = $3 AND conn_id = $4
                        FOR NO KEY UPDATE OF sliding_sync_connections",
                        &[&previous_position, &user_id, &device_id, &conn_id],
                    )
                    .await?;
                let Some(row) = rows.first() else {
                    return Err(UnknownPosition.into());
                };
                row.try_get(0)?
            }
            None => {
                // Starting over: drop the connection's old state, which
                // cascades to its positions and their rows, so one-shot
                // requests don't pile up.
                txn.query(
                    "DELETE FROM sliding_sync_connections
                    WHERE user_id = $1 AND effective_device_id = $2 AND conn_id = $3",
                    &[&user_id, &device_id, &conn_id],
                )
                .await?;
                let rows = txn
                    .query(
                        "INSERT INTO sliding_sync_connections
                        (user_id, effective_device_id, conn_id, created_ts, last_used_ts)
                        VALUES ($1, $2, $3, $4, $4)
                        RETURNING connection_key",
                        &[&user_id, &device_id, &conn_id, &now_ms],
                    )
                    .await?;
                rows.first()
                    .context("no connection key returned")?
                    .try_get(0)?
            }
        };

        let rows = txn
            .query(
                "INSERT INTO sliding_sync_connection_positions (connection_key, created_ts)
                VALUES ($1, $2)
                RETURNING connection_position",
                &[&connection_key, &now_ms],
            )
            .await?;
        let connection_position: i64 = rows
            .first()
            .context("no connection position returned")?
            .try_get(0)?;

        // Rooms share required state rows by their JSON, within the
        // connection.
        let mut required_state_to_id: HashMap<String, i64> = HashMap::new();
        if previous_position.is_some() {
            let rows = txn
                .query(
                    "SELECT required_state_id, required_state
                    FROM sliding_sync_connection_required_state
                    WHERE connection_key = $1",
                    &[&connection_key],
                )
                .await?;
            for row in &rows {
                required_state_to_id.insert(row.try_get(1)?, row.try_get(0)?);
            }
        }
        let mut room_to_state_id: HashMap<&str, i64> = HashMap::new();
        let mut unique_required_state: IndexMap<String, Vec<&str>> = IndexMap::new();
        for (room_id, config) in &updates.room_configs {
            let serialized = encode_required_state(&config.required_state_map);
            match required_state_to_id.get(&serialized) {
                Some(id) => {
                    room_to_state_id.insert(room_id, *id);
                }
                None => unique_required_state
                    .entry(serialized)
                    .or_default()
                    .push(room_id),
            }
        }
        for (serialized, room_ids) in &unique_required_state {
            let rows = txn
                .query(
                    "INSERT INTO sliding_sync_connection_required_state
                    (connection_key, required_state)
                    VALUES ($1, $2)
                    RETURNING required_state_id",
                    &[&connection_key, serialized],
                )
                .await?;
            let required_state_id: i64 = rows
                .first()
                .context("no required state ID returned")?
                .try_get(0)?;
            for room_id in room_ids {
                room_to_state_id.insert(room_id, required_state_id);
            }
        }

        // Start from the previous position's rows. The updates overwrite
        // them.
        if let Some(previous_position) = previous_position {
            txn.query(
                "INSERT INTO sliding_sync_connection_streams
                (connection_position, stream, room_id, room_status, last_token)
                SELECT $1, stream, room_id, room_status, last_token
                FROM sliding_sync_connection_streams
                WHERE connection_position = $2",
                &[&connection_position, &previous_position],
            )
            .await?;
            txn.query(
                "INSERT INTO sliding_sync_connection_room_configs
                (connection_position, room_id, timeline_limit, required_state_id)
                SELECT $1, room_id, timeline_limit, required_state_id
                FROM sliding_sync_connection_room_configs
                WHERE connection_position = $2",
                &[&connection_position, &previous_position],
            )
            .await?;
        }

        let mut streams: Vec<&str> = Vec::new();
        let mut stream_rooms: Vec<&str> = Vec::new();
        let mut statuses: Vec<&str> = Vec::new();
        let mut tokens: Vec<Option<&str>> = Vec::new();
        for (stream, updates) in [
            ("rooms", &updates.rooms),
            ("receipts", &updates.receipts),
            ("account_data", &updates.account_data),
        ] {
            for (room_id, status) in updates {
                let (status, token) = status_to_row(status);
                streams.push(stream);
                stream_rooms.push(room_id);
                statuses.push(status);
                tokens.push(token);
            }
        }
        if !streams.is_empty() {
            txn.query(
                "INSERT INTO sliding_sync_connection_streams
                (connection_position, stream, room_id, room_status, last_token)
                SELECT $1, * FROM UNNEST($2::text[], $3::text[], $4::text[], $5::text[])
                ON CONFLICT (connection_position, stream, room_id)
                DO UPDATE SET room_status = EXCLUDED.room_status, last_token = EXCLUDED.last_token",
                &[
                    &connection_position,
                    &streams,
                    &stream_rooms,
                    &statuses,
                    &tokens,
                ],
            )
            .await?;
        }

        let mut config_rooms: Vec<&str> = Vec::with_capacity(updates.room_configs.len());
        let mut timeline_limits: Vec<i64> = Vec::with_capacity(updates.room_configs.len());
        let mut required_state_ids: Vec<i64> = Vec::with_capacity(updates.room_configs.len());
        for (room_id, config) in &updates.room_configs {
            config_rooms.push(room_id);
            timeline_limits.push(config.timeline_limit);
            required_state_ids.push(room_to_state_id[room_id.as_str()]);
        }
        if !config_rooms.is_empty() {
            txn.query(
                "INSERT INTO sliding_sync_connection_room_configs
                (connection_position, room_id, timeline_limit, required_state_id)
                SELECT $1, * FROM UNNEST($2::text[], $3::bigint[], $4::bigint[])
                ON CONFLICT (connection_position, room_id)
                DO UPDATE SET
                    timeline_limit = EXCLUDED.timeline_limit,
                    required_state_id = EXCLUDED.required_state_id",
                &[
                    &connection_position,
                    &config_rooms,
                    &timeline_limits,
                    &required_state_ids,
                ],
            )
            .await?;
        }

        persist_lazy_members(
            &txn,
            connection_key,
            connection_position,
            &updates.room_lazy_membership,
            now_ms,
        )
        .await?;

        txn.commit().await?;
        Ok(connection_position)
    }
}

/// `_persist_sliding_sync_connection_lazy_members_txn`: record the lazy
/// members sent, and forget those whose membership changed unsent.
async fn persist_lazy_members(
    txn: &Borrowed<'_, '_>,
    connection_key: i64,
    connection_position: i64,
    all_changes: &HashMap<String, RoomLazyMembershipChanges>,
    now_ms: i64,
) -> anyhow::Result<()> {
    // New entries, and those whose timestamp is old enough to rewrite.
    let mut rooms: Vec<&str> = Vec::new();
    let mut users: Vec<&str> = Vec::new();
    for (room_id, changes) in all_changes {
        for user_id in changes.get_returned_user_ids_to_update(now_ms) {
            rooms.push(room_id);
            users.push(user_id);
        }
    }
    if !rooms.is_empty() {
        // An entry of another fork of the connection is left alone. This is
        // a cache, so missing a row costs a few extra members down sync.
        txn.query(
            "INSERT INTO sliding_sync_connection_lazy_members
            (connection_key, connection_position, room_id, user_id, last_seen_ts)
            SELECT $1, $2, room_id, user_id, $3
            FROM UNNEST($4::text[], $5::text[]) AS u(room_id, user_id)
            ON CONFLICT (connection_key, room_id, user_id)
            DO UPDATE SET last_seen_ts = EXCLUDED.last_seen_ts
            WHERE sliding_sync_connection_lazy_members.connection_position IS NULL
                OR sliding_sync_connection_lazy_members.connection_position = EXCLUDED.connection_position",
            &[&connection_key, &connection_position, &now_ms, &rooms, &users],
        )
        .await?;
    }

    // Removed from every fork: expiring an entry of a cache is always safe.
    let mut rooms: Vec<&str> = Vec::new();
    let mut users: Vec<&str> = Vec::new();
    for (room_id, changes) in all_changes {
        for user_id in &changes.invalidated_user_ids {
            rooms.push(room_id);
            users.push(user_id);
        }
    }
    if !rooms.is_empty() {
        txn.query(
            "DELETE FROM sliding_sync_connection_lazy_members
            WHERE connection_key = $1
                AND (room_id, user_id) IN (SELECT * FROM UNNEST($2::text[], $3::text[]))",
            &[&connection_key, &rooms, &users],
        )
        .await?;
    }
    Ok(())
}

// Test-only entry points, so trial can compare the Rust store against the
// Python one on a real schema.

/// A [`RoomForUser`] as tests read it: `(room_id, sender, membership,
/// event_id, room_version_id, instance_name, stream, has_known_state,
/// room_type, is_encrypted)`.
type PySnapshotRoom = (
    String,
    Option<String>,
    String,
    Option<String>,
    Option<String>,
    String,
    i64,
    bool,
    Option<String>,
    bool,
);

fn opt_string(value: &Option<Arc<str>>) -> Option<String> {
    value.as_deref().map(str::to_owned)
}

fn snapshot_to_py(room: &RoomForUser) -> PySnapshotRoom {
    (
        room.room_id.to_string(),
        opt_string(&room.sender),
        room.membership.to_string(),
        opt_string(&room.event_id),
        opt_string(&room.room_version_id),
        room.event_pos.instance_name.to_string(),
        room.event_pos.stream,
        room.has_known_state,
        opt_string(&room.room_type),
        room.is_encrypted,
    )
}

fn snapshots_to_py(rooms: &SnapshotRooms) -> HashMap<String, PySnapshotRoom> {
    rooms
        .iter()
        .map(|(room_id, room)| (room_id.to_string(), snapshot_to_py(room)))
        .collect()
}

fn position_to_py(position: &PersistedEventPosition) -> (String, i64) {
    (position.instance_name.to_string(), position.stream)
}

/// A status as tests write it: `(flag, token)`.
type PyStatus = (String, Option<String>);

fn status_to_py(status: &HaveSentRoom<String>) -> PyStatus {
    let (flag, token) = status_to_row(status);
    (flag.to_owned(), token.map(str::to_owned))
}

fn status_from_py((flag, token): PyStatus) -> PyResult<HaveSentRoom<String>> {
    status_from_row(&flag, token).map_err(anyhow_to_py)
}

fn statuses_from_py(
    statuses: HashMap<String, PyStatus>,
) -> PyResult<HashMap<String, HaveSentRoom<String>>> {
    statuses
        .into_iter()
        .map(|(room_id, status)| Ok((room_id, status_from_py(status)?)))
        .collect()
}

fn statuses_to_py(statuses: &HashMap<String, HaveSentRoom<String>>) -> HashMap<String, PyStatus> {
    statuses
        .iter()
        .map(|(room_id, status)| (room_id.clone(), status_to_py(status)))
        .collect()
}

/// A room config as tests write it: `(timeline_limit, {type: [state_key]})`.
type PyRoomConfig = (i64, BTreeMap<String, Vec<String>>);

fn config_to_py(config: &RoomSyncConfig) -> PyRoomConfig {
    (
        config.timeline_limit,
        config
            .required_state_map
            .iter()
            .map(|(event_type, state_keys)| {
                (event_type.clone(), state_keys.iter().cloned().collect())
            })
            .collect(),
    )
}

fn config_from_py((timeline_limit, map): PyRoomConfig) -> RoomSyncConfig {
    RoomSyncConfig {
        timeline_limit,
        required_state_map: map
            .into_iter()
            .map(|(event_type, state_keys)| (event_type, state_keys.into_iter().collect()))
            .collect(),
    }
}

/// A stored connection state as tests read it: `(last_used_ts, rooms,
/// receipts, account_data, room_configs)`.
type PyStoredState = (
    Option<i64>,
    HashMap<String, PyStatus>,
    HashMap<String, PyStatus>,
    HashMap<String, PyStatus>,
    HashMap<String, PyRoomConfig>,
);

/// A room's lazy membership changes as tests write them:
/// `({user_id: last_seen_ts}, invalidated_user_ids)`.
type PyLazyChanges = (HashMap<String, Option<i64>>, HashSet<String>);

/// Raise [`UnknownPosition`] as Python's `SlidingSyncUnknownPosition`.
fn store_error_to_py(py: Python<'_>, e: anyhow::Error) -> PyErr {
    if e.downcast_ref::<UnknownPosition>().is_some() {
        return match py
            .import("synapse.api.errors")
            .and_then(|errors| errors.getattr("SlidingSyncUnknownPosition"))
            .and_then(|class| class.call0())
        {
            Ok(exception) => PyErr::from_value(exception),
            Err(e) => e,
        };
    }
    anyhow_to_py(e)
}

fn arcs(ids: Vec<String>) -> Vec<Arc<str>> {
    ids.into_iter().map(Arc::from).collect()
}

fn arc_keys<V>(map: HashMap<String, V>) -> HashMap<Arc<str>, V> {
    map.into_iter().map(|(k, v)| (Arc::from(k), v)).collect()
}

/// The sliding sync store's queries, each run to completion on the tokio
/// runtime with the GIL released, over the native driver's pool and the
/// Rust sliding sync handler's caches. For tests only.
#[pyclass(name = "SlidingSyncStoreForTest")]
pub struct PySlidingSyncStoreForTest {
    store: Arc<SyncStore>,
    pool: ConnectionPool,
    handle: Handle,
}

impl PySlidingSyncStoreForTest {
    /// Run `f` over a reader for one request, blocking until it is done.
    fn run<'a, T, F>(&'a self, py: Python<'_>, f: F) -> PyResult<T>
    where
        T: Send + 'a,
        F: FnOnce(SlidingSyncReader<'a>) -> BoxFuture<'a, anyhow::Result<T>> + Send,
    {
        let result = py.detach(|| {
            self.handle
                .block_on(f(self.store.sliding_sync_reader(self.pool.clone())))
        });
        result.map_err(|e| store_error_to_py(py, e))
    }
}

#[pymethods]
impl PySlidingSyncStoreForTest {
    #[new]
    fn new(pool: &Bound<'_, PyConnectionPool>, handler: &SlidingSyncHandler) -> Self {
        let pool = pool.get();
        PySlidingSyncStoreForTest {
            store: Arc::clone(handler.store()),
            pool: pool.pool.clone(),
            handle: pool.handle.clone(),
        }
    }

    fn rooms_from_snapshots(
        &self,
        py: Python<'_>,
        user_id: String,
    ) -> PyResult<HashMap<String, PySnapshotRoom>> {
        let rooms = self.run(py, |reader| {
            async move { reader.rooms_from_snapshots(&user_id).await }.boxed()
        })?;
        Ok(snapshots_to_py(&rooms))
    }

    fn self_leave_rooms_after(
        &self,
        py: Python<'_>,
        user_id: String,
        to_token: RoomStreamToken,
    ) -> PyResult<HashMap<String, PySnapshotRoom>> {
        let rooms = self.run(py, |reader| {
            async move { reader.self_leave_rooms_after(&user_id, &to_token).await }.boxed()
        })?;
        Ok(snapshots_to_py(&rooms))
    }

    fn snapshot_room(
        &self,
        py: Python<'_>,
        user_id: String,
        room_id: String,
    ) -> PyResult<Option<PySnapshotRoom>> {
        let room = self.run(py, |reader| {
            async move { reader.snapshot_room(&user_id, &room_id).await }.boxed()
        })?;
        Ok(room.as_ref().map(snapshot_to_py))
    }

    fn snapshot_rooms(
        &self,
        py: Python<'_>,
        user_id: String,
        room_ids: Vec<String>,
    ) -> PyResult<HashMap<String, PySnapshotRoom>> {
        let room_ids = arcs(room_ids);
        let rooms = self.run(py, |reader| {
            async move { reader.snapshot_rooms(&user_id, &room_ids).await }.boxed()
        })?;
        Ok(snapshots_to_py(&rooms))
    }

    /// The changes in order, as `(room_id, sender, membership, event_id,
    /// (instance_name, stream), room_version_id)`.
    #[allow(clippy::type_complexity)]
    fn snapshot_membership_changes(
        &self,
        py: Python<'_>,
        user_id: String,
        from_token: RoomStreamToken,
        to_token: RoomStreamToken,
        excluded_room_ids: Vec<String>,
        membership_may_have_changed: bool,
    ) -> PyResult<
        Vec<(
            String,
            Option<String>,
            String,
            Option<String>,
            (String, i64),
            Option<String>,
        )>,
    > {
        let excluded: HashSet<Arc<str>> = arcs(excluded_room_ids).into_iter().collect();
        let changes = self.run(py, |reader| {
            async move {
                reader
                    .snapshot_membership_changes(
                        &user_id,
                        &from_token,
                        &to_token,
                        &excluded,
                        membership_may_have_changed,
                    )
                    .await
            }
            .boxed()
        })?;
        Ok(changes
            .values()
            .map(|change| {
                (
                    change.room_id.to_string(),
                    opt_string(&change.sender),
                    change.membership.to_string(),
                    opt_string(&change.event_id),
                    position_to_py(&change.event_pos),
                    opt_string(&change.room_version_id),
                )
            })
            .collect())
    }

    /// The changes in order, as `(room_id, event_id, (instance_name,
    /// stream), membership, sender, prev_event_id, prev_event_pos,
    /// prev_membership, prev_sender)`.
    #[allow(clippy::type_complexity)]
    fn membership_delta_changes(
        &self,
        py: Python<'_>,
        user_id: String,
        from_token: RoomStreamToken,
        to_token: RoomStreamToken,
        excluded_room_ids: Vec<String>,
        membership_may_have_changed: bool,
    ) -> PyResult<
        Vec<(
            String,
            Option<String>,
            (String, i64),
            String,
            Option<String>,
            Option<String>,
            Option<(String, i64)>,
            Option<String>,
            Option<String>,
        )>,
    > {
        let excluded: HashSet<Arc<str>> = arcs(excluded_room_ids).into_iter().collect();
        let changes = self.run(py, |reader| {
            async move {
                reader
                    .membership_delta_changes(
                        &user_id,
                        &from_token,
                        &to_token,
                        &excluded,
                        membership_may_have_changed,
                    )
                    .await
            }
            .boxed()
        })?;
        Ok(changes
            .iter()
            .map(|change| {
                (
                    change.room_id.to_string(),
                    opt_string(&change.event_id),
                    position_to_py(&change.event_pos),
                    change.membership.to_string(),
                    opt_string(&change.sender),
                    opt_string(&change.prev_event_id),
                    change.prev_event_pos.as_ref().map(position_to_py),
                    opt_string(&change.prev_membership),
                    opt_string(&change.prev_sender),
                )
            })
            .collect())
    }

    #[pyo3(signature = (room_ids, from_token, changed_since))]
    fn rooms_with_updates_since(
        &self,
        py: Python<'_>,
        room_ids: Vec<String>,
        from_token: RoomStreamToken,
        changed_since: Option<HashSet<String>>,
    ) -> PyResult<HashSet<String>> {
        let room_ids = arcs(room_ids);
        let changed: Option<HashSet<Arc<str>>> =
            changed_since.map(|changed| changed.into_iter().map(Arc::from).collect());
        let rooms = self.run(py, |reader| {
            async move {
                reader
                    .rooms_with_updates_since(&room_ids, &from_token, changed.as_ref())
                    .await
            }
            .boxed()
        })?;
        Ok(rooms.iter().map(|id| id.to_string()).collect())
    }

    fn max_event_pos(
        &self,
        py: Python<'_>,
        room_ids: Vec<String>,
        now_token: RoomStreamToken,
        last_change_pos: HashMap<String, i64>,
    ) -> PyResult<HashMap<String, Option<i64>>> {
        let room_ids = arcs(room_ids);
        let last_change_pos = arc_keys(last_change_pos);
        let positions = self.run(py, |reader| {
            async move {
                reader
                    .max_event_pos(&room_ids, &now_token, &last_change_pos)
                    .await
            }
            .boxed()
        })?;
        Ok(positions
            .into_iter()
            .map(|(room_id, stream)| (room_id.to_string(), stream))
            .collect())
    }

    fn last_event_pos_before(
        &self,
        py: Python<'_>,
        room_ids: Vec<String>,
        end_token: RoomStreamToken,
        now_token: RoomStreamToken,
        last_change_pos: HashMap<String, i64>,
    ) -> PyResult<HashMap<String, i64>> {
        let room_ids = arcs(room_ids);
        let last_change_pos = arc_keys(last_change_pos);
        let positions = self.run(py, |reader| {
            async move {
                reader
                    .last_event_pos_before(&room_ids, &end_token, &now_token, &last_change_pos)
                    .await
            }
            .boxed()
        })?;
        Ok(positions
            .into_iter()
            .map(|(room_id, stream)| (room_id.to_string(), stream))
            .collect())
    }

    fn cached_member_counts(
        &self,
        py: Python<'_>,
        room_id: String,
    ) -> PyResult<HashMap<String, i64>> {
        let counts = self.run(py, |reader| {
            async move { reader.cached_member_counts(&room_id).await }.boxed()
        })?;
        Ok((*counts).clone())
    }

    /// `(known, room_type)`: `known` is false for `ROOM_UNKNOWN_SENTINEL`.
    fn room_type(&self, py: Python<'_>, room_id: String) -> PyResult<(bool, Option<String>)> {
        let room_type = self.run(py, |reader| {
            async move { reader.room_type(&room_id).await }.boxed()
        })?;
        Ok(match room_type {
            RoomType::Unknown => (false, None),
            RoomType::Known(room_type) => (true, opt_string(&room_type)),
        })
    }

    fn room_version_id(&self, py: Python<'_>, room_id: String) -> PyResult<Option<String>> {
        let version = self.run(py, |reader| {
            async move { reader.room_version_id(&room_id).await }.boxed()
        })?;
        Ok(opt_string(&version))
    }

    fn partial_rooms(&self, py: Python<'_>) -> PyResult<HashSet<String>> {
        let rooms = self.run(py, |reader| {
            async move { reader.partial_rooms().await }.boxed()
        })?;
        Ok(rooms.iter().map(|id| id.to_string()).collect())
    }

    fn tags_for_user<'py>(
        &self,
        py: Python<'py>,
        user_id: String,
    ) -> PyResult<HashMap<String, HashMap<String, Bound<'py, PyAny>>>> {
        let tags = self.run(py, |reader| {
            async move { reader.tags_for_user(&user_id).await }.boxed()
        })?;
        tags.iter()
            .map(|(room_id, tags)| {
                let tags = tags
                    .iter()
                    .map(|(tag, content)| {
                        Ok((
                            tag.clone(),
                            crate::storage::sync_store::json_to_py(py, content)?,
                        ))
                    })
                    .collect::<PyResult<_>>()?;
                Ok((room_id.to_string(), tags))
            })
            .collect()
    }

    fn is_encrypted_at(
        &self,
        py: Python<'_>,
        room_id: String,
        to_token: RoomStreamToken,
        now_token: RoomStreamToken,
    ) -> PyResult<bool> {
        self.run(py, |reader| {
            async move {
                reader
                    .is_encrypted_at(&room_id, &to_token, &now_token)
                    .await
            }
            .boxed()
        })
    }

    fn connection_lazy_members(
        &self,
        py: Python<'_>,
        connection_position: i64,
        room_id: String,
        user_ids: Vec<String>,
    ) -> PyResult<HashMap<String, i64>> {
        let user_ids = arcs(user_ids);
        let members = self.run(py, |reader| {
            async move {
                reader
                    .connection_lazy_members(connection_position, &room_id, &user_ids)
                    .await
            }
            .boxed()
        })?;
        Ok(members
            .into_iter()
            .map(|(user_id, ts)| (user_id.to_string(), ts))
            .collect())
    }

    fn load_connection_state(
        &self,
        py: Python<'_>,
        user_id: String,
        device_id: String,
        conn_id: String,
        connection_position: i64,
        now_ms: i64,
    ) -> PyResult<PyStoredState> {
        let state = self.run(py, |reader| {
            async move {
                reader
                    .load_connection_state(
                        &user_id,
                        &device_id,
                        &conn_id,
                        connection_position,
                        now_ms,
                    )
                    .await
            }
            .boxed()
        })?;
        Ok((
            state.last_used_ts,
            statuses_to_py(&state.rooms),
            statuses_to_py(&state.receipts),
            statuses_to_py(&state.account_data),
            state
                .room_configs
                .iter()
                .map(|(room_id, config)| (room_id.clone(), config_to_py(config)))
                .collect(),
        ))
    }

    #[pyo3(signature = (user_id, device_id, conn_id, previous_position, *, rooms, receipts, account_data, room_configs, room_lazy_membership, now_ms))]
    #[allow(clippy::too_many_arguments)]
    fn persist_connection_state(
        &self,
        py: Python<'_>,
        user_id: String,
        device_id: String,
        conn_id: String,
        previous_position: Option<i64>,
        rooms: HashMap<String, PyStatus>,
        receipts: HashMap<String, PyStatus>,
        account_data: HashMap<String, PyStatus>,
        room_configs: HashMap<String, PyRoomConfig>,
        room_lazy_membership: HashMap<String, PyLazyChanges>,
        now_ms: i64,
    ) -> PyResult<i64> {
        let updates = ConnectionStateUpdates {
            rooms: statuses_from_py(rooms)?,
            receipts: statuses_from_py(receipts)?,
            account_data: statuses_from_py(account_data)?,
            room_configs: room_configs
                .into_iter()
                .map(|(room_id, config)| (room_id, config_from_py(config)))
                .collect(),
            room_lazy_membership: room_lazy_membership
                .into_iter()
                .map(|(room_id, (returned, invalidated))| {
                    (
                        room_id,
                        RoomLazyMembershipChanges {
                            returned_user_id_to_last_seen_ts_map: returned,
                            invalidated_user_ids: invalidated,
                        },
                    )
                })
                .collect(),
        };
        self.run(py, |reader| {
            async move {
                reader
                    .persist_connection_state(
                        &user_id,
                        &device_id,
                        &conn_id,
                        previous_position,
                        &updates,
                        now_ms,
                    )
                    .await
            }
            .boxed()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn required_state_round_trips_as_python_encodes_it() {
        let mut map = RequiredStateMap::new();
        map.entry("m.room.member".to_owned())
            .or_default()
            .extend(["$ME".to_owned(), "@é:test".to_owned()]);
        map.entry("m.room.create".to_owned())
            .or_default()
            .insert(String::new());
        let json = encode_required_state(&map);
        // `json.dumps(sorted(...), separators=(",", ":"))`, ASCII only.
        assert_eq!(
            json,
            r#"[["m.room.create",""],["m.room.member","$ME"],["m.room.member","@\u00e9:test"]]"#
        );
        assert_eq!(decode_required_state(&json).unwrap(), map);
        assert_eq!(encode_required_state(&RequiredStateMap::new()), "[]");
    }

    #[test]
    fn statuses_round_trip() {
        for status in [
            HaveSentRoom::Never,
            HaveSentRoom::Live,
            HaveSentRoom::Previously("s5".to_owned()),
        ] {
            let (flag, token) = status_to_row(&status);
            assert_eq!(
                status_from_row(flag, token.map(str::to_owned)).unwrap(),
                status
            );
        }
        assert!(status_from_row("previously", None).is_err());
        assert!(status_from_row("sent", None).is_err());
    }
}
