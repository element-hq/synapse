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

//! What the sync handler reads from the database, through the native
//! Postgres driver.
//!
//! Each query is a port of the store method it is named after, and
//! `tests/storage/test_sync_store.py` runs both against the same database.
//!
//! [`SyncStore`] is long-lived: it holds the caches and the config the
//! queries need. A [`SyncReader`] is made per request, over the pool as it
//! is at the time, and counts what the request did for metrics.
//!
//! # Connections
//!
//! A reader borrows a connection from the pool for one group of
//! statements and gives it back before awaiting anything else, so a sync
//! worker serving hundreds of requests with ten rooms in flight each does
//! not pin a connection per room. A group of several statements that must
//! see one snapshot, as a Python `runInteraction` does, runs in a
//! `REPEATABLE READ READ ONLY` transaction. A single statement is its own
//! snapshot and runs outside one.

use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use anyhow::Context;
use async_trait::async_trait;
use futures::{future::BoxFuture, FutureExt};
use log::{debug, warn};
use pyo3::{prelude::*, types::PyModule};
use tokio::runtime::Handle;
use tokio_postgres::{types::ToSql, Row};

use crate::caches::lock_ignoring_poison;
use crate::config::SynapseHomeServerConfig;
use crate::database::postgres::pool::{ConnectionPool, PooledConnection, PyConnectionPool};
use crate::events::Event;
use crate::state::{StateKey, StateMap};
use crate::storage::caches::StoreCaches;
use crate::storage::events::{EventLoadStats, EventLoader, Querier, RedactBehaviour};
use crate::types::state_filter::{state_map_to_py, StateFilter};
use crate::types::stream_token::{
    filter_results, filter_results_by_stream, generate_next_token, generate_pagination_bounds,
    persisted_after, Bound2, Direction, RoomStreamToken,
};

/// The users of one membership in a room, as `get_room_summary` returns
/// them: a truncated list of `(user_id, event_id)` in stream order, and
/// the total.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MemberSummary {
    pub members: Vec<(Arc<str>, Arc<str>)>,
    pub count: i64,
}

/// `get_room_summary`: the room's members by membership.
pub type RoomSummary = HashMap<String, MemberSummary>;

/// `RetentionPolicy`: how long events in a room are kept, in milliseconds.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RetentionPolicy {
    pub min_lifetime: Option<i64>,
    pub max_lifetime: Option<i64>,
}

/// The `room_retention` row of a room's current retention event, if any.
pub type RetentionRow = Option<(Option<i64>, Option<i64>)>;

/// A row of `events` as a page of a room's timeline is read: event ID,
/// topological ordering (absent when paginating by stream ordering) and
/// stream ordering. `_EventDictReturn`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventRow {
    pub event_id: Arc<str>,
    pub topological_ordering: Option<i64>,
    pub stream_ordering: i64,
}

/// Which ordering a page of the timeline follows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaginationOrder {
    /// `paginate_room_events_by_stream_ordering`: for incremental syncs.
    Stream,
    /// `paginate_room_events_by_topological_ordering`: for initial syncs
    /// and gappy ones, where the client walks the DAG.
    Topological,
}

/// `PersistedEventPosition`: where an event was persisted, by whom.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PersistedEventPosition {
    pub instance_name: Arc<str>,
    pub stream: i64,
}

/// A change to a room's current state, as `current_state_delta_stream`
/// records it. `StateDelta`, less the room ID.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateDelta {
    pub stream_id: i64,
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
    /// The new event, or `None` when the state was removed.
    pub event_id: Option<Arc<str>>,
    /// The event before, or `None` for new state.
    pub prev_event_id: Option<Arc<str>>,
}

/// `NotifCounts`: a user's unread counts in one timeline of a room.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct NotifCounts {
    pub notify_count: i64,
    pub unread_count: i64,
    pub highlight_count: i64,
}

/// `RoomNotifCounts`: a user's unread counts in a room, for the main
/// timeline and for each thread with any.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoomNotifCounts {
    pub main_timeline: NotifCounts,
    pub threads: HashMap<Arc<str>, NotifCounts>,
}

/// The thread ID of the main timeline in `event_push_actions`.
const MAIN_TIMELINE: &str = "main";

/// The receipt types that mark events read.
const READ_RECEIPT_TYPES: [&str; 2] = ["m.read", "m.read.private"];

/// The parts of the homeserver config the queries apply.
#[derive(Debug, Clone)]
pub struct SyncStoreConfig {
    pub retention_enabled: bool,
    pub retention_default_min_lifetime: Option<i64>,
    pub retention_default_max_lifetime: Option<i64>,
}

impl SyncStoreConfig {
    pub fn from_config(config: &SynapseHomeServerConfig) -> Self {
        SyncStoreConfig {
            retention_enabled: config.retention.retention_enabled,
            retention_default_min_lifetime: config.retention.retention_default_min_lifetime,
            retention_default_max_lifetime: config.retention.retention_default_max_lifetime,
        }
    }
}

/// `(event_id, sender)` of an event related to another.
pub type RelatedEvent = (Arc<str>, Arc<str>);

/// The sync handler's store: the caches and config its queries need.
pub struct SyncStore {
    caches: Arc<StoreCaches>,
    config: SyncStoreConfig,
}

impl SyncStore {
    pub fn new(caches: Arc<StoreCaches>, config: SyncStoreConfig) -> Self {
        SyncStore { caches, config }
    }

    pub fn caches(&self) -> &Arc<StoreCaches> {
        &self.caches
    }

    /// A reader for one request, over `pool`.
    pub fn reader(&self, pool: ConnectionPool) -> SyncReader<'_> {
        SyncReader {
            store: self,
            pool,
            stats: Mutex::new(SyncDbStats::default()),
        }
    }
}

/// What a [`SyncReader`] did, for metrics.
#[derive(Debug, Default, Clone, Copy)]
pub struct SyncDbStats {
    /// Connections borrowed from the pool.
    pub checkouts: u64,
    /// Time spent waiting for a connection.
    pub checkout_time: Duration,
    /// Transactions begun.
    pub transactions: u64,
    /// Statements executed.
    pub queries: u64,
    /// Rows returned.
    pub rows: u64,
    /// Time spent waiting on statements.
    pub db_time: Duration,
    pub events: EventLoadStats,
    /// State groups loaded from the database.
    pub state_groups_fetched: u64,
    /// State groups found in the state group cache.
    pub state_groups_cached: u64,
}

/// The queries of one request. See the module docs.
pub struct SyncReader<'a> {
    store: &'a SyncStore,
    pool: ConnectionPool,
    stats: Mutex<SyncDbStats>,
}

/// A connection borrowed from the pool for one group of statements, and
/// returned when dropped. See the module docs.
///
/// Inside a transaction, dropping it without [`Borrowed::commit`] rolls the
/// transaction back on a background task before the connection goes back
/// to the pool, or closes the connection if there is no runtime to do that
/// on.
pub struct Borrowed<'r, 'a> {
    reader: &'r SyncReader<'a>,
    conn: Option<PooledConnection>,
    in_transaction: bool,
}

