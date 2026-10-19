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

use std::future::Future;
use std::ops::Deref;
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration, UNIX_EPOCH};

use anyhow::Context;
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
};
use tokio::runtime::{Handle, Runtime};

use crate::caches::registry::CacheRegistry;
use crate::clock::{set_virtual_time_msec, Clock};
use crate::homeserver::HomeServer;
use crate::logging::context::LogContextHandle;
use crate::logging::opentracing::thrift::{FinishedSpan, Tag};
use crate::logging::opentracing::{
    set_homeserver_whitelist, set_tracing_enabled, SamplerConfig, Tracer, TracerConfig,
};
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

/// How long shutdown waits for the Jaeger reporter to send its last spans.
const REPORTER_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);

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
    clock: Clock,
    /// This homeserver's Rust caches. See [`crate::caches`].
    caches: CacheRegistry,

    /// Runs closures on the Twisted reactor thread without taking the GIL on
    /// the calling thread. See [`crate::twisted_dispatch`].
    dispatcher: Arc<TwistedDispatcher>,
    /// The reactor-facing half of `dispatcher`, registered with the Twisted
    /// reactor via `addReader` until `shutdown`.
    dispatch_reader: Py<TwistedDispatchReader>,

    /// Set by `enable_tracing` when opentracing is enabled for this
    /// homeserver. See [`crate::logging::opentracing`].
    tracing: OnceLock<Arc<Tracer>>,
}

impl RustRuntimeInner {
    /// This homeserver's clock. See [`crate::clock`].
    pub fn clock(&self) -> &Clock {
        &self.clock
    }

    /// This homeserver's Rust caches, for building one or expiring them all.
    /// See [`crate::caches`].
    pub fn caches(&self) -> &CacheRegistry {
        &self.caches
    }

    /// Queue `f` to run on the Twisted reactor thread with the GIL held, and
    /// wake the reactor. Never takes the GIL itself, so a tokio task can call
    /// it to hand a result back to Twisted. See [`crate::twisted_dispatch`].
    ///
    /// This is the equivalent of calling `reactor.callFromThread` in Python and
    /// should be used by Rust code instead of `callFromThread`.
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

    /// Run `fut` to completion as a task on the runtime, blocking the calling
    /// thread with the GIL released.
    ///
    /// The task runs in the caller's logcontext, with its active span as the
    /// parent, the same as a task spawned by [`crate::deferred::create_deferred`].
    /// It runs on a worker thread rather than the calling one, so the calling
    /// thread never has a task-local logcontext in scope.
    ///
    /// For the `_blocking` entry points that tests call in place of the
    /// deferred ones.
    pub fn block_on_scoped<F>(&self, py: Python<'_>, fut: F) -> PyResult<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let logcontext = LogContextHandle::capture(py);
        let handle = self.tokio_handle()?;
        let task = handle.spawn(logcontext.scope(fut));

        match py.detach(|| handle.block_on(task)) {
            Ok(output) => Ok(output),
            Err(err) if err.is_panic() => std::panic::resume_unwind(err.into_panic()),
            Err(err) => Err(PyRuntimeError::new_err(format!(
                "blocking task did not finish: {err}"
            ))),
        }
    }

