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

//! Simplified sliding sync (MSC4186) in Rust, reading the database through
//! the native Postgres driver.
//!
//! [`SlidingSyncHandler`] is the Python entry point, used by
//! `synapse.handlers.sliding_sync.SlidingSyncHandler` when
//! `rust_sliding_sync` is on. It shares the v2 sync handler's
//! [`SyncStore`], so the two read through the same caches.
//!
//! The handler's entry points follow the v2 handler's shape: a `start_*`
//! method reads what it needs from Python and returns a future, which the
//! public method runs off the GIL on the runtime and returns as a deferred
//! ([`SlidingSyncHandler::defer`]), and a `_blocking` twin runs to completion
//! for tests ([`SlidingSyncHandler::block_on`]).

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use anyhow::Context;
use futures::future::BoxFuture;
use pyo3::{
    create_exception,
    exceptions::{PyException, PyRuntimeError},
    intern,
    prelude::*,
    types::PyModule,
};
use request::{SlidingSyncRequest, SlidingSyncRequestData};
use result::RoomsResult;
use room_entry::RoomInputData;
use room_lists::filter::NotImplementedFilter;
use room_lists::interested::{InterestedRooms, ListsSnapshot, PyInterestedRooms};
use room_lists::lists::UnknownPosition;
use state::{ConnectionState, ConnectionStateData, InstanceMap};

use crate::caches::lock_ignoring_poison;
use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::deferred::{create_deferred_with, OnCancel};
use crate::runtime::RustRuntime;
use crate::sliding_sync::connection::PerConnectionState;
use crate::storage::sliding_sync_store::{SlidingSyncReader, StoredConnectionState};
use crate::storage::sync_store::{anyhow_to_py, SyncDbStats, SyncStore};
use crate::types::stream_token::RoomStreamToken;

create_exception!(
    synapse.synapse_rust.sliding_sync,
    RustSlidingSyncFallback,
    PyException,
    "The request is one Rust leaves to Python."
);

pub mod config;
pub mod connection;
pub mod encode;
pub mod extensions;
pub mod request;
pub mod required_state;
pub mod result;
pub mod room;
pub mod room_entry;
pub mod room_lists;
pub mod state;

/// Computes sliding sync responses with the Rust implementation, reading
/// the database through the native Postgres driver.
#[pyclass(frozen)]
pub struct SlidingSyncHandler {
    /// The Python `RustConnectionPool`. Its `_pool` is read per call, as
    /// `close()`/`start()` replace it.
    db_pool: Py<PyAny>,
    runtime: RustRuntime,
    /// The queries, over the Rust store's caches. The v2 sync handler's.
    store: Arc<SyncStore>,
}

impl SlidingSyncHandler {
    pub fn new(db_pool: Py<PyAny>, runtime: RustRuntime, store: Arc<SyncStore>) -> Self {
        SlidingSyncHandler {
            db_pool,
            runtime,
            store,
        }
    }

    pub fn store(&self) -> &Arc<SyncStore> {
        &self.store
    }

    /// The connection pool to read through, or an error if it is closed.
    pub fn pool(&self, py: Python<'_>) -> PyResult<ConnectionPool> {
        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
    }

