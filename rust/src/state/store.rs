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

//! What state resolution reads from the database.
//!
//! [`StateResStore`] has two implementations: [`PostgresStateResStore`], which
//! reads through the native Postgres driver inside one read-only transaction,
//! and [`MemoryStateResStore`], which the algorithm's unit tests use.

use std::{
    collections::HashSet,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use anyhow::{bail, Context};
use async_trait::async_trait;
use log::{debug, error, warn};
use pyo3::{
    prelude::*,
    types::{PyDict, PyModule, PySet},
};
use tokio::runtime::Handle;
use tokio_postgres::{types::ToSql, Row};

use super::{auth_chain, lru::StateResCaches, EventMap, StateResEvent};
use crate::database::postgres::pool::{ConnectionPool, PooledConnection, PyConnectionPool};
use crate::events::{
    constants::event_type::M_ROOM_MEMBER, internal_metadata::EventInternalMetadata, Event,
};
use crate::room_versions::{EventFormatVersions, RoomVersion};

/// The result of an auth chain difference calculation.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct StateDifference {
    /// The event IDs in the auth difference.
    pub auth_difference: HashSet<Arc<str>>,
    /// The event IDs in the conflicted state subgraph. Used in v2.1 only.
    pub conflicted_subgraph: Option<HashSet<Arc<str>>>,
}

/// The room has no chain cover index, or the index is missing events. The
/// Rust implementation has no fallback for this, so the caller should resolve
/// with the Python implementation instead.
#[derive(Debug)]
pub struct NoChainCoverIndex(pub String);

impl std::fmt::Display for NoChainCoverIndex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Unexpectedly no chain cover for events in {}", self.0)
    }
}

impl std::error::Error for NoChainCoverIndex {}

/// Access to the database for state resolution.
///
/// The trait exists so the algorithm can run against an in-memory store in
/// unit tests.
#[async_trait]
pub trait StateResStore: Send + Sync {
    /// Fetch events by ID, including rejected ones. Events that are not found
    /// are left out of the result.
    async fn get_events(&self, ids: &[Arc<str>]) -> anyhow::Result<EventMap>;

    /// Port of `get_auth_chain_difference_extended`: the auth chain difference
    /// of the state sets, and for v2.1 (`conflicted` is set) the conflicted
    /// subgraph. All the events must be persisted.
    async fn get_auth_chain_difference(
        &self,
        room_id: &str,
        state_sets: &[HashSet<Arc<str>>],
        conflicted: Option<&HashSet<Arc<str>>>,
        additional_backwards: Option<&HashSet<Arc<str>>>,
    ) -> anyhow::Result<StateDifference>;
}

/// Database work done by a [`PostgresStateResStore`], for metrics.
#[derive(Debug, Default, Clone, Copy)]
pub struct DbStats {
    /// Statements executed.
    pub queries: u64,
    /// Rows returned.
    pub rows: u64,
    /// Events loaded from the database by [`StateResStore::get_events`].
    pub events_fetched: u64,
    /// Events [`StateResStore::get_events`] found in the event cache.
    pub events_cached: u64,
    /// Chain positions found in the chain position cache.
    pub chain_positions_cached: u64,
    /// Chains whose links came from the chain link cache.
    pub chain_links_cached: u64,
    /// Time spent waiting on statements.
    pub db_time: Duration,
    /// Time spent building events from rows: parsing the JSON and computing
    /// the event IDs.
    pub parse_time: Duration,
}

/// A [`StateResStore`] reading Postgres through the native driver.
///
/// Holds one pooled connection for its lifetime, inside a
/// `REPEATABLE READ READ ONLY` transaction, so every read sees one snapshot.
/// A read-only repeatable-read transaction cannot fail with a serialization
/// error or deadlock, so there is no retry loop.
///
/// Call [`PostgresStateResStore::finish`] to commit. If the store is dropped
/// without it, the transaction is rolled back on a background task before the
/// connection goes back to the pool.
pub struct PostgresStateResStore {
    conn: Option<PooledConnection>,
    handle: Handle,
    /// Events, chain positions and chain links loaded earlier, consulted
    /// before the database.
    caches: Option<Arc<StateResCaches>>,
    db_stats: Mutex<DbStats>,
}

