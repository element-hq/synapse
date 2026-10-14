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

use std::sync::{Arc, Mutex};

use pyo3::{create_exception, exceptions::PyException, prelude::*, types::PyModule};

use crate::caches::lock_ignoring_poison;
use crate::config::SynapseHomeServerConfig;
use crate::runtime::RustRuntime;
use crate::storage::store::Store;
use crate::storage::sync_store::{SyncStore, SyncStoreConfig};
use crate::types::stream_token::RoomStreamToken;

pub mod filter;
pub mod ll_members;
pub mod state_delta;
pub mod summary;

use ll_members::LazyLoadedMembersCaches;

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
    // Read by the per-room entry points, which are still to come.
    #[allow(dead_code)]
    db_pool: Py<PyAny>,
    #[allow(dead_code)]
    runtime: RustRuntime,
    /// The queries, over the Rust store's caches.
    store: Arc<SyncStore>,
    /// The lazy-loaded members sent to each device. Held here rather than
    /// on the store: they are per device, and no data change invalidates
    /// them.
    ll_members: Mutex<LazyLoadedMembersCaches>,
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
            ll_members: Mutex::new(LazyLoadedMembersCaches::new()),
        }
    }

    pub(crate) fn store(&self) -> &Arc<SyncStore> {
        &self.store
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
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sync")?;
    child_module.add_class::<SyncHandler>()?;
    child_module.add_class::<filter::FilterCollection>()?;
    child_module.add_class::<ll_members::LazyLoadedMembersCache>()?;
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