    /// Run `future` on the runtime and return a deferred for its result.
    /// Cancelling the deferred stops the computation. The servlet is not
    /// `@cancellable`, so today that only happens on shutdown.
    pub fn defer<'py, O>(
        &self,
        py: Python<'py>,
        future: BoxFuture<'static, PyResult<O>>,
    ) -> PyResult<Bound<'py, PyAny>>
    where
        for<'a> O: IntoPyObject<'a> + Send + 'static,
    {
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// Run `future` to completion on the runtime, releasing the GIL. For the
    /// `_blocking` entry points.
    pub fn block_on<T: Send + 'static>(
        &self,
        py: Python<'_>,
        future: BoxFuture<'static, PyResult<T>>,
    ) -> PyResult<T> {
        self.runtime.block_on_scoped(py, future)?
    }

    /// The previous state's slice that one room's computation reads.
    fn room_input(
        interested: &InterestedRooms,
        previous: &PerConnectionState<String>,
        room_id: &Arc<str>,
    ) -> anyhow::Result<RoomInputData> {
        let membership = interested
            .memberships
            .get(room_id)
            .with_context(|| format!("no membership for {room_id}"))?
            .clone();
        let room_sync_config = interested
            .relevant_room_map
            .get(room_id)
            .with_context(|| format!("no config for {room_id}"))?
            .clone();
        Ok(RoomInputData {
            membership,
            room_sync_config,
            prev_room_sync_config: previous.room_configs.get(&**room_id).cloned(),
            room_status: previous.rooms.have_sent_room(room_id),
            newly_joined: interested.newly_joined.contains(room_id),
            newly_left: interested.newly_left.contains(room_id),
            is_dm: interested.dm_room_ids.contains(&**room_id),
        })
    }

    /// Start loading the connection state at the request's position: an
    /// empty state for a first request.
    fn start_load_connection_state(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<BoxFuture<'static, PyResult<ConnectionState>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.sliding_sync_reader(pool);
            let result = async {
                let instances_of = |table: Arc<[(i64, Arc<str>)]>| {
                    InstanceMap::new(
                        table.iter().cloned().chain(
                            instance_ids
                                .iter()
                                .map(|(name, id)| (*id, Arc::from(name.as_str()))),
                        ),
                    )
                };
                let mut instances = instances_of(reader.instance_map().await?);
                let stored = match &request.from {
                    Some(from) if from.connection_position != 0 => {
                        let device_id = request
                            .device_id
                            .as_deref()
                            .context("sliding sync needs a device")?;
                        let conn_id = request.body.conn_id.as_deref().unwrap_or("");
                        reader
                            .load_connection_state(
                                &request.user_id,
                                device_id,
                                conn_id,
                                from.connection_position,
                                now_ms,
                            )
                            .await?
                    }
                    _ => Arc::new(StoredConnectionState::default()),
                };
                // A stored token may name a writer added since the table
                // was cached.
                if stored.names_unknown_writer(&instances) {
                    reader.forget_instance_map();
                    instances = instances_of(reader.instance_map().await?);
                }
                let data = ConnectionStateData::from_stored(&stored, &instances)?;
                let stats = SlidingSyncStats::from_db(&reader.stats());
                Ok::<_, anyhow::Error>(ConnectionState::new(data, instances, stats))
            }
            .await;
            result.map_err(error_to_py)
        }))
    }

    /// Start computing the room lists.
    fn start_interested_rooms(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        snapshot: ListsSnapshot,
    ) -> PyResult<BoxFuture<'static, PyResult<PyInterestedRooms>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let previous = lock_ignoring_poison(&state.get().data).previous.clone();
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.sliding_sync_reader(pool);
            let interested = room_lists::interested::compute_interested_rooms(
                &reader, &request, &previous, snapshot, now_ms,
            )
            .await
            .map_err(error_to_py)?;
            Ok(PyInterestedRooms {
                data: Arc::new(interested),
                stats: SlidingSyncStats::from_db(&reader.stats()),
            })
        }))
    }

    /// Start computing the entries of the rooms to send. The rooms'
    /// changes to the connection state go into `state`.
    fn start_room_entries(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        interested: &Bound<'_, PyInterestedRooms>,
    ) -> PyResult<BoxFuture<'static, PyResult<RoomsResult>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let interested = Arc::clone(&interested.get().data);
        let state_data = Arc::clone(&state.get().data);
        let instance_ids = state.get().instances.ids_by_name();
        let rooms = {
            let data = lock_ignoring_poison(&state_data);
            interested
                .rooms_to_send
                .iter()
                .map(|room_id| Self::room_input(&interested, &data.previous, room_id))
                .collect::<anyhow::Result<Vec<_>>>()
                .map_err(anyhow_to_py)?
        };
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.sliding_sync_reader(pool);
            let outcomes =
                room_entry::generate_room_entries(&reader, &request, &instance_ids, now_ms, &rooms)
                    .await
                    .map_err(anyhow_to_py)?;
            let stats = SlidingSyncStats::from_db(&reader.stats());
            let result = RoomsResult::new(outcomes, request.from.is_some(), stats);
            result.apply_to(&mut lock_ignoring_poison(&state_data).new);
            Ok(result)
        }))
    }

    /// Start the end of the request: record what was sent and what was
    /// not, and persist the state if it changed. Resolves to the new
    /// connection position.
    fn start_finish_request(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        interested: &Bound<'_, PyInterestedRooms>,
    ) -> PyResult<BoxFuture<'static, PyResult<(i64, SlidingSyncStats)>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let interested = Arc::clone(&interested.get().data);
        let state_data = Arc::clone(&state.get().data);
        let instances = Arc::clone(&state.get().instances);
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.sliding_sync_reader(pool);
            let position = finish_request(
                &reader,
                &request,
                &state_data,
                &instances,
                &interested,
                now_ms,
            )
            .await
            .map_err(error_to_py)?;
            Ok((position, SlidingSyncStats::from_db(&reader.stats())))
        }))
    }
}

