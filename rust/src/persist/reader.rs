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

//! What persisting reads before its write transaction, through the native
//! Postgres driver.
//!
//! A [`PersistReader`] is a [`SyncReader`] (for the state, events and
//! positions the sync store already reads) plus the reads the persist
//! controller needs on top. Each is a port of the store method it is named
//! after, and `tests/storage/test_persist_reader.py` runs both against the
//! same database.
//!
//! None of these reads is cached. Python caches a few of them
//! (`get_latest_event_ids_in_room`, `have_seen_event`,
//! `get_room_version_id`), but each runs once per chunk of up to 100
//! events, and a cache would need invalidating from both languages.
//!
//! The chain cover reads are free functions over a [`Querier`] in place of
//! reader methods. The chain cover step reads, allocates chain IDs and
//! writes `event_auth_chain_to_calculate` in one transaction, so they run
//! on that transaction's connection.

use std::{
    collections::{HashMap, HashSet},
    ops::Deref,
    sync::Arc,
};

use anyhow::Context;
use futures::{future::BoxFuture, FutureExt};
use pyo3::{prelude::*, types::PyDict};
use serde::Deserialize;
use serde_json::Value;
use tokio::runtime::Handle;
use tokio_postgres::Row;

use super::{
    chain_cover,
    context::PersistContext,
    extremities::{self, NewEvent},
    metrics::PersistCounts,
    python::persist_error_to_py,
    sliding_sync,
    state_delta::{self, DeltaState, NativeStateResolver, StateAndExtremities},
    sticky,
};
use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::events::Event;
use crate::state::{ConflictCache, EventMap, StateKey, StateMap, StateResEvent};
use crate::storage::caches::StoreCaches;
use crate::storage::events::Querier;
use crate::storage::sync_store::{
    PersistedEventPosition, SyncReader, SyncStore, SELECT_MANY_BATCH_SIZE,
};
use crate::types::state_filter::state_map_to_py;

/// How many events `_get_events_which_are_prevs` and
/// `_get_prevs_before_rejected` handle per transaction.
const PREVS_BATCH_SIZE: usize = 100;

/// `chain_links` fetches the links of this many chains per statement, as
/// `_get_chain_links` does.
const CHAIN_LINKS_BATCH_SIZE: usize = 1000;

/// `EventIdMembership`: the user and membership of a membership event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EventIdMembership {
    pub user_id: Arc<str>,
    pub membership: Arc<str>,
}

/// A row of the chain cover step's auth event query: an auth event's type
/// and state key, and its `(chain ID, sequence number)` if it has one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthEventChain {
    pub event_id: Arc<str>,
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
    pub chain: Option<(i64, i64)>,
}

/// A row of `event_auth_chain_links`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ChainLink {
    pub origin_chain_id: i64,
    pub origin_sequence_number: i64,
    pub target_chain_id: i64,
    pub target_sequence_number: i64,
}

/// A row of `event_auth_chain_to_calculate`, less the room.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChainToCalculate {
    pub event_id: Arc<str>,
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
}

/// The reads of one persist step. See the module docs.
pub struct PersistReader<'a> {
    sync: SyncReader<'a>,
}

impl<'a> Deref for PersistReader<'a> {
    type Target = SyncReader<'a>;

    fn deref(&self) -> &SyncReader<'a> {
        &self.sync
    }
}

impl<'a> PersistReader<'a> {
    pub fn new(sync: SyncReader<'a>) -> Self {
        PersistReader { sync }
    }

