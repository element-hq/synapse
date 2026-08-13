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

//! The per-homeserver state for the Rust side of Synapse.
//!
//! A [`RustRuntime`] is created once per homeserver (`hs.get_rust_runtime()`)
//! and holds everything the Rust side keeps for the lifetime of that
//! homeserver: currently the tokio thread pool and a handle to the Twisted
//! reactor. Rust consumers (e.g. the HTTP client) clone the inner
//! [`Arc<RustRuntimeInner>`] at construction time and don't need the GIL (or
//! the Python-facing object) to reach it afterwards.

use std::ops::Deref;
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use anyhow::Context;
use pyo3::{exceptions::PyRuntimeError, prelude::*};
use tokio::runtime::{Handle, Runtime};

use crate::reactor::Reactor;

/// How long to wait for in-flight tokio tasks when shutting down with the
/// reactor.
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

/// State of the lazily-started tokio runtime.
enum TokioState {
    /// Not started yet; the runtime is built on first use.
    NotStarted,
    Running(Runtime),
    /// Shut down after the reactor stopped. Cannot be restarted.
    Shutdown,
}

/// The state shared between the Python-facing [`RustRuntime`] handle and any
/// Rust-side consumers holding an `Arc` of this.
pub struct RustRuntimeInner {
    reactor: Reactor,
    tokio: Mutex<TokioState>,
    worker_threads: usize,
}

impl RustRuntimeInner {
    /// The Twisted reactor this homeserver runs on.
    pub fn reactor(&self) -> &Reactor {
        &self.reactor
    }

    /// Get a handle to the tokio runtime, starting the runtime if it hasn't
    /// been started yet.
    pub fn tokio_handle(&self) -> PyResult<Handle> {
        let mut state = self
            .tokio
            .lock()
            .map_err(|_| PyRuntimeError::new_err("tokio runtime lock poisoned"))?;

        match &*state {
            TokioState::Running(runtime) => Ok(runtime.handle().clone()),
            TokioState::NotStarted => {
                let runtime = tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(self.worker_threads)
                    .enable_all()
                    .build()
                    .context("building tokio runtime")?;
                let handle = runtime.handle().clone();
                *state = TokioState::Running(runtime);
                Ok(handle)
            }
            TokioState::Shutdown => Err(PyRuntimeError::new_err(
                "the tokio runtime has been shut down",
            )),
        }
    }

    /// Shut the tokio runtime down, waiting (with the GIL released) for
    /// in-flight tasks to finish. Called via [`ShutdownHook`] when the
    /// reactor shuts down.
    fn shutdown(&self, py: Python<'_>) -> PyResult<()> {
        let mut state = self
            .tokio
            .lock()
            .map_err(|_| PyRuntimeError::new_err("tokio runtime lock poisoned"))?;
        let previous_state = std::mem::replace(&mut *state, TokioState::Shutdown);
        // Don't hold the lock while blocking on the shutdown below.
        drop(state);

        if let TokioState::Running(runtime) = previous_state {
            py.detach(|| runtime.shutdown_timeout(SHUTDOWN_TIMEOUT));
        }

        Ok(())
    }
}

impl Drop for RustRuntimeInner {
    fn drop(&mut self) {
        // Backstop for reactors whose shutdown trigger never fires (e.g.
        // `MemoryReactorClock` in tests, which is never actually run).
        // `shutdown_background` rather than a blocking shutdown, because the
        // last `Arc` may be dropped from a task running on this very
        // runtime, where blocking would panic.
        if let Ok(state) = self.tokio.get_mut() {
            if let TokioState::Running(runtime) = std::mem::replace(state, TokioState::Shutdown) {
                runtime.shutdown_background();
            }
        }
    }
}

/// A cheaply-clonable handle to the per-homeserver Rust state, and the
/// Python-facing class for it.
///
/// One instance is constructed per homeserver by
/// `HomeServer.get_rust_runtime()`. Rust classes that need it take it as a
/// constructor argument and store their own clone, which is just an `Arc`
/// refcount bump. Derefs to [`RustRuntimeInner`].
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct RustRuntime {
    inner: Arc<RustRuntimeInner>,
}

impl Deref for RustRuntime {
    type Target = RustRuntimeInner;

    fn deref(&self) -> &Self::Target {
        &self.inner
    }
}

#[pymethods]
impl RustRuntime {
    #[new]
    #[pyo3(signature = (reactor, worker_threads = 4))]
    fn py_new(py: Python<'_>, reactor: Reactor, worker_threads: usize) -> PyResult<Self> {
        let inner = Arc::new(RustRuntimeInner {
            reactor,
            tokio: Mutex::new(TokioState::NotStarted),
            worker_threads,
        });

        // Shut the tokio runtime down when the reactor does. The trigger
        // holds only a `Weak` reference. Twisted keeping the hook alive must
        // not keep the runtime (nor, via it, the reactor) alive, as that
        // would be a reference cycle passing through a Rust field that
        // Python's GC cannot see into.
        let hook = Py::new(
            py,
            ShutdownHook {
                inner: Arc::downgrade(&inner),
            },
        )?;
        inner
            .reactor
            .add_shutdown_trigger(py, hook.bind(py).as_any())?;

        Ok(RustRuntime { inner })
    }
}

/// The callable registered with
/// `reactor.addSystemEventTrigger("after", "shutdown", ...)`.
#[pyclass(frozen)]
struct ShutdownHook {
    inner: Weak<RustRuntimeInner>,
}

#[pymethods]
impl ShutdownHook {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(inner) = self.inner.upgrade() {
            inner.shutdown(py)?;
        }

        Ok(())
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "runtime")?;

    child_module.add_class::<RustRuntime>()?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.runtime", child_module)?;

    Ok(())
}