/// The rest of `current_sync_for_user` after the extensions.
async fn finish_request(
    reader: &SlidingSyncReader<'_>,
    request: &SlidingSyncRequestData,
    state: &Mutex<ConnectionStateData>,
    instances: &InstanceMap,
    interested: &InterestedRooms,
    now_ms: i64,
) -> anyhow::Result<i64> {
    let unchanged_position = request
        .from
        .as_ref()
        .map_or(0, |from| from.connection_position);
    if !interested.has_lists_or_subscriptions {
        return Ok(unchanged_position);
    }

    if let Some(from) = &request.from {
        // The rooms the client may care about but that are in no range nor
        // subscribed to. Recording rooms without anything new for the user
        // is fine.
        let missing: Vec<Arc<str>> = interested
            .all_rooms
            .iter()
            .filter(|room_id| !interested.relevant_room_map.contains_key(*room_id))
            .cloned()
            .collect();
        let unsent = reader
            .rooms_with_updates_since(
                &missing,
                &from.room_key,
                interested.events_changed_since_from.as_ref(),
            )
            .await?;
        lock_ignoring_poison(state)
            .new
            .rooms
            .record_unsent_rooms(unsent.iter().map(|room_id| &**room_id), &from.room_key);
    }

    let updates = {
        let mut data = lock_ignoring_poison(state);
        data.new
            .rooms
            .record_sent_rooms(interested.rooms_to_send.iter().map(|room_id| &**room_id));
        if !data.new.has_updates(now_ms) {
            return Ok(unchanged_position);
        }
        data.updates(instances)?
    };

    // A zero position means no state was stored before.
    let previous_position = request
        .from
        .as_ref()
        .map(|from| from.connection_position)
        .filter(|position| *position != 0);
    let device_id = request
        .device_id
        .as_deref()
        .context("sliding sync needs a device")?;
    let conn_id = request.body.conn_id.as_deref().unwrap_or("");
    reader
        .persist_connection_state(
            &request.user_id,
            device_id,
            conn_id,
            previous_position,
            &updates,
            now_ms,
        )
        .await
}

/// An error for Python: Python's `SlidingSyncUnknownPosition` for an
/// unknown position or a connection to reset, `RustSlidingSyncFallback`
/// for what Rust leaves to Python.
fn error_to_py(e: anyhow::Error) -> PyErr {
    if e.downcast_ref::<UnknownPosition>().is_some() {
        return Python::attach(unknown_position_to_py);
    }
    if let Some(filter) = e.downcast_ref::<NotImplementedFilter>() {
        return RustSlidingSyncFallback::new_err(filter.to_string());
    }
    anyhow_to_py(e)
}

fn unknown_position_to_py(py: Python<'_>) -> PyErr {
    match py
        .import("synapse.api.errors")
        .and_then(|errors| errors.getattr("SlidingSyncUnknownPosition"))
        .and_then(|class| class.call0())
    {
        Ok(exception) => PyErr::from_value(exception),
        Err(e) => e,
    }
}