    /// `get_latest_event_ids_in_room`: the room's forward extremities.
    pub async fn latest_event_ids_in_room(
        &self,
        room_id: &str,
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        let rows = self
            .query_one_shot(
                "SELECT event_id FROM event_forward_extremities WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        first_column_set(&rows)
    }

    /// `get_state_dag_extremities`: the room's MSC4242 state DAG forward
    /// extremities.
    pub async fn state_dag_extremities(&self, room_id: &str) -> anyhow::Result<HashSet<Arc<str>>> {
        let rows = self
            .query_one_shot(
                "SELECT event_id FROM msc4242_state_dag_forward_extremities WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        first_column_set(&rows)
    }

    /// `_get_events_which_are_prevs`: those of the events that are a prev
    /// event of a persisted event that is not an outlier, rejected or soft
    /// failed. An event appears once per such child, as in Python.
    pub async fn events_which_are_prevs(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<Vec<Arc<str>>> {
        let mut results = Vec::new();
        if event_ids.is_empty() {
            return Ok(results);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(PREVS_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT prev_event_id, internal_metadata
                    FROM event_edges
                        INNER JOIN events USING (event_id)
                        LEFT JOIN rejections USING (event_id)
                        LEFT JOIN event_json USING (event_id)
                    WHERE
                        NOT events.outlier
                        AND rejections.event_id IS NULL
                        AND prev_event_id = ANY($1)",
                    &[&ids],
                )
                .await?;
            for row in rows {
                let metadata: Option<String> = row.try_get(1)?;
                if !is_soft_failed(metadata.as_deref())? {
                    let prev_event_id: String = row.try_get(0)?;
                    results.push(Arc::from(prev_event_id));
                }
            }
        }
        Ok(results)
    }

    /// `_get_prevs_before_rejected`: the soft failed and rejected events
    /// among the prev events of `event_ids`, and their prev events, walking
    /// up the DAG until it finds none. With `include_soft_failed` false,
    /// only rejected events count.
    ///
    /// Each batch walks in one snapshot, as each Python transaction does.
    pub async fn prevs_before_rejected(
        &self,
        event_ids: &[Arc<str>],
        include_soft_failed: bool,
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        let mut existing_prevs: HashSet<Arc<str>> = HashSet::new();
        for batch in event_ids.chunks(PREVS_BATCH_SIZE) {
            let txn = self.begin().await?;
            let mut to_check: Vec<Arc<str>> = batch.to_vec();
            while !to_check.is_empty() {
                let ids: Vec<&str> = to_check.iter().map(|id| &**id).collect();
                let rows = txn
                    .query(
                        "SELECT
                            prev_event_id, internal_metadata,
                            rejections.event_id IS NOT NULL
                        FROM event_edges
                            INNER JOIN events USING (event_id)
                            LEFT JOIN rejections USING (event_id)
                            LEFT JOIN event_json USING (event_id)
                        WHERE
                            NOT events.outlier
                            AND event_id = ANY($1)",
                        &[&ids],
                    )
                    .await?;
                to_check = Vec::new();
                for row in rows {
                    let prev_event_id: String = row.try_get(0)?;
                    if existing_prevs.contains(prev_event_id.as_str()) {
                        continue;
                    }
                    let metadata: Option<String> = row.try_get(1)?;
                    let rejected: bool = row.try_get(2)?;
                    if (include_soft_failed && is_soft_failed(metadata.as_deref())?) || rejected {
                        let prev_event_id: Arc<str> = Arc::from(prev_event_id);
                        existing_prevs.insert(Arc::clone(&prev_event_id));
                        to_check.push(prev_event_id);
                    }
                }
            }
            txn.commit().await?;
        }
        Ok(existing_prevs)
    }

    /// `have_seen_events`: which of the events are in `events`, in any
    /// room.
    pub async fn have_seen_events(
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
                    "SELECT event_id FROM events WHERE event_id = ANY($1)",
                    &[&ids],
                )
                .await?;
            found.extend(first_column_set(&rows)?);
        }
        Ok(found)
    }

    /// `get_membership_from_event_ids`, less the events that are not
    /// membership events (which Python maps to `None`).
    pub async fn membership_from_event_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, EventIdMembership>> {
        let mut memberships = HashMap::new();
        if event_ids.is_empty() {
            return Ok(memberships);
        }
        let borrowed = self.borrow().await?;
        for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
            let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = borrowed
                .query(
                    "SELECT user_id, membership, event_id FROM room_memberships
                    WHERE event_id = ANY($1)",
                    &[&ids],
                )
                .await?;
            for row in rows {
                let user_id: String = row.try_get(0)?;
                let membership: String = row.try_get(1)?;
                let event_id: String = row.try_get(2)?;
                memberships.insert(
                    Arc::from(event_id),
                    EventIdMembership {
                        user_id: Arc::from(user_id),
                        membership: Arc::from(membership),
                    },
                );
            }
        }
        Ok(memberships)
    }

    /// `is_local_host_in_room_ignoring_users`: whether a local user other
    /// than those in `ignore_users` is joined to the room.
    pub async fn is_local_host_in_room_ignoring_users(
        &self,
        room_id: &str,
        ignore_users: &[Arc<str>],
    ) -> anyhow::Result<bool> {
        let ignore: Vec<&str> = ignore_users.iter().map(|id| &**id).collect();
        let rows = self
            .query_one_shot(
                "SELECT 1 FROM local_current_membership
                WHERE
                    room_id = $1 AND membership = 'join'
                    AND NOT (user_id = ANY($2))
                LIMIT 1",
                &[&room_id, &ignore],
            )
            .await?;
        Ok(!rows.is_empty())
    }

    /// `get_room_version_id`, with `None` where Python raises
    /// `NotFoundError`.
    pub async fn room_version_id(&self, room_id: &str) -> anyhow::Result<Option<Arc<str>>> {
        let rows = self
            .query_one_shot(
                "SELECT room_version FROM rooms WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        let Some(row) = rows.first() else {
            return Ok(None);
        };
        let version: Option<String> = row.try_get(0)?;
        Ok(version.map(Arc::from))
    }

    /// Whether the room has a `sliding_sync_joined_rooms` row, as
    /// `_calculate_sliding_sync_table_changes` checks.
    pub async fn has_sliding_sync_joined_room(&self, room_id: &str) -> anyhow::Result<bool> {
        let rows = self
            .query_one_shot(
                "SELECT room_id FROM sliding_sync_joined_rooms WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        Ok(!rows.is_empty())
    }

    /// `get_last_event_pos_in_room`: the last event in the room that is not
    /// an outlier or rejected, of one of `event_types` if given and not
    /// empty, and where it was persisted.
    pub async fn last_event_pos_in_room(
        &self,
        room_id: &str,
        event_types: Option<&[&str]>,
    ) -> anyhow::Result<Option<(Arc<str>, PersistedEventPosition)>> {
        let rows = match event_types {
            Some(types) if !types.is_empty() => {
                self.query_one_shot(
                    "SELECT event_id, stream_ordering, instance_name
                    FROM events
                    LEFT JOIN rejections USING (event_id)
                    WHERE room_id = $1
                        AND type = ANY($2)
                        AND NOT outlier
                        AND rejections.event_id IS NULL
                    ORDER BY stream_ordering DESC
                    LIMIT 1",
                    &[&room_id, &types],
                )
                .await?
            }
            _ => {
                self.query_one_shot(
                    "SELECT event_id, stream_ordering, instance_name
                    FROM events
                    LEFT JOIN rejections USING (event_id)
                    WHERE room_id = $1
                        AND NOT outlier
                        AND rejections.event_id IS NULL
                    ORDER BY stream_ordering DESC
                    LIMIT 1",
                    &[&room_id],
                )
                .await?
            }
        };
        let Some(row) = rows.first() else {
            return Ok(None);
        };
        let event_id: String = row.try_get(0)?;
        let stream: i64 = row.try_get(1)?;
        let instance_name: Option<String> = row.try_get(2)?;
        Ok(Some((
            Arc::from(event_id),
            PersistedEventPosition {
                // A null instance name is a row from before sharded persisters.
                // Python's `instance_name or "master"` maps "" the same way.
                instance_name: Arc::from(
                    instance_name
                        .as_deref()
                        .filter(|name| !name.is_empty())
                        .unwrap_or("master"),
                ),
                stream,
            },
        )))
    }

    /// `_get_soft_failed_sticky_events_txn`: the room's sticky events that
    /// are soft failed and expire after `now_ms`, sent by one of `senders`
    /// if given.
    pub async fn soft_failed_sticky_events(
        &self,
        room_id: &str,
        senders: Option<&[Arc<str>]>,
        now_ms: i64,
    ) -> anyhow::Result<Vec<Arc<str>>> {
        let rows = match senders {
            Some([]) => return Ok(Vec::new()),
            Some(senders) => {
                let senders: Vec<&str> = senders.iter().map(|id| &**id).collect();
                self.query_one_shot(
                    "SELECT se.event_id
                    FROM sticky_events se
                    INNER JOIN event_json ej USING (event_id)
                    WHERE
                        se.room_id = $1
                        AND $2 < se.expires_at
                        AND COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)
                        AND se.sender = ANY($3)",
                    &[&room_id, &now_ms, &senders],
                )
                .await?
            }
            None => {
                self.query_one_shot(
                    "SELECT se.event_id
                    FROM sticky_events se
                    INNER JOIN event_json ej USING (event_id)
                    WHERE
                        se.room_id = $1
                        AND $2 < se.expires_at
                        AND COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)",
                    &[&room_id, &now_ms],
                )
                .await?
            }
        };
        first_column_vec(&rows)
    }
}