    /// The tracer, if opentracing is enabled for this homeserver.
    pub fn tracer(&self) -> Option<&Arc<Tracer>> {
        self.tracing.get()
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
        let removed = self
            .reactor
            .remove_reader(py, self.dispatch_reader.bind(py).as_any());
        if removed.is_ok() {
            self.dispatch_reader.get().close_and_drain(py);
        }

        // Last, so that spans finished by the work above still go out, and
        // even if removing the reader failed.
        if let Some(tracer) = self.tracing.get() {
            py.detach(|| tracer.shutdown(REPORTER_SHUTDOWN_TIMEOUT));
        }

        removed
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
#[pyclass(frozen, weakref, skip_from_py_object)]
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
            clock: Clock::new(),
            caches: CacheRegistry::new(),
            dispatcher,
            dispatch_reader,
            tracing: OnceLock::new(),
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

    /// The current time, in milliseconds since the Unix epoch.
    fn time_msec(&self) -> u64 {
        self.inner.clock.now_millis()
    }

    /// Report spans from Rust to the Jaeger agent alongside Python's. Called
    /// by `synapse.logging.opentracing.init_tracer` once the Python tracer
    /// exists, with the settings it computed, so that both report as the
    /// same service.
    ///
    /// `sampler_type` and `sampler_param` decide whether a trace that starts
    /// in Rust is sampled: `const` and `probabilistic` are implemented, any
    /// other type never samples such a trace. Rust spans with a Python parent
    /// follow the parent's decision.
    ///
    /// The agent's host name is resolved when spans are sent, so one that
    /// does not resolve yet is not an error. Raises `OSError` only if the
    /// reporter thread cannot start. A second call is ignored.
    ///
    /// `homeserver_whitelist` is the opentracing config's list of patterns
    /// of the servers Rust may send trace context to.
    #[pyo3(signature = (service_name, process_tags, reporting_host, reporting_port, sampler_type, sampler_param, homeserver_whitelist = Vec::new()))]
    #[allow(clippy::too_many_arguments)]
    fn enable_tracing(
        &self,
        service_name: String,
        process_tags: Vec<(String, String)>,
        reporting_host: String,
        reporting_port: u16,
        sampler_type: &str,
        sampler_param: f64,
        homeserver_whitelist: Vec<String>,
    ) -> PyResult<()> {
        if self.inner.tracing.get().is_some() {
            log::warn!("Rust tracing was already enabled for this homeserver");
            return Ok(());
        }

        let config = TracerConfig {
            service_name,
            process_tags,
            reporting_host,
            reporting_port,
            sampler: SamplerConfig::from_type_and_param(sampler_type, sampler_param),
        };

        let tracer = Tracer::start(config)?;

        if self.inner.tracing.set(Arc::new(tracer)).is_err() {
            log::warn!("Rust tracing was already enabled for this homeserver");
            return Ok(());
        }

        set_homeserver_whitelist(&homeserver_whitelist);
        set_tracing_enabled(true);
        Ok(())
    }

    /// The settings `enable_tracing` stored, as `(service_name,
    /// process_tags, reporting_host, reporting_port, sampler)` with the
    /// sampler in its debug form, or `None`. For tests.
    #[allow(clippy::type_complexity)]
    fn tracing_config_for_test(
        &self,
    ) -> Option<(String, Vec<(String, String)>, String, u16, String)> {
        self.inner.tracing.get().map(|tracer| {
            let config = tracer.config();
            (
                config.service_name.clone(),
                config.process_tags.clone(),
                config.reporting_host.clone(),
                config.reporting_port,
                format!("{:?}", config.sampler),
            )
        })
    }

    /// The Jaeger reporter's counters, as `(queue_depth, spans_sent,
    /// batches_sent, dropped_queue_full, dropped_oversize, dropped_closed,
    /// send_errors)`, or `None` if tracing is not enabled.
    #[allow(clippy::type_complexity)]
    fn tracing_stats(&self) -> Option<(u64, u64, u64, u64, u64, u64, u64)> {
        use std::sync::atomic::Ordering::Relaxed;

        self.inner.tracing.get().map(|tracer| {
            let stats = tracer.reporter().stats();
            (
                stats.queue_depth.load(Relaxed),
                stats.spans_sent.load(Relaxed),
                stats.batches_sent.load(Relaxed),
                stats.dropped_queue_full.load(Relaxed),
                stats.dropped_oversize.load(Relaxed),
                stats.dropped_closed.load(Relaxed),
                stats.send_errors.load(Relaxed),
            )
        })
    }

    /// Report a span with the given IDs and string tags, started now and one
    /// millisecond long. For tests.
    #[pyo3(signature = (operation, trace_id, span_id, parent_id, flags, tags))]
    fn report_span_for_test(
        &self,
        operation: String,
        trace_id: u128,
        span_id: u64,
        parent_id: u64,
        flags: u8,
        tags: Vec<(String, String)>,
    ) -> PyResult<()> {
        let tracer = self
            .inner
            .tracing
            .get()
            .ok_or_else(|| PyRuntimeError::new_err("tracing is not enabled"))?;
        tracer.report(FinishedSpan {
            trace_id,
            span_id,
            parent_id,
            flags,
            operation: operation.into(),
            start: std::time::SystemTime::now(),
            duration: Duration::from_millis(1),
            tags: tags
                .into_iter()
                .map(|(key, value)| Tag::string(key, value))
                .collect(),
            logs: vec![],
            references: vec![],
        });
        Ok(())
    }

    /// Send the queued spans now, waiting up to `timeout_secs`. Returns
    /// whether they went in time. For tests.
    fn flush_tracing_for_test(&self, py: Python<'_>, timeout_secs: f64) -> bool {
        let Some(tracer) = self.inner.tracing.get() else {
            return false;
        };
        let timeout = Duration::from_secs_f64(timeout_secs.max(0.0));
        py.detach(|| tracer.reporter().flush(timeout))
    }

    /// Shut the Jaeger reporter down as homeserver shutdown does. For tests.
    fn shutdown_tracing_for_test(&self, py: Python<'_>) {
        if let Some(tracer) = self.inner.tracing.get() {
            py.detach(|| tracer.shutdown(REPORTER_SHUTDOWN_TIMEOUT));
        }
    }

    /// Evict entries not read for `expiry_secs` from every Rust cache of this
    /// homeserver. Returns how many went. See [`crate::caches::registry`].
    fn expire_caches(&self, expiry_secs: f64) -> PyResult<usize> {
        let max_age = Duration::try_from_secs_f64(expiry_secs).map_err(|err| {
            PyValueError::new_err(format!("bad cache expiry {expiry_secs}: {err}"))
        })?;

        Ok(self
            .inner
            .caches
            .expire_caches(self.inner.clock.now(), max_age))
    }

    /// Evict up to `max` entries last read before `before_secs` (seconds
    /// since the Unix epoch), coldest first across every Rust cache of this
    /// homeserver. For memory-pressure eviction, which Python drives.
    ///
    /// Returns how many went, and when the coldest entry left was last read,
    /// in seconds since the epoch, or `None` if the caches are empty. See
    /// [`crate::caches::registry::CacheRegistry::evict_coldest`].
    fn evict_coldest(&self, before_secs: f64, max: usize) -> PyResult<(usize, Option<f64>)> {
        let before = Duration::try_from_secs_f64(before_secs.max(0.0))
            .map(|since_epoch| UNIX_EPOCH + since_epoch)
            .map_err(|err| PyValueError::new_err(format!("bad time {before_secs}: {err}")))?;

        let (evicted, coldest) = self.inner.caches.evict_coldest(before, max);
        let coldest = coldest.map(|time| {
            time.duration_since(UNIX_EPOCH)
                .unwrap_or(Duration::ZERO)
                .as_secs_f64()
        });
        Ok((evicted, coldest))
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
    child_module.add_function(wrap_pyfunction!(set_virtual_time_msec, m)?)?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.runtime", child_module)?;

    Ok(())
}