impl<'a> SyncReader<'a> {
    pub fn store(&self) -> &'a SyncStore {
        self.store
    }

    pub fn stats(&self) -> SyncDbStats {
        *lock_ignoring_poison(&self.stats)
    }

    pub(crate) fn update_stats(&self, f: impl FnOnce(&mut SyncDbStats)) {
        f(&mut lock_ignoring_poison(&self.stats));
    }

    /// Borrow a connection for a group of statements that need no common
    /// snapshot.
    pub(crate) async fn borrow(&self) -> anyhow::Result<Borrowed<'_, 'a>> {
        let start = Instant::now();
        let conn = self
            .pool
            .get()
            .await
            .map_err(|e| anyhow::anyhow!("checking out a database connection: {e}"))?;
        self.update_stats(|stats| {
            stats.checkouts += 1;
            stats.checkout_time += start.elapsed();
        });
        Ok(Borrowed {
            reader: self,
            conn: Some(conn),
            in_transaction: false,
        })
    }

    /// Borrow a connection and open a `REPEATABLE READ READ ONLY`
    /// transaction on it, for a group of statements that must see one
    /// snapshot. Such a transaction cannot fail with a serialization error
    /// or a deadlock, so there is no retry loop. Finish with
    /// [`Borrowed::commit`].
    pub(crate) async fn begin(&self) -> anyhow::Result<Borrowed<'_, 'a>> {
        let mut borrowed = self.borrow().await?;
        borrowed
            .execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        borrowed.in_transaction = true;
        self.update_stats(|stats| stats.transactions += 1);
        Ok(borrowed)
    }

    /// Borrow a connection and open a `REPEATABLE READ` transaction on it
    /// that may write, as a Python `runInteraction` does. Unlike
    /// [`SyncReader::begin`]'s, such a transaction can fail with a
    /// serialization error or a deadlock, which the caller retries. Finish
    /// with [`Borrowed::commit`].
    pub(crate) async fn begin_write(&self) -> anyhow::Result<Borrowed<'_, 'a>> {
        let mut borrowed = self.borrow().await?;
        // Set before awaiting: if we are dropped after BEGIN is sent but
        // before it returns, the transaction is open and must be rolled back.
        // A ROLLBACK outside one only warns.
        borrowed.in_transaction = true;
        borrowed
            .execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            .await?;
        self.update_stats(|stats| stats.transactions += 1);
        Ok(borrowed)
    }

    /// Run one statement on a connection borrowed for it.
    pub(crate) async fn query_one_shot(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> anyhow::Result<Vec<Row>> {
        self.borrow().await?.query(sql, params).await
    }

    // The queries, grouped as in `storage/sync_store.rs`'s tests.

    /// `get_rooms_for_user`: the rooms the user is joined to.
    pub async fn rooms_for_user(&self, user_id: &str) -> anyhow::Result<Arc<HashSet<Arc<str>>>> {
        let key: Arc<str> = Arc::from(user_id);
        self.store
            .caches
            .rooms_for_user
            .get_or_load(Arc::clone(&key), || async {
                let rows = self
                    .query_one_shot(
                        "SELECT room_id FROM current_state_events
                        WHERE type = 'm.room.member' AND membership = 'join' AND state_key = $1",
                        &[&&*key],
                    )
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

    /// `check_if_events_in_current_state`: which of the events are in the
    /// current state of their room.
    pub async fn events_in_current_state(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        let mut found = HashSet::new();
        if event_ids.is_empty() {
            return Ok(found);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT event_id FROM current_state_events WHERE event_id = ANY($1)",
                    &[&ids],
                )
                .await?;
            for row in rows {
                let event_id: String = row.try_get(0)?;
                found.insert(Arc::from(event_id));
            }
        }
        Ok(found)
    }

    /// `events_have_relations`: which of `event_ids` have a relation from
    /// one of `senders`, of one of `rel_types`. With neither given, all of
    /// them.
    pub async fn events_have_relations(
        &self,
        event_ids: &[&str],
        senders: Option<&[String]>,
        rel_types: Option<&[String]>,
    ) -> anyhow::Result<HashSet<String>> {
        let senders = senders.filter(|s| !s.is_empty());
        let rel_types = rel_types.filter(|t| !t.is_empty());
        if senders.is_none() && rel_types.is_none() {
            return Ok(event_ids.iter().map(|id| (*id).to_owned()).collect());
        }
        let mut found = HashSet::new();
        if event_ids.is_empty() {
            return Ok(found);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let rows = borrowed
                .query(
                    "SELECT relates_to_id FROM event_relations
                    INNER JOIN events USING (event_id)
                    WHERE relates_to_id = ANY($1)
                        AND ($2::text[] IS NULL OR sender = ANY($2))
                        AND ($3::text[] IS NULL OR relation_type = ANY($3))",
                    &[&batch, &senders, &rel_types],
                )
                .await?;
            for row in rows {
                found.insert(row.try_get::<_, String>(0)?);
            }
        }
        Ok(found)
    }

    /// `get_references_for_events`: the `(event_id, sender)` of each event
    /// that references one of `event_ids`, oldest first.
    pub async fn references_for_events(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, Vec<RelatedEvent>>> {
        let mut references: HashMap<Arc<str>, Vec<RelatedEvent>> = HashMap::new();
        if event_ids.is_empty() {
            return Ok(references);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT relates_to_id, ref.event_id, ref.sender
                    FROM events AS ref
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS parent ON
                        parent.event_id = relates_to_id
                        AND parent.room_id = ref.room_id
                    WHERE relates_to_id = ANY($1) AND relation_type = 'm.reference'
                    ORDER BY ref.topological_ordering, ref.stream_ordering",
                    &[&ids],
                )
                .await?;
            for row in rows {
                let parent: &str = row.try_get(0)?;
                references.entry(Arc::from(parent)).or_default().push((
                    Arc::from(row.try_get::<_, &str>(1)?),
                    Arc::from(row.try_get::<_, &str>(2)?),
                ));
            }
        }
        Ok(references)
    }

    /// The IDs of `get_applicable_edits`: the latest edit of each of
    /// `event_ids` with the same type and sender, if it has one.
    pub async fn applicable_edit_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, Arc<str>>> {
        let mut edits = HashMap::new();
        if event_ids.is_empty() {
            return Ok(edits);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            // `DISTINCT ON` picks the first row of each original, so the
            // ordering picks the latest edit.
            let rows = borrowed
                .query(
                    "SELECT DISTINCT ON (original.event_id) original.event_id, edit.event_id
                    FROM events AS edit
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS original ON
                        original.event_id = relates_to_id
                        AND edit.type = original.type
                        AND edit.sender = original.sender
                        AND edit.room_id = original.room_id
                    WHERE relates_to_id = ANY($1) AND relation_type = 'm.replace'
                    ORDER BY original.event_id DESC, edit.origin_server_ts DESC, edit.event_id DESC",
                    &[&ids],
                )
                .await?;
            for row in rows {
                edits.insert(
                    Arc::from(row.try_get::<_, &str>(0)?),
                    Arc::from(row.try_get::<_, &str>(1)?),
                );
            }
        }
        Ok(edits)
    }

    /// The IDs and counts of `get_thread_summaries`: for each of
    /// `event_ids` with threaded replies, the latest reply and how many
    /// there are.
    pub async fn thread_summary_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, (Arc<str>, i64)>> {
        let mut summaries = HashMap::new();
        if event_ids.is_empty() {
            return Ok(summaries);
        }
        // One snapshot, as Python's one transaction, so that a count never
        // misses its latest reply.
        let borrowed = self.begin().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT DISTINCT ON (parent.event_id) parent.event_id, child.event_id
                    FROM events AS child
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS parent ON
                        parent.event_id = relates_to_id
                        AND parent.room_id = child.room_id
                    WHERE relates_to_id = ANY($1) AND relation_type = 'm.thread'
                    ORDER BY parent.event_id, child.topological_ordering DESC,
                        child.stream_ordering DESC",
                    &[&ids],
                )
                .await?;
            let mut latest: HashMap<Arc<str>, Arc<str>> = HashMap::new();
            for row in rows {
                latest.insert(
                    Arc::from(row.try_get::<_, &str>(0)?),
                    Arc::from(row.try_get::<_, &str>(1)?),
                );
            }
            if latest.is_empty() {
                continue;
            }
            let parents: Vec<&str> = latest.keys().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT parent.event_id, COUNT(child.event_id)
                    FROM events AS child
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS parent ON
                        parent.event_id = relates_to_id
                        AND parent.room_id = child.room_id
                    WHERE relates_to_id = ANY($1) AND relation_type = 'm.thread'
                    GROUP BY parent.event_id",
                    &[&parents],
                )
                .await?;
            let mut counts: HashMap<&str, i64> = HashMap::new();
            for row in &rows {
                counts.insert(row.try_get(0)?, row.try_get(1)?);
            }
            for (parent, latest_id) in &latest {
                let count = counts.get(&**parent).copied().unwrap_or(0);
                summaries.insert(Arc::clone(parent), (Arc::clone(latest_id), count));
            }
        }
        borrowed.commit().await?;
        Ok(summaries)
    }

    /// `get_threads_participated`: which of the threads of `event_ids` the
    /// user has replied in.
    pub async fn threads_participated(
        &self,
        event_ids: &[Arc<str>],
        user_id: &str,
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        let mut participated = HashSet::new();
        if event_ids.is_empty() {
            return Ok(participated);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT DISTINCT relates_to_id
                    FROM events AS child
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS parent ON
                        parent.event_id = relates_to_id
                        AND parent.room_id = child.room_id
                    WHERE relates_to_id = ANY($1) AND relation_type = 'm.thread'
                        AND child.sender = $2",
                    &[&ids, &user_id],
                )
                .await?;
            for row in rows {
                participated.insert(Arc::from(row.try_get::<_, &str>(0)?));
            }
        }
        Ok(participated)
    }

    /// `get_threaded_messages_per_user`: how many replies each of `users`
    /// sent to each thread of `event_ids`.
    pub async fn threaded_messages_per_user(
        &self,
        event_ids: &[Arc<str>],
        users: &[&str],
    ) -> anyhow::Result<HashMap<(Arc<str>, Arc<str>), i64>> {
        let mut counts = HashMap::new();
        if event_ids.is_empty() || users.is_empty() {
            return Ok(counts);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT parent.event_id, child.sender, COUNT(child.event_id)
                    FROM events AS child
                    INNER JOIN event_relations USING (event_id)
                    INNER JOIN events AS parent ON
                        parent.event_id = relates_to_id
                        AND parent.room_id = child.room_id
                    WHERE relation_type = 'm.thread' AND child.sender = ANY($1)
                        AND relates_to_id = ANY($2)
                    GROUP BY parent.event_id, child.sender",
                    &[&users, &ids],
                )
                .await?;
            for row in rows {
                counts.insert(
                    (
                        Arc::from(row.try_get::<_, &str>(0)?),
                        Arc::from(row.try_get::<_, &str>(1)?),
                    ),
                    row.try_get(2)?,
                );
            }
        }
        Ok(counts)
    }

    /// `get_timeline_gaps`: a token marking the last gap in the room's
    /// timeline between the tokens, if there is one.
    pub async fn timeline_gap(
        &self,
        room_id: &str,
        from_token: Option<&RoomStreamToken>,
        to_token: &RoomStreamToken,
    ) -> anyhow::Result<Option<RoomStreamToken>> {
        let rows = self
            .query_one_shot(
                "SELECT instance_name, stream_ordering FROM timeline_gaps
                WHERE room_id = $1 AND $2 < stream_ordering AND stream_ordering <= $3
                ORDER BY stream_ordering",
                &[
                    &room_id,
                    &from_token.map_or(0, |token| token.stream),
                    &to_token.get_max_stream_pos(),
                ],
            )
            .await?;

        let mut last_position = None;
        for row in rows {
            let instance_name: String = row.try_get(0)?;
            let stream: i64 = row.try_get(1)?;
            if let Some(from_token) = from_token {
                if !persisted_after(&instance_name, stream, from_token) {
                    continue;
                }
            }
            if persisted_after(&instance_name, stream, to_token) {
                continue;
            }
            last_position = Some(stream);
        }

        // The gap is before the event at the position, so the token points
        // at the event before it, which includes the event.
        Ok(last_position.map(|stream| RoomStreamToken::live(stream - 1)))
    }

    /// `is_partial_state_room_batched`: whether each room's state may be
    /// incomplete, having been joined with faster joins.
    pub async fn partial_state_rooms(
        &self,
        room_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, bool>> {
        let cache = &self.store.caches.partial_state_rooms;
        let mut result = HashMap::with_capacity(room_ids.len());
        let mut missing: Vec<&str> = Vec::new();
        for room_id in room_ids {
            match cache.get(room_id) {
                Some(partial) => {
                    result.insert(Arc::clone(room_id), partial);
                }
                None => missing.push(room_id),
            }
        }
        if missing.is_empty() {
            return Ok(result);
        }

        let mut partial_rooms: HashSet<String> = HashSet::new();
        let borrowed = self.borrow().await?;
        for batch in missing.chunks(SELECT_MANY_BATCH_SIZE) {
            let rows = borrowed
                .query(
                    "SELECT room_id FROM partial_state_rooms WHERE room_id = ANY($1)",
                    &[&batch],
                )
                .await?;
            for row in rows {
                partial_rooms.insert(row.try_get(0)?);
            }
        }
        for room_id in missing {
            let partial = partial_rooms.contains(room_id);
            let room_id: Arc<str> = Arc::from(room_id);
            drop(cache.insert(Arc::clone(&room_id), partial));
            result.insert(room_id, partial);
        }
        Ok(result)
    }

    /// `get_partial_state_events`, less the cache: which of the events
    /// have only partial state, from a faster join.
    pub async fn partial_state_events(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        let mut partial = HashSet::new();
        if event_ids.is_empty() {
            return Ok(partial);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let batch: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT event_id FROM partial_state_events WHERE event_id = ANY($1)",
                    &[&batch],
                )
                .await?;
            for row in rows {
                let event_id: String = row.try_get(0)?;
                partial.insert(Arc::from(event_id));
            }
        }
        Ok(partial)
    }

    /// `is_partial_state_room`.
    pub async fn is_partial_state_room(&self, room_id: &str) -> anyhow::Result<bool> {
        let rooms = self.partial_state_rooms(&[Arc::from(room_id)]).await?;
        Ok(rooms.values().next().copied().unwrap_or(false))
    }

    /// `get_room_summary`: the count of members by membership and the
    /// first six joined or invited members (or failing those, the first
    /// six who left or were banned), for the room summary's heroes.
    pub async fn room_summary(&self, room_id: &str) -> anyhow::Result<Arc<RoomSummary>> {
        let key: Arc<str> = Arc::from(room_id);
        self.store
            .caches
            .room_summary
            .get_or_load(Arc::clone(&key), || async {
                let txn = self.begin().await?;
                let rows = txn
                    .query(
                        "SELECT count(*), membership FROM current_state_events
                        WHERE type = 'm.room.member' AND room_id = $1
                            AND membership IS NOT NULL
                        GROUP BY membership",
                        &[&&*key],
                    )
                    .await?;
                let mut summary = RoomSummary::new();
                for row in rows {
                    let count: i64 = row.try_get(0)?;
                    let membership: String = row.try_get(1)?;
                    summary.insert(
                        membership,
                        MemberSummary {
                            members: Vec::new(),
                            count,
                        },
                    );
                }

                // Joins, then invites, then leaves, then the rest, each in
                // stream order, so the heroes are stable. Six is five heroes
                // plus one in case one of them is the calling user.
                let rows = txn
                    .query(
                        "SELECT state_key, membership, event_id
                        FROM current_state_events
                        WHERE type = 'm.room.member' AND room_id = $1
                            AND membership IS NOT NULL
                        ORDER BY
                            CASE membership
                                WHEN 'join' THEN 1 WHEN 'invite' THEN 2 WHEN 'leave' THEN 3
                                ELSE 4
                            END ASC,
                            event_stream_ordering ASC
                        LIMIT 6",
                        &[&&*key],
                    )
                    .await?;
                for row in rows {
                    let user_id: String = row.try_get(0)?;
                    let membership: String = row.try_get(1)?;
                    let event_id: String = row.try_get(2)?;
                    summary
                        .entry(membership)
                        .or_default()
                        .members
                        .push((Arc::from(user_id), Arc::from(event_id)));
                }
                txn.commit().await?;
                Ok(Arc::new(summary))
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `get_tags_for_room`: the user's tags on the room, by tag.
    pub async fn tags_for_room(
        &self,
        user_id: &str,
        room_id: &str,
    ) -> anyhow::Result<HashMap<String, serde_json::Value>> {
        let rows = self
            .query_one_shot(
                "SELECT tag, content FROM room_tags WHERE user_id = $1 AND room_id = $2",
                &[&user_id, &room_id],
            )
            .await?;
        let mut tags = HashMap::with_capacity(rows.len());
        for row in rows {
            let tag: String = row.try_get(0)?;
            let content: String = row.try_get(1)?;
            let content = serde_json::from_str(&content)
                .with_context(|| format!("tag {tag} of {user_id} in {room_id}"))?;
            tags.insert(tag, content);
        }
        Ok(tags)
    }

    /// `get_global_account_data_by_type_for_user`: the user's account data
    /// of the type, if any, through its mirror.
    pub async fn global_account_data_by_type(
        &self,
        user_id: &str,
        data_type: &str,
    ) -> anyhow::Result<Option<Arc<serde_json::Value>>> {
        self.store
            .caches
            .global_account_data
            .get_or_load((Arc::from(user_id), Arc::from(data_type)), || async {
                let rows = self
                    .query_one_shot(
                        "SELECT content FROM account_data
                        WHERE user_id = $1 AND account_data_type = $2",
                        &[&user_id, &data_type],
                    )
                    .await?;
                let Some(row) = rows.first() else {
                    return Ok(None);
                };
                let content: Option<String> = row.try_get(0)?;
                match content.filter(|content| !content.is_empty()) {
                    Some(content) => Ok(Some(Arc::new(
                        serde_json::from_str(&content)
                            .with_context(|| format!("account data {data_type} of {user_id}"))?,
                    ))),
                    None => Ok(None),
                }
            })
            .await
            .map_err(anyhow::Error::new)
    }

    /// `ignored_users`: the users the user ignores.
    pub async fn ignored_users(&self, user_id: &str) -> anyhow::Result<HashSet<Arc<str>>> {
        let rows = self
            .query_one_shot(
                "SELECT ignored_user_id FROM ignored_users WHERE ignorer_user_id = $1",
                &[&user_id],
            )
            .await?;
        let mut users = HashSet::with_capacity(rows.len());
        for row in rows {
            let user_id: String = row.try_get(0)?;
            users.insert(Arc::from(user_id));
        }
        Ok(users)
    }

    /// `are_users_erased`: which of the users have asked for their events
    /// to be erased.
    pub async fn are_users_erased(
        &self,
        user_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, bool>> {
        let cache = &self.store.caches.erased_users;
        let mut result = HashMap::with_capacity(user_ids.len());
        let mut missing: Vec<&str> = Vec::new();
        for user_id in user_ids {
            match cache.get(user_id) {
                Some(erased) => {
                    result.insert(Arc::clone(user_id), erased);
                }
                None => missing.push(user_id),
            }
        }
        if missing.is_empty() {
            return Ok(result);
        }

        let mut erased_users: HashSet<String> = HashSet::new();
        let borrowed = self.borrow().await?;
        for batch in missing.chunks(SELECT_MANY_BATCH_SIZE) {
            let rows = borrowed
                .query(
                    "SELECT user_id FROM erased_users WHERE user_id = ANY($1)",
                    &[&batch],
                )
                .await?;
            for row in rows {
                erased_users.insert(row.try_get(0)?);
            }
        }
        for user_id in missing {
            let erased = erased_users.contains(user_id);
            let user_id: Arc<str> = Arc::from(user_id);
            drop(cache.insert(Arc::clone(&user_id), erased));
            result.insert(user_id, erased);
        }
        Ok(result)
    }

    /// `get_retention_policy_for_room`: the room's retention policy, with
    /// the configured defaults filled in. With retention disabled, a policy
    /// with no limits, so that nothing is filtered out on the way to the
    /// client.
    pub async fn retention_policy(&self, room_id: &str) -> anyhow::Result<RetentionPolicy> {
        let config = &self.store.config;
        if !config.retention_enabled {
            return Ok(RetentionPolicy::default());
        }
        let key: Arc<str> = Arc::from(room_id);
        let row = self
            .store
            .caches
            .retention_policies
            .get_or_load(Arc::clone(&key), || async {
                let rows = self
                    .query_one_shot(
                        "SELECT min_lifetime, max_lifetime FROM room_retention
                        INNER JOIN current_state_events USING (event_id, room_id)
                        WHERE room_id = $1",
                        &[&&*key],
                    )
                    .await?;
                let row: RetentionRow = match rows.first() {
                    Some(row) => Some((row.try_get(0)?, row.try_get(1)?)),
                    None => None,
                };
                Ok(row)
            })
            .await
            .map_err(anyhow::Error::new)?;
        let defaults = RetentionPolicy {
            min_lifetime: config.retention_default_min_lifetime,
            max_lifetime: config.retention_default_max_lifetime,
        };
        // An unknown room has the default policy, and so does each
        // attribute the room's policy leaves out.
        Ok(match row {
            None => defaults,
            Some((min_lifetime, max_lifetime)) => RetentionPolicy {
                min_lifetime: min_lifetime.or(defaults.min_lifetime),
                max_lifetime: max_lifetime.or(defaults.max_lifetime),
            },
        })
    }

    // Events and the timeline.

    /// `get_events_as_list`: see [`EventLoader::get_events`].
    pub async fn events(
        &self,
        event_ids: &[Arc<str>],
        behaviour: RedactBehaviour,
        allow_rejected: bool,
        prev_content: bool,
    ) -> anyhow::Result<Vec<Event>> {
        if event_ids.is_empty() {
            return Ok(Vec::new());
        }
        let borrowed = self.borrow().await?;
        self.load_events(
            &borrowed,
            event_ids,
            behaviour,
            allow_rejected,
            prev_content,
        )
        .await
    }

    /// [`SyncReader::events`] on a connection already borrowed.
    pub(crate) async fn load_events(
        &self,
        borrowed: &Borrowed<'_, '_>,
        event_ids: &[Arc<str>],
        behaviour: RedactBehaviour,
        allow_rejected: bool,
        prev_content: bool,
    ) -> anyhow::Result<Vec<Event>> {
        let mut load_stats = EventLoadStats::default();
        let events = EventLoader::new(borrowed, Some(&self.store.caches.events))
            .get_events(
                event_ids,
                behaviour,
                allow_rejected,
                prev_content,
                &mut load_stats,
            )
            .await?;
        self.update_stats(|stats| {
            stats.events.fetched += load_stats.fetched;
            stats.events.cached += load_stats.cached;
            stats.events.parse_time += load_stats.parse_time;
        });
        Ok(events)
    }

    /// `paginate_room_events_by_stream_ordering` and
    /// `paginate_room_events_by_topological_ordering`, without an event
    /// filter, which the sync handler never passes: the page of up to
    /// `limit` events from `from_token` in `direction` up to `to_token`,
    /// with the previous content of state events, the token of the end of
    /// the page, and whether the page was cut short by `limit`.
    pub async fn paginate_room_events(
        &self,
        room_id: &str,
        from_token: &RoomStreamToken,
        to_token: Option<&RoomStreamToken>,
        direction: Direction,
        limit: i64,
        order: PaginationOrder,
    ) -> anyhow::Result<(Vec<Event>, RoomStreamToken, bool)> {
        let borrowed = self.borrow().await?;
        let (rows, next_token, limited) = self
            .paginate_rows(
                &borrowed, room_id, from_token, to_token, direction, limit, order,
            )
            .await?;
        let ids: Vec<Arc<str>> = rows.into_iter().map(|row| row.event_id).collect();
        let events = self
            .load_events(&borrowed, &ids, RedactBehaviour::Redact, false, true)
            .await?;
        Ok((events, next_token, limited))
    }

    /// The query of [`SyncReader::paginate_room_events`]: the rows of the
    /// page, in the order of the page.
    #[allow(clippy::too_many_arguments)]
    async fn paginate_rows(
        &self,
        borrowed: &Borrowed<'_, '_>,
        room_id: &str,
        from_token: &RoomStreamToken,
        to_token: Option<&RoomStreamToken>,
        direction: Direction,
        limit: i64,
        order: PaginationOrder,
    ) -> anyhow::Result<(Vec<EventRow>, RoomStreamToken, bool)> {
        // Without rows, the token is the end of the range asked for.
        let no_rows = || {
            (
                Vec::new(),
                to_token.cloned().unwrap_or_else(|| from_token.clone()),
                false,
            )
        };
        // Nothing to page over once the range is empty.
        let empty_range = match direction {
            Direction::Forwards => to_token.is_some_and(|to| to.is_before_or_eq(from_token)),
            Direction::Backwards => to_token.is_some_and(|to| from_token.is_before_or_eq(to)),
        };
        if empty_range {
            return Ok(no_rows());
        }
        if order == PaginationOrder::Stream
            && (from_token.is_historic() || to_token.is_some_and(|to| to.is_historic()))
        {
            anyhow::bail!("paginating by stream ordering needs live tokens");
        }

        let (sort, from_bound, to_bound) =
            generate_pagination_bounds(direction, Some(from_token), to_token);
        let columns = match order {
            PaginationOrder::Stream => ("", "stream_ordering"),
            PaginationOrder::Topological => ("topological_ordering", "stream_ordering"),
        };
        // The bounds as parameters rather than inline, so that the statement
        // text, and its plan, is shared between calls.
        let mut bound_params: Vec<i64> = Vec::new();
        let mut clauses = Vec::new();
        if let Some(from) = from_bound {
            let bound = match direction {
                Direction::Backwards => ">=",
                Direction::Forwards => "<",
            };
            clauses.push(bound_clause(bound, columns, from, &mut bound_params));
        }
        if let Some(to) = to_bound {
            let bound = match direction {
                Direction::Backwards => "<",
                Direction::Forwards => ">=",
            };
            clauses.push(bound_clause(bound, columns, to, &mut bound_params));
        }
        let bounds = clauses.join(" AND ");
        let order_by = match order {
            PaginationOrder::Stream => format!("stream_ordering {sort}"),
            PaginationOrder::Topological => {
                format!("topological_ordering {sort}, stream_ordering {sort}")
            }
        };
        // Twice the limit, as the rows are filtered below.
        let requested_limit = 2 * limit;
        let sql = format!(
            "SELECT event_id, instance_name, topological_ordering, stream_ordering
            FROM events
            WHERE room_id = $1 AND NOT outlier AND {bounds}
            ORDER BY {order_by} LIMIT ${}",
            bound_params.len() + 2
        );
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&room_id];
        params.extend(
            bound_params
                .iter()
                .map(|value| value as &(dyn ToSql + Sync)),
        );
        params.push(&requested_limit);
        let fetched = borrowed.query(&sql, &params).await?;

        let mut limited = fetched.len() as i64 >= requested_limit;
        let (lower, upper) = match direction {
            Direction::Backwards => (to_token, Some(from_token)),
            Direction::Forwards => (Some(from_token), to_token),
        };
        let mut rows = Vec::with_capacity(fetched.len());
        for row in fetched {
            let event_id: String = row.try_get(0)?;
            let instance_name: Option<String> = row.try_get(1)?;
            let topological_ordering: i64 = row.try_get(2)?;
            let stream_ordering: i64 = row.try_get(3)?;
            let keep = match order {
                PaginationOrder::Stream => filter_results_by_stream(
                    lower,
                    upper,
                    instance_name.as_deref(),
                    stream_ordering,
                )?,
                PaginationOrder::Topological => filter_results(
                    lower,
                    upper,
                    instance_name.as_deref(),
                    topological_ordering,
                    stream_ordering,
                ),
            };
            if keep {
                rows.push(EventRow {
                    event_id: Arc::from(event_id),
                    topological_ordering: match order {
                        PaginationOrder::Stream => None,
                        PaginationOrder::Topological => Some(topological_ordering),
                    },
                    stream_ordering,
                });
            }
        }
        if rows.len() as i64 > limit {
            limited = true;
        }
        rows.truncate(limit.max(0) as usize);

        let next_token = match rows.last() {
            Some(last) => {
                generate_next_token(direction, last.topological_ordering, last.stream_ordering)
            }
            None => to_token.cloned().unwrap_or_else(|| from_token.clone()),
        };
        Ok((rows, next_token, limited))
    }

    /// `get_recent_event_ids_for_room`: the last `limit` events of the room
    /// at `end_token` in topological order, oldest first, and the token
    /// before them.
    pub async fn recent_event_ids(
        &self,
        room_id: &str,
        limit: i64,
        end_token: &RoomStreamToken,
    ) -> anyhow::Result<(Vec<EventRow>, RoomStreamToken)> {
        if limit == 0 {
            return Ok((Vec::new(), end_token.clone()));
        }
        let borrowed = self.borrow().await?;
        let (mut rows, token, _) = self
            .paginate_rows(
                &borrowed,
                room_id,
                end_token,
                None,
                Direction::Backwards,
                limit,
                PaginationOrder::Topological,
            )
            .await?;
        rows.reverse();
        Ok((rows, token))
    }

    /// `get_membership_changes_for_user`: the user's membership events
    /// after `from_token` and up to `to_token`, oldest first, outside
    /// `excluded_rooms`.
    pub async fn membership_changes(
        &self,
        user_id: &str,
        from_token: &RoomStreamToken,
        to_token: &RoomStreamToken,
        excluded_rooms: &[Arc<str>],
    ) -> anyhow::Result<Vec<Event>> {
        if from_token == to_token {
            return Ok(Vec::new());
        }
        // A token with an instance map covers more of the stream than its
        // minimum: fetch up to its maximum and filter.
        let min_from = from_token.stream;
        let max_to = to_token.get_max_stream_pos();
        let excluded: Vec<&str> = excluded_rooms.iter().map(|room| &**room).collect();
        let exclude_clause = if excluded.is_empty() {
            ""
        } else {
            "AND e.room_id != ALL($4)"
        };
        let sql = format!(
            "SELECT m.event_id, instance_name, topological_ordering, stream_ordering
            FROM events AS e, room_memberships AS m
            WHERE e.event_id = m.event_id
                AND m.user_id = $1
                AND e.stream_ordering > $2 AND e.stream_ordering <= $3
                {exclude_clause}
            ORDER BY e.stream_ordering ASC"
        );
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&user_id, &min_from, &max_to];
        if !excluded.is_empty() {
            params.push(&excluded);
        }

        let borrowed = self.borrow().await?;
        let fetched = borrowed.query(&sql, &params).await?;
        let mut ids = Vec::with_capacity(fetched.len());
        for row in fetched {
            let event_id: String = row.try_get(0)?;
            let instance_name: Option<String> = row.try_get(1)?;
            let topological_ordering: i64 = row.try_get(2)?;
            let stream_ordering: i64 = row.try_get(3)?;
            if filter_results(
                Some(from_token),
                Some(to_token),
                instance_name.as_deref(),
                topological_ordering,
                stream_ordering,
            ) {
                ids.push(Arc::from(event_id));
            }
        }
        self.load_events(&borrowed, &ids, RedactBehaviour::Redact, false, true)
            .await
    }

    /// `get_position_for_event`: where the event was persisted, or `None`
    /// if it is not in the database.
    pub async fn event_position(
        &self,
        event_id: &str,
    ) -> anyhow::Result<Option<PersistedEventPosition>> {
        let rows = self
            .query_one_shot(
                "SELECT instance_name, stream_ordering FROM events WHERE event_id = $1",
                &[&event_id],
            )
            .await?;
        let Some(row) = rows.first() else {
            return Ok(None);
        };
        let instance_name: Option<String> = row.try_get(0)?;
        let stream: i64 = row.try_get(1)?;
        Ok(Some(PersistedEventPosition {
            // A null instance name is a row from before sharded persisters.
            instance_name: Arc::from(instance_name.as_deref().unwrap_or("master")),
            stream,
        }))
    }

    /// `get_last_event_pos_in_room_before_stream_ordering`: the last event
    /// in the room at or before `end_token`, and where it was persisted.
    pub async fn last_event_before(
        &self,
        room_id: &str,
        end_token: &RoomStreamToken,
    ) -> anyhow::Result<Option<(Arc<str>, PersistedEventPosition)>> {
        self.last_event_of_types_before(room_id, end_token, &[])
            .await
    }

    /// `get_last_event_pos_in_room_before_stream_ordering` with
    /// `event_types`: as [`Self::last_event_before`], counting only events
    /// of one of `event_types`. No types means any type, as in Python.
    pub async fn last_event_of_types_before(
        &self,
        room_id: &str,
        end_token: &RoomStreamToken,
        event_types: &[String],
    ) -> anyhow::Result<Option<(Arc<str>, PersistedEventPosition)>> {
        // The first subquery is the events within the token's instance map,
        // between its minimum and maximum, to be filtered against the map;
        // the second is the last event before the minimum. The first match
        // in stream order is the answer.
        let min_stream = end_token.stream;
        let max_stream = end_token.get_max_stream_pos();
        let type_clause = if event_types.is_empty() {
            ""
        } else {
            "AND type = ANY($4)"
        };
        let sql = format!(
            "SELECT * FROM (
                SELECT instance_name, stream_ordering, topological_ordering, event_id
                FROM events
                LEFT JOIN rejections USING (event_id)
                WHERE room_id = $1
                    {type_clause}
                    AND $2 < stream_ordering AND stream_ordering <= $3
                    AND NOT outlier
                    AND rejections.event_id IS NULL
                ORDER BY stream_ordering DESC
            ) AS a
            UNION ALL
            SELECT * FROM (
                SELECT instance_name, stream_ordering, topological_ordering, event_id
                FROM events
                LEFT JOIN rejections USING (event_id)
                WHERE room_id = $1
                    {type_clause}
                    AND stream_ordering <= $2
                    AND NOT outlier
                    AND rejections.event_id IS NULL
                ORDER BY stream_ordering DESC
                LIMIT 1
            ) AS b
            ORDER BY stream_ordering DESC"
        );
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&room_id, &min_stream, &max_stream];
        if !event_types.is_empty() {
            params.push(&event_types);
        }
        let rows = self.query_one_shot(&sql, &params).await?;
        for row in rows {
            let instance_name: Option<String> = row.try_get(0)?;
            let stream_ordering: i64 = row.try_get(1)?;
            let topological_ordering: i64 = row.try_get(2)?;
            let event_id: String = row.try_get(3)?;
            if filter_results(
                None,
                Some(end_token),
                instance_name.as_deref(),
                topological_ordering,
                stream_ordering,
            ) {
                return Ok(Some((
                    Arc::from(event_id),
                    PersistedEventPosition {
                        // A null instance name is a row from before sharded
                        // persisters.
                        instance_name: Arc::from(instance_name.as_deref().unwrap_or("master")),
                        stream: stream_ordering,
                    },
                )));
            }
        }
        Ok(None)
    }

    // State.

    /// `get_current_state_ids` with a filter: the room's current state, as
    /// `current_state_events` has it, which for a room joined with faster
    /// joins may be partial.
    pub async fn current_state_ids(
        &self,
        room_id: &str,
        state_filter: &StateFilter,
    ) -> anyhow::Result<StateMap> {
        let (clause, args) = state_filter.sql_clause(2);
        if clause == "1 = 2" {
            return Ok(StateMap::new());
        }
        let sql = if clause.is_empty() {
            "SELECT type, state_key, event_id FROM current_state_events WHERE room_id = $1"
                .to_owned()
        } else {
            format!(
                "SELECT type, state_key, event_id FROM current_state_events
                WHERE room_id = $1 AND ({clause})"
            )
        };
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&room_id];
        params.extend(args.iter().map(|arg| arg as &(dyn ToSql + Sync)));
        let rows = self.query_one_shot(&sql, &params).await?;
        let mut interner = Interner::default();
        let mut state = StateMap::with_capacity(rows.len());
        for row in rows {
            let event_type: String = row.try_get(0)?;
            let state_key: String = row.try_get(1)?;
            let event_id: String = row.try_get(2)?;
            state.insert(
                StateKey(interner.intern(&event_type), Arc::from(state_key)),
                Arc::from(event_id),
            );
        }
        Ok(state)
    }

    /// `get_current_state_deltas_for_room_by_event_position`: the changes
    /// to the room's current state after `from_token` and up to `to_token`,
    /// in `stream_id` order. A delta is positioned at the later of its
    /// row's `stream_id` and its event's stream ordering, for the reasons
    /// the Python explains at length.
    pub async fn state_deltas(
        &self,
        room_id: &str,
        from_token: Option<&RoomStreamToken>,
        to_token: Option<&RoomStreamToken>,
    ) -> anyhow::Result<Vec<StateDelta>> {
        if let (Some(from), Some(to)) = (from_token, to_token) {
            if to.is_before_or_eq(from) {
                return Ok(Vec::new());
            }
        }
        let txn = self.begin().await?;

        // The from-token batch's `stream_id`: the latest `stream_id` at or
        // before `from_token`. Fetching from it inclusive takes in that
        // batch's rows, whose events may sit after the token.
        let mut lower_clause = String::new();
        let mut lower_value: Option<i64> = None;
        if let Some(from) = from_token {
            let rows = txn
                .query(
                    "SELECT MAX(stream_id) FROM current_state_delta_stream
                    WHERE room_id = $1 AND stream_id <= $2",
                    &[&room_id, &from.stream],
                )
                .await?;
            let from_batch: Option<i64> = match rows.first() {
                Some(row) => row.try_get(0)?,
                None => None,
            };
            match from_batch {
                Some(stream_id) => {
                    lower_clause = "AND $2 <= stream_id".to_owned();
                    lower_value = Some(stream_id);
                }
                None => {
                    lower_clause = "AND $2 < stream_id".to_owned();
                    lower_value = Some(from.stream);
                }
            }
        }
        let upper_value = to_token.map(|to| to.get_max_stream_pos());
        let upper_clause = match (lower_value, upper_value) {
            (_, None) => "",
            (Some(_), Some(_)) => "AND stream_id <= $3",
            (None, Some(_)) => "AND stream_id <= $2",
        };
        let sql = format!(
            "SELECT instance_name, stream_id, type, state_key, event_id, prev_event_id
            FROM current_state_delta_stream
            WHERE room_id = $1 {lower_clause} {upper_clause}
            ORDER BY stream_id ASC"
        );
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&room_id];
        if let Some(value) = &lower_value {
            params.push(value);
        }
        if let Some(value) = &upper_value {
            params.push(value);
        }
        let fetched = txn.query(&sql, &params).await?;

        struct DeltaRow {
            instance_name: Option<String>,
            stream_id: i64,
            event_type: String,
            state_key: String,
            event_id: Option<String>,
            prev_event_id: Option<String>,
        }
        let mut rows = Vec::with_capacity(fetched.len());
        for row in fetched {
            rows.push(DeltaRow {
                instance_name: row.try_get(0)?,
                stream_id: row.try_get(1)?,
                event_type: row.try_get(2)?,
                state_key: row.try_get(3)?,
                event_id: row.try_get(4)?,
                prev_event_id: row.try_get(5)?,
            });
        }

        // The `stream_id` of the from-token batch and of the to-token
        // batch, per writer: the latest `stream_id` at or before the
        // writer's position in each token. A historic row has no instance
        // name and counts as "master".
        let mut token_batches: HashSet<(String, i64)> = HashSet::new();
        for token in [from_token, to_token].into_iter().flatten() {
            let mut latest_by_instance: HashMap<&str, i64> = HashMap::new();
            for row in &rows {
                let instance_name = row.instance_name.as_deref().unwrap_or("master");
                if row.stream_id <= token.get_stream_pos_for_instance(instance_name) {
                    let latest = latest_by_instance
                        .entry(instance_name)
                        .or_insert(row.stream_id);
                    *latest = (*latest).max(row.stream_id);
                }
            }
            token_batches.extend(
                latest_by_instance
                    .into_iter()
                    .map(|(instance_name, stream_id)| (instance_name.to_owned(), stream_id)),
            );
        }
        let in_token_batch = |row: &DeltaRow| {
            token_batches.contains(&(
                row.instance_name.as_deref().unwrap_or("master").to_owned(),
                row.stream_id,
            ))
        };

        // Only the rows of the two token batches need their event's position.
        let token_batch_event_ids: Vec<&str> = rows
            .iter()
            .filter(|row| in_token_batch(row))
            .filter_map(|row| row.event_id.as_deref())
            .collect();
        let mut event_positions: HashMap<String, (Option<String>, i64)> = HashMap::new();
        for batch in token_batch_event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let fetched = txn
                .query(
                    "SELECT event_id, instance_name, stream_ordering FROM events
                    WHERE event_id = ANY($1)",
                    &[&batch],
                )
                .await?;
            for row in fetched {
                let event_id: String = row.try_get(0)?;
                let instance_name: Option<String> = row.try_get(1)?;
                let stream_ordering: Option<i64> = row.try_get(2)?;
                if let Some(stream_ordering) = stream_ordering {
                    event_positions.insert(event_id, (instance_name, stream_ordering));
                }
            }
        }
        txn.commit().await?;

        let mut interner = Interner::default();
        let mut deltas = Vec::with_capacity(rows.len());
        for row in &rows {
            // The delta's position: its `stream_id`, unless it belongs to a
            // token batch and its event sits later in the stream.
            let mut instance_name = row.instance_name.as_deref();
            let mut stream = row.stream_id;
            if let Some(event_id) = &row.event_id {
                if in_token_batch(row) {
                    if let Some((event_instance, event_stream)) = event_positions.get(event_id) {
                        if *event_stream > row.stream_id {
                            instance_name = event_instance.as_deref();
                            stream = *event_stream;
                        }
                    }
                }
            }
            if filter_results_by_stream(from_token, to_token, instance_name, stream)? {
                deltas.push(StateDelta {
                    stream_id: row.stream_id,
                    event_type: interner.intern(&row.event_type),
                    state_key: Arc::from(&*row.state_key),
                    event_id: row.event_id.as_deref().map(Arc::from),
                    prev_event_id: row.prev_event_id.as_deref().map(Arc::from),
                });
            }
        }
        Ok(deltas)
    }

    /// `get_current_state_deltas_for_room`: the changes to the room's
    /// current state after `from_token` and up to `to_token`, in `stream_id`
    /// order, bounded on the delta rows' own `stream_id`. Sliding sync reads
    /// these. A token that falls inside a persist batch misses that batch's
    /// deltas, as the Python explains: [`Self::state_deltas`] is the variant
    /// that pairs with a timeline.
    ///
    /// Python first asks its delta stream cache whether the room changed
    /// after `from_token`. Rust always reads the table, which gives the same
    /// answer.
    pub async fn state_deltas_by_stream_id(
        &self,
        room_id: &str,
        from_token: Option<&RoomStreamToken>,
        to_token: Option<&RoomStreamToken>,
    ) -> anyhow::Result<Vec<StateDelta>> {
        if let (Some(from), Some(to)) = (from_token, to_token) {
            if to.is_before_or_eq(from) {
                return Ok(Vec::new());
            }
        }
        let lower_value = from_token.map(|from| from.stream);
        let upper_value = to_token.map(|to| to.get_max_stream_pos());
        let lower_clause = if lower_value.is_some() {
            "AND $2 < stream_id"
        } else {
            ""
        };
        let upper_clause = match (lower_value, upper_value) {
            (_, None) => "",
            (Some(_), Some(_)) => "AND stream_id <= $3",
            (None, Some(_)) => "AND stream_id <= $2",
        };
        let sql = format!(
            "SELECT instance_name, stream_id, type, state_key, event_id, prev_event_id
            FROM current_state_delta_stream
            WHERE room_id = $1 {lower_clause} {upper_clause}
            ORDER BY stream_id ASC"
        );
        let mut params: Vec<&(dyn ToSql + Sync)> = vec![&room_id];
        if let Some(value) = &lower_value {
            params.push(value);
        }
        if let Some(value) = &upper_value {
            params.push(value);
        }
        let rows = self.query_one_shot(&sql, &params).await?;

        let mut interner = Interner::default();
        let mut deltas = Vec::with_capacity(rows.len());
        for row in rows {
            let instance_name: Option<String> = row.try_get(0)?;
            let stream_id: i64 = row.try_get(1)?;
            if !filter_results_by_stream(from_token, to_token, instance_name.as_deref(), stream_id)?
            {
                continue;
            }
            let event_type: String = row.try_get(2)?;
            let state_key: String = row.try_get(3)?;
            let event_id: Option<String> = row.try_get(4)?;
            let prev_event_id: Option<String> = row.try_get(5)?;
            deltas.push(StateDelta {
                stream_id,
                event_type: interner.intern(&event_type),
                state_key: Arc::from(state_key),
                event_id: event_id.map(Arc::from),
                prev_event_id: prev_event_id.map(Arc::from),
            });
        }
        Ok(deltas)
    }

    /// `get_member_counts`: the number of members of the room by
    /// membership, from its current state. A rejected membership event has
    /// no membership and is not counted.
    pub async fn member_counts(&self, room_id: &str) -> anyhow::Result<HashMap<String, i64>> {
        let rows = self
            .query_one_shot(
                "SELECT count(*), membership FROM current_state_events
                WHERE type = 'm.room.member' AND room_id = $1
                    AND membership IS NOT NULL
                GROUP BY membership",
                &[&room_id],
            )
            .await?;
        let mut counts = HashMap::with_capacity(rows.len());
        for row in rows {
            let count: i64 = row.try_get(0)?;
            let membership: String = row.try_get(1)?;
            counts.insert(membership, count);
        }
        Ok(counts)
    }

    /// `get_latest_bump_stamp_for_room`: the room's `bump_stamp` in
    /// `sliding_sync_joined_rooms`, or `None` if it has none or no row.
    pub async fn latest_bump_stamp(&self, room_id: &str) -> anyhow::Result<Option<i64>> {
        let rows = self
            .query_one_shot(
                "SELECT bump_stamp FROM sliding_sync_joined_rooms WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        match rows.first() {
            Some(row) => Ok(row.try_get(0)?),
            None => Ok(None),
        }
    }

    /// `_get_state_group_for_events`: the state group of each event, which
    /// is the state after it. An event with none, an outlier or an event
    /// that is not known, is left out, as it is when the Python is read
    /// through its cache.
    pub async fn state_group_for_events(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        let cache = &self.store.caches.event_to_state_group;
        let mut groups = HashMap::with_capacity(event_ids.len());
        let mut missing: Vec<&str> = Vec::new();
        for event_id in event_ids {
            match cache.get(event_id) {
                Some(group) => {
                    groups.insert(Arc::clone(event_id), group);
                }
                None => missing.push(event_id),
            }
        }
        if !missing.is_empty() {
            let borrowed = self.borrow().await?;
            for batch in missing.chunks(SELECT_MANY_BATCH_SIZE) {
                let rows = borrowed
                    .query(
                        "SELECT event_id, state_group FROM event_to_state_groups
                        WHERE event_id = ANY($1)",
                        &[&batch],
                    )
                    .await?;
                for row in rows {
                    let event_id: String = row.try_get(0)?;
                    let group: i64 = row.try_get(1)?;
                    let event_id: Arc<str> = Arc::from(event_id);
                    drop(cache.insert(Arc::clone(&event_id), group));
                    groups.insert(event_id, group);
                }
            }
        }
        Ok(groups)
    }

    /// The state group of each event, read from the database and not the
    /// cache. For when the cache may be behind a write we know has
    /// committed, such as an event getting its full state.
    pub async fn state_group_for_events_uncached(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        let mut groups = HashMap::with_capacity(event_ids.len());
        if event_ids.is_empty() {
            return Ok(groups);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let batch: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT event_id, state_group FROM event_to_state_groups
                    WHERE event_id = ANY($1)",
                    &[&batch],
                )
                .await?;
            for row in rows {
                let event_id: String = row.try_get(0)?;
                let group: i64 = row.try_get(1)?;
                groups.insert(Arc::from(event_id), group);
            }
        }
        Ok(groups)
    }

    /// `_get_state_for_groups` for whole groups: the full state of each
    /// group, from the state group cache or the database.
    pub async fn state_for_groups(
        &self,
        groups: &[i64],
    ) -> anyhow::Result<HashMap<i64, Arc<StateMap>>> {
        let cache = &self.store.caches.state_groups;
        let mut state = HashMap::with_capacity(groups.len());
        let mut missing: Vec<i64> = Vec::new();
        for &group in groups {
            if state.contains_key(&group) || missing.contains(&group) {
                continue;
            }
            match cache.get(&group) {
                Some(map) => {
                    state.insert(group, map);
                }
                None => missing.push(group),
            }
        }
        self.update_stats(|stats| stats.state_groups_cached += state.len() as u64);
        if missing.is_empty() {
            return Ok(state);
        }

        // `_get_state_groups_from_groups_txn`: walk each group's chain of
        // predecessors with a recursive CTE and take the latest entry per
        // key, with sequential scans off as the Python has it. One
        // read-only transaction per batch of groups, sent as one simple
        // query so the whole batch is a single round trip: the group IDs
        // are integers, so they can be spliced into the text.
        let borrowed = self.borrow().await?;
        for batch in missing.chunks(STATE_GROUP_BATCH_SIZE) {
            let mut sql = String::from(
                "BEGIN READ ONLY; SET LOCAL enable_seqscan = off;
",
            );
            for group in batch {
                sql.push_str(&format!(
                    "WITH RECURSIVE sgs(state_group) AS (
                        VALUES({group}::bigint)
                        UNION ALL
                        SELECT prev_state_group FROM state_group_edges e, sgs s
                        WHERE s.state_group = e.state_group
                    )
                    SELECT DISTINCT ON (type, state_key) type, state_key, event_id
                    FROM state_groups_state
                    WHERE state_group IN (SELECT state_group FROM sgs)
                    ORDER BY type, state_key, state_group DESC;
"
                ));
            }
            sql.push_str("COMMIT");
            let results = borrowed.simple_query(&sql).await?;
            // The results of the two leading statements are empty; then one
            // per group, in order.
            let mut interner = Interner::default();
            for (group, rows) in batch.iter().zip(results.into_iter().skip(2)) {
                let mut map = StateMap::with_capacity(rows.len());
                for row in rows {
                    let event_type = row.get(0).context("type column")?;
                    let state_key = row.get(1).context("state_key column")?;
                    let event_id = row.get(2).context("event_id column")?;
                    map.insert(
                        StateKey(interner.intern(event_type), Arc::from(state_key)),
                        Arc::from(event_id),
                    );
                }
                let map = Arc::new(map);
                drop(cache.insert(*group, Arc::clone(&map)));
                state.insert(*group, map);
            }
        }
        self.update_stats(|stats| stats.state_groups_fetched += missing.len() as u64);
        Ok(state)
    }

    /// `get_state_ids_for_event`: the state after the event, filtered.
    pub async fn state_ids_for_event(
        &self,
        event_id: &str,
        state_filter: &StateFilter,
    ) -> anyhow::Result<StateMap> {
        let event_id: Arc<str> = Arc::from(event_id);
        let groups = self
            .state_group_for_events(&[Arc::clone(&event_id)])
            .await?;
        let Some(&group) = groups.get(&event_id) else {
            anyhow::bail!("No state group for unknown or outlier event {event_id}");
        };
        let state = self.state_for_groups(&[group]).await?;
        Ok(state_filter.filter_state(&state[&group]))
    }

    /// `get_state_after_event`: as [`SyncReader::state_ids_for_event`], with
    /// the event itself in the state if it is a state event that was not
    /// rejected.
    pub async fn state_after_event(
        &self,
        event_id: &str,
        state_filter: &StateFilter,
    ) -> anyhow::Result<StateMap> {
        let mut state = self.state_ids_for_event(event_id, state_filter).await?;
        // `get_metadata_for_events`: the event's type and state key, whether
        // it is rejected or redacted, without loading it.
        let rows = self
            .query_one_shot(
                "SELECT e.type, se.state_key, r.reason
                FROM events AS e
                LEFT JOIN state_events se USING (event_id)
                LEFT JOIN rejections r USING (event_id)
                WHERE e.event_id = $1",
                &[&event_id],
            )
            .await?;
        if let Some(row) = rows.first() {
            let event_type: String = row.try_get(0)?;
            let state_key: Option<String> = row.try_get(1)?;
            let rejection_reason: Option<String> = row.try_get(2)?;
            if let (Some(state_key), None) = (state_key, rejection_reason) {
                state.insert(StateKey::new(&event_type, &state_key), Arc::from(event_id));
            }
        }
        Ok(state)
    }

    /// `get_state_ids_at`: the state at the last event of the room at or
    /// before `token`, filtered. Empty if the room has no event there.
    pub async fn state_ids_at(
        &self,
        room_id: &str,
        token: &RoomStreamToken,
        state_filter: &StateFilter,
    ) -> anyhow::Result<StateMap> {
        match self.last_event_before(room_id, token).await? {
            Some((event_id, _)) => self.state_after_event(&event_id, state_filter).await,
            None => {
                log::info!("Failed to find any events in room {room_id} at {token:?}");
                Ok(StateMap::new())
            }
        }
    }

    // Notifications.

    /// `get_unread_event_push_actions_by_room_for_user`: the user's unread
    /// counts in the room since their last read receipt, or since they
    /// joined if they have none. The user is assumed to be a member.
    pub async fn unread_counts(
        &self,
        room_id: &str,
        user_id: &str,
    ) -> anyhow::Result<Arc<RoomNotifCounts>> {
        let key = (Arc::<str>::from(room_id), Arc::<str>::from(user_id));
        self.store
            .caches
            .unread_counts
            .get_or_load(key, || self.load_unread_counts(room_id, user_id))
            .await
            .map_err(anyhow::Error::new)
    }

    /// `_get_unread_counts_by_receipt_txn` and `_get_unread_counts_by_pos_txn`.
    async fn load_unread_counts(
        &self,
        room_id: &str,
        user_id: &str,
    ) -> anyhow::Result<Arc<RoomNotifCounts>> {
        let receipt_types: Vec<&str> = READ_RECEIPT_TYPES.to_vec();
        let txn = self.begin().await?;

        // The stream ordering of the user's latest unthreaded receipt, or
        // failing one, of their latest membership event in the room.
        let rows = txn
            .query(
                "SELECT event_id, event_stream_ordering FROM receipts_linearized
                WHERE receipt_type = ANY($1) AND user_id = $2 AND room_id = $3
                    AND thread_id IS NULL
                ORDER BY event_stream_ordering DESC
                LIMIT 1",
                &[&receipt_types, &user_id, &room_id],
            )
            .await?;
        let receipt_stream: i64 = match rows.first() {
            Some(row) => row.try_get(1)?,
            None => {
                let rows = txn
                    .query(
                        "SELECT event_id FROM local_current_membership
                        WHERE room_id = $1 AND user_id = $2",
                        &[&room_id, &user_id],
                    )
                    .await?;
                let Some(row) = rows.first() else {
                    anyhow::bail!("{user_id} has no membership in {room_id}");
                };
                let event_id: String = row.try_get(0)?;
                let rows = txn
                    .query(
                        "SELECT stream_ordering FROM events WHERE event_id = $1",
                        &[&event_id],
                    )
                    .await?;
                let Some(row) = rows.first() else {
                    anyhow::bail!(
                        "membership event {event_id} of {user_id} in {room_id} is unknown"
                    );
                };
                row.try_get(0)?
            }
        };

        let mut counts = RoomNotifCounts::default();
        let thread = |counts: &mut RoomNotifCounts, thread_id: &str| -> NotifCounts {
            if thread_id == MAIN_TIMELINE {
                counts.main_timeline
            } else {
                counts.threads.get(thread_id).copied().unwrap_or_default()
            }
        };
        let set_thread = |counts: &mut RoomNotifCounts, thread_id: &str, value: NotifCounts| {
            if thread_id == MAIN_TIMELINE {
                counts.main_timeline = value;
            } else {
                counts.threads.insert(Arc::from(thread_id), value);
            }
        };

        // The summarised counts, where the summary is current for the
        // thread: its `last_receipt_stream_ordering` matches the latest
        // receipt for the thread (threaded, or else the unthreaded one), or
        // is null, from a version that kept summaries current on receipt.
        // Threads with a stale summary are counted from the actions below.
        let rows = txn
            .query(
                "SELECT notif_count, COALESCE(unread_count, 0), thread_id
                FROM event_push_summary
                LEFT JOIN (
                    SELECT thread_id, MAX(event_stream_ordering) AS threaded_receipt_stream_ordering
                    FROM receipts_linearized
                    WHERE user_id = $1 AND room_id = $2 AND event_stream_ordering > $3
                        AND receipt_type = ANY($4)
                    GROUP BY thread_id
                ) AS receipts USING (thread_id)
                WHERE room_id = $2 AND user_id = $1
                AND (
                    (last_receipt_stream_ordering IS NULL
                        AND stream_ordering > COALESCE(threaded_receipt_stream_ordering, $3))
                    OR last_receipt_stream_ordering = COALESCE(threaded_receipt_stream_ordering, $3)
                ) AND (notif_count != 0 OR COALESCE(unread_count, 0) != 0)",
                &[&user_id, &room_id, &receipt_stream, &receipt_types],
            )
            .await?;
        let mut summarised_threads: HashSet<String> = HashSet::new();
        for row in rows {
            let notif_count: i64 = row.try_get(0)?;
            let unread_count: i64 = row.try_get(1)?;
            let thread_id: String = row.try_get(2)?;
            let mut value = thread(&mut counts, &thread_id);
            value.notify_count += notif_count;
            value.unread_count += unread_count;
            set_thread(&mut counts, &thread_id, value);
            summarised_threads.insert(thread_id);
        }

        // Highlights are not summarised.
        let rows = txn
            .query(
                "SELECT COUNT(*), thread_id FROM event_push_actions
                LEFT JOIN (
                    SELECT thread_id, MAX(event_stream_ordering) AS threaded_receipt_stream_ordering
                    FROM receipts_linearized
                    WHERE user_id = $1 AND room_id = $2 AND event_stream_ordering > $3
                        AND receipt_type = ANY($4)
                    GROUP BY thread_id
                ) AS receipts USING (thread_id)
                WHERE user_id = $1 AND room_id = $2
                    AND stream_ordering > COALESCE(threaded_receipt_stream_ordering, $3)
                    AND highlight = 1
                GROUP BY thread_id",
                &[&user_id, &room_id, &receipt_stream, &receipt_types],
            )
            .await?;
        for row in rows {
            let highlight_count: i64 = row.try_get(0)?;
            let thread_id: String = row.try_get(1)?;
            let mut value = thread(&mut counts, &thread_id);
            value.highlight_count += highlight_count;
            set_thread(&mut counts, &thread_id, value);
        }

        // For the summarised threads, the actions since the summary was
        // last rotated.
        let rows = txn
            .query(
                "SELECT stream_ordering FROM event_push_summary_stream_ordering",
                &[],
            )
            .await?;
        let Some(row) = rows.first() else {
            anyhow::bail!("event_push_summary_stream_ordering is empty");
        };
        let rotated_upto: i64 = row.try_get(0)?;
        let rows = txn
            .query(
                "SELECT
                    COUNT(CASE WHEN notif = 1 THEN 1 END),
                    COUNT(CASE WHEN unread = 1 THEN 1 END),
                    thread_id
                FROM event_push_actions ea
                WHERE user_id = $1 AND room_id = $2 AND ea.stream_ordering > $3
                GROUP BY thread_id",
                &[&user_id, &room_id, &rotated_upto],
            )
            .await?;
        for row in rows {
            let notif_count: i64 = row.try_get(0)?;
            let unread_count: i64 = row.try_get(1)?;
            let thread_id: String = row.try_get(2)?;
            if !summarised_threads.contains(&thread_id) {
                continue;
            }
            let mut value = thread(&mut counts, &thread_id);
            value.notify_count += notif_count;
            value.unread_count += unread_count;
            set_thread(&mut counts, &thread_id, value);
        }

        // For the rest, the actions since the receipt: recent events not
        // summarised yet, or a summary made stale by a recent receipt.
        let summarised: Vec<&str> = summarised_threads.iter().map(|id| &**id).collect();
        let rows = txn
            .query(
                "SELECT
                    COUNT(CASE WHEN notif = 1 THEN 1 END),
                    COUNT(CASE WHEN unread = 1 THEN 1 END),
                    thread_id
                FROM event_push_actions
                LEFT JOIN (
                    SELECT thread_id, MAX(event_stream_ordering) AS threaded_receipt_stream_ordering
                    FROM receipts_linearized
                    WHERE user_id = $1 AND room_id = $2 AND event_stream_ordering > $3
                        AND receipt_type = ANY($4)
                    GROUP BY thread_id
                ) AS receipts USING (thread_id)
                WHERE user_id = $1 AND room_id = $2
                    AND stream_ordering > COALESCE(threaded_receipt_stream_ordering, $3)
                    AND NOT thread_id = ANY($5)
                GROUP BY thread_id",
                &[
                    &user_id,
                    &room_id,
                    &receipt_stream,
                    &receipt_types,
                    &summarised,
                ],
            )
            .await?;
        for row in rows {
            let notif_count: i64 = row.try_get(0)?;
            let unread_count: i64 = row.try_get(1)?;
            let thread_id: String = row.try_get(2)?;
            let mut value = thread(&mut counts, &thread_id);
            value.notify_count += notif_count;
            value.unread_count += unread_count;
            set_thread(&mut counts, &thread_id, value);
        }
        txn.commit().await?;

        Ok(Arc::new(counts))
    }

    /// `is_server_admin`.
    pub async fn is_server_admin(&self, user_id: &str) -> anyhow::Result<bool> {
        let rows = self
            .query_one_shot("SELECT admin FROM users WHERE name = $1", &[&user_id])
            .await?;
        let Some(row) = rows.first() else {
            return Ok(false);
        };
        let admin: Option<i16> = row.try_get(0)?;
        Ok(admin.is_some_and(|admin| admin != 0))
    }
}

