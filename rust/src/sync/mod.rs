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
//! [`filter`] ports `synapse.api.filtering`.
//! [`crate::visibility`] and [`crate::types::stream_token`] are shared with
//! later ports.

use std::sync::Arc;

use pyo3::{create_exception, exceptions::PyException, prelude::*, types::PyModule};

use crate::runtime::RustRuntime;
use crate::storage::store::Store;

pub mod filter;

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
    /// The Rust store, for its event and state group caches.
    #[allow(dead_code)]
    store: Arc<Store>,
}

impl SyncHandler {
    pub fn new(db_pool: Py<PyAny>, runtime: RustRuntime, store: Arc<Store>) -> Self {
        SyncHandler {
            db_pool,
            runtime,
            store,
        }
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "sync")?;
    child_module.add_class::<SyncHandler>()?;
    child_module.add_class::<filter::FilterCollection>()?;
    child_module.add("RustSyncFallback", py.get_type::<RustSyncFallback>())?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import sync` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.sync", child_module)?;

    Ok(())
}
