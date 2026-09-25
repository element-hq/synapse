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
//! homeserver. Rust consumers (e.g. the HTTP client) clone the inner
//! [`Arc<RustRuntimeInner>`] at construction time and don't need the GIL (or
//! the Python-facing object) to reach it afterwards.
//!
//! The tokio runtime is shut down with the homeserver, via a handler
//! registered with `HomeServer.register_sync_shutdown_handler`.

use std::ops::Deref;
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use anyhow::Context;
use pyo3::{exceptions::PyRuntimeError, prelude::*};
use tokio::runtime::{Handle, Runtime};

use crate::homeserver::HomeServer;
use crate::reactor::Reactor;
use crate::twisted_dispatch::{self, TwistedDispatchReader, TwistedDispatcher};

/// How long to wait for in-flight tokio tasks to be cancelled when shutting
/// down with the reactor.
///
/// Note that any [`Runtime::spawn_blocking`] work that is still running when
/// the timeout expires is leaked, along with the worker thread running it.
///
/// See [`tokio::runtime`] for details.
const SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(100);

/// State of the lazily-started tokio runtime.
enum TokioState {
    /// Not started yet; the runtime is built on first use.
    NotStarted,
    Running(Runtime),
    /// Shut down with the homeserver. Cannot be restarted.
    Shutdown,
}

/// The state shared between the Python-facing [`RustRuntime`] handle and any
/// Rust-side consumers holding an `Arc` of this.
pub struct RustRuntimeInner {
    reactor: Reactor,
    tokio: Mutex<TokioState>,
    worker_threads: usize,

    /// Runs closures on the Twisted reactor thread without taking the GIL on
    /// the calling thread. See [`crate::twisted_dispatch`].
    dispatcher: Arc<TwistedDispatcher>,
    /// The reactor-facing half of `dispatcher`, registered with the Twisted
    /// reactor via `addReader` until `shutdown`.
    dispatch_reader: Py<TwistedDispatchReader>,
}

impl RustRuntimeInner {
    /// Queue `f` to run on the Twisted reactor thread with the GIL held, and
    /// wake the reactor. Never takes the GIL itself, so a tokio task can call
    /// it to hand a result back to Twisted. See [`crate::twisted_dispatch`].
    ///
    /// Returns an error once the homeserver has shut down.
    pub fn dispatch_to_twisted<F>(&self, f: F) -> anyhow::Result<()>
    where
        F: FnOnce(Python<'_>) + Send + 'static,
    {
        self.dispatcher.dispatch(f)
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

    /// Shut the tokio runtime down, cancelling all in-flight tasks and waiting
    /// for up to [`SHUTDOWN_TIMEOUT`] for them to finish. Called via
    /// [`ShutdownHook`] when the reactor shuts down.
    ///
    /// Note that any [`Runtime::spawn_blocking`] work is leaked until it is
    /// finished, along with the worker thread running it.
    fn shutdown(&self, py: Python<'_>) -> PyResult<()> {
        let mut state = self
            .tokio
            .lock()
            .map_err(|_| PyRuntimeError::new_err("tokio runtime lock poisoned"))?;
        let previous_state = std::mem::replace(&mut *state, TokioState::Shutdown);
        // Don't hold the lock while blocking on the shutdown below.
        drop(state);

        if let TokioState::Running(runtime) = previous_state {
            // Shutdown the runtime, waiting for a small grace period for
            // in-flight tasks to be cancelled.
            //
            // See [`tokio::runtime`] for details.
            py.detach(|| runtime.shutdown_timeout(SHUTDOWN_TIMEOUT));
        }

        // Unregister the wakeup socket from the Twisted reactor, then close
        // the dispatcher and run the closures still queued so that their
        // deferreds fire.
        //
        // All tokio tasks should be stopped by now, so only long running
        // blocking threads may still be active. If they try to dispatch new
        // work they get an error and should stop.
        self.reactor
            .remove_reader(py, self.dispatch_reader.bind(py).as_any())?;
        self.dispatch_reader.get().close_and_drain(py);

        Ok(())
    }
}

impl Drop for RustRuntimeInner {
    fn drop(&mut self) {
        // Backstop for homeservers whose shutdown trigger never fires (e.g. in
        // tests). We use `shutdown_background` rather than a blocking shutdown,
        // because the last `Arc` may be dropped from a task running on this
        // very runtime, where blocking would panic.
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
    #[pyo3(signature = (hs, worker_threads = 4))]
    fn py_new(py: Python<'_>, hs: HomeServer, worker_threads: usize) -> PyResult<Self> {
        let reactor = hs.get_reactor(py)?;

        // Register the read end of the dispatcher's wakeup socket with the
        // Twisted reactor, so that closures dispatched by tokio tasks run on
        // the reactor thread. `shutdown` removes it again.
        let (dispatcher, dispatch_reader) = twisted_dispatch::new_pair()?;
        let dispatch_reader = Py::new(py, dispatch_reader)?;
        reactor.add_reader(py, dispatch_reader.bind(py).as_any())?;

        let inner = Arc::new(RustRuntimeInner {
            reactor,
            tokio: Mutex::new(TokioState::NotStarted),
            worker_threads,
            dispatcher,
            dispatch_reader,
        });

        // Shut the tokio runtime down when the homeserver is shut down. The
        // trigger holds only a `Weak` reference, as otherwise we risk a
        // reference cycle passing through a Rust field that Python's GC cannot
        // see into.
        let hook = Py::new(
            py,
            ShutdownHook {
                inner: Arc::downgrade(&inner),
            },
        )?;
        hs.register_sync_shutdown_handler(py, hook.bind(py).as_any())?;

        Ok(RustRuntime { inner })
    }
}

/// The callable registered with `HomeServer.register_sync_shutdown_handler`,
/// which runs it on `HomeServer.shutdown()` or when the reactor stops.
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