// The chain cover reads. See the module docs.

/// Whether the chain cover index is kept for the room, as
/// `calculate_chain_cover_index_for_events_txn` checks: not for an unknown
/// room, nor for a legacy room marked `false`. A null counts as kept.
pub async fn has_auth_chain_index(q: &dyn Querier, room_id: &str) -> anyhow::Result<bool> {
    let rows = q
        .query(
            "SELECT has_auth_chain_index FROM rooms WHERE room_id = $1",
            &[&room_id],
        )
        .await?;
    let Some(row) = rows.first() else {
        return Ok(false);
    };
    let has_index: Option<bool> = row.try_get(0)?;
    Ok(has_index != Some(false))
}

/// Which of the events already have a chain ID.
pub async fn events_with_chain_ids(
    q: &dyn Querier,
    event_ids: &[Arc<str>],
) -> anyhow::Result<HashSet<Arc<str>>> {
    let mut found = HashSet::new();
    for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
        let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
        let rows = q
            .query(
                "SELECT event_id FROM event_auth_chains WHERE event_id = ANY($1)",
                &[&ids],
            )
            .await?;
        found.extend(first_column_set(&rows)?);
    }
    Ok(found)
}

/// The `event_auth_chain_to_calculate` rows of the rooms: out of band
/// memberships whose chains were put off until their auth events arrived.
pub async fn chains_to_calculate(
    q: &dyn Querier,
    room_ids: &[&str],
) -> anyhow::Result<Vec<ChainToCalculate>> {
    if room_ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows = q
        .query(
            "SELECT event_id, type, state_key FROM event_auth_chain_to_calculate
            WHERE room_id = ANY($1)",
            &[&room_ids],
        )
        .await?;
    rows.iter()
        .map(|row| {
            Ok(ChainToCalculate {
                event_id: Arc::from(row.try_get::<_, String>(0)?),
                event_type: Arc::from(row.try_get::<_, String>(1)?),
                state_key: Arc::from(row.try_get::<_, String>(2)?),
            })
        })
        .collect()
}

/// The auth event IDs of a persisted event, from `event_auth`.
pub async fn auth_event_ids(q: &dyn Querier, event_id: &str) -> anyhow::Result<Vec<Arc<str>>> {
    let rows = q
        .query(
            "SELECT auth_id FROM event_auth WHERE event_id = $1",
            &[&event_id],
        )
        .await?;
    first_column_vec(&rows)
}

/// The type, state key and chain position of persisted state events, for
/// the auth events of the events being persisted. Events that are not
/// persisted state events are left out.
pub async fn auth_event_chains(
    q: &dyn Querier,
    event_ids: &[Arc<str>],
) -> anyhow::Result<Vec<AuthEventChain>> {
    let mut chains = Vec::with_capacity(event_ids.len());
    for batch in event_ids.chunks(SELECT_MANY_BATCH_SIZE) {
        let ids: Vec<&str> = batch.iter().map(|id| &**id).collect();
        let rows = q
            .query(
                "SELECT event_id, events.type, se.state_key, chain_id, sequence_number
                FROM events
                INNER JOIN state_events AS se USING (event_id)
                LEFT JOIN event_auth_chains USING (event_id)
                WHERE event_id = ANY($1)",
                &[&ids],
            )
            .await?;
        for row in rows {
            let chain_id: Option<i64> = row.try_get(3)?;
            let sequence_number: Option<i64> = row.try_get(4)?;
            chains.push(AuthEventChain {
                event_id: Arc::from(row.try_get::<_, String>(0)?),
                event_type: Arc::from(row.try_get::<_, String>(1)?),
                state_key: Arc::from(row.try_get::<_, String>(2)?),
                chain: chain_id.zip(sequence_number),
            });
        }
    }
    Ok(chains)
}

