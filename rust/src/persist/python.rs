/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 */

//! The Python face of event persistence: the [`Persister`] that
//! `RustHandlers.persister` hands out, and helpers for tests.

use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex},
};

use futures::{future::BoxFuture, FutureExt};
use http::StatusCode;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyDict};

use super::{
    context::PersistContext,
    effects::PersistEffects,
    extremities::MissingPrevStateEvents,
    metrics::{PersistCounts, PersistMetrics},
    prepare::{self, PrepareInput, PrepareOptions, Prepared},
    reader::{self, PersistReader, PyDelta, PySlidingSync},
    state_delta::NativeStateResolver,
    txn::{self, json, streams::TxnStream, PartialStateConflict, TxnEvent, TxnInput},
};
use crate::{
    database::postgres::write::{
        is_retryable_error, run_write_interaction, WriteError, MAX_ATTEMPTS,
    },
    deferred::{create_deferred_with, OnCancel},
    errors::SynapseError,
    event_auth::AuthFailure,
    events::Event,
    homeserver::HomeServer,
    runtime::RustRuntime,
    state::{ConflictCache, StateResStats},
    storage::{
        native_pool::NativePool,
        store::Store,
        stream_ids::{PyStreamIdTracker, StreamIdTracker},
        sync_store::{SyncStore, SyncStoreConfig},
    },
    types::state_filter::state_map_to_py,
};

/// Persists events in Rust, on the events writer, over the native driver.
///
/// Holds what every level needs: the pool, the shared caches, the stream ID
/// trackers of the events, backfill, cache, sticky events and profile
/// updates streams
/// (shared with their Python `MultiWriterIdGenerator`s) and the metrics.
///
/// Each entry point returns a Deferred and has a `_blocking` twin for
/// tests (see `_BlockingRustCalls`).
#[pyclass(frozen)]
pub struct Persister {
    pub(super) inner: Arc<PersisterInner>,
}

#[allow(dead_code)] // Read by the levels to come.
pub(super) struct PersisterInner {
    pub pool: NativePool,
    pub runtime: RustRuntime,
    pub store: Arc<Store>,
    /// The sync store over the same caches, which the
    /// [`super::reader::PersistReader`] builds on.
    pub sync: Arc<SyncStore>,
    pub events_stream: Arc<StreamIdTracker>,
    pub backfill_stream: Arc<StreamIdTracker>,
    pub cache_stream: Arc<StreamIdTracker>,
    pub sticky_stream: Arc<StreamIdTracker>,
    pub profile_stream: Arc<StreamIdTracker>,
    pub server_name: String,
    pub instance_name: String,
    pub metrics: PersistMetrics,
    /// The Rust state resolution handler's conflict cache.
    pub conflict_cache: Arc<ConflictCache>,
    /// `experimental_features.msc4354_enabled`.
    pub msc4354_enabled: bool,
    /// `server.enable_search`.
    pub enable_search: bool,
    /// `server.enable_ephemeral_messages`.
    pub enable_ephemeral_messages: bool,
    /// `server.include_profile_updates_in_sync`.
    pub include_profile_updates_in_sync: bool,
}

impl Persister {
    /// Build the persister for `homeserver`, whose main database uses the
    /// native driver through `connection_pool` (a `RustConnectionPool`).
    /// `None` if this instance does not write events, or if the state tables
    /// are in another database: persisting reads and writes them in the
    /// main database's transactions.
    pub fn for_homeserver(
        py: Python<'_>,
        homeserver: &HomeServer,
        connection_pool: Py<PyAny>,
        runtime: RustRuntime,
        store: Arc<Store>,
        conflict_cache: Arc<ConflictCache>,
    ) -> PyResult<Option<Persister>> {
        let instance_name = homeserver.instance_name(py)?;
        if !homeserver.event_writers(py)?.contains(&instance_name) {
            return Ok(None);
        }

        if !homeserver
            .state_database_pool(py)?
            .is(&homeserver.main_database_pool(py)?)
        {
            log::warn!(
                "The state tables are not in the main database: persisting events in Python"
            );
            return Ok(None);
        }

        let config = homeserver.config(py)?;
        let main = homeserver.main_datastore(py)?;
        let tracker = |attr: &Bound<'_, pyo3::types::PyString>| -> PyResult<Arc<StreamIdTracker>> {
            let id_gen = main.getattr(attr)?;
            if id_gen.is_none() {
                return Err(PyRuntimeError::new_err(format!(
                    "the main datastore has no {attr}"
                )));
            }
            Ok(id_gen
                .getattr(intern!(py, "_tracker"))?
                .cast::<PyStreamIdTracker>()?
                .get()
                .tracker())
        };

