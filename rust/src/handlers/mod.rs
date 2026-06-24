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
use crate::storage::db::python_db_pool::PythonDatabasePoolWrapper;
use crate::storage::store::Store;

pub mod versions;

#[pyclass]
struct RustHandlers {
    versions: Py<versions::VersionsHandler>,
}

#[pymethods]
impl RustHandlers {
    #[new]
    #[pyo3(signature = (homeserver))]
    pub fn py_new(py: Python<'_>, homeserver: &Bound<'_, PyAny>) -> PyResult<RustHandlers> {
        let config: SynapseConfig = homeserver.getattr("config")?.extract()?;

        // The Twisted reactor, used both to drive our Tokio runtime and to
        // marshal database work back onto the reactor thread.
        let reactor: Py<PyAny> = homeserver.call_method0("get_reactor")?.unbind();

        // hs.get_datastores().main.db_pool
        let db_pool_py: Py<PyAny> = homeserver
            .call_method0("get_datastores")?
            .getattr("main")?
            .getattr("db_pool")?
            .unbind();
        let db_pool = PythonDatabasePoolWrapper::new(db_pool_py, reactor.clone_ref(py));

        // Store is shared across all of the handlers so let's use an `Arc`
        let store = Arc::new(Store {
            db_pool: Box::new(db_pool),
        });

        let global_unstable_feature_map = Arc::new(
            versions::synapse_config_to_global_unstable_feature_map(&config),
        );

        let versions = Py::new(
            py,
            versions::VersionsHandler {
                global_unstable_feature_map: Arc::clone(&global_unstable_feature_map),
                store: Arc::clone(&store),
                reactor: reactor.clone_ref(py),
            },
        )?;

        Ok(RustHandlers { versions })
    }

    #[getter]
    fn versions(&self, py: Python<'_>) -> Py<versions::VersionsHandler> {
        self.versions.clone_ref(py)
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
