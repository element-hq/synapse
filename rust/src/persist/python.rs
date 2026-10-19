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
    collections::{BTreeSet, HashMap, HashSet},
    sync::{Arc, Mutex},
};

use futures::{future::BoxFuture, FutureExt};
use http::StatusCode;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyDict};

use super::{
    batch,
    context::PersistContext,
    effects::PersistEffects,
    extremities::MissingPrevStateEvents,
    follow_ups,
    metrics::{PersistCounts, PersistMetrics},
    prepare::{self, PrepareInput, PrepareOptions, Prepared},
    reader::{self, PersistReader, PyDelta, PySlidingSync},
    sliding_sync,
    state_delta::{self, NativeStateResolver, StateResFallback},
    txn::{
        self, json, streams::TxnStream, PartialStateConflict, RoomIdCollision, TxnEvent, TxnInput,
    },
};
use crate::{
    database::postgres::{
        pool::ConnectionPool,
        write::{
            is_retryable_error, record_txn_timing, run_write_interaction, with_txn_timings,
            TxnTimings, WriteError, MAX_ATTEMPTS,
        },
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
        pool_querier::PoolQuerier,
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
    /// [`Self::persist_events_txn`] and [`Self::persist_chunk`].
    fn start_persist_events_txn(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        orderings: StreamOrderings,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<BoxFuture<'static, PyResult<PersistEffects>>> {
        let prepared = Arc::clone(&prepared.prepared);
        if !matches!(&*prepared, Prepared::Chunk(_)) {
            return Err(PyRuntimeError::new_err("the chunk was prepared in Python"));
        }
        let events = shallow_events(py, events_and_contexts);
        let pool = self.inner.pool.get()?;
        let inner = Arc::clone(&self.inner);
        Ok(async move {
            let Prepared::Chunk(chunk) = &*prepared else {
                unreachable!("checked above");
            };
            let (effects, timings) = with_txn_timings(write_chunk(
                &inner,
                &pool,
                &room_id,
                events,
                chunk,
                orderings,
                inhibit_local_membership_updates,
            ))
            .await;
            let mut effects = reported(&inner, effects, &timings)?;
            effects.add_txn_timings(timings);
            Ok(effects)
        }
        .boxed())
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
        let events = shallow_events(py, events_and_contexts);
        let input = PrepareInput::new(room_id, &events, backfilled).map_err(persist_error_to_py)?;
        let pool = self.inner.pool.get()?;
        let inner = Arc::clone(&self.inner);
        Ok(async move {
            let (prepared, txn_timings) =
                with_txn_timings(prepare_chunk(&inner, &pool, &input, shadow)).await;
            let (prepared, counts) =
                reported(&inner, prepared.map_err(persist_error_to_py), &txn_timings)?;
            Ok(PreparedChunk {
                prepared: Arc::new(prepared),
                counts: Mutex::new(Some(counts)),
                txn_timings,
            })
        }
        .boxed())
    }

    /// The future of the batch's next step. See [`Self::batch_next`].
    fn start_batch_next(&self, batch: &PersistBatch) -> BoxFuture<'static, PyResult<BatchStep>> {
        let state = Arc::clone(&batch.state);
        let inner = Arc::clone(&self.inner);
        async move {
            let (step, timings) = with_txn_timings(batch_next(&mut *state.lock().await)).await;
            Ok(match reported(&inner, step, &timings)? {
                BatchStep::Written(mut chunk) => {
                    chunk.effects.add_txn_timings(timings);
                    BatchStep::Written(chunk)
                }
                BatchStep::Done(mut batch) => {
                    batch.txn_timings = timings;
                    BatchStep::Done(batch)
                }
            })
        }
        .boxed()
    }

    /// The future that releases the batch. See [`Self::batch_abort`].
    fn start_batch_abort(&self, batch: &PersistBatch) -> BoxFuture<'static, PyResult<()>> {
        let state = Arc::clone(&batch.state);
        let inner = Arc::clone(&self.inner);
        async move {
            let (released, timings) = with_txn_timings(async {
                let mut state = state.lock().await;
                state.done = true;
                state.release_guard(true).await
            })
            .await;
            // Nothing carries them back to Python, which gives up.
            inner.metrics.record_failed_txn_timings(timings);
            released
        }
        .boxed()
    }

    /// The future of [`Self::update_current_state`].
    fn start_update_current_state(
        &self,
        room_id: String,
    ) -> PyResult<BoxFuture<'static, PyResult<CurrentStateUpdate>>> {
        let pool = self.inner.pool.get()?;
        let inner = Arc::clone(&self.inner);
        Ok(async move {
            let (update, timings) =
                with_txn_timings(update_current_state(&inner, &pool, &room_id)).await;
            let mut update = reported(&inner, update, &timings)?;
            update.txn_timings = timings;
            Ok(update)
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
        let dict = self.inner.metrics.take().to_py(py)?;
        dict.set_item("txn_timings", self.inner.metrics.take_failed_txn_timings())?;
        Ok(dict)
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
            StreamOrderings::Set,
            inhibit_local_membership_updates,
        )?;
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::persist_events_txn`], but Rust takes the chunk's stream
    /// orderings, from the backfill stream if `backfilled`, and sets them on
    /// the events with the time and this instance, as
    /// `_persist_events_and_state_updates` does. They come back in the
    /// effects' `stream_ids`, unfinished. If the persist fails, Rust
    /// finishes them.
    #[pyo3(signature = (room_id, events_and_contexts, prepared, *, backfilled, inhibit_local_membership_updates))]
    fn persist_chunk<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        backfilled: bool,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_persist_events_txn(
            py,
            room_id,
            events_and_contexts,
            prepared,
            StreamOrderings::Allocate { backfilled },
            inhibit_local_membership_updates,
        )?;
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::persist_chunk`], but blocks.
    #[pyo3(signature = (room_id, events_and_contexts, prepared, *, backfilled, inhibit_local_membership_updates))]
    fn persist_chunk_blocking(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        prepared: &PreparedChunk,
        backfilled: bool,
        inhibit_local_membership_updates: bool,
    ) -> PyResult<PersistEffects> {
        let future = self.start_persist_events_txn(
            py,
            room_id,
            events_and_contexts,
            prepared,
            StreamOrderings::Allocate { backfilled },
            inhibit_local_membership_updates,
        )?;
        self.block_on(py, future)
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
            StreamOrderings::Set,
            inhibit_local_membership_updates,
        )?;
        self.block_on(py, future)
    }

    /// `_persist_event_batch` for a queued batch of `room_id`'s events,
    /// one chunk at a time: Python awaits [`Self::batch_next`] until it
    /// resolves with a [`PersistedBatch`], applying the effects of each
    /// [`WrittenChunk`] before the next step, and calls
    /// [`Self::batch_abort`] if it cannot. Converts the events, without
    /// I/O.
    #[pyo3(signature = (room_id, events_and_contexts, *, backfilled, msc4293_enabled))]
    fn persist_batch(
        &self,
        py: Python<'_>,
        room_id: String,
        events_and_contexts: Vec<(Py<Event>, PersistContext)>,
        backfilled: bool,
        msc4293_enabled: bool,
    ) -> PyResult<PersistBatch> {
        let state = BatchState {
            inner: Arc::clone(&self.inner),
            pool: self.inner.pool.get()?,
            room_id,
            backfilled,
            msc4293_enabled,
            events: shallow_events(py, events_and_contexts),
            deduplicated: None,
            next: 0,
            guard_held: false,
            done: false,
        };
        Ok(PersistBatch {
            state: Arc::new(tokio::sync::Mutex::new(state)),
        })
    }

    /// The batch's next step: a Deferred resolving to the [`WrittenChunk`]
    /// written, or to the [`PersistedBatch`] once the batch is done, or
    /// failing as the Python path would. The chunk's stream IDs are handed
    /// to Python unfinished, so Python must not cancel the Deferred: if it
    /// is cancelled, the step runs on and the IDs are lost. If the step
    /// fails, Rust finishes them and releases the state groups.
    fn batch_next<'py>(
        &self,
        py: Python<'py>,
        batch: &PersistBatch,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_batch_next(batch);
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::batch_next`], but blocks.
    fn batch_next_blocking(&self, py: Python<'_>, batch: &PersistBatch) -> PyResult<BatchStep> {
        let future = self.start_batch_next(batch);
        self.block_on(py, future)
    }

    /// Give up the batch, as Python could not apply a chunk's effects:
    /// release its state groups as a failed persist does.
    fn batch_abort<'py>(
        &self,
        py: Python<'py>,
        batch: &PersistBatch,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_batch_abort(batch);
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::batch_abort`], but blocks.
    fn batch_abort_blocking(&self, py: Python<'_>, batch: &PersistBatch) -> PyResult<()> {
        let future = self.start_batch_abort(batch);
        self.block_on(py, future)
    }

    /// `_UpdateCurrentStateTask`: recalculate the room's current state
    /// from its forward extremities and write the change, taking one
    /// stream ID from the events stream. Resolves with a
    /// [`CurrentStateUpdate`], whose effects Python must apply. Must not be
    /// cancelled, as [`Self::persist_events_txn`].
    fn update_current_state<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_update_current_state(room_id)?;
        create_deferred_with(py, &self.inner.runtime, future, OnCancel::RunOn)
    }

    /// As [`Self::update_current_state`], but blocks.
    fn update_current_state_blocking(
        &self,
        py: Python<'_>,
        room_id: String,
    ) -> PyResult<CurrentStateUpdate> {
        let future = self.start_update_current_state(room_id)?;
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

/// `result`, keeping `timings` for the next metrics scrape if it is an
/// error, as no result carries them back to Python then.
fn reported<T>(inner: &PersisterInner, result: PyResult<T>, timings: &TxnTimings) -> PyResult<T> {
    if result.is_err() {
        inner.metrics.record_failed_txn_timings(timings.clone());
    }
    result
}

/// The events of `events_and_contexts`, sharing their data, internal
/// metadata included, with the Python events, so that Rust can read them
/// without the GIL and set their stream orderings.
fn shallow_events(
    py: Python<'_>,
    events_and_contexts: Vec<(Py<Event>, PersistContext)>,
) -> Vec<(Event, PersistContext)> {
    events_and_contexts
        .into_iter()
        .map(|(event, context)| (event.bind(py).get().shallow_clone(), context))
        .collect()
}

/// Prepare the chunk `input`, with what it counted.
async fn prepare_chunk(
    inner: &PersisterInner,
    pool: &ConnectionPool,
    input: &PrepareInput,
    shadow: bool,
) -> anyhow::Result<(Prepared, PersistCounts)> {
    let resolver = NativeStateResolver {
        pool: pool.clone(),
        handle: inner.runtime.tokio_handle()?,
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
    // Python reads in `runInteraction`s, which retry on a lost connection
    // or a cancelled statement. Here the whole chunk is prepared again.
    // That is safe as its only write, bumping the state groups it uses, can
    // be repeated, and runs in a write interaction with its own retries.
    let mut attempt = 1;
    loop {
        let mut counts = PersistCounts::default();
        match prepare::prepare_chunk(&reader, &resolver, pool, input, &options, &mut counts).await {
            Ok(prepared) => return Ok((prepared, counts)),
            Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(&e) => {
                log::warn!("[TXN RETRY] {{prepare_chunk}} {e:#} {attempt}/{MAX_ATTEMPTS}");
                attempt += 1;
            }
            Err(e) => return Err(e),
        }
    }
}

/// Write the chunk `events`, which Rust prepared as `chunk`, taking its
/// stream orderings first if `orderings` says so. Resolves with the
/// effects, whose stream IDs are unfinished.
async fn write_chunk(
    inner: &PersisterInner,
    pool: &ConnectionPool,
    room_id: &str,
    events: Vec<(Event, PersistContext)>,
    chunk: &prepare::PreparedChunk,
    orderings: StreamOrderings,
    inhibit_local_membership_updates: bool,
) -> PyResult<PersistEffects> {
    // Taken as late as possible, as Python takes them: until the chunk is
    // written, no later event on the stream can be announced.
    let event_stream = match orderings {
        StreamOrderings::Set => None,
        StreamOrderings::Allocate { backfilled } => {
            let stream = if backfilled {
                TxnStream::backfill(Arc::clone(&inner.backfill_stream))
            } else {
                TxnStream::events(Arc::clone(&inner.events_stream))
            };
            let ids = allocate_stream_orderings(pool, &stream, events.len())
                .await
                .map_err(persist_error_to_py)?;
            set_persisted_positions(inner, &events, &ids, backfilled)
                .map_err(persist_error_to_py)?;
            Some((stream, ids, backfilled))
        }
    };
    let events = events
        .into_iter()
        .map(|(event, context)| TxnEvent::new(event, context))
        .collect::<anyhow::Result<Vec<_>>>()
        .map_err(persist_error_to_py)?;

    let cache_stream = TxnStream::caches(Arc::clone(&inner.cache_stream));
    let sticky_stream = TxnStream::sticky_events(Arc::clone(&inner.sticky_stream));
    let profile_stream = TxnStream::profile_updates(Arc::clone(&inner.profile_stream));
    let input = TxnInput {
        room_id,
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
    let mut effects = run_write_interaction(pool, "persist_events", |txn, _| {
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
    // Finished on drop if the transaction failed, as `get_next_mult`
    // finishes them when its block raises.
    if let Some((stream, ids, backfilled)) = event_stream {
        stream.hand_over(&ids);
        effects.add_stream_ids(ids, backfilled);
    }
    Ok(effects)
}

/// A queued batch of one room's events, persisted chunk by chunk by
/// [`batch_next`]. See [`Persister::persist_batch`].
struct BatchState {
    inner: Arc<PersisterInner>,
    pool: ConnectionPool,
    room_id: String,
    backfilled: bool,
    /// `experimental_features.msc4293_enabled`, read per batch as Python
    /// reads it per chunk.
    msc4293_enabled: bool,
    events: Vec<(Event, PersistContext)>,
    /// Set by the first step.
    deduplicated: Option<Deduplicated>,
    /// The position in `kept` of the next chunk.
    next: usize,
    /// The state groups are marked as being persisted for the chunk last
    /// handed to Python, which the next step or [`batch_abort`] releases.
    guard_held: bool,
    /// The batch has been persisted, handed back or failed.
    done: bool,
}

/// The batch once the events already persisted are dropped.
struct Deduplicated {
    replaced_events: HashMap<String, String>,
    /// The positions in the batch of the events to persist.
    kept: Vec<usize>,
    /// What `persisting_state_group_references` holds back from deletion
    /// around each chunk: the groups of the whole batch, as in Python.
    state_groups: BTreeSet<i64>,
}

impl BatchState {
    fn shallow(&self, i: usize) -> (Event, PersistContext) {
        (self.events[i].0.shallow_clone(), self.events[i].1.clone())
    }

    /// `_finish_persisting_txn` for the chunk last written, if its state
    /// groups are still marked.
    /// Release the mark of a chunk handed back to Python after the marking,
    /// which runs alongside preparing, succeeded. As a failure: that only
    /// pushes back any pending deletion of the groups, as Python's own
    /// failure path does, where a success would cancel it before Python has
    /// persisted anything. A failure to release is logged: Python persists
    /// the chunk either way.
    async fn release_early_mark(&mut self) {
        self.guard_held = true;
        if let Err(e) = self.release_guard(true).await {
            log::warn!(
                "Failed to release the state groups of a chunk of {} handed back to Python: {e}",
                self.room_id
            );
        }
    }

    async fn release_guard(&mut self, error: bool) -> PyResult<()> {
        if !std::mem::take(&mut self.guard_held) {
            return Ok(());
        }
        let Some(deduplicated) = &self.deduplicated else {
            return Ok(());
        };
        batch::finish_persisting(
            &self.pool,
            self.inner.runtime.clock(),
            &self.inner.instance_name,
            &deduplicated.state_groups,
            error,
        )
        .await
        .map_err(write_error_to_py)
    }
}

impl Drop for BatchState {
    /// A batch dropped with its guard held, which Python should not let
    /// happen, releases it as a failure, as Python's `finally` would.
    fn drop(&mut self) {
        if !self.guard_held {
            return;
        }
        let (Some(deduplicated), Ok(handle)) =
            (self.deduplicated.take(), self.inner.runtime.tokio_handle())
        else {
            return;
        };
        let inner = Arc::clone(&self.inner);
        let pool = self.pool.clone();
        handle.spawn(async move {
            let result = batch::finish_persisting(
                &pool,
                inner.runtime.clock(),
                &inner.instance_name,
                &deduplicated.state_groups,
                true,
            )
            .await;
            if let Err(e) = result {
                log::warn!("Failed to release the state groups of a dropped batch: {e}");
            }
        });
    }
}

/// What one [`batch_next`] step did.
enum BatchStep {
    /// A chunk was written, and its effects are Python's to apply.
    Written(Box<WrittenChunk>),
    /// The batch is done.
    Done(PersistedBatch),
}

impl<'py> IntoPyObject<'py> for BatchStep {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            BatchStep::Written(chunk) => Ok(Bound::new(py, *chunk)?.into_any()),
            BatchStep::Done(batch) => Ok(Bound::new(py, batch)?.into_any()),
        }
    }
}

/// One step of `_persist_event_batch` for `state`. The first step drops the
/// events already persisted under the same transaction ID
/// (`get_already_persisted_events`). Each step releases the state groups
/// of the chunk before it, then prepares the next chunk, marks the batch's
/// state groups as being persisted, takes the chunk's stream orderings and
/// writes it.
///
/// A chunk Rust cannot prepare, because its state resolution falls back to
/// Python or preparing it fails, is handed back with the rest of the batch
/// in [`PersistedBatch::remaining`], unwritten.
async fn batch_next(state: &mut BatchState) -> PyResult<BatchStep> {
    if state.done {
        return Err(PyRuntimeError::new_err("the batch is done"));
    }
    let result = batch_step(state).await;
    if !matches!(result, Ok(BatchStep::Written(_))) {
        state.done = true;
    }
    result
}

async fn batch_step(state: &mut BatchState) -> PyResult<BatchStep> {
    if state.deduplicated.is_none() {
        // Retried as Python's `runInteraction`s are.
        let mut attempt = 1;
        let replaced_events = loop {
            let result = async {
                let querier = PoolQuerier::checkout(&state.pool).await?;
                batch::already_persisted_events(&querier, &state.events).await
            }
            .await;
            match result {
                Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(&e) => {
                    log::warn!(
                        "[TXN RETRY] {{get_already_persisted_events}} {e:#} {attempt}/{MAX_ATTEMPTS}"
                    );
                    attempt += 1;
                }
                result => break result.map_err(persist_error_to_py)?,
            }
        };
        let kept: Vec<usize> = (0..state.events.len())
            .filter(|&i| !replaced_events.contains_key(state.events[i].0.event_id()))
            .collect();
        let kept_events: Vec<_> = kept.iter().map(|&i| state.shallow(i)).collect();
        let state_groups =
            batch::referenced_state_groups(&kept_events).map_err(persist_error_to_py)?;
        state.deduplicated = Some(Deduplicated {
            replaced_events,
            kept,
            state_groups,
        });
    }
    state.release_guard(false).await?;

    let deduplicated = state.deduplicated.as_ref().expect("set above");
    let start = state.next;
    if start >= deduplicated.kept.len() {
        return Ok(BatchStep::Done(PersistedBatch {
            replaced_events: deduplicated.replaced_events.clone(),
            remaining: Vec::new(),
            fallback_reason: None,
            txn_timings: Vec::new(),
        }));
    }
    let end = (start + batch::CHUNK_SIZE).min(deduplicated.kept.len());
    let indices = deduplicated.kept[start..end].to_vec();
    let chunk: Vec<(Event, PersistContext)> = indices.iter().map(|&i| state.shallow(i)).collect();
    let hand_back = |reason: String| {
        BatchStep::Done(PersistedBatch {
            replaced_events: deduplicated.replaced_events.clone(),
            remaining: deduplicated.kept[start..].to_vec(),
            fallback_reason: Some(reason),
            txn_timings: Vec::new(),
        })
    };

    let inner = &state.inner;
    let pool = &state.pool;
    let prepare = async {
        let input = PrepareInput::new(state.room_id.clone(), &chunk, state.backfilled)?;
        prepare_chunk(inner, pool, &input, false).await
    };
    // Marking the state groups needs nothing from preparing, so it runs at
    // the same time, on its own connection.
    let guarded = !deduplicated.state_groups.is_empty();
    let mark = async {
        if !guarded {
            return Ok(());
        }
        batch::mark_state_groups_as_persisting(
            pool,
            inner.runtime.clock(),
            &inner.instance_name,
            &deduplicated.state_groups,
        )
        .await
    };
    let (prepared, marked) = futures::future::join(prepare, mark).await;
    let marked_ok = guarded && marked.is_ok();
    let (prepared, counts) = match prepared {
        Ok((Prepared::StateResFallback(reason), _)) => {
            // Python persists the chunk, and marks the groups itself.
            let done = hand_back(reason);
            if marked_ok {
                state.release_early_mark().await;
            }
            return Ok(done);
        }
        // The chunk path prepares it again, and raises or falls back to
        // Python as the error and the strictness say.
        Err(e) => {
            log::warn!(
                "Failed to prepare events for {} in Rust, handing them back: {e:#}",
                state.room_id
            );
            let done = hand_back(format!("{e:#}"));
            if marked_ok {
                state.release_early_mark().await;
            }
            return Ok(done);
        }
        Ok(prepared) => prepared,
    };
    inner.metrics.record(|total| total.add(counts));
    let prepared = Arc::new(prepared);
    let Prepared::Chunk(prepared_chunk) = &*prepared else {
        unreachable!("handed back above");
    };

    marked.map_err(write_error_to_py)?;
    if guarded {
        state.guard_held = true;
    }
    // The follow-ups read the events after the write sets their orderings.
    let chunk_copy: Vec<(Event, PersistContext)> = chunk
        .iter()
        .map(|(event, context)| (event.shallow_clone(), context.clone()))
        .collect();
    let written = write_chunk(
        inner,
        pool,
        &state.room_id,
        chunk,
        prepared_chunk,
        StreamOrderings::Allocate {
            backfilled: state.backfilled,
        },
        state.backfilled,
    )
    .await;
    let mut effects = match written {
        Ok(effects) => effects,
        Err(e) => {
            // As Python's `finally`, whose error replaces the persist's.
            state.release_guard(true).await?;
            return Err(e);
        }
    };
    // The chunk is written, so its effects go to Python whatever follows.
    let follow_up_error = follow_ups(state, &chunk_copy, prepared_chunk, &mut effects)
        .await
        .err();
    state.next = end;
    Ok(BatchStep::Written(Box::new(WrittenChunk {
        effects,
        prepared,
        indices,
        follow_up_error: Mutex::new(follow_up_error),
    })))
}

/// `_after_persisting_events` for a chunk Rust wrote: count it, record the
/// MSC4293 redactions it starts or ends, and prefill the room's forward
/// extremities. Runs before the chunk's stream orderings are finished, as
/// in Python, so the events are not announced before their redactions are
/// recorded.
async fn follow_ups(
    state: &BatchState,
    chunk: &[(Event, PersistContext)],
    prepared: &prepare::PreparedChunk,
    effects: &mut PersistEffects,
) -> PyResult<()> {
    let (inner, pool, room_id) = (&state.inner, &state.pool, state.room_id.as_str());
    let counts = follow_ups::count_chunk(chunk, &inner.server_name, state.backfilled);
    inner.metrics.record(|total| total.add(counts));
    if state.msc4293_enabled {
        let reader = inner.sync.reader(pool.clone());
        let mut started = false;
        let recorded = follow_ups::record_room_ban_redactions(
            pool,
            &reader,
            room_id,
            chunk,
            || inner.events_stream.get_persisted_upto_position(),
            &mut started,
        )
        .await;
        // Even if a later event failed, for those that started.
        if started {
            effects.add_event_cache_room_invalidation(room_id.to_owned());
        }
        recorded.map_err(persist_error_to_py)?;
    }
    if let Some(extremities) = prepared
        .state
        .new_forward_extremities
        .as_ref()
        .filter(|e| !e.is_empty())
    {
        let mut extremities: Vec<String> = extremities.iter().map(|e| e.to_string()).collect();
        extremities.sort();
        effects.set_latest_event_ids(room_id.to_owned(), extremities);
    }
    Ok(())
}

/// [`batch_next`]'s chunk, written, whose effects Python must apply.
#[pyclass(frozen)]
pub struct WrittenChunk {
    effects: PersistEffects,
    prepared: Arc<Prepared>,
    indices: Vec<usize>,
    /// Why what follows the write failed, for Python to raise once it has
    /// applied the effects.
    follow_up_error: Mutex<Option<PyErr>>,
}

#[pymethods]
impl WrittenChunk {
    /// What Python applies with `apply_rust_persist_effects`, with the
    /// chunk's stream IDs unfinished.
    #[getter]
    fn effects(&self) -> PersistEffects {
        self.effects.clone()
    }

    /// What Rust prepared for the chunk.
    #[getter]
    fn prepared(&self) -> PreparedChunk {
        PreparedChunk {
            prepared: Arc::clone(&self.prepared),
            // Counted when it was prepared.
            counts: Mutex::new(None),
            // Recorded with the chunk's effects.
            txn_timings: Vec::new(),
        }
    }

    /// The chunk's positions in the batch.
    #[getter]
    fn indices(&self) -> Vec<usize> {
        self.indices.clone()
    }

    /// Raise the error of what followed the write, if it failed. Python
    /// calls this once it has applied the effects, as the chunk is written
    /// either way.
    fn check_follow_ups(&self) -> PyResult<()> {
        match self
            .follow_up_error
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }
}

/// `_update_current_state`: recalculate `room_id`'s current state and
/// write the change, if there is one. Reads are retried as Python's
/// `runInteraction`s are.
async fn update_current_state(
    inner: &Arc<PersisterInner>,
    pool: &ConnectionPool,
    room_id: &str,
) -> PyResult<CurrentStateUpdate> {
    let resolver = NativeStateResolver {
        pool: pool.clone(),
        handle: inner.runtime.tokio_handle()?,
        caches: Arc::clone(&inner.store.caches),
        conflict_cache: Arc::clone(&inner.conflict_cache),
    };
    let reader = PersistReader::new(inner.sync.reader(pool.clone()));
    let mut attempt = 1;
    let computed = loop {
        let result = async {
            let (state, stats) =
                state_delta::calculate_current_state(&reader, &resolver, room_id).await?;
            let delta = state_delta::calculate_state_delta(&reader, room_id, &state, true).await?;
            let changes = sliding_sync::calculate_sliding_sync_table_changes(
                &reader,
                room_id,
                &inner.server_name,
                &[],
                &delta,
            )
            .await?;
            anyhow::Ok((delta, changes, stats))
        }
        .await;
        match result {
            Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(&e) => {
                log::warn!("[TXN RETRY] {{update_current_state}} {e:#} {attempt}/{MAX_ATTEMPTS}");
                attempt += 1;
            }
            result => break result,
        }
    };
    let (delta, changes, stats) = match computed {
        Ok(computed) => computed,
        Err(e) => {
            return match e.downcast::<StateResFallback>() {
                Ok(fallback) => Ok(CurrentStateUpdate {
                    effects: None,
                    state_res_stats: None,
                    fallback_reason: Some(fallback.0),
                    txn_timings: Vec::new(),
                }),
                Err(e) => Err(persist_error_to_py(e)),
            };
        }
    };
    // `DeltaState.is_noop`.
    if delta.to_delete.is_empty() && delta.to_insert.is_empty() && !delta.no_longer_in_room {
        return Ok(CurrentStateUpdate {
            effects: None,
            state_res_stats: stats,
            fallback_reason: None,
            txn_timings: Vec::new(),
        });
    }

    // `get_next`, as late as can be.
    let event_stream = TxnStream::events(Arc::clone(&inner.events_stream));
    let ids = allocate_stream_orderings(pool, &event_stream, 1)
        .await
        .map_err(persist_error_to_py)?;
    let stream_id = *ids
        .first()
        .ok_or_else(|| PyRuntimeError::new_err("no stream ID"))?;

    let no_chunk = prepare::PreparedChunk {
        state: Default::default(),
        new_event_links: HashMap::new(),
        sliding_sync: None,
        sticky_events_to_un_soft_fail: HashSet::new(),
    };
    let cache_stream = TxnStream::caches(Arc::clone(&inner.cache_stream));
    let sticky_stream = TxnStream::sticky_events(Arc::clone(&inner.sticky_stream));
    let profile_stream = TxnStream::profile_updates(Arc::clone(&inner.profile_stream));
    let input = TxnInput {
        room_id,
        events: &[],
        prepared: &no_chunk,
        instance_name: &inner.instance_name,
        server_name: &inner.server_name,
        clock: inner.runtime.clock(),
        inhibit_local_membership_updates: false,
        enable_search: inner.enable_search,
        enable_ephemeral_messages: inner.enable_ephemeral_messages,
        msc4354_enabled: inner.msc4354_enabled,
        include_profile_updates_in_sync: inner.include_profile_updates_in_sync,
        cache_stream: &cache_stream,
        sticky_stream: &sticky_stream,
        profile_stream: &profile_stream,
    };
    let mut effects = run_write_interaction(pool, "update_current_state", |txn, _| {
        let (input, delta, changes) = (&input, &delta, &changes);
        async move {
            let mut effects = PersistEffects::default();
            txn::update_current_state_txn(txn, input, delta, changes, stream_id, &mut effects)
                .await?;
            Ok(effects)
        }
        .boxed()
    })
    .await
    .map_err(write_error_to_py)?;
    cache_stream.hand_over(effects.cache_stream_ids());
    sticky_stream.hand_over(effects.sticky_stream_ids());
    profile_stream.hand_over(effects.profile_stream_ids());
    // Finished on drop if the transaction failed, as `get_next` finishes
    // it when its block raises.
    event_stream.hand_over(&ids);
    effects.add_stream_ids(ids, false);
    Ok(CurrentStateUpdate {
        effects: Some(effects),
        state_res_stats: stats,
        fallback_reason: None,
        txn_timings: Vec::new(),
    })
}

/// What [`Persister::update_current_state`] did.
#[pyclass(frozen)]
pub struct CurrentStateUpdate {
    effects: Option<PersistEffects>,
    state_res_stats: Option<StateResStats>,
    fallback_reason: Option<String>,
    /// What the update's write interactions took.
    txn_timings: TxnTimings,
}

#[pymethods]
impl CurrentStateUpdate {
    /// What Python applies with `apply_rust_persist_effects`, with the
    /// stream ID unfinished. `None` if the state did not change, or if
    /// Rust fell back.
    #[getter]
    fn effects(&self) -> Option<PersistEffects> {
        self.effects.clone()
    }

    /// The stats of the state resolution, if the state was resolved.
    #[getter]
    fn state_res_stats(&self) -> Option<StateResStats> {
        self.state_res_stats.clone()
    }

    /// Why the Rust state resolution could not resolve the room, in which
    /// case nothing was written and Python should recalculate the state.
    #[getter]
    fn fallback_reason(&self) -> Option<String> {
        self.fallback_reason.clone()
    }

    /// `(name, seconds)` of each write interaction the update ran.
    #[getter]
    fn txn_timings(&self) -> TxnTimings {
        self.txn_timings.clone()
    }
}

/// Where the stream orderings of a chunk come from.
#[derive(Clone, Copy)]
enum StreamOrderings {
    /// Python set them on the events.
    Set,
    /// Rust takes them from the events stream, or from the backfill stream
    /// if the chunk is backfilled.
    Allocate { backfilled: bool },
}

/// `n` IDs from `stream`, unfinished, retried as Python's `runInteraction`
/// retries the fetch.
async fn allocate_stream_orderings(
    pool: &ConnectionPool,
    stream: &TxnStream,
    n: usize,
) -> anyhow::Result<Vec<i64>> {
    let start = std::time::Instant::now();
    let mut attempt = 1;
    loop {
        let result = async {
            let querier = PoolQuerier::checkout(pool).await?;
            stream.next_ids_outside_txn(&querier, n).await
        }
        .await;
        // As Python's `runInteraction`, retries included.
        if !matches!(&result, Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(e)) {
            record_txn_timing("_load_next_mult_id", start.elapsed());
        }
        match result {
            Err(e) if attempt < MAX_ATTEMPTS && is_retryable_error(&e) => {
                log::warn!("[TXN RETRY] {{_load_next_mult_id}} {e:#} {attempt}/{MAX_ATTEMPTS}");
                attempt += 1;
            }
            result => return result,
        }
    }
}

/// Set each event's stream ordering from `ids`, in order, with the time
/// and this instance, as `_persist_events_and_state_updates` does once it
/// has its stream orderings. Backfilled events take theirs negated. An
/// event that appears twice keeps the later ordering, as in Python.
fn set_persisted_positions(
    inner: &PersisterInner,
    events: &[(Event, PersistContext)],
    ids: &[i64],
    backfilled: bool,
) -> anyhow::Result<()> {
    if ids.len() != events.len() {
        anyhow::bail!(
            "took {} stream orderings for {} events",
            ids.len(),
            events.len()
        );
    }
    let received_ts = i64::try_from(inner.runtime.clock().now_millis())?;
    for ((event, _), &id) in events.iter().zip(ids) {
        let stream_ordering = if backfilled { -id } else { id };
        event.internal_metadata().set_persisted_position(
            Some(stream_ordering),
            Some(received_ts),
            Some(inner.instance_name.clone()),
        )?;
    }
    Ok(())
}

/// A batch being persisted. See [`Persister::persist_batch`].
#[pyclass(frozen)]
pub struct PersistBatch {
    state: Arc<tokio::sync::Mutex<BatchState>>,
}

/// What [`Persister::persist_batch`] did, once the batch is done.
#[pyclass(frozen)]
pub struct PersistedBatch {
    replaced_events: HashMap<String, String>,
    remaining: Vec<usize>,
    fallback_reason: Option<String>,
    /// What the last step's write interactions took.
    txn_timings: TxnTimings,
}

#[pymethods]
impl PersistedBatch {
    /// The events not persisted as they repeat an event already persisted
    /// with the same transaction ID, mapped to that event.
    #[getter]
    fn replaced_events(&self) -> HashMap<String, String> {
        self.replaced_events.clone()
    }

    /// The positions in the batch of the events left for Python to
    /// persist, in order: the chunk Rust could not prepare and those after
    /// it. Empty if Rust persisted them all.
    #[getter]
    fn remaining(&self) -> Vec<usize> {
        self.remaining.clone()
    }

    /// Why Rust left the `remaining` events.
    #[getter]
    fn fallback_reason(&self) -> Option<String> {
        self.fallback_reason.clone()
    }

    /// `(name, seconds)` of each write interaction the last step ran.
    #[getter]
    fn txn_timings(&self) -> TxnTimings {
        self.txn_timings.clone()
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
    /// What preparing's write interactions took.
    txn_timings: TxnTimings,
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
    /// `(name, seconds)` of each write interaction preparing ran.
    #[getter]
    fn txn_timings(&self) -> TxnTimings {
        self.txn_timings.clone()
    }

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
    if let Some(collision) = e.downcast_ref::<RoomIdCollision>() {
        return match py_room_id_collision_error(&collision.0) {
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

/// A `RoomIdCollisionError`, as the Python transaction raises.
fn py_room_id_collision_error(room_id: &str) -> PyResult<PyErr> {
    crate::gil::with_gil("persist_room_id_collision", |py| {
        let error = py
            .import("synapse.api.errors")?
            .getattr("RoomIdCollisionError")?
            .call1((room_id,))?;
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
