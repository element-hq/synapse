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
//! the `rooms` section of a sync response ([`rooms`]): which rooms to
//! report, then each joined or archived room's entry ([`room`]). It leaves
//! a room it cannot handle to Python, which computes it itself.
//!
//! The pure parts of the per-room loop are each a port of the Python they
//! are named after, checked against it by the tests in `tests/sync/`:
//! [`filter`] ports `synapse.api.filtering`, [`state_delta`] the state
//! delta arithmetic of the sync handler, [`ll_members`] the per-device cache
//! of lazy-loaded members sent, and [`summary`] the room summary's heroes.
//! [`crate::visibility`] and [`crate::types::stream_token`] are shared with
//! later ports.

use std::sync::{Arc, Mutex};

use futures::future::BoxFuture;
use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyModule};
use tracing::Instrument;

use crate::caches::lock_ignoring_poison;
use crate::config::SynapseHomeServerConfig;
use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::deferred::{create_deferred_with, OnCancel};
use crate::runtime::RustRuntime;
use crate::storage::store::Store;
use crate::storage::sync_store::{SyncStore, SyncStoreConfig};
use crate::types::stream_token::RoomStreamToken;

pub mod client_events;
pub mod encode;
pub mod filter;
pub mod ll_members;
pub mod relations;
pub mod request;
pub mod room;
pub mod room_state;
pub mod rooms;
pub mod state_delta;
pub mod summary;
pub mod timeline;

use ll_members::LazyLoadedMembersCaches;
use request::{RoomSyncResult, SyncRequest};
use rooms::{RoomChanges, RoomsInput, RoomsResult};

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

    /// The connection pool to read through, or an error if it is closed.
    fn pool(&self, py: Python<'_>) -> PyResult<ConnectionPool> {
        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
    }

    /// Start working out the request's room changes: read what it needs
    /// from Python, and return the future that does it off the GIL.
    fn start_room_changes(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        input: &Bound<'_, RoomsInput>,
    ) -> PyResult<BoxFuture<'static, PyResult<RoomChanges>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let input = input.get().take()?;
        let store = Arc::clone(&self.store);
        Ok(Box::pin(async move {
            let reader = store.reader(pool);
            let data = rooms::room_changes(&reader, &request, input)
                .instrument(tracing::info_span!("sync.room_changes"))
                .await
                .map_err(anyhow_to_py)?;
            Ok(RoomChanges {
                data: Arc::new(data),
            })
        }))
    }

    /// Start computing the entries of `changes`' rooms: read each room's
    /// extras from Python, and return the future that computes them off the
    /// GIL.
    #[allow(clippy::too_many_arguments)]
    fn start_room_entries(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        changes: &Bound<'_, RoomChanges>,
        ephemeral_by_room: &Bound<'_, PyAny>,
        tags_by_room: &Bound<'_, PyAny>,
        account_data_by_room: &Bound<'_, PyAny>,
        sticky_by_room: &Bound<'_, PyAny>,
    ) -> PyResult<BoxFuture<'static, PyResult<RoomsResult>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let changes = Arc::clone(&changes.get().data);
        let rooms = changes
            .entries
            .iter()
            .map(|plan| {
                let extras = rooms::RoomExtras::from_py(
                    &plan.room_id,
                    ephemeral_by_room,
                    tags_by_room,
                    account_data_by_room,
                    sticky_by_room,
                )?;
                Ok(rooms::room_input(&changes, plan, extras))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let store = Arc::clone(&self.store);
        let ll_members = Arc::clone(&self.ll_members);
        Ok(Box::pin(async move {
            let reader = store.reader(pool);
            let span = tracing::info_span!("sync.room_entries", rooms = rooms.len() as u64);
            let outcomes = rooms::generate_room_entries(&reader, &request, &ll_members, rooms)
                .instrument(span)
                .await;
            Ok(RoomsResult::new(&changes, outcomes))
        }))
    }

    /// Run `future` to completion on the runtime, releasing the GIL. For the
    /// `_blocking` entry points.
    fn block_on<T: Send + 'static>(
        &self,
        py: Python<'_>,
        future: BoxFuture<'static, PyResult<T>>,
    ) -> PyResult<T> {
        self.runtime.block_on_scoped(py, future)?
    }
}