/// `_get_chain_links`: the links from the chains, and from the chains they
/// reach, recursively. May return links not reachable from them.
pub async fn chain_links(
    q: &dyn Querier,
    chains: impl IntoIterator<Item = i64>,
) -> anyhow::Result<Vec<ChainLink>> {
    let mut to_fetch: HashSet<i64> = chains.into_iter().collect();
    let mut links = Vec::new();
    while !to_fetch.is_empty() {
        let batch: Vec<i64> = to_fetch
            .iter()
            .copied()
            .take(CHAIN_LINKS_BATCH_SIZE)
            .collect();
        for chain_id in &batch {
            to_fetch.remove(chain_id);
        }
        let rows = q
            .query(
                "WITH RECURSIVE links(chain_id) AS (
                    SELECT
                        DISTINCT origin_chain_id
                    FROM event_auth_chain_links WHERE origin_chain_id = ANY($1)
                    UNION
                    SELECT
                        target_chain_id
                    FROM event_auth_chain_links
                    INNER JOIN links ON (chain_id = origin_chain_id)
                )
                SELECT
                    origin_chain_id, origin_sequence_number,
                    target_chain_id, target_sequence_number
                FROM links
                INNER JOIN event_auth_chain_links ON (chain_id = origin_chain_id)",
                &[&batch],
            )
            .await?;
        for row in rows {
            let link = ChainLink {
                origin_chain_id: row.try_get(0)?,
                origin_sequence_number: row.try_get(1)?,
                target_chain_id: row.try_get(2)?,
                target_sequence_number: row.try_get(3)?,
            };
            // A chain reached from this batch needs no fetch of its own.
            to_fetch.remove(&link.origin_chain_id);
            links.push(link);
        }
    }
    Ok(links)
}

/// The highest sequence number allocated on each of the chains, for
/// `_allocate_chain_ids`.
pub async fn max_sequence_numbers(
    q: &dyn Querier,
    chain_ids: &[i64],
) -> anyhow::Result<HashMap<i64, i64>> {
    if chain_ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = q
        .query(
            "SELECT chain_id, MAX(sequence_number) FROM event_auth_chains
            WHERE chain_id = ANY($1)
            GROUP BY chain_id",
            &[&chain_ids],
        )
        .await?;
    rows.iter()
        .map(|row| Ok((row.try_get(0)?, row.try_get(1)?)))
        .collect()
}

/// The first column of each row, as strings.
fn first_column_vec(rows: &[Row]) -> anyhow::Result<Vec<Arc<str>>> {
    rows.iter()
        .map(|row| Ok(Arc::from(row.try_get::<_, String>(0)?)))
        .collect()
}

fn first_column_set(rows: &[Row]) -> anyhow::Result<HashSet<Arc<str>>> {
    rows.iter()
        .map(|row| Ok(Arc::from(row.try_get::<_, String>(0)?)))
        .collect()
}

/// Whether an `event_json.internal_metadata` value marks the event soft
/// failed: `db_to_json(metadata).get("soft_failed")` is truthy.
fn is_soft_failed(metadata: Option<&str>) -> anyhow::Result<bool> {
    #[derive(Deserialize)]
    struct Metadata {
        #[serde(default)]
        soft_failed: Value,
    }
    // Python's `db_to_json(None)` raises here, so a missing `event_json`
    // row fails the persist rather than counting the event as live.
    let metadata = metadata.context("event has no internal metadata")?;
    let metadata: Metadata = serde_json::from_str(metadata)?;
    Ok(is_truthy(&metadata.soft_failed))
}

/// Python's truth value of a JSON value.
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

// Test-only entry points, so trial can compare the reads against the Python
// store on a real schema.

fn arcs(ids: Vec<String>) -> Vec<Arc<str>> {
    ids.into_iter().map(Arc::from).collect()
}

fn strings(ids: impl IntoIterator<Item = Arc<str>>) -> HashSet<String> {
    ids.into_iter().map(|id| id.to_string()).collect()
}

/// The persist reads, each run to completion on the tokio runtime with the
/// GIL released, over the native driver's pool and the persister's sync
/// store. For tests only.
#[pyclass(name = "PersistReaderForTest")]
pub struct PyPersistReaderForTest {
    store: Arc<SyncStore>,
    pool: ConnectionPool,
    handle: Handle,
    caches: Arc<StoreCaches>,
    conflict_cache: Arc<ConflictCache>,
}

impl PyPersistReaderForTest {
    /// Run `f` over a reader, blocking until it is done.
    fn run<'a, T, F>(&'a self, py: Python<'_>, f: F) -> PyResult<T>
    where
        T: Send + 'a,
        F: FnOnce(PersistReader<'a>) -> BoxFuture<'a, anyhow::Result<T>> + Send,
    {
        py.detach(|| {
            self.handle
                .block_on(f(PersistReader::new(self.store.reader(self.pool.clone()))))
        })
        .map_err(persist_error_to_py)
    }

    /// Run one of the two orchestrating steps.
    fn run_orchestration(
        &self,
        py: Python<'_>,
        room_id: String,
        server_name: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        now_ms: i64,
        state_dag: bool,
    ) -> PyResult<(StateAndExtremities, PersistCounts)> {
        let (events, event_map) = chunk(py, &events_and_contexts)?;
        let resolver = self.resolver();
        self.run(py, |reader| {
            async move {
                let mut counts = PersistCounts::default();
                let result = if state_dag {
                    state_delta::process_state_dag_forward_extremities_and_state_delta(
                        &reader,
                        &resolver,
                        &room_id,
                        &server_name,
                        &events,
                        &event_map,
                        now_ms,
                        &mut counts,
                    )
                    .await?
                } else {
                    state_delta::calculate_new_forward_extremities_and_state_delta(
                        &reader,
                        &resolver,
                        &room_id,
                        &server_name,
                        &events,
                        &event_map,
                        now_ms,
                        &mut counts,
                    )
                    .await?
                };
                Ok((result, counts))
            }
            .boxed()
        })
    }

    /// A state resolver over the same pool.
    fn resolver(&self) -> NativeStateResolver {
        NativeStateResolver {
            pool: self.pool.clone(),
            handle: self.handle.clone(),
            caches: Arc::clone(&self.caches),
            conflict_cache: Arc::clone(&self.conflict_cache),
        }
    }
}

#[pymethods]
impl PyPersistReaderForTest {
    #[new]
    fn new(pool: &Bound<'_, PyConnectionPool>, persister: &super::python::Persister) -> Self {
        let pool = pool.get();
        PyPersistReaderForTest {
            store: Arc::clone(&persister.inner.sync),
            pool: pool.pool.clone(),
            handle: pool.handle.clone(),
            caches: Arc::clone(&persister.inner.store.caches),
            conflict_cache: Arc::clone(&persister.inner.conflict_cache),
        }
    }