/// How many event IDs to send per event-loading statement.
const EVENT_BATCH_SIZE: usize = 500;

/// SPIKE: build events with the row's event ID instead of recomputing it
/// from the canonical JSON.
const TRUST_DB_EVENT_ID: bool = std::option_env!("SYNAPSE_TRUST_DB_EVENT_ID").is_some();

const LOAD_EVENTS_SQL: &str = "
    SELECT e.event_id, ej.internal_metadata, ej.json, ej.format_version,
        r.room_version, rej.reason, e.outlier
    FROM events AS e
    JOIN event_json AS ej USING (event_id)
    LEFT JOIN rooms AS r ON r.room_id = e.room_id
    LEFT JOIN rejections AS rej USING (event_id)
    WHERE e.event_id = ANY($1)
";

impl PostgresStateResStore {
    /// Check a connection out of `pool` and open the read-only transaction.
    /// Reads consult `caches` first, and what they load from the database is
    /// added to it.
    pub async fn begin(
        pool: &ConnectionPool,
        handle: Handle,
        caches: Option<Arc<StateResCaches>>,
    ) -> anyhow::Result<Self> {
        let conn = pool
            .get()
            .await
            .map_err(|e| anyhow::anyhow!("checking out a database connection: {e}"))?;
        let store = PostgresStateResStore {
            conn: Some(conn),
            handle,
            caches,
            db_stats: Mutex::default(),
        };
        // If this fails, dropping the store sends a ROLLBACK, which is
        // harmless outside a transaction.
        let start = Instant::now();
        store
            .client()
            .batch_execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await
            .context("BEGIN")?;
        store.record(start, 0);
        Ok(store)
    }

    /// Commit the transaction and return the connection to the pool.
    pub async fn finish(mut self) -> anyhow::Result<DbStats> {
        let stats = self.stats();
        if let Some(conn) = self.conn.take() {
            conn.batch_execute("COMMIT").await.context("COMMIT")?;
        }
        Ok(stats)
    }

    pub fn stats(&self) -> DbStats {
        *self.db_stats.lock().expect("not poisoned")
    }

    pub(super) fn caches(&self) -> Option<&StateResCaches> {
        self.caches.as_deref()
    }

    /// Count cache hits for metrics.
    pub(super) fn record_cached(&self, positions: u64, chains: u64) {
        let mut stats = self.db_stats.lock().expect("not poisoned");
        stats.chain_positions_cached += positions;
        stats.chain_links_cached += chains;
    }

    fn client(&self) -> &PooledConnection {
        self.conn.as_ref().expect("connection is held until finish")
    }

    /// Record a statement started at `start` that returned `rows` rows.
    fn record(&self, start: Instant, rows: usize) {
        let mut stats = self.db_stats.lock().expect("not poisoned");
        stats.queries += 1;
        stats.rows += rows as u64;
        stats.db_time += start.elapsed();
    }

    /// Run a statement through the connection's prepared statement cache.
    pub(crate) async fn query(
        &self,
        sql: &str,
        params: &[&(dyn ToSql + Sync)],
    ) -> anyhow::Result<Vec<Row>> {
        let start = Instant::now();
        let client = self.client();
        let statement = client
            .prepare_cached(sql, false)
            .await
            .with_context(|| format!("preparing {}", sql.trim()))?;
        let rows = client
            .query(&statement, params)
            .await
            .with_context(|| format!("running {}", sql.trim()))?;
        self.record(start, rows.len());
        if log::log_enabled!(log::Level::Debug) {
            let head: String = sql.split_whitespace().take(6).collect::<Vec<_>>().join(" ");
            debug!(
                "State res query [{head}] returned {} rows in {:?}",
                rows.len(),
                start.elapsed()
            );
        }
        Ok(rows)
    }
}

