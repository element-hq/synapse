/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2024 New Vector, Ltd
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

use log::info;
use pyo3::{pyclass, pymethods, types::PyModule, PyResult, Python};

#[pyclass]
struct Rendezvous {}

#[pymethods]
impl Rendezvous {
    #[new]
    fn new() -> Self {
        Rendezvous {}
    }

    fn store_session(&mut self, content_type: String, body: Vec<u8>) -> PyResult<()> {
        info!(
            "Received new rendezvous message: content_type: {}, len(body): {}",
            content_type,
            body.len()
        );

        Ok(())
    }
}

pub fn register_module(py: Python<'_>, m: &PyModule) -> PyResult<()> {
    let child_module = PyModule::new(py, "rendezvous")?;

    child_module.add_class::<Rendezvous>()?;

    m.add_submodule(child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import rendezvous` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.rendezvous", child_module)?;

    Ok(())
}