    fn latest_event_ids_in_room(
        &self,
        py: Python<'_>,
        room_id: String,
    ) -> PyResult<HashSet<String>> {
        self.run(py, |reader| {
            async move { reader.latest_event_ids_in_room(&room_id).await }.boxed()
        })
        .map(strings)
    }

    fn state_dag_extremities(&self, py: Python<'_>, room_id: String) -> PyResult<HashSet<String>> {
        self.run(py, |reader| {
            async move { reader.state_dag_extremities(&room_id).await }.boxed()
        })
        .map(strings)
    }

    fn events_which_are_prevs(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<Vec<String>> {
        let ids = arcs(event_ids);
        let prevs = self.run(py, |reader| {
            async move { reader.events_which_are_prevs(&ids).await }.boxed()
        })?;
        Ok(prevs.iter().map(|id| id.to_string()).collect())
    }

    fn prevs_before_rejected(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
        include_soft_failed: bool,
    ) -> PyResult<HashSet<String>> {
        let ids = arcs(event_ids);
        self.run(py, |reader| {
            async move {
                reader
                    .prevs_before_rejected(&ids, include_soft_failed)
                    .await
            }
            .boxed()
        })
        .map(strings)
    }

    fn have_seen_events(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<HashSet<String>> {
        let ids = arcs(event_ids);
        self.run(py, |reader| {
            async move { reader.have_seen_events(&ids).await }.boxed()
        })
        .map(strings)
    }

    /// Event ID to `(user_id, membership)`.
    fn membership_from_event_ids(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<HashMap<String, (String, String)>> {
        let ids = arcs(event_ids);
        let memberships = self.run(py, |reader| {
            async move { reader.membership_from_event_ids(&ids).await }.boxed()
        })?;
        Ok(memberships
            .into_iter()
            .map(|(event_id, m)| {
                (
                    event_id.to_string(),
                    (m.user_id.to_string(), m.membership.to_string()),
                )
            })
            .collect())
    }

    fn is_local_host_in_room_ignoring_users(
        &self,
        py: Python<'_>,
        room_id: String,
        ignore_users: Vec<String>,
    ) -> PyResult<bool> {
        let ignore = arcs(ignore_users);
        self.run(py, |reader| {
            async move {
                reader
                    .is_local_host_in_room_ignoring_users(&room_id, &ignore)
                    .await
            }
            .boxed()
        })
    }

    fn room_version_id(&self, py: Python<'_>, room_id: String) -> PyResult<Option<String>> {
        let version = self.run(py, |reader| {
            async move { reader.room_version_id(&room_id).await }.boxed()
        })?;
        Ok(version.map(|v| v.to_string()))
    }

    fn has_sliding_sync_joined_room(&self, py: Python<'_>, room_id: String) -> PyResult<bool> {
        self.run(py, |reader| {
            async move { reader.has_sliding_sync_joined_room(&room_id).await }.boxed()
        })
    }

    /// `(event_id, instance_name, stream)`.
    #[pyo3(signature = (room_id, event_types=None))]
    fn last_event_pos_in_room(
        &self,
        py: Python<'_>,
        room_id: String,
        event_types: Option<Vec<String>>,
    ) -> PyResult<Option<(String, String, i64)>> {
        let last = self.run(py, |reader| {
            async move {
                let types: Option<Vec<&str>> = event_types
                    .as_ref()
                    .map(|types| types.iter().map(String::as_str).collect());
                reader
                    .last_event_pos_in_room(&room_id, types.as_deref())
                    .await
            }
            .boxed()
        })?;
        Ok(last.map(|(event_id, pos)| {
            (
                event_id.to_string(),
                pos.instance_name.to_string(),
                pos.stream,
            )
        }))
    }

    #[pyo3(signature = (room_id, senders, now_ms))]
    fn soft_failed_sticky_events(
        &self,
        py: Python<'_>,
        room_id: String,
        senders: Option<Vec<String>>,
        now_ms: i64,
    ) -> PyResult<Vec<String>> {
        let senders = senders.map(arcs);
        let events = self.run(py, |reader| {
            async move {
                reader
                    .soft_failed_sticky_events(&room_id, senders.as_deref(), now_ms)
                    .await
            }
            .boxed()
        })?;
        Ok(events.iter().map(|id| id.to_string()).collect())
    }

    // The chain cover reads, each on a connection of its own.

    fn has_auth_chain_index(&self, py: Python<'_>, room_id: String) -> PyResult<bool> {
        self.run(py, |reader| {
            async move { has_auth_chain_index(&reader.borrow().await?, &room_id).await }.boxed()
        })
    }

    fn events_with_chain_ids(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<HashSet<String>> {
        let ids = arcs(event_ids);
        self.run(py, |reader| {
            async move { events_with_chain_ids(&reader.borrow().await?, &ids).await }.boxed()
        })
        .map(strings)
    }

    /// `(event_id, type, state_key)` rows.
    fn chains_to_calculate(
        &self,
        py: Python<'_>,
        room_ids: Vec<String>,
    ) -> PyResult<Vec<(String, String, String)>> {
        let rows = self.run(py, |reader| {
            async move {
                let rooms: Vec<&str> = room_ids.iter().map(String::as_str).collect();
                chains_to_calculate(&reader.borrow().await?, &rooms).await
            }
            .boxed()
        })?;
        Ok(rows
            .into_iter()
            .map(|r| {
                (
                    r.event_id.to_string(),
                    r.event_type.to_string(),
                    r.state_key.to_string(),
                )
            })
            .collect())
    }

    fn auth_event_ids(&self, py: Python<'_>, event_id: String) -> PyResult<Vec<String>> {
        let ids = self.run(py, |reader| {
            async move { auth_event_ids(&reader.borrow().await?, &event_id).await }.boxed()
        })?;
        Ok(ids.iter().map(|id| id.to_string()).collect())
    }

    /// `(event_id, type, state_key, chain_id, sequence_number)` rows.
    #[allow(clippy::type_complexity)]
    fn auth_event_chains(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<Vec<(String, String, String, Option<i64>, Option<i64>)>> {
        let ids = arcs(event_ids);
        let rows = self.run(py, |reader| {
            async move { auth_event_chains(&reader.borrow().await?, &ids).await }.boxed()
        })?;
        Ok(rows
            .into_iter()
            .map(|r| {
                (
                    r.event_id.to_string(),
                    r.event_type.to_string(),
                    r.state_key.to_string(),
                    r.chain.map(|(chain_id, _)| chain_id),
                    r.chain.map(|(_, seq)| seq),
                )
            })
            .collect())
    }

    /// `(origin_chain_id, origin_sequence_number, target_chain_id,
    /// target_sequence_number)` rows.
    fn chain_links(&self, py: Python<'_>, chains: Vec<i64>) -> PyResult<Vec<(i64, i64, i64, i64)>> {
        let links = self.run(py, |reader| {
            async move { chain_links(&reader.borrow().await?, chains).await }.boxed()
        })?;
        Ok(links
            .into_iter()
            .map(|l| {
                (
                    l.origin_chain_id,
                    l.origin_sequence_number,
                    l.target_chain_id,
                    l.target_sequence_number,
                )
            })
            .collect())
    }

    fn max_sequence_numbers(
        &self,
        py: Python<'_>,
        chain_ids: Vec<i64>,
    ) -> PyResult<HashMap<i64, i64>> {
        self.run(py, |reader| {
            async move { max_sequence_numbers(&reader.borrow().await?, &chain_ids).await }.boxed()
        })
    }

    /// The chain cover step, in a write transaction of its own. Event ID to
    /// `(chain_id, sequence_number, links)`.
    #[allow(clippy::type_complexity)]
    fn calculate_chain_cover_index_for_events(
        &self,
        py: Python<'_>,
        room_id: String,
        events: Vec<Py<Event>>,
    ) -> PyResult<HashMap<String, (i64, i64, Vec<(i64, i64)>)>> {
        let events: Vec<chain_cover::ChainCoverEvent> = events
            .iter()
            .filter_map(|event| chain_cover::ChainCoverEvent::new(event.bind(py).get()))
            .collect();
        let result = py
            .detach(|| {
                self.handle
                    .block_on(chain_cover::calculate_chain_cover_index_for_events(
                        &self.pool, &room_id, &events,
                    ))
            })
            .map_err(persist_error_to_py)?;
        Ok(result
            .into_iter()
            .map(|(event_id, links)| {
                (
                    event_id.to_string(),
                    (links.chain_id, links.sequence_number, links.links),
                )
            })
            .collect())
    }

    // The extremity calculations over the reader, each returning its result
    // and what it counted.

    fn calculate_new_extremities<'py>(
        &self,
        py: Python<'py>,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        latest_event_ids: Vec<String>,
    ) -> PyResult<(HashSet<String>, Bound<'py, PyDict>)> {
        let events = new_events(py, &events_and_contexts)?;
        let latest: HashSet<Arc<str>> = arcs(latest_event_ids).into_iter().collect();
        let (result, counts) = self.run(py, |reader| {
            async move {
                let mut counts = PersistCounts::default();
                let result =
                    extremities::calculate_new_extremities(&reader, &events, &latest, &mut counts)
                        .await?;
                Ok((result, counts))
            }
            .boxed()
        })?;
        Ok((strings(result), counts.to_py(py)?))
    }

    fn calculate_new_state_dag_extremities<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        existing: Vec<String>,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
    ) -> PyResult<(HashSet<String>, Bound<'py, PyDict>)> {
        let events = new_events(py, &events_and_contexts)?;
        let existing: HashSet<Arc<str>> = arcs(existing).into_iter().collect();
        let (result, counts) = self.run(py, |reader| {
            async move {
                let mut counts = PersistCounts::default();
                let result = extremities::calculate_new_state_dag_extremities(
                    &reader,
                    &room_id,
                    &existing,
                    &events,
                    &mut counts,
                )
                .await?;
                Ok((result, counts))
            }
            .boxed()
        })?;
        Ok((strings(result), counts.to_py(py)?))
    }

    #[allow(clippy::too_many_arguments)]
    fn prune_extremities<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        server_name: String,
        new_latest_event_ids: Vec<String>,
        resolved_state_group: i64,
        event_id_to_state_group: HashMap<String, i64>,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        now_ms: i64,
    ) -> PyResult<(HashSet<String>, Bound<'py, PyDict>)> {
        let events = new_events(py, &events_and_contexts)?;
        let latest: HashSet<Arc<str>> = arcs(new_latest_event_ids).into_iter().collect();
        let groups: HashMap<Arc<str>, i64> = event_id_to_state_group
            .into_iter()
            .map(|(id, group)| (Arc::from(id), group))
            .collect();
        let (result, counts) = self.run(py, |reader| {
            async move {
                let mut counts = PersistCounts::default();
                let result = extremities::prune_extremities(
                    &reader,
                    &room_id,
                    &server_name,
                    latest,
                    resolved_state_group,
                    &groups,
                    &events,
                    now_ms,
                    &mut counts,
                )
                .await?;
                Ok((result, counts))
            }
            .boxed()
        })?;
        Ok((strings(result), counts.to_py(py)?))
    }

    // The state delta over the reader and the native state resolver, each
    // returning its result and what it counted.

    /// `(current_state, delta_ids, new_latest_event_ids, counts)`.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn get_new_state_after_events<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        server_name: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        old_latest_event_ids: Vec<String>,
        new_latest_event_ids: Vec<String>,
        should_prune: bool,
        now_ms: i64,
    ) -> PyResult<(
        Option<Bound<'py, PyDict>>,
        Option<Bound<'py, PyDict>>,
        HashSet<String>,
        Bound<'py, PyDict>,
    )> {
        let (events, event_map) = chunk(py, &events_and_contexts)?;
        let old: HashSet<Arc<str>> = arcs(old_latest_event_ids).into_iter().collect();
        let new: HashSet<Arc<str>> = arcs(new_latest_event_ids).into_iter().collect();
        let resolver = self.resolver();
        let (result, counts) = self.run(py, |reader| {
            async move {
                let mut counts = PersistCounts::default();
                let result = state_delta::get_new_state_after_events(
                    &reader,
                    &resolver,
                    &room_id,
                    &server_name,
                    &events,
                    &event_map,
                    &old,
                    new,
                    should_prune,
                    now_ms,
                    &mut counts,
                )
                .await?;
                Ok((result, counts))
            }
            .boxed()
        })?;
        Ok((
            result
                .current_state
                .map(|state| state_map_to_py(py, &state))
                .transpose()?,
            result
                .delta_ids
                .map(|delta| state_map_to_py(py, &delta))
                .transpose()?,
            strings(result.new_latest_event_ids),
            counts.to_py(py)?,
        ))
    }

    /// `(to_delete, to_insert)`.
    #[allow(clippy::type_complexity)]
    fn calculate_state_delta<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        current_state: HashMap<(String, String), String>,
    ) -> PyResult<(Vec<(String, String)>, Bound<'py, PyDict>)> {
        let current_state = state_map(current_state);
        let delta = self.run(py, |reader| {
            async move {
                    state_delta::calculate_state_delta(&reader, &room_id, &current_state).await
                }
                .boxed()
        })?;
        let (to_delete, to_insert, _) = delta_to_py(py, &delta)?;
        Ok((to_delete, to_insert))
    }