impl Drop for PostgresStateResStore {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            // Roll back before the connection goes back to the pool, so the
            // next user doesn't find it inside our transaction.
            self.handle.spawn(async move {
                if let Err(e) = conn.batch_execute("ROLLBACK").await {
                    warn!("Failed to roll back state res transaction: {e}");
                }
                drop(conn);
            });
        }
    }
}

#[async_trait]
impl StateResStore for PostgresStateResStore {
    async fn get_events(&self, ids: &[Arc<str>]) -> anyhow::Result<EventMap> {
        let mut events = EventMap::with_capacity(ids.len());

        let event_cache = self.caches.as_ref().map(|caches| &caches.events);
        let missing: Vec<Arc<str>> = match event_cache {
            Some(cache) => {
                let mut missing = Vec::new();
                for id in ids {
                    match cache.get(id) {
                        Some(event) => {
                            events.insert(Arc::clone(id), event);
                        }
                        None => missing.push(Arc::clone(id)),
                    }
                }
                missing
            }
            None => ids.to_vec(),
        };
        let cached = events.len() as u64;

        for batch in missing.chunks(EVENT_BATCH_SIZE) {
            let batch: Vec<&str> = batch.iter().map(|id| &**id).collect();
            let rows = self.query(LOAD_EVENTS_SQL, &[&batch]).await?;
            let start = Instant::now();
            for row in rows {
                if let Some((event_id, event)) = event_from_row(&row)? {
                    let event = StateResEvent::new(event)?;
                    if let Some(cache) = event_cache {
                        cache.insert(Arc::clone(&event_id), event.clone(), 1);
                    }
                    events.insert(event_id, event);
                }
            }
            self.db_stats.lock().expect("not poisoned").parse_time += start.elapsed();
        }

        let mut stats = self.db_stats.lock().expect("not poisoned");
        stats.events_cached += cached;
        stats.events_fetched += events.len() as u64 - cached;
        Ok(events)
    }

    async fn get_auth_chain_difference(
        &self,
        room_id: &str,
        state_sets: &[HashSet<Arc<str>>],
        conflicted: Option<&HashSet<Arc<str>>>,
        additional_backwards: Option<&HashSet<Arc<str>>>,
    ) -> anyhow::Result<StateDifference> {
        let rows = self
            .query(
                "SELECT has_auth_chain_index FROM rooms WHERE room_id = $1",
                &[&room_id],
            )
            .await?;
        let has_index = match rows.first() {
            Some(row) => row.get::<_, Option<bool>>(0).unwrap_or(false),
            None => bail!("Unknown room {room_id}"),
        };
        if !has_index {
            return Err(NoChainCoverIndex(room_id.to_owned()).into());
        }

        auth_chain::get_auth_chain_difference_using_cover_index(
            self,
            room_id,
            state_sets,
            conflicted,
            additional_backwards,
        )
        .await
    }
}

