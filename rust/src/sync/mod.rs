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

//! `/sync` in Rust, reading the database through the native Postgres driver.
//!
//! [`SyncHandler`] is the Python entry point, used by
//! `synapse.handlers.sync.SyncHandler` when `rust_sync` is on. It computes
//! the per-room part of a sync response, one room at a time, and raises
//! [`RustSyncFallback`] for a room it cannot handle, which Python then
//! computes itself.
//!
//! The pure parts of the per-room loop are each a port of the Python they
//! are named after, checked against it by the tests in `tests/sync/`:
//! [`filter`] ports `synapse.api.filtering`, [`state_delta`] the state
//! delta arithmetic of the sync handler, [`ll_members`] the per-device cache
//! of lazy-loaded members sent, and [`summary`] the room summary's heroes.
//! [`crate::visibility`] and [`crate::types::stream_token`] are shared with
//! later ports.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use pyo3::{
    create_exception,
    exceptions::{PyException, PyRuntimeError},
    intern,
    prelude::*,
    types::PyModule,
};

use crate::caches::lock_ignoring_poison;
use crate::clock::Clock;
use crate::config::SynapseHomeServerConfig;
use crate::database::postgres::pool::PyConnectionPool;
use crate::deferred::{create_deferred_with, OnCancel};
use crate::runtime::RustRuntime;
use crate::storage::store::Store;
use crate::storage::sync_store::{SyncStore, SyncStoreConfig};
use crate::types::stream_token::RoomStreamToken;

pub mod client_events;
pub mod filter;
pub mod ll_members;
pub mod request;
pub mod room;
pub mod room_state;
pub mod state_delta;
pub mod summary;
pub mod timeline;

use client_events::ClientFilterContext;
use ll_members::LazyLoadedMembersCaches;
use request::{RoomSyncInput, RoomSyncResult, SyncRequest};
use room::{Fallback, RoomContext};

create_exception!(
    synapse.synapse_rust.sync,
    RustSyncFallback,
    PyException,
    "The Rust sync handler cannot compute this room's entry. Compute it with \
     the Python implementation."
);

/// Computes sync responses with the Rust implementation, reading the
/// database through the native Postgres driver.
#[pyclass(frozen)]
pub struct SyncHandler {
    /// The Python `RustConnectionPool`. Its `_pool` is read per call, as
    /// `close()`/`start()` replace it.
    db_pool: Py<PyAny>,
    runtime: RustRuntime,
    /// The queries, over the Rust store's caches.
    store: Arc<SyncStore>,
    /// The lazy-loaded members sent to each device. Held here rather than
    /// on the store: they are per device, and no data change invalidates
    /// them.
    ll_members: Arc<Mutex<LazyLoadedMembersCaches>>,
}

/// One room's entry, started. See [`SyncHandler::start_room`].
type RoomFuture = Pin<Box<dyn Future<Output = PyResult<Option<RoomSyncResult>>> + Send>>;

impl SyncHandler {
    pub fn new(
        db_pool: Py<PyAny>,
        runtime: RustRuntime,
        store: Arc<Store>,
        config: &SynapseHomeServerConfig,
    ) -> Self {
        let store = SyncStore::new(
            Arc::clone(&store.caches),
            SyncStoreConfig::from_config(config),
        );
        SyncHandler {
            db_pool,
            runtime,
            store: Arc::new(store),
            ll_members: Arc::new(Mutex::new(LazyLoadedMembersCaches::new())),
        }
    }

    pub(crate) fn store(&self) -> &Arc<SyncStore> {
        &self.store
    }

    /// Start computing `room`'s entry: read what it needs from Python, and
    /// return the future that computes it off the GIL.
    fn start_room(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        room: &Bound<'_, RoomSyncInput>,
    ) -> PyResult<RoomFuture> {
        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        let pool = pool.cast::<PyConnectionPool>()?.get().pool.clone();

        let request = Arc::clone(&request.get().data);
        let room = Arc::clone(&room.get().data);
        let store = Arc::clone(&self.store);
        let ll_members = Arc::clone(&self.ll_members);

        Ok(Box::pin(async move {
            let reader = store.reader(pool);
            let context = RoomContext {
                reader: &reader,
                request: &request,
                client_filter: ClientFilterContext {
                    user_id: Arc::clone(&request.user_id),
                    ignored_users: Arc::clone(&request.ignored_users),
                    admin_policy: request.admin_policy,
                    msc4354_enabled: request.msc4354_enabled,
                },
                ll_members: &ll_members,
                ll_pending: Mutex::default(),
                clock: Clock::new(),
            };
            match room::generate_room_entry(&context, &room).await {
                Ok(result) => {
                    context.commit_ll_members();
                    Ok(result.map(|data| RoomSyncResult { data }))
                }
                Err(err) => Err(room_error_to_py(err, &room.room_id)),
            }
        }))
    }
}

