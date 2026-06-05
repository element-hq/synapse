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
    prelude::*,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, PyResult, Python,
};

use crate::config::SynapseConfig;
use crate::storage::db::python_db_pool::PythonDatabasePool;
use crate::storage::store::Store;
use crate::UnwrapInfallible;

pub mod versions;

#[pyclass]
struct RustHandlers {
    versions: versions::VersionsHandler,
}

#[pymethods]
impl RustHandlers {
    #[new]
    #[pyo3(signature = (homeserver))]
    pub fn py_new(py: Python<'_>, homeserver: &Bound<'_, PyAny>) -> PyResult<RustHandlers> {
        let config: SynapseConfig = homeserver.getattr("config")?.extract()?;

        // hs.get_datastores().main.db_pool
        let db_pool: PythonDatabasePool = homeserver
            .call_method0("get_datastores")?
            .into_pyobject(py)
            .unwrap_infallible()
            .getattr("main")?
            .getattr("db_pool")?
            .extract()?;

        // Store is shared across all of the handlers so let's use an `Arc`
        let store = Arc::new(Store { config, db_pool });

        Ok(RustHandlers {
            versions: versions::VersionsHandler {
                config,
                store: Arc::clone(&store),
            },
        })
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