#[pymethods]
impl SlidingSyncHandler {
    /// `get_and_clear_connection_positions`: a deferred resolving to the
    /// request's `ConnectionState`. `instance_ids` are `get_id_for_instance`
    /// for the writers of the request's tokens. Fails with
    /// `SlidingSyncUnknownPosition` for a position the store does not know.
    #[pyo3(signature = (request, *, instance_ids))]
    fn load_connection_state<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SlidingSyncRequest>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_load_connection_state(py, request, instance_ids)?;
        self.defer(py, future)
    }

    /// As [`Self::load_connection_state`], but blocks.
    #[pyo3(signature = (request, *, instance_ids))]
    fn load_connection_state_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        instance_ids: HashMap<String, i64>,
    ) -> PyResult<ConnectionState> {
        let future = self.start_load_connection_state(py, request, instance_ids)?;
        self.block_on(py, future)
    }

    /// `compute_interested_rooms`: a deferred resolving to an
    /// `InterestedRooms`. The arguments after `state` are the stream change
    /// caches' answers and the config the lists read; see
    /// [`ListsSnapshot`]. Fails with `RustSlidingSyncFallback` for a filter
    /// Python does not implement either.
    #[pyo3(signature = (request, state, *, server_name, msc4155_enabled, num_rooms_threshold, membership_changed_since_from, membership_changed_since_to, events_changed_since_from, room_max_token, events_stream_cache))]
    #[allow(clippy::too_many_arguments)]
    fn compute_interested_rooms<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SlidingSyncRequest>,
        state: &Bound<'py, ConnectionState>,
        server_name: &str,
        msc4155_enabled: bool,
        num_rooms_threshold: usize,
        membership_changed_since_from: bool,
        membership_changed_since_to: bool,
        events_changed_since_from: Option<HashSet<String>>,
        room_max_token: RoomStreamToken,
        events_stream_cache: Py<PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let snapshot = ListsSnapshot {
            server_name: Arc::from(server_name),
            msc4155_enabled,
            num_rooms_threshold,
            membership_changed_since_from,
            membership_changed_since_to,
            events_changed_since_from: events_changed_since_from
                .map(|rooms| rooms.into_iter().map(Arc::from).collect()),
            room_max_token,
            events_stream_cache,
        };
        let future = self.start_interested_rooms(py, request, state, snapshot)?;
        self.defer(py, future)
    }

    /// As [`Self::compute_interested_rooms`], but blocks.
    #[pyo3(signature = (request, state, *, server_name, msc4155_enabled, num_rooms_threshold, membership_changed_since_from, membership_changed_since_to, events_changed_since_from, room_max_token, events_stream_cache))]
    #[allow(clippy::too_many_arguments)]
    fn compute_interested_rooms_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        server_name: &str,
        msc4155_enabled: bool,
        num_rooms_threshold: usize,
        membership_changed_since_from: bool,
        membership_changed_since_to: bool,
        events_changed_since_from: Option<HashSet<String>>,
        room_max_token: RoomStreamToken,
        events_stream_cache: Py<PyAny>,
    ) -> PyResult<PyInterestedRooms> {
        let snapshot = ListsSnapshot {
            server_name: Arc::from(server_name),
            msc4155_enabled,
            num_rooms_threshold,
            membership_changed_since_from,
            membership_changed_since_to,
            events_changed_since_from: events_changed_since_from
                .map(|rooms| rooms.into_iter().map(Arc::from).collect()),
            room_max_token,
            events_stream_cache,
        };
        let future = self.start_interested_rooms(py, request, state, snapshot)?;
        self.block_on(py, future)
    }

    /// `get_room_sync_data` for each of the rooms to send, twenty at a
    /// time: a deferred resolving to a `RoomsResult`. The rooms' changes to
    /// the connection state go into `state`.
    fn generate_room_entries<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SlidingSyncRequest>,
        state: &Bound<'py, ConnectionState>,
        interested: &Bound<'py, PyInterestedRooms>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_room_entries(py, request, state, interested)?;
        self.defer(py, future)
    }

    /// As [`Self::generate_room_entries`], but blocks.
    fn generate_room_entries_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        interested: &Bound<'_, PyInterestedRooms>,
    ) -> PyResult<RoomsResult> {
        let future = self.start_room_entries(py, request, state, interested)?;
        self.block_on(py, future)
    }

    /// The end of the request, after the extensions: a deferred resolving
    /// to `(connection_position, stats)`.
    fn finish_request<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SlidingSyncRequest>,
        state: &Bound<'py, ConnectionState>,
        interested: &Bound<'py, PyInterestedRooms>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_finish_request(py, request, state, interested)?;
        self.defer(py, future)
    }

    /// As [`Self::finish_request`], but blocks.
    fn finish_request_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SlidingSyncRequest>,
        state: &Bound<'_, ConnectionState>,
        interested: &Bound<'_, PyInterestedRooms>,
    ) -> PyResult<(i64, SlidingSyncStats)> {
        let future = self.start_finish_request(py, request, state, interested)?;
        self.block_on(py, future)
    }
}