    fn is_server_still_joined(
        &self,
        py: Python<'_>,
        room_id: String,
        server_name: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        to_delete: Vec<(String, String)>,
        to_insert: HashMap<(String, String), String>,
    ) -> PyResult<bool> {
        let (events, _) = chunk(py, &events_and_contexts)?;
        let delta = DeltaState {
            to_delete: to_delete
                .iter()
                .map(|(event_type, state_key)| StateKey::new(event_type, state_key))
                .collect(),
            to_insert: state_map(to_insert),
            no_longer_in_room: false,
        };
        self.run(py, |reader| {
            async move {
                state_delta::is_server_still_joined(
                    &reader,
                    &room_id,
                    &server_name,
                    &events,
                    &delta,
                )
                .await
            }
            .boxed()
        })
    }

    /// `(new_forward_extremities, delta, counts)`, the delta as
    /// `(to_delete, to_insert, no_longer_in_room)`.
    #[allow(clippy::type_complexity)]
    fn calculate_new_forward_extremities_and_state_delta<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        server_name: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        now_ms: i64,
    ) -> PyResult<(
        Option<HashSet<String>>,
        Option<PyDelta<'py>>,
        Bound<'py, PyDict>,
    )> {
        let (result, counts) =
            self.run_orchestration(py, room_id, server_name, events_and_contexts, now_ms, false)?;
        Ok((
            result.new_forward_extremities.map(strings),
            result
                .state_delta
                .map(|delta| delta_to_py(py, &delta))
                .transpose()?,
            counts.to_py(py)?,
        ))
    }

