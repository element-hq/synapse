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

use std::sync::Arc;

use pyo3::{
    intern,
    prelude::*,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, PyResult, Python,
};

use crate::homeserver::HomeServer;
use crate::persist::python::Persister;
use crate::state::StateResolutionHandler;
use crate::sync::SyncHandler;

pub mod versions;

#[pyclass]
struct RustHandlers {
    versions: Py<versions::VersionsHandler>,
    /// Only when the main database uses the native Rust driver.
    state_resolution: Option<Py<StateResolutionHandler>>,
    /// Only when the main database uses the native Rust driver.
    sync: Option<Py<SyncHandler>>,
    /// Only when `rust_event_persistence` is not `off`, the main database
    /// uses the native Rust driver and this instance writes events.
    persister: Option<Py<Persister>>,
}

#[pymethods]
impl RustHandlers {
    #[new]
    #[pyo3(signature = (homeserver))]
    pub fn py_new(py: Python<'_>, homeserver: HomeServer) -> PyResult<RustHandlers> {
        let config = homeserver.config(py)?;

        // The per-homeserver Rust state, which gives us the tokio runtime
        // and the Twisted reactor.
        let runtime = homeserver.get_rust_runtime(py)?;

        // The main datastore builds the store and keeps it; we share it. See
        // `crate::storage::python`.
        let store = homeserver.get_rust_store(py)?;

        let global_unstable_feature_map = Arc::new(
            versions::synapse_config_to_global_unstable_feature_map(&config),
        );

        let versions = Py::new(
            py,
            versions::VersionsHandler {
                global_unstable_feature_map: Arc::clone(&global_unstable_feature_map),
                store: Arc::clone(&store),
                runtime: runtime.clone(),
            },
        )?;

        // The Rust state resolution and sync read the database through the
        // native driver's pool, so they need the main database to use that
        // driver.
        let connection_pool = homeserver
            .main_database_pool(py)?
            .getattr(intern!(py, "_db_pool"))?;
        let rust_connection_pool_class = py
            .import("synapse.storage.rust_pool")?
            .getattr("RustConnectionPool")?;
        let (state_resolution, sync, persister) =
            if connection_pool.is_instance(&rust_connection_pool_class)? {
                let connection_pool = connection_pool.unbind();
                let state_resolution = StateResolutionHandler::new(
                    connection_pool.clone_ref(py),
                    runtime.clone(),
                    Arc::clone(&store),
                );
                let persister = if config.experimental.rust_event_persistence != "off" {
                    Persister::for_homeserver(
                        py,
                        &homeserver,
                        connection_pool.clone_ref(py),
                        runtime.clone(),
                        Arc::clone(&store),
                        state_resolution.conflict_cache(),
                    )?
                    .map(|persister| Py::new(py, persister))
                    .transpose()?
                } else {
                    None
                };
                (
                    Some(Py::new(py, state_resolution)?),
                    Some(Py::new(
                        py,
                        SyncHandler::new(connection_pool, runtime.clone(), store, &config),
                    )?),
                    persister,
                )
            } else {
                (None, None, None)
            };

        Ok(RustHandlers {
            versions,
            state_resolution,
            sync,
            persister,
        })
    }

    #[getter]
    fn versions(&self, py: Python<'_>) -> Py<versions::VersionsHandler> {
        self.versions.clone_ref(py)
    }

    /// The Rust state resolution, or `None` if the main database does not
    /// use the native Rust driver.
    #[getter]
    fn state_resolution(&self, py: Python<'_>) -> Option<Py<StateResolutionHandler>> {
        self.state_resolution.as_ref().map(|h| h.clone_ref(py))
    }

    /// The Rust sync handler, or `None` if the main database does not use
    /// the native Rust driver.
    #[getter]
    fn sync(&self, py: Python<'_>) -> Option<Py<SyncHandler>> {
        self.sync.as_ref().map(|h| h.clone_ref(py))
    }

    /// The Rust event persister, or `None` if `rust_event_persistence` is
    /// `off`, the main database does not use the native Rust driver, or this
    /// instance does not write events.
    #[getter]
    fn persister(&self, py: Python<'_>) -> Option<Py<Persister>> {
        self.persister.as_ref().map(|h| h.clone_ref(py))
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "handlers")?;
    child_module.add_class::<RustHandlers>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import push` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.handlers", child_module)?;

    Ok(())
}