/// What the Rust implementation did for one request, for the
/// `synapse_sliding_sync_rust_*` histograms. Python observes it once per
/// request.
#[pyclass(frozen, get_all, skip_from_py_object)]
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct SlidingSyncStats {
    /// Time spent waiting on statements, in seconds.
    pub db_time: f64,
    /// Time spent waiting for a connection from the pool, in seconds.
    pub checkout_time: f64,
    /// Transactions begun.
    pub transactions: u64,
    /// Statements executed.
    pub queries: u64,
    /// Rooms whose entry Rust computed.
    pub rooms_computed: u64,
    /// Rooms whose lazy-loaded members were looked up in the connection
    /// state.
    pub lazy_member_lookups: u64,
}

impl SlidingSyncStats {
    /// The database half of the stats, from a reader's.
    pub fn from_db(db: &SyncDbStats) -> Self {
        SlidingSyncStats {
            db_time: db.db_time.as_secs_f64(),
            checkout_time: db.checkout_time.as_secs_f64(),
            transactions: db.transactions,
            queries: db.queries,
            ..Default::default()
        }
    }
}

#[pymethods]
impl SlidingSyncStats {
    #[new]
    #[pyo3(signature = (*, db_time=0.0, checkout_time=0.0, transactions=0, queries=0, rooms_computed=0, lazy_member_lookups=0))]
    fn py_new(
        db_time: f64,
        checkout_time: f64,
        transactions: u64,
        queries: u64,
        rooms_computed: u64,
        lazy_member_lookups: u64,
    ) -> Self {
        SlidingSyncStats {
            db_time,
            checkout_time,
            transactions,
            queries,
            rooms_computed,
            lazy_member_lookups,
        }
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sliding_sync")?;
    child_module.add_class::<SlidingSyncHandler>()?;
    child_module.add_class::<SlidingSyncStats>()?;
    child_module.add_class::<request::SlidingSyncRequest>()?;
    child_module.add_class::<result::RoomResult>()?;
    child_module.add_class::<RoomsResult>()?;
    child_module.add_class::<PyInterestedRooms>()?;
    child_module.add(
        "RustSlidingSyncFallback",
        py.get_type::<RustSlidingSyncFallback>(),
    )?;
    state::register_module(&child_module)?;
    child_module.add_function(wrap_pyfunction!(encode::encode_rooms, &child_module)?)?;
    child_module.add_class::<crate::storage::sliding_sync_store::PySlidingSyncStoreForTest>()?;
    config::register_test_functions(&child_module)?;
    connection::register_test_functions(&child_module)?;
    extensions::register_test_functions(&child_module)?;
    required_state::register_test_functions(&child_module)?;
    room::register_test_functions(&child_module)?;
    room_lists::register_test_functions(&child_module)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import sliding_sync` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.sliding_sync", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[test]
    fn stats_from_db() {
        let db = SyncDbStats {
            checkouts: 3,
            checkout_time: Duration::from_millis(5),
            transactions: 2,
            queries: 7,
            db_time: Duration::from_millis(250),
            ..Default::default()
        };
        let stats = SlidingSyncStats::from_db(&db);
        assert_eq!(stats.transactions, 2);
        assert_eq!(stats.queries, 7);
        assert!((stats.db_time - 0.25).abs() < 1e-9);
        assert!((stats.checkout_time - 0.005).abs() < 1e-9);
        assert_eq!(stats.rooms_computed, 0);
    }
}