        Ok(Some(Persister {
            inner: Arc::new(PersisterInner {
                pool: NativePool::new(connection_pool, "persist_db_pool"),
                runtime,
                sync: Arc::new(SyncStore::new(
                    Arc::clone(&store.caches),
                    SyncStoreConfig::from_config(&config),
                )),
                store,
                events_stream: tracker(intern!(py, "_stream_id_gen"))?,
                backfill_stream: tracker(intern!(py, "_backfill_id_gen"))?,
                cache_stream: tracker(intern!(py, "_cache_id_gen"))?,
                sticky_stream: tracker(intern!(py, "_sticky_events_id_gen"))?,
                profile_stream: tracker(intern!(py, "_profile_updates_id_gen"))?,
                server_name: homeserver.hostname(py)?,
                instance_name,
                metrics: PersistMetrics::default(),
                conflict_cache,
                msc4354_enabled: config.experimental.msc4354_enabled,
                enable_search: config.server.enable_search,
                enable_ephemeral_messages: config.server.enable_ephemeral_messages,
                include_profile_updates_in_sync: config.server.include_profile_updates_in_sync,
            }),
        }))
    }

    /// Convert the chunk while the GIL is held, and return the future that
    /// runs the write transaction over it without the GIL. See
    /// [`Self::persist_events_txn`].
    fn start_persist_events_txn(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<BoxFuture<'static, PyResult<PersistEffects>>> {
        let prepared = Arc::clone(&prepared.prepared);
        if !matches!(&*prepared, Prepared::Chunk(_)) {
            return Err(PyRuntimeError::new_err("the chunk was prepared in Python"));
        }
        let events = events_and_contexts
            .into_iter()
            .map(|(event, context)| TxnEvent::new(event.bind(py).get().shallow_clone(), context))
            .collect::<anyhow::Result<Vec<_>>>()
            .map_err(persist_error_to_py)?;
        let pool = self.inner.pool.get()?;
        let inner = Arc::clone(&self.inner);
        let future = async move {
            let Prepared::Chunk(chunk) = &*prepared else {
                unreachable!("checked above");
            };
            let cache_stream = TxnStream::caches(Arc::clone(&inner.cache_stream));
            let sticky_stream = TxnStream::sticky_events(Arc::clone(&inner.sticky_stream));
            let profile_stream = TxnStream::profile_updates(Arc::clone(&inner.profile_stream));
            let input = TxnInput {
                room_id: &room_id,
                events: &events,
                prepared: chunk,
                instance_name: &inner.instance_name,
                server_name: &inner.server_name,
                clock: inner.runtime.clock(),
                inhibit_local_membership_updates,
                enable_search: inner.enable_search,
                enable_ephemeral_messages: inner.enable_ephemeral_messages,
                msc4354_enabled: inner.msc4354_enabled,
                include_profile_updates_in_sync: inner.include_profile_updates_in_sync,
                cache_stream: &cache_stream,
                sticky_stream: &sticky_stream,
                profile_stream: &profile_stream,
            };
            let effects = run_write_interaction(&pool, "persist_events", |txn, _| {
                let input = &input;
                async move {
                    let mut effects = PersistEffects::default();
                    txn::persist_events_txn(txn, input, &mut effects).await?;
                    Ok(effects)
                }
                .boxed()
            })
            .await
            .map_err(write_error_to_py)?;
            cache_stream.hand_over(effects.cache_stream_ids());
            sticky_stream.hand_over(effects.sticky_stream_ids());
            profile_stream.hand_over(effects.profile_stream_ids());
            Ok(effects)
        };
        Ok(future.boxed())
    }

    /// Convert the chunk while the GIL is held, and return the future that
    /// prepares it without the GIL.
    fn start_prepare_chunk(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        backfilled: bool,
        shadow: bool,
    ) -> PyResult<BoxFuture<'static, PyResult<PreparedChunk>>> {
        let input = PrepareInput::new(py, room_id, &events_and_contexts, backfilled)?;
        let pool = self.inner.pool.get()?;
        let handle = self.inner.runtime.tokio_handle()?;
        let inner = Arc::clone(&self.inner);
        Ok(async move {
            let resolver = NativeStateResolver {
                pool: pool.clone(),
                handle,
                caches: Arc::clone(&inner.store.caches),
                conflict_cache: Arc::clone(&inner.conflict_cache),
            };
            let reader = PersistReader::new(inner.sync.reader(pool.clone()));
            let options = PrepareOptions {
                server_name: &inner.server_name,
                clock: inner.runtime.clock(),
                sticky: inner.msc4354_enabled,
                dry_run: shadow,
            };
            // Python reads in `runInteraction`s, which retry on a lost
            // connection or a cancelled statement. Here the whole chunk is
            // prepared again. That is safe as its only write, bumping the
            // state groups it uses, can be repeated, and runs in a write
            // interaction with its own retries.
            let mut attempt = 1;
            loop {
                let mut counts = PersistCounts::default();
                match prepare::prepare_chunk(
                    &reader,
                    &resolver,
                    &pool,
                    &input,
                    &options,
                    &mut counts,
                )
                .await
                {
                    Ok(prepared) => {
                        return Ok(PreparedChunk {
                            prepared: Arc::new(prepared),
                            counts: Mutex::new(Some(counts)),
                        })
                    }
                    Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(&e) => {
                        log::warn!("[TXN RETRY] {{prepare_chunk}} {e:#} {attempt}/{MAX_ATTEMPTS}");
                        attempt += 1;
                    }
                    Err(e) => return Err(persist_error_to_py(e)),
                }
            }
        }
        .boxed())
    }

    /// Run `future` to completion on the runtime, releasing the GIL. For the
    /// `_blocking` entry points.
    fn block_on<T: Send>(
        &self,
        py: Python<'_>,
        future: BoxFuture<'static, PyResult<T>>,
    ) -> PyResult<T> {
        let handle = self.inner.runtime.tokio_handle()?;
        py.detach(|| handle.block_on(future))
    }
}