/// How many values to send per `= ANY` statement, as
/// `simple_select_many_batch` does.
pub(crate) const SELECT_MANY_BATCH_SIZE: usize = 1000;

/// How many state groups to load per transaction, as
/// `_get_state_groups_from_groups` does.
const STATE_GROUP_BATCH_SIZE: usize = 100;

/// Interns strings, so the event types of one result share an allocation.
#[derive(Default)]
pub(crate) struct Interner(HashSet<Arc<str>>);

impl Interner {
    pub(crate) fn intern(&mut self, s: &str) -> Arc<str> {
        if let Some(existing) = self.0.get(s) {
            return Arc::clone(existing);
        }
        let new: Arc<str> = Arc::from(s);
        self.0.insert(Arc::clone(&new));
        new
    }
}

/// `_make_generic_sql_bound` with the values as parameters: pushes them on
/// `params`, which follow one parameter (`$1`) in the statement, and
/// returns the clause with their placeholders. A bound without a first
/// value compares the second column alone.
fn bound_clause(
    bound: &str,
    columns: (&str, &str),
    values: Bound2,
    params: &mut Vec<i64>,
) -> String {
    let (name1, name2) = columns;
    match values {
        (None, val2) => {
            params.push(val2);
            format!("(${} {bound} {name2})", params.len() + 1)
        }
        (Some(val1), val2) => {
            params.push(val1);
            params.push(val2);
            let first = params.len();
            format!("((${first},${}) {bound} ({name1},{name2}))", first + 1)
        }
    }
}