    /// `(new_forward_extremities, delta, new_state_dag_extremities, counts)`,
    /// the delta as `(to_delete, to_insert, no_longer_in_room)`.
    #[allow(clippy::type_complexity)]
    fn process_state_dag_forward_extremities_and_state_delta<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        server_name: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        now_ms: i64,
    ) -> PyResult<(
        Option<HashSet<String>>,
        Option<PyDelta<'py>>,
        Option<HashSet<String>>,
        Bound<'py, PyDict>,
    )> {
        let (result, counts) =
            self.run_orchestration(py, room_id, server_name, events_and_contexts, now_ms, true)?;
        Ok((
            result.new_forward_extremities.map(strings),
            result
                .state_delta
                .map(|delta| delta_to_py(py, &delta))
                .transpose()?,
            result.new_state_dag_extremities.map(strings),
            counts.to_py(py)?,
        ))
    }

    /// The sliding sync step (`rust/src/persist/sliding_sync.rs`), as
    /// `(bump_stamp_to_fully_insert, joined_room_updates,
    /// membership_snapshot_shared_insert_values,
    /// to_insert_membership_snapshots, to_delete_membership_snapshots)`.
    /// Each snapshot is `(user_id, sender, membership_event_id,
    /// membership)`.
    #[allow(clippy::type_complexity, clippy::too_many_arguments)]
    fn calculate_sliding_sync_table_changes<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        server_name: String,
        events: Vec<Py<Event>>,
        to_delete: Vec<(String, String)>,
        to_insert: HashMap<(String, String), String>,
        no_longer_in_room: bool,
    ) -> PyResult<(
        Option<i64>,
        Bound<'py, PyDict>,
        Bound<'py, PyDict>,
        Vec<(String, String, String, String)>,
        Vec<String>,
    )> {
        let events: Vec<sliding_sync::SlidingSyncEvent> = events
            .iter()
            .filter_map(|event| sliding_sync::SlidingSyncEvent::new(event.bind(py).get()))
            .collect();
        let delta = DeltaState {
            to_delete: to_delete
                .iter()
                .map(|(event_type, state_key)| StateKey::new(event_type, state_key))
                .collect(),
            to_insert: state_map(to_insert),
            no_longer_in_room,
        };
        let changes = self.run(py, |reader| {
            async move {
                sliding_sync::calculate_sliding_sync_table_changes(
                    &reader,
                    &room_id,
                    &server_name,
                    &events,
                    &delta,
                )
                .await
            }
            .boxed()
        })?;
        let shared =
            state_values_to_py(py, &changes.membership_snapshot_shared_insert_values.state)?;
        if let Some(has_known_state) = changes
            .membership_snapshot_shared_insert_values
            .has_known_state
        {
            shared.set_item("has_known_state", has_known_state)?;
        }
        Ok((
            changes.joined_room_bump_stamp_to_fully_insert,
            state_values_to_py(py, &changes.joined_room_updates)?,
            shared,
            changes
                .to_insert_membership_snapshots
                .iter()
                .map(|info| {
                    (
                        info.user_id.to_string(),
                        info.sender.to_string(),
                        info.membership_event_id.to_string(),
                        info.membership.to_string(),
                    )
                })
                .collect(),
            changes
                .to_delete_membership_snapshots
                .iter()
                .map(|user_id| user_id.to_string())
                .collect(),
        ))
    }

    /// The passing event IDs.
    #[allow(clippy::too_many_arguments)]
    fn compute_sticky_events_to_un_soft_fail(
        &self,
        py: Python<'_>,
        room_id: String,
        events: Vec<Py<Event>>,
        to_delete: Vec<(String, String)>,
        to_insert: HashMap<(String, String), String>,
        no_longer_in_room: bool,
        now_ms: i64,
    ) -> PyResult<HashSet<String>> {
        let events: Vec<Event> = events
            .iter()
            .map(|event| event.bind(py).get().shallow_clone())
            .collect();
        let delta = DeltaState {
            to_delete: to_delete
                .iter()
                .map(|(event_type, state_key)| StateKey::new(event_type, state_key))
                .collect(),
            to_insert: state_map(to_insert),
            no_longer_in_room,
        };
        let passing = self.run(py, |reader| {
            async move {
                sticky::compute_sticky_events_to_un_soft_fail(
                    &reader, &room_id, &events, &delta, now_ms,
                )
                .await
            }
            .boxed()
        })?;
        Ok(strings(passing))
    }
}