#[pymethods]
impl Persister {
    /// What persisting has counted since the last call, for
    /// `flush_rust_persist_metrics`.
    fn take_metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.inner.metrics.take().to_py(py)
    }

    /// What `_persist_event_batch` computes for a chunk of one room's events
    /// before the write transaction: a Deferred resolving to a
    /// [`PreparedChunk`]. In `shadow` mode the chain cover step's write is
    /// rolled back. Nothing is counted until [`Self::record_prepare_counts`].
    /// Cancelling the Deferred stops the computation.
    #[pyo3(signature = (room_id, events_and_contexts, backfilled, *, shadow))]
    fn prepare_chunk<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        backfilled: bool,
        shadow: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future =
            self.start_prepare_chunk(py, room_id, events_and_contexts, backfilled, shadow)?;
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::Abort)
    }

    /// Add what preparing `chunk` counted to the metrics, once Python has
    /// decided to use it. Python counts the chunks it computes itself. Does
    /// nothing the second time.
    fn record_prepare_counts(&self, chunk: &PreparedChunk) {
        let counts = chunk
            .counts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        if let Some(counts) = counts {
            self.inner.metrics.record(|total| total.add(counts));
        }
    }

    /// Run the Rust write transaction over the chunk, whose events must
    /// have their stream orderings set, and commit it. `prepared` is what
    /// Rust prepared for it.
    ///
    /// Resolves with what Python applies with `apply_rust_persist_effects`,
    /// in which the stream IDs the transaction took are still unfinished.
    ///
    /// Python must not cancel the Deferred: the effects of a transaction
    /// that commits must be applied. If it is cancelled, the transaction
    /// runs on and its effects are lost.
    #[pyo3(signature = (room_id, events_and_contexts, prepared, *, inhibit_local_membership_updates))]
    fn persist_events_txn<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_persist_events_txn(
            py,
            room_id,
            events_and_contexts,
            prepared,
            inhibit_local_membership_updates,
        )?;
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::persist_events_txn`], but blocks.
    #[pyo3(signature = (room_id, events_and_contexts, prepared, *, inhibit_local_membership_updates))]
    fn persist_events_txn_blocking(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<PersistEffects> {
        let future = self.start_persist_events_txn(
            py,
            room_id,
            events_and_contexts,
            prepared,
            inhibit_local_membership_updates,
        )?;
        self.block_on(py, future)
    }

    /// As [`Self::prepare_chunk`], but blocks.
    #[pyo3(signature = (room_id, events_and_contexts, backfilled, *, shadow))]
    fn prepare_chunk_blocking(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        backfilled: bool,
        shadow: bool,
    ) -> PyResult<PreparedChunk> {
        let future =
            self.start_prepare_chunk(py, room_id, events_and_contexts, backfilled, shadow)?;
        self.block_on(py, future)
    }
}

/// What [`Persister::prepare_chunk`] computed, in the shapes Python builds
/// its own types from. If `state_res_fallback` is set, the rest is empty
/// and Python should compute the chunk itself.
#[pyclass(frozen)]
pub struct PreparedChunk {
    /// Shared with the transaction that writes the chunk.
    prepared: Arc<Prepared>,
    /// What preparing counted, until it is recorded.
    counts: Mutex<Option<PersistCounts>>,
}

impl PreparedChunk {
    fn chunk(&self) -> Option<&prepare::PreparedChunk> {
        match &*self.prepared {
            Prepared::Chunk(chunk) => Some(chunk),
            Prepared::StateResFallback(_) => None,
        }
    }
}

#[pymethods]
impl PreparedChunk {
    /// Why the Rust state resolution could not resolve the room.
    #[getter]
    fn state_res_fallback(&self) -> Option<&str> {
        match &*self.prepared {
            Prepared::Chunk(_) => None,
            Prepared::StateResFallback(reason) => Some(reason),
        }
    }

    #[getter]
    fn new_forward_extremities(&self) -> Option<HashSet<String>> {
        let ids = self.chunk()?.state.new_forward_extremities.clone()?;
        Some(reader::strings(ids))
    }

    /// `(to_delete, to_insert, no_longer_in_room)`.
    #[getter]
    fn state_delta<'py>(&self, py: Python<'py>) -> PyResult<Option<PyDelta<'py>>> {
        self.chunk()
            .and_then(|chunk| chunk.state.state_delta.as_ref())
            .map(|delta| reader::delta_to_py(py, delta))
            .transpose()
    }

    #[getter]
    fn new_state_dag_extremities(&self) -> Option<HashSet<String>> {
        let ids = self.chunk()?.state.new_state_dag_extremities.clone()?;
        Some(reader::strings(ids))
    }

    #[getter]
    fn state_res_stats(&self) -> Option<StateResStats> {
        self.chunk()?.state.state_res_stats.clone()
    }

    /// Event ID to `(chain_id, sequence_number, links)`.
    #[getter]
    #[allow(clippy::type_complexity)]
    fn new_event_links(&self) -> HashMap<String, (i64, i64, Vec<(i64, i64)>)> {
        self.chunk()
            .map(|chunk| {
                chunk
                    .new_event_links
                    .iter()
                    .map(|(event_id, links)| {
                        (
                            event_id.to_string(),
                            (links.chain_id, links.sequence_number, links.links.clone()),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// As `PersistReaderForTest.calculate_sliding_sync_table_changes`
    /// returns it. `None` when the state does not change.
    #[getter]
    fn sliding_sync_table_changes<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<Option<PySlidingSync<'py>>> {
        self.chunk()
            .and_then(|chunk| chunk.sliding_sync.as_ref())
            .map(|changes| reader::sliding_sync_to_py(py, changes))
            .transpose()
    }

    #[getter]
    fn sticky_events_to_un_soft_fail(&self) -> HashSet<String> {
        self.chunk()
            .map(|chunk| reader::strings(chunk.sticky_events_to_un_soft_fail.iter().cloned()))
            .unwrap_or_default()
    }
}

/// Convert an error from a persist transaction to the Python exception the
/// Python transaction would raise. See [`persist_error_to_py`].
pub fn write_error_to_py(e: WriteError) -> PyErr {
    match e {
        WriteError::Other(e) => persist_error_to_py(e),
        e => persist_error_to_py(anyhow::Error::new(e)),
    }
}

/// Convert an error from persisting to the Python exception the Python
/// path would raise: a Python exception as it was raised, an auth
/// failure as the auth rules raise it (from any step, state resolution
/// included), a 500 `SynapseError` for missing
/// prev state events, as `_calculate_new_state_dag_extremities` raises,
/// and a `RuntimeError` otherwise.
pub fn persist_error_to_py(e: anyhow::Error) -> PyErr {
    let e = match e.downcast::<PyErr>() {
        Ok(err) => return err,
        Err(e) => e,
    };
    if e.downcast_ref::<AuthFailure>().is_some() {
        // The Python exception carries only the failure's message, so log
        // the context, which names the event.
        log::warn!("Auth checks failed while persisting: {e:#}");
    }
    let e = match e.downcast::<AuthFailure>() {
        Ok(failure) => return failure.into_py_err(),
        Err(e) => e,
    };
    if e.downcast_ref::<PartialStateConflict>().is_some() {
        return match py_partial_state_conflict_error() {
            Ok(err) | Err(err) => err,
        };
    }
    if let Some(missing) = e.downcast_ref::<MissingPrevStateEvents>() {
        return SynapseError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            missing.to_string(),
            "M_UNKNOWN",
            None,
            None,
        );
    }
    PyRuntimeError::new_err(format!("{e:#}"))
}

/// A `PartialStateConflictError`, as the Python transaction raises.
fn py_partial_state_conflict_error() -> PyResult<PyErr> {
    crate::gil::with_gil("persist_partial_state_conflict", |py| {
        let error = py
            .import("synapse.api.errors")?
            .getattr("PartialStateConflictError")?
            .call0()?;
        Ok(PyErr::from_value(error))
    })
}

/// `(internal_metadata, json)`: the two `event_json` columns as Rust would
/// write them for `event`, for tests to compare with Python's.
#[pyfunction]
pub fn persistence_json_for_test(event: &Bound<'_, Event>) -> PyResult<(String, String)> {
    let event = event.get();
    let internal_metadata = json::encode_object(&event.internal_metadata().to_persistence_pairs()?);
    let json = event.with_persistence_dict(|dict| json::encode(dict))?;
    let to_py = |e: serde_json::Error| PyRuntimeError::new_err(format!("encoding: {e}"));
    Ok((internal_metadata.map_err(to_py)?, json.map_err(to_py)?))
}

/// The [`PersistContext`] of `context` as a dict, for tests to check the
/// conversion.
#[pyfunction]
pub fn persist_context_for_test<'py>(
    py: Python<'py>,
    context: PersistContext,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("rejected", context.rejected)?;
    dict.set_item("state_group", context.state_group)?;
    dict.set_item("state_group_before_event", context.state_group_before_event)?;
    dict.set_item(
        "state_delta_due_to_event",
        context
            .state_delta_due_to_event
            .as_ref()
            .map(|delta| state_map_to_py(py, delta))
            .transpose()?,
    )?;
    let deltas = PyDict::new(py);
    for (key, delta) in context.state_group_deltas.iter() {
        deltas.set_item(key, state_map_to_py(py, delta)?)?;
    }
    dict.set_item("state_group_deltas", deltas)?;
    dict.set_item("partial_state", context.partial_state)?;
    dict.set_item("app_service", context.app_service)?;
    Ok(dict)
}
