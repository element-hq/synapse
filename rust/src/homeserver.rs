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

use std::sync::Arc;

use pyo3::{intern, prelude::*, types::PyDict};

use crate::config::SynapseHomeServerConfig;
use crate::federation::signing::FederationSigningKey;
use crate::reactor::Reactor;
use crate::runtime::RustRuntime;
use crate::storage::{python::RustStore, store::Store};

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
    /// Fetch the Twisted reactor in use by this HomeServer.
    pub fn get_reactor(&self, py: Python<'_>) -> PyResult<Reactor> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_reactor"))?
            .extract()
    }

    /// Register a system event trigger with the HomeServer so it can be cleanly
    /// removed when the HomeServer is shutdown.
    pub fn register_sync_shutdown_handler(
        &self,
        py: Python<'_>,
        callable: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "phase"), intern!(py, "after"))?;
        kwargs.set_item(intern!(py, "eventType"), intern!(py, "shutdown"))?;
        kwargs.set_item(intern!(py, "shutdown_func"), callable)?;
        self.0.bind(py).call_method(
            intern!(py, "register_sync_shutdown_handler"),
            (),
            Some(&kwargs),
        )?;

        Ok(())
    }

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

    /// This homeserver's name (`hs.hostname`).
    pub fn hostname(&self, py: Python<'_>) -> PyResult<String> {
        self.0.bind(py).getattr(intern!(py, "hostname"))?.extract()
    }

    /// The key this homeserver signs federation requests with
    /// (`hs.signing_key`).
    pub fn signing_key(&self, py: Python<'_>) -> PyResult<FederationSigningKey> {
        self.0
            .bind(py)
            .getattr(intern!(py, "signing_key"))?
            .extract()
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

    /// This instance's name (`hs.get_instance_name()`).
    pub fn instance_name(&self, py: Python<'_>) -> PyResult<String> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_instance_name"))?
            .extract()
    }

    /// The instances that write events (`hs.config.worker.writers.events`).
    pub fn event_writers(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        self.0
            .bind(py)
            .getattr(intern!(py, "config"))?
            .getattr(intern!(py, "worker"))?
            .getattr(intern!(py, "writers"))?
            .getattr(intern!(py, "events"))?
            .extract()
    }

    /// The main datastore (`hs.get_datastores().main`).
    pub fn main_datastore<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_datastores"))?
            .getattr(intern!(py, "main"))
    }

    /// The main database pool (`hs.get_datastores().main.db_pool`).
    pub fn main_database_pool<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_datastores"))?
            .getattr(intern!(py, "main"))?
            .getattr(intern!(py, "db_pool"))
    }

    /// The state database pool (`hs.get_datastores().state.db_pool`).
    pub fn state_database_pool<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.0
            .bind(py)
            .call_method0(intern!(py, "get_datastores"))?
            .getattr(intern!(py, "state"))?
            .getattr(intern!(py, "db_pool"))
    }

    /// The Rust `Store` (`hs.get_rust_store()`), which the main datastore
    /// builds and owns. See [`crate::storage::python`].
    pub fn get_rust_store(&self, py: Python<'_>) -> PyResult<Arc<Store>> {
        Ok(self
            .0
            .bind(py)
            .call_method0(intern!(py, "get_rust_store"))?
            .cast::<RustStore>()?
            .get()
            .store()
            .clone())
    }
}