/// `SlidingSyncStateInsertValues` as a dict holding only the keys set.
fn state_values_to_py<'py>(
    py: Python<'py>,
    values: &sliding_sync::SlidingSyncStateValues,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    if let Some(room_type) = &values.room_type {
        dict.set_item("room_type", room_type.as_deref())?;
    }
    if let Some(is_encrypted) = values.is_encrypted {
        dict.set_item("is_encrypted", is_encrypted)?;
    }
    if let Some(room_name) = &values.room_name {
        dict.set_item("room_name", room_name.as_deref())?;
    }
    if let Some(successor) = &values.tombstone_successor_room_id {
        dict.set_item("tombstone_successor_room_id", successor.as_deref())?;
    }
    Ok(dict)
}

/// A delta as Python's test reads it: `(to_delete, to_insert,
/// no_longer_in_room)`.
type PyDelta<'py> = (Vec<(String, String)>, Bound<'py, PyDict>, bool);

fn delta_to_py<'py>(py: Python<'py>, delta: &DeltaState) -> PyResult<PyDelta<'py>> {
    Ok((
        delta
            .to_delete
            .iter()
            .map(|key| (key.0.to_string(), key.1.to_string()))
            .collect(),
        state_map_to_py(py, &delta.to_insert)?,
        delta.no_longer_in_room,
    ))
}

fn state_map(map: HashMap<(String, String), String>) -> StateMap {
    map.into_iter()
        .map(|((event_type, state_key), event_id)| {
            (StateKey::new(&event_type, &state_key), Arc::from(event_id))
        })
        .collect()
}

/// The chunk's events as the computation reads them, and as state
/// resolution's map of events in flight.
fn chunk(
    py: Python<'_>,
    events_and_contexts: &[(Py<Event>, PersistContext)],
) -> PyResult<(Vec<NewEvent>, EventMap)> {
    let events = new_events(py, events_and_contexts)?;
    let event_map = events_and_contexts
        .iter()
        .map(|(event, _)| {
            let event = event.bind(py).get();
            (
                Arc::from(event.event_id()),
                StateResEvent::new(event.shallow_clone()),
            )
        })
        .collect();
    Ok((events, event_map))
}

fn new_events(
    py: Python<'_>,
    events_and_contexts: &[(Py<Event>, PersistContext)],
) -> PyResult<Vec<NewEvent>> {
    events_and_contexts
        .iter()
        .map(|(event, context)| {
            NewEvent::new(event.bind(py).get(), context).map_err(persist_error_to_py)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn soft_failed_follows_python_truthiness() {
        assert!(is_soft_failed(None).is_err());
        assert!(!is_soft_failed(Some("{}")).unwrap());
        assert!(!is_soft_failed(Some(r#"{"soft_failed": false}"#)).unwrap());
        assert!(!is_soft_failed(Some(r#"{"soft_failed": null}"#)).unwrap());
        assert!(!is_soft_failed(Some(r#"{"soft_failed": 0}"#)).unwrap());
        assert!(is_soft_failed(Some(r#"{"soft_failed": true}"#)).unwrap());
        assert!(is_soft_failed(Some(r#"{"soft_failed": 1, "outlier": true}"#)).unwrap());
        assert!(is_soft_failed(Some("not json")).is_err());
    }
}