/// Build an [`Event`] from a row of [`LOAD_EVENTS_SQL`], the way
/// `EventsWorkerStore._fetch_event_rows` and `_get_events_from_db` do.
///
/// Returns `None` for rows the Python skips: an unknown room version, an
/// event in the wrong format for its room, or JSON that does not parse.
fn event_from_row(row: &Row) -> anyhow::Result<Option<(Arc<str>, Event)>> {
    let event_id: String = row.try_get(0)?;
    let internal_metadata: String = row.try_get(1)?;
    let json: String = row.try_get(2)?;
    let format_version: Option<i32> = row.try_get(3)?;
    let room_version_id: Option<String> = row.try_get(4)?;
    let rejected_reason: Option<String> = row.try_get(5)?;
    let outlier: bool = row.try_get(6)?;

    // Events stored before Synapse had event format versions are v1.
    let format_version = format_version.unwrap_or(EventFormatVersions::ROOM_V1_V2);

    let room_version_id = room_version_id.filter(|id| !id.is_empty());
    let room_version: &'static RoomVersion = match room_version_id.as_deref() {
        // Only out-of-band membership events from before
        // matrix-org/synapse#6983 have no `rooms` entry. Those rooms are v5 or
        // earlier, which all redact the same way.
        None => match format_version {
            EventFormatVersions::ROOM_V1_V2 => &RoomVersion::V1,
            EventFormatVersions::ROOM_V3 => &RoomVersion::V3,
            _ => &RoomVersion::V5,
        },
        Some(id) => match id.parse::<&'static RoomVersion>() {
            Ok(room_version) => room_version,
            Err(_) => {
                warn!("Event {event_id} has unknown room version {id}");
                return Ok(None);
            }
        },
    };
    if room_version_id.is_some() && room_version.event_format != format_version {
        error!(
            "Event {event_id} with room version {room_version} has wrong format: expected {}, was {format_version}",
            room_version.event_format
        );
        return Ok(None);
    }

    let metadata = EventInternalMetadata::from_json_str(&internal_metadata)
        .with_context(|| format!("internal metadata of {event_id}"))?;
    metadata
        .set_outlier_flag(outlier)
        .map_err(|e| anyhow::anyhow!("{e}"))?;

    let rejected_reason = rejected_reason.map(String::into_boxed_str);
    let parsed = if TRUST_DB_EVENT_ID {
        Event::from_json_str_with_id(
            &json,
            Arc::from(&*event_id),
            room_version,
            metadata,
            rejected_reason,
        )
    } else {
        Event::from_json_str(&json, room_version, metadata, rejected_reason)
    };
    let event = match parsed {
        Ok(event) => event,
        Err(e) => {
            error!("Unable to parse event from database {event_id}: {e:#}");
            return Ok(None);
        }
    };

    if room_version_id.is_none() && event.type_() != M_ROOM_MEMBER {
        bail!("Room for event {event_id} is unknown");
    }

    // If the content was modified in the database, the computed event ID
    // will not match.
    if event.event_id() != event_id {
        bail!(
            "Database corruption: event {event_id} in room {} has event ID {}",
            event.room_id(),
            event.event_id()
        );
    }

    Ok(Some((Arc::from(event_id), event)))
}

/// A [`StateResStore`] over an in-memory map of persisted events, with the
/// auth chain difference computed by walking the auth events. Port of
/// `TestStateResolutionStore` in `tests/state/test_v2.py`.
#[derive(Default)]
pub struct MemoryStateResStore {
    pub events: EventMap,
}

impl MemoryStateResStore {
    pub fn new(events: EventMap) -> Self {
        MemoryStateResStore { events }
    }

    /// The auth chain of the events, including the events themselves.
    fn auth_chain(&self, event_ids: &HashSet<Arc<str>>) -> anyhow::Result<HashSet<Arc<str>>> {
        let mut result = HashSet::new();
        let mut stack: Vec<Arc<str>> = event_ids.iter().cloned().collect();
        while let Some(event_id) = stack.pop() {
            if !result.insert(Arc::clone(&event_id)) {
                continue;
            }
            let Some(event) = self.events.get(&event_id) else {
                bail!("Unknown event {event_id}");
            };
            stack.extend(event.auth_event_ids().iter().cloned());
        }
        Ok(result)
    }
}

#[async_trait]
impl StateResStore for MemoryStateResStore {
    async fn get_events(&self, ids: &[Arc<str>]) -> anyhow::Result<EventMap> {
        Ok(ids
            .iter()
            .filter_map(|id| Some((Arc::clone(id), self.events.get(id)?.clone())))
            .collect())
    }

    async fn get_auth_chain_difference(
        &self,
        _room_id: &str,
        state_sets: &[HashSet<Arc<str>>],
        _conflicted: Option<&HashSet<Arc<str>>>,
        _additional_backwards: Option<&HashSet<Arc<str>>>,
    ) -> anyhow::Result<StateDifference> {
        let chains = state_sets
            .iter()
            .map(|set| self.auth_chain(set))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let union: HashSet<Arc<str>> = chains.iter().flatten().cloned().collect();
        let difference = union
            .into_iter()
            .filter(|id| !chains.iter().all(|chain| chain.contains(id)))
            .collect();
        Ok(StateDifference {
            auth_difference: difference,
            conflicted_subgraph: Some(HashSet::new()),
        })
    }
}

