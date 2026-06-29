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
use tokio::runtime::{Handle, Runtime};

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
        self.ensure_started()
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
    /// Build the runtime if it hasn't been built yet.
    ///
    /// Idempotent, so it is safe to call both from the reactor's
    /// `callWhenRunning(start)` hook and from a caller that needs the runtime
    /// before the reactor has run that hook (see [`runtime_handle`]): whichever
    /// runs first builds it, the other is a no-op.
    fn ensure_started(&mut self) -> PyResult<()> {
        if self.runtime.is_some() {
            return Ok(());
        }

        // TODO: allow customization of the runtime like the number of threads
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()?;

        self.runtime = Some(runtime);

        Ok(())
    }

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
    Ok(get_or_install(reactor)?.borrow())
}

/// Get a clonable handle to the shared runtime, starting it on demand.
///
/// Unlike [`runtime`], this does not require the reactor to have already run
/// its `callWhenRunning(start)` hook: it starts the runtime if necessary. That
/// lets callers that need the runtime before the reactor is up — the database
/// backend's schema setup, `synapse_port_db`, and trial tests — still get a
/// working handle. Once the reactor does run, its `start` hook finds the
/// runtime already built and is a no-op, so there is still only one runtime.
pub fn runtime_handle(reactor: &Bound<'_, PyAny>) -> PyResult<Handle> {
    let runtime = get_or_install(reactor)?;
    let mut runtime = runtime.borrow_mut();
    runtime.ensure_started()?;
    Ok(runtime.handle()?.clone())
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

/// Get the [`PyTokioRuntime`] stored on the reactor instance, installing a
/// fresh one (wired to the reactor's start/shutdown) if it isn't there yet.
fn get_or_install<'a>(reactor: &Bound<'a, PyAny>) -> PyResult<Bound<'a, PyTokioRuntime>> {
    if !reactor.hasattr(TOKIO_RUNTIME_ATTR)? {
        install_runtime(reactor)?;
    }

    // This will raise if `TOKIO_RUNTIME_ATTR` is not set or if it is
    // not a `Runtime`. Careful that this could happen if the user sets it
    // manually, or if multiple versions of `pyo3-twisted` are used!
    let runtime: Bound<PyTokioRuntime> = reactor.getattr(TOKIO_RUNTIME_ATTR)?.extract()?;
    Ok(runtime)
}
