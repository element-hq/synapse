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

//! What the sliding sync handler reads from the database.
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
    collections::{HashMap, HashSet},
    ops::Deref,
    sync::Arc,
};

use anyhow::Context;
use futures::{future::BoxFuture, FutureExt};
use indexmap::IndexMap;
use pyo3::prelude::*;
use tokio::runtime::Handle;
use tokio_postgres::Row;

use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::room_versions::is_known_room_version;
use crate::sliding_sync::room_lists::RoomForUser;
use crate::sliding_sync::SlidingSyncHandler;
use crate::state::StateKey;
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::{
    anyhow_to_py, Interner, PersistedEventPosition, SyncReader, SyncStore, SELECT_MANY_BATCH_SIZE,
};
use crate::types::state_filter::StateFilter;
use crate::types::stream_token::{filter_results_by_stream, RoomStreamToken};

/// The rooms of a user's membership snapshots, by room ID.
pub type SnapshotRooms = HashMap<Arc<str>, RoomForUser>;

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
    /// `_get_max_event_pos`, which Python fills from the events stream
    /// cache too.
    pub async fn max_event_pos(
        &self,
        room_ids: &[Arc<str>],
        now_token: &RoomStreamToken,
    ) -> anyhow::Result<HashMap<Arc<str>, Option<i64>>> {
        self.store()
            .caches()
            .max_event_pos
            .get_or_load_many(room_ids, |missing| async move {
                let mut results: HashMap<Arc<str>, Option<i64>> = missing
                    .iter()
                    .map(|room_id| (Arc::clone(room_id), None))
                    .collect();
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
    /// that has one. `now_token` is as for [`Self::max_event_pos`].
    pub async fn last_event_pos_before(
        &self,
        room_ids: &[Arc<str>],
        end_token: &RoomStreamToken,
        now_token: &RoomStreamToken,
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        // Most rooms' latest event is before the token, and cached.
        let uncapped = self.max_event_pos(room_ids, now_token).await?;
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

fn arcs(ids: Vec<String>) -> Vec<Arc<str>> {
    ids.into_iter().map(Arc::from).collect()
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
        result.map_err(anyhow_to_py)
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
    ) -> PyResult<HashMap<String, Option<i64>>> {
        let room_ids = arcs(room_ids);
        let positions = self.run(py, |reader| {
            async move { reader.max_event_pos(&room_ids, &now_token).await }.boxed()
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
    ) -> PyResult<HashMap<String, i64>> {
        let room_ids = arcs(room_ids);
        let positions = self.run(py, |reader| {
            async move {
                reader
                    .last_event_pos_before(&room_ids, &end_token, &now_token)
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
}