impl Borrowed<'_, '_> {
    fn conn(&self) -> &PooledConnection {
        self.conn.as_ref().expect("held until dropped")
    }

    /// Run a statement through the connection's prepared statement cache.
    pub async fn query(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> anyhow::Result<Vec<Row>> {
        let start = Instant::now();
        let conn = self.conn();
        let statement = conn
            .prepare_cached(sql, false)
            .await
            .with_context(|| format!("preparing {}", sql.trim()))?;
        let rows = conn
            .query(&statement, params)
            .await
            .with_context(|| format!("running {}", sql.trim()))?;
        self.record(start, rows.len());
        if log::log_enabled!(log::Level::Debug) {
            let head: String = sql.split_whitespace().take(6).collect::<Vec<_>>().join(" ");
            debug!(
                "Sync query [{head}] returned {} rows in {:?}",
                rows.len(),
                start.elapsed()
            );
        }
        Ok(rows)
    }

    /// Run several statements as one simple query, a single round trip,
    /// returning each statement's rows as text. See `state_for_groups`.
    async fn simple_query(
        &self,
        sql: &str,
    ) -> anyhow::Result<Vec<Vec<tokio_postgres::SimpleQueryRow>>> {
        let start = Instant::now();
        let messages = self
            .conn()
            .simple_query(sql)
            .await
            .with_context(|| format!("running {}", sql.trim()))?;
        let mut results: Vec<Vec<tokio_postgres::SimpleQueryRow>> = Vec::new();
        let mut current = Vec::new();
        let mut rows = 0;
        for message in messages {
            match message {
                tokio_postgres::SimpleQueryMessage::Row(row) => current.push(row),
                tokio_postgres::SimpleQueryMessage::CommandComplete(_) => {
                    rows += current.len();
                    results.push(std::mem::take(&mut current));
                }
                _ => {}
            }
        }
        self.record(start, rows);
        Ok(results)
    }

    /// Run statements that return nothing.
    pub(crate) async fn execute(&self, sql: &str) -> anyhow::Result<()> {
        let start = Instant::now();
        self.conn()
            .batch_execute(sql)
            .await
            .with_context(|| format!("running {sql}"))?;
        self.record(start, 0);
        Ok(())
    }

    fn record(&self, start: Instant, rows: usize) {
        self.reader.update_stats(|stats| {
            stats.queries += 1;
            stats.rows += rows as u64;
            stats.db_time += start.elapsed();
        });
    }

    /// Commit the transaction opened by [`SyncReader::begin`] and return
    /// the connection to the pool.
    pub async fn commit(mut self) -> anyhow::Result<()> {
        if self.in_transaction {
            self.execute("COMMIT").await?;
            self.in_transaction = false;
        }
        Ok(())
    }
}

impl Drop for Borrowed<'_, '_> {
    fn drop(&mut self) {
        let Some(conn) = self.conn.take() else { return };
        if !self.in_transaction {
            return;
        }
        // Roll back before the connection goes back to the pool, so the next
        // user doesn't find it inside our transaction. Without a runtime to
        // do that on, the connection is dropped rather than returned.
        match Handle::try_current() {
            Ok(handle) => {
                handle.spawn(async move {
                    if let Err(e) = conn.batch_execute("ROLLBACK").await {
                        warn!("Failed to roll back a sync transaction: {e}");
                    }
                    drop(conn);
                });
            }
            Err(_) => {
                drop(PooledConnection::take(conn));
            }
        }
    }
}

#[async_trait]
impl Querier for Borrowed<'_, '_> {
    async fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> anyhow::Result<Vec<Row>> {
        Borrowed::query(self, sql, params).await
    }
}

// Test-only entry points, so trial can compare the Rust store against the
// Python one on a real schema.

pub(crate) fn anyhow_to_py(e: anyhow::Error) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("{e:#}"))
}