/// A failed room as Python sees it: [`RustSyncFallback`] for a room it
/// should compute itself, an error otherwise.
fn room_error_to_py(err: anyhow::Error, room_id: &str) -> PyErr {
    match err.downcast::<Fallback>() {
        Ok(fallback) => RustSyncFallback::new_err(fallback.0),
        Err(err) => match err.downcast::<PyErr>() {
            Ok(err) => err,
            Err(err) => PyRuntimeError::new_err(format!("syncing room {room_id}: {err:#}")),
        },
    }
}

#[pymethods]
impl SyncHandler {
    /// Forget the lazy-loaded members a device did not receive, at the
    /// start of its request from `since`, the request's room position
    /// (`None` for an initial sync). See [`ll_members`].
    #[pyo3(signature = (user_id, device_id, since))]
    fn start_request(
        &self,
        user_id: &str,
        device_id: Option<&str>,
        since: Option<RoomStreamToken>,
    ) {
        let now = self.runtime.clock().now();
        lock_ignoring_poison(&self.ll_members)
            .get_or_create(user_id, device_id, now)
            .start_request(since.as_ref());
    }

    /// Whether `event_id` is the membership of `member` last sent to the
    /// device. For rooms Python computes in `rust_sync: on` mode, which
    /// share the Rust handler's caches.
    #[pyo3(signature = (user_id, device_id, member, event_id))]
    fn ll_member_was_sent(
        &self,
        user_id: &str,
        device_id: Option<&str>,
        member: &str,
        event_id: &str,
    ) -> bool {
        let now = self.runtime.clock().now();
        lock_ignoring_poison(&self.ll_members)
            .get_or_create(user_id, device_id, now)
            .was_sent(member, event_id)
    }

    /// Record that `event_id` is being sent to the device as the membership
    /// of `member`, in the response ending at `next_batch`. As
    /// [`Self::ll_member_was_sent`].
    #[pyo3(signature = (user_id, device_id, member, event_id, next_batch))]
    fn ll_member_mark_sent(
        &self,
        user_id: &str,
        device_id: Option<&str>,
        member: &str,
        event_id: &str,
        next_batch: RoomStreamToken,
    ) {
        let now = self.runtime.clock().now();
        lock_ignoring_poison(&self.ll_members)
            .get_or_create(user_id, device_id, now)
            .mark_sent(member, event_id, &next_batch);
    }

    /// `_generate_room_entry` for one joined or archived room.
    ///
    /// Returns a deferred resolving to a `RoomSyncResult`, or to `None`
    /// when the room has nothing to send. Fails with `RustSyncFallback` for
    /// a room the Python implementation must compute. Cancelling the
    /// deferred stops the computation.
    fn generate_room_entry<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SyncRequest>,
        room: &Bound<'py, RoomSyncInput>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_room(py, request, room)?;
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// As [`Self::generate_room_entry`], but blocks (releasing the GIL) and
    /// returns the result directly. For tests: see
    /// `_BlockingRustSyncHandler`.
    fn generate_room_entry_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        room: &Bound<'_, RoomSyncInput>,
    ) -> PyResult<Option<RoomSyncResult>> {
        let future = self.start_room(py, request, room)?;
        let handle = self.runtime.tokio_handle()?;
        py.detach(|| handle.block_on(future))
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sync")?;
    child_module.add_class::<SyncHandler>()?;
    child_module.add_class::<filter::FilterCollection>()?;
    child_module.add_class::<ll_members::LazyLoadedMembersCache>()?;
    child_module.add_class::<SyncRequest>()?;
    child_module.add_class::<RoomSyncInput>()?;
    child_module.add_class::<RoomSyncResult>()?;
    state_delta::register_test_functions(py, &child_module)?;
    summary::register_test_functions(py, &child_module)?;
    crate::storage::sync_store::register_test_classes(&child_module)?;
    child_module.add("RustSyncFallback", py.get_type::<RustSyncFallback>())?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import sync` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.sync", child_module)?;

    Ok(())
}
