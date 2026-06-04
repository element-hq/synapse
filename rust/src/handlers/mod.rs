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

use pyo3::{
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, PyResult, Python,
};

pub mod versions;

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "handlers")?;
    // TODO: Expose `get_versions`
    // child_module.add_class::<PushRule>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import push` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.handlers", child_module)?;

    Ok(())
}