// Test-only entry points, so trial can compare the Rust store against the
// Python one on a real schema.

fn pool_and_handle(pool: &Bound<'_, PyConnectionPool>) -> (ConnectionPool, Handle) {
    let pool = pool.get();
    (pool.pool.clone(), pool.handle.clone())
}

fn to_id_set(ids: Option<HashSet<String>>) -> Option<HashSet<Arc<str>>> {
    ids.map(|ids| ids.into_iter().map(Arc::from).collect())
}

fn py_set<'py>(py: Python<'py>, ids: &HashSet<Arc<str>>) -> PyResult<Bound<'py, PySet>> {
    PySet::new(py, ids.iter().map(|id| &**id))
}

fn anyhow_to_py(e: anyhow::Error) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("{e:#}"))
}

/// Compute the auth chain difference with the chain cover index, as
/// `_get_auth_chain_difference_using_cover_index_txn` does: without checking
/// the room's `has_auth_chain_index` first. With `handler`, reads go
/// through its chain caches.
///
/// Returns `(auth_difference, conflicted_subgraph)`. Blocks, releasing the
/// GIL. For tests only.
#[pyfunction]
#[pyo3(signature = (pool, room_id, state_sets, conflicted=None, additional_backwards=None, handler=None))]
fn get_auth_chain_difference_for_test<'py>(
    py: Python<'py>,
    pool: &Bound<'py, PyConnectionPool>,
    room_id: String,
    state_sets: Vec<HashSet<String>>,
    conflicted: Option<HashSet<String>>,
    additional_backwards: Option<HashSet<String>>,
    handler: Option<&Bound<'py, super::StateResolutionHandler>>,
) -> PyResult<(Bound<'py, PySet>, Option<Bound<'py, PySet>>)> {
    let (pool, handle) = pool_and_handle(pool);
    let caches = handler.map(|handler| handler.get().caches());
    let state_sets: Vec<HashSet<Arc<str>>> = state_sets
        .into_iter()
        .map(|set| set.into_iter().map(Arc::from).collect())
        .collect();
    let conflicted = to_id_set(conflicted);
    let additional_backwards = to_id_set(additional_backwards);

    let difference = py
        .detach(|| {
            handle.block_on(async {
                let store = PostgresStateResStore::begin(&pool, handle.clone(), caches).await?;
                let difference = auth_chain::get_auth_chain_difference_using_cover_index(
                    &store,
                    &room_id,
                    &state_sets,
                    conflicted.as_ref(),
                    additional_backwards.as_ref(),
                )
                .await?;
                store.finish().await?;
                anyhow::Ok(difference)
            })
        })
        .map_err(anyhow_to_py)?;

    Ok((
        py_set(py, &difference.auth_difference)?,
        difference
            .conflicted_subgraph
            .as_ref()
            .map(|subgraph| py_set(py, subgraph))
            .transpose()?,
    ))
}

/// Load events with [`PostgresStateResStore`], returning a dict of event ID
/// to `Event`. Blocks, releasing the GIL. For tests only.
#[pyfunction]
fn load_events_for_test<'py>(
    py: Python<'py>,
    pool: &Bound<'py, PyConnectionPool>,
    event_ids: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let (pool, handle) = pool_and_handle(pool);
    let ids: Vec<Arc<str>> = event_ids.into_iter().map(Arc::from).collect();

    let events = py
        .detach(|| {
            handle.block_on(async {
                let store = PostgresStateResStore::begin(&pool, handle.clone(), None).await?;
                let events = store.get_events(&ids).await?;
                store.finish().await?;
                anyhow::Ok(events)
            })
        })
        .map_err(anyhow_to_py)?;

    let dict = PyDict::new(py);
    for (event_id, event) in events {
        dict.set_item(&*event_id, event.to_py(py)?)?;
    }
    Ok(dict)
}

pub(super) fn register_test_functions(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let _ = py;
    m.add_function(wrap_pyfunction!(get_auth_chain_difference_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(load_events_for_test, m)?)?;
    Ok(())
}
