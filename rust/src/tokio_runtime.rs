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

use anyhow::Context;
use pyo3::prelude::*;
use tokio::runtime::Runtime;

/// This is the name of the attribute where we store the runtime on the reactor
static TOKIO_RUNTIME_ATTR: &str = "__synapse_rust_tokio_runtime";

/// A Python wrapper around a Tokio runtime.
///
/// This allows us to 'store' the runtime on the reactor instance, starting it
/// when the reactor starts, and stopping it when the reactor shuts down.
#[pyclass]
pub struct PyTokioRuntime {
    runtime: Option<Runtime>,
}

#[pymethods]
impl PyTokioRuntime {
    fn start(&mut self) -> PyResult<()> {
        // TODO: allow customization of the runtime like the number of threads
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()?;

        self.runtime = Some(runtime);

        Ok(())
    }

    fn shutdown(&mut self) -> PyResult<()> {
        let runtime = self
            .runtime
            .take()
            .context("Runtime was already shutdown")?;

        // Dropping the runtime will shut it down
        drop(runtime);

        Ok(())
    }
}

impl PyTokioRuntime {
    /// Get the handle to the Tokio runtime, if it is running.
    pub fn handle(&self) -> PyResult<&tokio::runtime::Handle> {
        let handle = self
            .runtime
            .as_ref()
            .context("Tokio runtime is not running")?
            .handle();

        Ok(handle)
    }
}

/// Get a handle to the Tokio runtime stored on the reactor instance, or create
/// a new one.
pub fn runtime<'a>(reactor: &Bound<'a, PyAny>) -> PyResult<PyRef<'a, PyTokioRuntime>> {
    if !reactor.hasattr(TOKIO_RUNTIME_ATTR)? {
        install_runtime(reactor)?;
    }

    get_runtime(reactor)
}

/// Install a new Tokio runtime on the reactor instance.
fn install_runtime(reactor: &Bound<PyAny>) -> PyResult<()> {
    let py = reactor.py();
    let runtime = PyTokioRuntime { runtime: None };
    let runtime = runtime.into_pyobject(py)?;

    // Attach the runtime to the reactor, starting it when the reactor is
    // running, stopping it when the reactor is shutting down
    reactor.call_method1("callWhenRunning", (runtime.getattr("start")?,))?;
    reactor.call_method1(
        "addSystemEventTrigger",
        ("after", "shutdown", runtime.getattr("shutdown")?),
    )?;
    reactor.setattr(TOKIO_RUNTIME_ATTR, runtime)?;

    Ok(())
}

/// Get a reference to a Tokio runtime handle stored on the reactor instance.
fn get_runtime<'a>(reactor: &Bound<'a, PyAny>) -> PyResult<PyRef<'a, PyTokioRuntime>> {
    // This will raise if `TOKIO_RUNTIME_ATTR` is not set or if it is
    // not a `Runtime`. Careful that this could happen if the user sets it
    // manually, or if multiple versions of `pyo3-twisted` are used!
    let runtime: Bound<PyTokioRuntime> = reactor.getattr(TOKIO_RUNTIME_ATTR)?.extract()?;
    Ok(runtime.borrow())
}
