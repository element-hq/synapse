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

//! A typed wrapper around the Python `HomeServer`.

use pyo3::{intern, prelude::*};

use crate::config::SynapseHomeServerConfig;
use crate::runtime::RustRuntime;

/// The Python `HomeServer`, as seen from Rust.
///
/// This is a wrapper around a foreign Python object. No validation is
/// performed, on the assumption the Python side is correctly typed.
pub struct HomeServer(Py<PyAny>);

impl<'a, 'py> FromPyObject<'a, 'py> for HomeServer {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        Ok(HomeServer(obj.to_owned().unbind()))
    }
}

impl HomeServer {
    /// The per-homeserver Rust state (`hs.get_rust_runtime()`), which gives
    /// access to the tokio runtime and the reactor.
    pub fn get_rust_runtime(&self, py: Python<'_>) -> PyResult<RustRuntime> {
        Ok(self
            .0
            .bind(py)
            .call_method0(intern!(py, "get_rust_runtime"))?
            .cast::<RustRuntime>()?
            .get()
            .clone())
    }

    /// The Rust-side view of `hs.config`.
    pub fn config(&self, py: Python<'_>) -> PyResult<SynapseHomeServerConfig> {
        self.0.bind(py).getattr(intern!(py, "config"))?.extract()
    }

    /// The Synapse `Clock` (`hs.get_clock()`).
    // TODO: give the clock a typed wrapper of its own.
    pub fn get_clock(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self
            .0
            .bind(py)
            .call_method0(intern!(py, "get_clock"))?
            .unbind())
    }

    /// The main database pool (`hs.get_datastores().main.db_pool`).
    pub fn main_database_pool<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_datastores"))?
            .getattr(intern!(py, "main"))?
            .getattr(intern!(py, "db_pool"))
    }
}