/// An error as Python sees it: the Python exception it wraps, or a
/// `RuntimeError`.
fn anyhow_to_py(err: anyhow::Error) -> PyErr {
    match err.downcast::<PyErr>() {
        Ok(err) => err,
        Err(err) => PyRuntimeError::new_err(format!("{err:#}")),
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

    /// `_get_room_changes_for_incremental_sync` or
    /// `_get_room_changes_for_initial_sync`: a deferred resolving to a
    /// `RoomChanges`. Consumes `input`. Cancelling the deferred stops the
    /// computation.
    fn room_changes<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SyncRequest>,
        input: &Bound<'py, RoomsInput>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_room_changes(py, request, input)?;
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// As [`Self::room_changes`], but blocks. For tests: see
    /// `_BlockingRustSyncHandler`.
    fn room_changes_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        input: &Bound<'_, RoomsInput>,
    ) -> PyResult<RoomChanges> {
        let future = self.start_room_changes(py, request, input)?;
        self.block_on(py, future)
    }

    /// The entries of the joined and archived rooms of `changes`, ten at a
    /// time: a deferred resolving to a `RoomsResult`. The `*_by_room`
    /// mappings are what `handle_room_entries` reads for each room.
    /// Cancelling the deferred stops the computation.
    #[pyo3(signature = (request, changes, *, ephemeral_by_room, tags_by_room, account_data_by_room, sticky_by_room))]
    #[allow(clippy::too_many_arguments)]
    fn generate_room_entries<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, SyncRequest>,
        changes: &Bound<'py, RoomChanges>,
        ephemeral_by_room: &Bound<'py, PyAny>,
        tags_by_room: &Bound<'py, PyAny>,
        account_data_by_room: &Bound<'py, PyAny>,
        sticky_by_room: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_room_entries(
            py,
            request,
            changes,
            ephemeral_by_room,
            tags_by_room,
            account_data_by_room,
            sticky_by_room,
        )?;
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// As [`Self::generate_room_entries`], but blocks.
    #[pyo3(signature = (request, changes, *, ephemeral_by_room, tags_by_room, account_data_by_room, sticky_by_room))]
    #[allow(clippy::too_many_arguments)]
    fn generate_room_entries_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, SyncRequest>,
        changes: &Bound<'_, RoomChanges>,
        ephemeral_by_room: &Bound<'_, PyAny>,
        tags_by_room: &Bound<'_, PyAny>,
        account_data_by_room: &Bound<'_, PyAny>,
        sticky_by_room: &Bound<'_, PyAny>,
    ) -> PyResult<RoomsResult> {
        let future = self.start_room_entries(
            py,
            request,
            changes,
            ephemeral_by_room,
            tags_by_room,
            account_data_by_room,
            sticky_by_room,
        )?;
        self.block_on(py, future)
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sync")?;
    child_module.add_class::<SyncHandler>()?;
    child_module.add_class::<filter::FilterCollection>()?;
    child_module.add_class::<ll_members::LazyLoadedMembersCache>()?;
    child_module.add_class::<SyncRequest>()?;
    child_module.add_class::<RoomSyncResult>()?;
    child_module.add_class::<RoomsInput>()?;
    child_module.add_class::<RoomChanges>()?;
    child_module.add_class::<RoomsResult>()?;
    child_module.add_class::<encode::RoomsEncoder>()?;
    state_delta::register_test_functions(py, &child_module)?;
    summary::register_test_functions(py, &child_module)?;
    crate::storage::sync_store::register_test_classes(&child_module)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import sync` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.sync", child_module)?;

    Ok(())
}