pub(crate) fn json_to_py<'py>(
    py: Python<'py>,
    value: &serde_json::Value,
) -> PyResult<Bound<'py, PyAny>> {
    pythonize::pythonize(py, value).map_err(Into::into)
}

/// The sync store's queries, each run to completion on the tokio runtime
/// with the GIL released, over the native driver's pool and the Rust sync
/// handler's caches. For tests only.
#[pyclass(name = "SyncStoreForTest")]
pub struct PySyncStoreForTest {
    store: Arc<SyncStore>,
    pool: ConnectionPool,
    handle: Handle,
}

impl PySyncStoreForTest {
    /// Run `f` over a reader for one request, blocking until it is done.
    fn run<'a, T, F>(&'a self, py: Python<'_>, f: F) -> PyResult<T>
    where
        T: Send + 'a,
        F: FnOnce(SyncReader<'a>) -> BoxFuture<'a, anyhow::Result<T>> + Send,
    {
        py.detach(|| {
            self.handle
                .block_on(f(self.store.reader(self.pool.clone())))
        })
        .map_err(anyhow_to_py)
    }
}

fn arcs(ids: Vec<String>) -> Vec<Arc<str>> {
    ids.into_iter().map(Arc::from).collect()
}

fn arc_set(ids: HashSet<Arc<str>>) -> HashSet<String> {
    ids.into_iter().map(|id| id.to_string()).collect()
}

fn arc_map<V>(map: HashMap<Arc<str>, V>) -> HashMap<String, V> {
    map.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

#[pymethods]
impl PySyncStoreForTest {
    #[new]
    fn new(pool: &Bound<'_, PyConnectionPool>, handler: &crate::sync::SyncHandler) -> Self {
        let pool = pool.get();
        PySyncStoreForTest {
            store: Arc::clone(handler.store()),
            pool: pool.pool.clone(),
            handle: pool.handle.clone(),
        }
    }

    /// [`crate::visibility::filter_events_for_server`]. `None` where Python
    /// would wait for the full state.
    #[pyo3(signature = (target_server_name, local_server_name, events, *, redact, filter_out_erased_senders, filter_out_remote_partial_state_events))]
    #[allow(clippy::too_many_arguments)]
    fn filter_events_for_server(
        &self,
        py: Python<'_>,
        target_server_name: String,
        local_server_name: String,
        events: Vec<Bound<'_, Event>>,
        redact: bool,
        filter_out_erased_senders: bool,
        filter_out_remote_partial_state_events: bool,
    ) -> PyResult<Option<Vec<Event>>> {
        let events: Vec<Event> = events.iter().map(|e| e.get().shallow_clone()).collect();
        let filtered = self.run(py, |reader| {
            async move {
                let filter = crate::visibility::ServerFilter {
                    target_server_name: &target_server_name,
                    local_server_name: &local_server_name,
                    redact,
                    filter_out_erased_senders,
                    filter_out_remote_partial_state_events,
                };
                crate::visibility::filter_events_for_server(&reader, &filter, events).await
            }
            .boxed()
        })?;
        Ok(match filtered {
            crate::visibility::ServerFiltered::Events(events) => Some(events),
            crate::visibility::ServerFiltered::NeedsFullState => None,
        })
    }

    fn rooms_for_user(&self, py: Python<'_>, user_id: String) -> PyResult<HashSet<String>> {
        let rooms = self.run(py, |reader| {
            async move { reader.rooms_for_user(&user_id).await }.boxed()
        })?;
        Ok(rooms.iter().map(|id| id.to_string()).collect())
    }

    fn events_in_current_state(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<HashSet<String>> {
        let ids = arcs(event_ids);
        self.run(py, |reader| {
            async move { reader.events_in_current_state(&ids).await }.boxed()
        })
        .map(arc_set)
    }

    #[pyo3(signature = (room_id, from_token, to_token))]
    fn timeline_gap(
        &self,
        py: Python<'_>,
        room_id: String,
        from_token: Option<RoomStreamToken>,
        to_token: RoomStreamToken,
    ) -> PyResult<Option<RoomStreamToken>> {
        self.run(py, |reader| {
            async move {
                reader
                    .timeline_gap(&room_id, from_token.as_ref(), &to_token)
                    .await
            }
            .boxed()
        })
    }

    fn partial_state_rooms(
        &self,
        py: Python<'_>,
        room_ids: Vec<String>,
    ) -> PyResult<HashMap<String, bool>> {
        let ids = arcs(room_ids);
        self.run(py, |reader| {
            async move { reader.partial_state_rooms(&ids).await }.boxed()
        })
        .map(arc_map)
    }

    /// The summary as a dict of membership to `(members, count)`.
    #[allow(clippy::type_complexity)]
    fn room_summary(
        &self,
        py: Python<'_>,
        room_id: String,
    ) -> PyResult<HashMap<String, (Vec<(String, String)>, i64)>> {
        let summary = self.run(py, |reader| {
            async move { reader.room_summary(&room_id).await }.boxed()
        })?;
        Ok(summary
            .iter()
            .map(|(membership, summary)| {
                let members = summary
                    .members
                    .iter()
                    .map(|(user_id, event_id)| (user_id.to_string(), event_id.to_string()))
                    .collect();
                (membership.clone(), (members, summary.count))
            })
            .collect())
    }

    fn tags_for_room<'py>(
        &self,
        py: Python<'py>,
        user_id: String,
        room_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let tags = self.run(py, |reader| {
            async move { reader.tags_for_room(&user_id, &room_id).await }.boxed()
        })?;
        json_to_py(py, &serde_json::Value::Object(tags.into_iter().collect()))
    }

    fn global_account_data_by_type<'py>(
        &self,
        py: Python<'py>,
        user_id: String,
        data_type: String,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let data = self.run(py, |reader| {
            async move {
                reader
                    .global_account_data_by_type(&user_id, &data_type)
                    .await
            }
            .boxed()
        })?;
        data.as_deref().map(|data| json_to_py(py, data)).transpose()
    }

    fn ignored_users(&self, py: Python<'_>, user_id: String) -> PyResult<HashSet<String>> {
        self.run(py, |reader| {
            async move { reader.ignored_users(&user_id).await }.boxed()
        })
        .map(arc_set)
    }

    fn are_users_erased(
        &self,
        py: Python<'_>,
        user_ids: Vec<String>,
    ) -> PyResult<HashMap<String, bool>> {
        let ids = arcs(user_ids);
        self.run(py, |reader| {
            async move { reader.are_users_erased(&ids).await }.boxed()
        })
        .map(arc_map)
    }

    /// `(min_lifetime, max_lifetime)`.
    fn retention_policy(
        &self,
        py: Python<'_>,
        room_id: String,
    ) -> PyResult<(Option<i64>, Option<i64>)> {
        let policy = self.run(py, |reader| {
            async move { reader.retention_policy(&room_id).await }.boxed()
        })?;
        Ok((policy.min_lifetime, policy.max_lifetime))
    }

    fn is_server_admin(&self, py: Python<'_>, user_id: String) -> PyResult<bool> {
        self.run(py, |reader| {
            async move { reader.is_server_admin(&user_id).await }.boxed()
        })
    }

    /// `get_events_as_list`, with `redact_behaviour` as the name of a
    /// member of `EventRedactBehaviour`.
    #[pyo3(signature = (event_ids, redact_behaviour = "redact", allow_rejected = false, get_prev_content = false))]
    fn events(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
        redact_behaviour: &str,
        allow_rejected: bool,
        get_prev_content: bool,
    ) -> PyResult<Vec<Py<Event>>> {
        let behaviour = parse_behaviour(redact_behaviour)?;
        let ids = arcs(event_ids);
        let events = self.run(py, |reader| {
            async move {
                reader
                    .events(&ids, behaviour, allow_rejected, get_prev_content)
                    .await
            }
            .boxed()
        })?;
        events_to_py(py, events)
    }

    /// `paginate_room_events_by_stream_ordering` (`order="stream"`) or
    /// `paginate_room_events_by_topological_ordering` (`"topological"`),
    /// with `direction` as `"f"` or `"b"`.
    #[pyo3(signature = (room_id, from_key, to_key, direction, limit, order))]
    #[allow(clippy::too_many_arguments)]
    fn paginate_room_events(
        &self,
        py: Python<'_>,
        room_id: String,
        from_key: RoomStreamToken,
        to_key: Option<RoomStreamToken>,
        direction: &str,
        limit: i64,
        order: &str,
    ) -> PyResult<(Vec<Py<Event>>, RoomStreamToken, bool)> {
        let direction = parse_direction(direction)?;
        let order = match order {
            "stream" => PaginationOrder::Stream,
            "topological" => PaginationOrder::Topological,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown order {other:?}"
                )))
            }
        };
        let (events, token, limited) = self.run(py, |reader| {
            async move {
                reader
                    .paginate_room_events(
                        &room_id,
                        &from_key,
                        to_key.as_ref(),
                        direction,
                        limit,
                        order,
                    )
                    .await
            }
            .boxed()
        })?;
        Ok((events_to_py(py, events)?, token, limited))
    }

    /// `get_recent_event_ids_for_room`, with each row as
    /// `(event_id, topological_ordering, stream_ordering)`.
    #[allow(clippy::type_complexity)]
    fn recent_event_ids(
        &self,
        py: Python<'_>,
        room_id: String,
        limit: i64,
        end_token: RoomStreamToken,
    ) -> PyResult<(Vec<(String, Option<i64>, i64)>, RoomStreamToken)> {
        let (rows, token) = self.run(py, |reader| {
            async move { reader.recent_event_ids(&room_id, limit, &end_token).await }.boxed()
        })?;
        let rows = rows
            .into_iter()
            .map(|row| {
                (
                    row.event_id.to_string(),
                    row.topological_ordering,
                    row.stream_ordering,
                )
            })
            .collect();
        Ok((rows, token))
    }

    /// `get_membership_changes_for_user`.
    #[pyo3(signature = (user_id, from_key, to_key, excluded_rooms = None))]
    fn membership_changes(
        &self,
        py: Python<'_>,
        user_id: String,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken,
        excluded_rooms: Option<Vec<String>>,
    ) -> PyResult<Vec<Py<Event>>> {
        let excluded = arcs(excluded_rooms.unwrap_or_default());
        let events = self.run(py, |reader| {
            async move {
                reader
                    .membership_changes(&user_id, &from_key, &to_key, &excluded)
                    .await
            }
            .boxed()
        })?;
        events_to_py(py, events)
    }

    /// `get_last_event_pos_in_room_before_stream_ordering`, as
    /// `(event_id, instance_name, stream_ordering)`.
    fn last_event_before(
        &self,
        py: Python<'_>,
        room_id: String,
        end_token: RoomStreamToken,
    ) -> PyResult<Option<(String, String, i64)>> {
        let last = self.run(py, |reader| {
            async move { reader.last_event_before(&room_id, &end_token).await }.boxed()
        })?;
        Ok(last.map(|(event_id, position)| {
            (
                event_id.to_string(),
                position.instance_name.to_string(),
                position.stream,
            )
        }))
    }

    /// `get_last_event_pos_in_room_before_stream_ordering` with
    /// `event_types`, as `(event_id, instance_name, stream_ordering)`.
    fn last_event_of_types_before(
        &self,
        py: Python<'_>,
        room_id: String,
        end_token: RoomStreamToken,
        event_types: Vec<String>,
    ) -> PyResult<Option<(String, String, i64)>> {
        let last = self.run(py, |reader| {
            async move {
                reader
                    .last_event_of_types_before(&room_id, &end_token, &event_types)
                    .await
            }
            .boxed()
        })?;
        Ok(last.map(|(event_id, position)| {
            (
                event_id.to_string(),
                position.instance_name.to_string(),
                position.stream,
            )
        }))
    }

    /// `get_member_counts`.
    fn member_counts(&self, py: Python<'_>, room_id: String) -> PyResult<HashMap<String, i64>> {
        self.run(py, |reader| {
            async move { reader.member_counts(&room_id).await }.boxed()
        })
    }

    /// `get_latest_bump_stamp_for_room`.
    fn latest_bump_stamp(&self, py: Python<'_>, room_id: String) -> PyResult<Option<i64>> {
        self.run(py, |reader| {
            async move { reader.latest_bump_stamp(&room_id).await }.boxed()
        })
    }

    /// `get_unread_event_push_actions_by_room_for_user`, with each timeline's
    /// counts as `(notify_count, unread_count, highlight_count)`: the main
    /// timeline's, and the threads' by thread.
    #[allow(clippy::type_complexity)]
    fn unread_counts(
        &self,
        py: Python<'_>,
        room_id: String,
        user_id: String,
    ) -> PyResult<((i64, i64, i64), HashMap<String, (i64, i64, i64)>)> {
        let counts = self.run(py, |reader| {
            async move { reader.unread_counts(&room_id, &user_id).await }.boxed()
        })?;
        let tuple = |c: NotifCounts| (c.notify_count, c.unread_count, c.highlight_count);
        Ok((
            tuple(counts.main_timeline),
            counts
                .threads
                .iter()
                .map(|(thread_id, c)| (thread_id.to_string(), tuple(*c)))
                .collect(),
        ))
    }

    /// `get_current_state_ids` with a filter.
    fn current_state_ids<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        state_filter: StateFilter,
    ) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let state = self.run(py, |reader| {
            async move { reader.current_state_ids(&room_id, &state_filter).await }.boxed()
        })?;
        state_map_to_py(py, &state)
    }

    /// `get_current_state_deltas_for_room_by_event_position`, with each
    /// delta as `(stream_id, type, state_key, event_id, prev_event_id)`.
    #[pyo3(signature = (room_id, from_token, to_token))]
    #[allow(clippy::type_complexity)]
    fn state_deltas(
        &self,
        py: Python<'_>,
        room_id: String,
        from_token: Option<RoomStreamToken>,
        to_token: Option<RoomStreamToken>,
    ) -> PyResult<Vec<(i64, String, String, Option<String>, Option<String>)>> {
        let deltas = self.run(py, |reader| {
            async move {
                reader
                    .state_deltas(&room_id, from_token.as_ref(), to_token.as_ref())
                    .await
            }
            .boxed()
        })?;
        Ok(deltas
            .into_iter()
            .map(|delta| {
                (
                    delta.stream_id,
                    delta.event_type.to_string(),
                    delta.state_key.to_string(),
                    delta.event_id.map(|id| id.to_string()),
                    delta.prev_event_id.map(|id| id.to_string()),
                )
            })
            .collect())
    }

    /// `get_current_state_deltas_for_room`, with each delta as
    /// `(stream_id, type, state_key, event_id, prev_event_id)`.
    #[pyo3(signature = (room_id, from_token, to_token))]
    #[allow(clippy::type_complexity)]
    fn state_deltas_by_stream_id(
        &self,
        py: Python<'_>,
        room_id: String,
        from_token: Option<RoomStreamToken>,
        to_token: Option<RoomStreamToken>,
    ) -> PyResult<Vec<(i64, String, String, Option<String>, Option<String>)>> {
        let deltas = self.run(py, |reader| {
            async move {
                reader
                    .state_deltas_by_stream_id(&room_id, from_token.as_ref(), to_token.as_ref())
                    .await
            }
            .boxed()
        })?;
        Ok(deltas
            .into_iter()
            .map(|delta| {
                (
                    delta.stream_id,
                    delta.event_type.to_string(),
                    delta.state_key.to_string(),
                    delta.event_id.map(|id| id.to_string()),
                    delta.prev_event_id.map(|id| id.to_string()),
                )
            })
            .collect())
    }

    /// `_get_state_group_for_events`.
    fn state_group_for_events(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<HashMap<String, i64>> {
        let ids = arcs(event_ids);
        self.run(py, |reader| {
            async move { reader.state_group_for_events(&ids).await }.boxed()
        })
        .map(arc_map)
    }

    /// `get_state_ids_for_event`.
    fn state_ids_for_event<'py>(
        &self,
        py: Python<'py>,
        event_id: String,
        state_filter: StateFilter,
    ) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let state = self.run(py, |reader| {
            async move { reader.state_ids_for_event(&event_id, &state_filter).await }.boxed()
        })?;
        state_map_to_py(py, &state)
    }

    /// `get_state_after_event`.
    fn state_after_event<'py>(
        &self,
        py: Python<'py>,
        event_id: String,
        state_filter: StateFilter,
    ) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let state = self.run(py, |reader| {
            async move { reader.state_after_event(&event_id, &state_filter).await }.boxed()
        })?;
        state_map_to_py(py, &state)
    }

    /// `get_state_ids_at`, with the room part of the stream position.
    fn state_ids_at<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        token: RoomStreamToken,
        state_filter: StateFilter,
    ) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let state = self.run(py, |reader| {
            async move { reader.state_ids_at(&room_id, &token, &state_filter).await }.boxed()
        })?;
        state_map_to_py(py, &state)
    }
}

fn parse_behaviour(name: &str) -> PyResult<RedactBehaviour> {
    RedactBehaviour::parse(name).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(format!("unknown redact behaviour {name:?}"))
    })
}

fn parse_direction(name: &str) -> PyResult<Direction> {
    Direction::parse(name).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(format!("unknown direction {name:?}"))
    })
}

fn events_to_py(py: Python<'_>, events: Vec<Event>) -> PyResult<Vec<Py<Event>>> {
    events.into_iter().map(|event| Py::new(py, event)).collect()
}

pub fn register_test_classes(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PySyncStoreForTest>()
}
