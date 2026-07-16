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

//! Native storage for the Synapse "current logcontext".
//!
//! Historically the current logcontext lived in a Python `threading.local`
//! (`synapse.logging.context._thread_local`). That is invisible to Rust: each
//! tokio worker thread has its own slot which is permanently the sentinel, so
//! logging emitted from Rust — including from spawned tokio tasks — could not be
//! attributed to the request that caused it.
//!
//! This module moves the storage into Rust and unifies two sources of truth so
//! that a single [`current_context`] answer is correct from *both* worlds:
//!
//! 1. a per-OS-thread slot ([`THREAD_LOCAL_CONTEXT`]), the direct replacement for
//!    the old Python `threading.local` — used by the reactor thread and any
//!    reactor-managed threadpool threads; and
//! 2. a per-tokio-task slot ([`TASK_LOCAL_CONTEXT`]), which rides with a task as it
//!    migrates between worker threads across `.await` points.
//!
//! [`current_context`] consults the task-local first (when called from inside a
//! runtime task) and falls back to the thread-local, then the sentinel. Because
//! `LoggingContextFilter` (and therefore `pyo3-log`) resolves the context by
//! calling [`current_context`] at log-record time, log records emitted while a
//! task is being polled are attributed to the task's captured context with no
//! per-record stamping machinery.
//!
//! Python keeps the accounting policy: `set_current_context` still does the
//! `getrusage` start/stop bookkeeping and merely uses [`swap_current_context`] for
//! the raw slot write. The switch primitive is only ever driven on the reactor
//! (or threadpool) threads — never on tokio worker threads — so it always writes
//! the thread-local, and the task-local (populated only by [`LogContext::scope`]
//! at spawn time) takes read precedence during a poll.

use std::{cell::RefCell, future::Future};

use log::{debug, error, log_enabled, Level};
use once_cell::sync::OnceCell;
use pyo3::call::PyCallArgs;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

/// The sentinel logcontext singleton (`synapse.logging.context.SENTINEL_CONTEXT`),
/// created lazily by [`sentinel`]. Owned natively: Rust defines the [`Sentinel`]
/// type *and* holds the one instance, so there is no Python-side bootstrap and no
/// import of `synapse.logging.context` at registration time (which would be a
/// circular import; see [`crate::deferred`]).
static SENTINEL: OnceCell<Py<PyAny>> = OnceCell::new();

/// The root "no logcontext" marker (`synapse.logging.context.SENTINEL_CONTEXT`).
///
/// A drop-in for the former Python `_Sentinel`: a singleton whose fields are inert
/// defaults and whose methods are no-ops, and which is *falsy* so callers can test
/// `if not current_context()` to detect "no logcontext". [`switch_context`]
/// special-cases it by identity, so its `start`/`stop` never run on the hot path;
/// the other no-op methods exist only so code holding a `LoggingContextOrSentinel`
/// can call them without first checking the concrete type.
#[pyclass(name = "_Sentinel", get_all, set_all)]
pub struct Sentinel {
    previous_context: Option<Py<PyAny>>,
    finished: bool,
    scope: Option<Py<PyAny>>,
    server_name: String,
    request: Option<Py<PyAny>>,
    tag: Option<Py<PyAny>>,
}

impl Sentinel {
    /// The singleton's initial state (mirrors the former Python `_Sentinel.__init__`).
    fn instance() -> Self {
        Sentinel {
            previous_context: None,
            finished: false,
            scope: None,
            server_name: "unknown_server_from_sentinel_context".to_owned(),
            request: None,
            tag: None,
        }
    }
}

#[pymethods]
impl Sentinel {
    fn __str__(&self) -> &'static str {
        "sentinel"
    }

    /// No-op: the sentinel is never actually running, so there is nothing to
    /// account.
    fn start(&self, _rusage: Option<(f64, f64)>) {}

    /// No-op counterpart to [`Self::start`].
    fn stop(&self, _rusage: Option<(f64, f64)>) {}

    /// No-op: work done under the sentinel is attributed to no context.
    fn add_database_transaction(&self, _duration_sec: f64) {}

    /// No-op counterpart to [`Self::add_database_transaction`].
    fn add_database_scheduled(&self, _sched_sec: f64) {}

    /// No-op: event fetches under the sentinel are attributed to no context.
    fn record_event_fetch(&self, _event_count: i64) {}

    /// The sentinel is falsy, matching the former Python `_Sentinel.__bool__`.
    fn __bool__(&self) -> bool {
        false
    }
}

/// Name of the opt-in logger for logcontext switch tracing.
///
/// This is the single source of truth for the logger name: it is used as the
/// `debug!` `target:` for the switch traces emitted below, and it is exported to
/// Python via [`register_module`] so that `synapse.logging.context` builds
/// *exactly* this logger (see `logcontext_debug_logger` there). Keeping one
/// constant stops the Rust `target:` and the Python `getLogger` name from
/// drifting apart. The messages only surface when this logger is explicitly
/// configured — see `ExplicitlyConfiguredLogger` on the Python side, whose
/// `isEnabledFor` pyo3-log honours, so the no-inherit opt-in works from Rust too.
pub const DEBUG_LOGGER_NAME: &str = "synapse.logging.context.debug";

thread_local! {
    /// The current logcontext for this OS thread. `None` means "no context set on
    /// this thread", which is reported as the sentinel — matching the old
    /// `getattr(_thread_local, "current_context", SENTINEL_CONTEXT)` default.
    static THREAD_LOCAL_CONTEXT: RefCell<Option<Py<PyAny>>> = const { RefCell::new(None) };
}

tokio::task_local! {
    /// The logcontext captured for the current tokio task, set by
    /// [`LogContext::scope`] when the task is spawned. Only present inside a
    /// scoped task; readable synchronously during any poll of that task,
    /// regardless of which worker thread the poll runs on.
    static TASK_LOCAL_CONTEXT: LogContext;
}

/// A cheap, clone-able, GIL-free handle on a Python logcontext object (a
/// `LoggingContext` or the sentinel).
///
/// `Py<PyAny>` is `Send + Sync`, so this can travel with a tokio task across
/// worker threads and be dropped on a detached thread (pyo3 defers the decref).
/// Cloning only needs the GIL for the underlying object, so we clone the `Py`
/// eagerly (with the GIL) at capture time and hand out clones of the handle,
/// which are GIL-free — see [`LogContext::current`], called during a poll where
/// the GIL may not be held.
#[derive(Clone)]
pub struct LogContext {
    // Held behind an `Arc` so that cloning the handle (e.g. `LogContext::current`,
    // called during a poll where the GIL may not be held) and dropping it are
    // both GIL-free; cloning a bare `Py<PyAny>` would require the GIL.
    context: std::sync::Arc<Py<PyAny>>,
}

impl LogContext {
    /// Capture the calling thread's current logcontext.
    ///
    /// Must be called with the GIL held, on the thread whose context we want
    /// (i.e. at the FFI boundary, before spawning onto tokio).
    pub fn capture(py: Python<'_>) -> Self {
        LogContext {
            context: std::sync::Arc::new(current_context(py)),
        }
    }

    /// The logcontext of the current tokio task, if we are running inside one
    /// that was spawned through [`LogContext::scope`].
    pub fn current() -> Option<LogContext> {
        TASK_LOCAL_CONTEXT.try_with(|c| c.clone()).ok()
    }

    /// Run `fut` with this logcontext active (visible to [`current_context`] and
    /// therefore to logging) for the duration of the task.
    pub fn scope<F>(self, fut: F) -> impl Future<Output = F::Output>
    where
        F: Future,
    {
        TASK_LOCAL_CONTEXT.scope(self, fut)
    }

    /// Borrow the underlying Python object.
    pub fn as_py<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        self.context.bind(py).clone()
    }
}

/// Tracks the resources used by a log context.
///
/// A native drop-in for the former Python `ContextResourceUsage` class; the
/// public attribute surface, operators and `repr` are preserved so callers
/// (Measure, request/background-process metrics, task scheduler, ...) are
/// unaffected. Keeping this native lets the switch machinery do its rusage
/// accounting without allocating a Python object per operation.
#[pyclass(skip_from_py_object, get_all, set_all)]
#[derive(Clone, Default)]
pub struct ContextResourceUsage {
    /// System CPU time, in seconds.
    pub ru_stime: f64,
    /// User CPU time, in seconds.
    pub ru_utime: f64,
    /// Number of database transactions done.
    pub db_txn_count: i64,
    /// Time spent doing database transactions (excluding scheduling), in seconds.
    pub db_txn_duration_sec: f64,
    /// Time spent waiting for a database connection, in seconds.
    pub db_sched_duration_sec: f64,
    /// Number of events requested from the database.
    pub evt_db_fetch_count: i64,
}

impl ContextResourceUsage {
    fn add_assign(&mut self, other: &ContextResourceUsage) {
        self.ru_utime += other.ru_utime;
        self.ru_stime += other.ru_stime;
        self.db_txn_count += other.db_txn_count;
        self.db_txn_duration_sec += other.db_txn_duration_sec;
        self.db_sched_duration_sec += other.db_sched_duration_sec;
        self.evt_db_fetch_count += other.evt_db_fetch_count;
    }

    fn sub_assign(&mut self, other: &ContextResourceUsage) {
        self.ru_utime -= other.ru_utime;
        self.ru_stime -= other.ru_stime;
        self.db_txn_count -= other.db_txn_count;
        self.db_txn_duration_sec -= other.db_txn_duration_sec;
        self.db_sched_duration_sec -= other.db_sched_duration_sec;
        self.evt_db_fetch_count -= other.evt_db_fetch_count;
    }
}

#[pymethods]
impl ContextResourceUsage {
    /// `ContextResourceUsage(copy_from=None)` — if `copy_from` is given, copy its
    /// stats; otherwise start at zero.
    #[new]
    #[pyo3(signature = (copy_from=None))]
    fn new(copy_from: Option<&ContextResourceUsage>) -> Self {
        copy_from.cloned().unwrap_or_default()
    }

    /// Return a copy of this object.
    fn copy(&self) -> ContextResourceUsage {
        self.clone()
    }

    /// Reset all stats to zero.
    fn reset(&mut self) {
        *self = ContextResourceUsage::default();
    }

    fn __repr__(&self) -> String {
        // Matches the historical Python `__repr__` (values were interpolated with
        // `%r`, i.e. `repr()`, inside single quotes).
        format!(
            "<ContextResourceUsage ru_stime='{:?}', ru_utime='{:?}', \
             db_txn_count='{}', db_txn_duration_sec='{:?}', \
             db_sched_duration_sec='{:?}', evt_db_fetch_count='{}'>",
            self.ru_stime,
            self.ru_utime,
            self.db_txn_count,
            self.db_txn_duration_sec,
            self.db_sched_duration_sec,
            self.evt_db_fetch_count,
        )
    }

    /// `self += other`; mutate in place. pyo3 returns `self` for the in-place slot.
    fn __iadd__(&mut self, other: &ContextResourceUsage) {
        self.add_assign(other);
    }

    /// `self -= other`; mutate in place. pyo3 returns `self` for the in-place slot.
    fn __isub__(&mut self, other: &ContextResourceUsage) {
        self.sub_assign(other);
    }

    /// `self + other`, returning a new object.
    fn __add__(&self, other: &ContextResourceUsage) -> ContextResourceUsage {
        let mut res = self.clone();
        res.add_assign(other);
        res
    }

    /// `self - other`, returning a new object.
    fn __sub__(&self, other: &ContextResourceUsage) -> ContextResourceUsage {
        let mut res = self.clone();
        res.sub_assign(other);
        res
    }
}

/// Call the (possibly test-patched) module-level `logcontext_error(msg)`.
fn logcontext_error(py: Python<'_>, msg: String) -> PyResult<()> {
    let module = py.import("synapse.logging.context")?;
    module.getattr("logcontext_error")?.call1((msg,))?;
    Ok(())
}

/// `threading.get_ident`, cached (it never changes and this is on a hot path).
static GET_THREAD_ID: OnceCell<Py<PyAny>> = OnceCell::new();

/// The current OS thread id, matching Python's `threading.get_ident()`.
fn get_thread_id(py: Python<'_>) -> PyResult<u64> {
    let get_ident = GET_THREAD_ID.get_or_try_init(|| -> PyResult<Py<PyAny>> {
        Ok(py.import("threading")?.getattr("get_ident")?.unbind())
    })?;
    get_ident.bind(py).call0()?.extract()
}

/// Normalise a Python value to `None` if it is `None`, else `Some`.
fn none_to_option(obj: Bound<'_, PyAny>) -> Option<Py<PyAny>> {
    if obj.is_none() {
        None
    } else {
        Some(obj.unbind())
    }
}

/// Propagate a usage update to the parent context, if there is a (truthy) one.
///
/// Dispatched via `call_method1` so subclass overrides are respected. The
/// truthiness guard matches Python's `if self.parent_context:` and is
/// load-bearing: the sentinel is falsy *and* implements no `add_*` methods, so a
/// bare `is_some()` check would call a nonexistent method on it.
fn forward_to_parent<'py>(
    parent: &Option<Py<PyAny>>,
    py: Python<'py>,
    method: &str,
    args: impl PyCallArgs<'py>,
) -> PyResult<()> {
    if let Some(parent) = parent {
        let parent = parent.bind(py);
        if parent.is_truthy()? {
            parent.call_method1(method, args)?;
        }
    }
    Ok(())
}

/// The current thread's CPU usage as `(ru_utime, ru_stime)` in seconds, read
/// directly via `getrusage(RUSAGE_THREAD)`.
///
/// Returns `None` where per-thread rusage isn't available — which we take to be
/// any non-Linux target (`RUSAGE_THREAD` is Linux-only; macOS gets no per-context
/// CPU accounting, matching the historical Python behaviour). Doing this in Rust
/// avoids allocating a Python `resource.struct_rusage` object on every switch.
#[cfg(target_os = "linux")]
fn get_thread_rusage() -> Option<(f64, f64)> {
    fn timeval_to_secs(tv: libc::timeval) -> f64 {
        tv.tv_sec as f64 + tv.tv_usec as f64 / 1_000_000.0
    }

    // SAFETY: `getrusage` only writes into `usage`, and we only read it once it
    // reports success.
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    let ret = unsafe { libc::getrusage(libc::RUSAGE_THREAD, usage.as_mut_ptr()) };
    if ret != 0 {
        return None;
    }
    let usage = unsafe { usage.assume_init() };
    Some((
        timeval_to_secs(usage.ru_utime),
        timeval_to_secs(usage.ru_stime),
    ))
}

#[cfg(not(target_os = "linux"))]
fn get_thread_rusage() -> Option<(f64, f64)> {
    None
}

/// The `(user, system)` CPU seconds elapsed between `start` and `current`.
///
/// Mirrors the former `_get_cputime`: guards against the clock going backwards
/// (clamping to zero and logging, as the accounting must never go negative).
fn cputime_delta(current: (f64, f64), start: (f64, f64)) -> PyResult<(f64, f64)> {
    let mut utime_delta = current.0 - start.0;
    let mut stime_delta = current.1 - start.1;

    // sanity check
    if utime_delta < 0.0 {
        error!("utime went backwards! {} < {}", current.0, start.0);
        utime_delta = 0.0;
    }
    if stime_delta < 0.0 {
        error!("stime went backwards! {} < {}", current.1, start.1);
        stime_delta = 0.0;
    }

    Ok((utime_delta, stime_delta))
}

/// Additional context for log formatting, tracking which request a unit of work
/// belongs to and accounting CPU/DB usage against it. Contexts are scoped within
/// a `with` block.
///
/// A native port of the former Python `LoggingContext`; the attribute surface,
/// methods, error-message wording and abuse-detection behaviour are preserved so
/// callers (and Python subclasses) are unaffected.
///
/// Construction is deliberately split between `__new__` (which allocates a blank
/// instance) and `__init__` (which does the real initialisation), mirroring how a
/// pure-Python class behaves. This lets Python subclasses — in particular
/// `synapse.metrics.background_process_metrics.BackgroundProcessLoggingContext`,
/// which composes a name and then calls `super().__init__(name=..., ...)` — work
/// unchanged.
#[pyclass(subclass)]
pub struct LoggingContext {
    /// Name for the context, used in logging.
    #[pyo3(get, set)]
    name: String,
    /// The homeserver name this context is associated with.
    #[pyo3(get, set)]
    server_name: String,
    /// The OS thread id (`threading.get_ident()`) this context was created on;
    /// activity on any other thread is an error.
    #[pyo3(get, set)]
    main_thread: u64,
    /// Whether `__exit__` has run. Re-activating a finished context is an error.
    #[pyo3(get, set)]
    finished: bool,
    /// The thread CPU usage `(ru_utime, ru_stime)` in seconds captured when this
    /// context became active, or `None` if it is not currently active. Private
    /// (native `(f64, f64)` rather than a Python `struct_rusage`) so the switch
    /// path does no per-switch Python allocation; nothing outside this module
    /// reads it.
    usage_start: Option<(f64, f64)>,
    /// A short human-readable tag (e.g. the sync type); always a `str`.
    #[pyo3(get, set)]
    tag: String,
    /// The resources used by this context so far. Exposed to Python as
    /// `_resource_usage` (see the getter below); mutated in place.
    resource_usage: Py<ContextResourceUsage>,
    /// The context that was current when this one was created; restored on exit.
    #[pyo3(get, set)]
    previous_context: Option<Py<PyAny>>,
    /// The parent context, if any; usage is propagated up to it.
    #[pyo3(get, set)]
    parent_context: Option<Py<PyAny>>,
    /// The `ContextRequest` this work belongs to, if any.
    #[pyo3(get, set)]
    request: Option<Py<PyAny>>,
    /// The opentracing scope associated with this context, if any.
    #[pyo3(get, set)]
    scope: Option<Py<PyAny>>,
}

#[pymethods]
impl LoggingContext {
    /// Allocate a blank context. The real initialisation happens in `__init__`;
    /// see the type docstring for why this is split. Extra positional/keyword
    /// arguments are accepted and ignored so that subclasses passing their own
    /// constructor arguments up through `type.__call__` (which feeds the same
    /// arguments to both `__new__` and `__init__`) are not rejected here.
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn __new__(
        py: Python<'_>,
        _args: &Bound<'_, PyTuple>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        Ok(LoggingContext {
            name: String::new(),
            server_name: String::new(),
            main_thread: 0,
            finished: false,
            usage_start: None,
            tag: String::new(),
            resource_usage: Py::new(py, ContextResourceUsage::default())?,
            previous_context: None,
            parent_context: None,
            request: None,
            scope: None,
        })
    }

    #[pyo3(signature = (*, name, server_name, parent_context=None, request=None))]
    fn __init__(
        &mut self,
        py: Python<'_>,
        name: String,
        server_name: String,
        parent_context: Option<Py<PyAny>>,
        request: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        self.previous_context = Some(current_context(py));

        // track the resources used by this context so far
        self.resource_usage = Py::new(py, ContextResourceUsage::default())?;

        // The thread resource usage when the logcontext became active. None if
        // the context is not currently active.
        self.usage_start = None;

        self.name = name;
        self.server_name = server_name;
        self.main_thread = get_thread_id(py)?;
        self.request = None;
        self.tag = String::new();
        self.scope = None;

        // keep track of whether we have hit the __exit__ block for this context
        self.finished = false;

        // Inherit some fields from the parent context (read before we move it
        // into `self`, so no borrow of `self.parent_context` is held).
        if let Some(parent) = &parent_context {
            let parent = parent.bind(py);
            // which request this corresponds to
            self.request = none_to_option(parent.getattr("request")?);
            // we also track the current scope
            self.scope = none_to_option(parent.getattr("scope")?);
        }

        if let Some(request) = request {
            // the request param overrides the request from the parent context
            self.request = Some(request);
        }

        self.parent_context = parent_context;

        Ok(())
    }

    /// The resources used by this context so far (mutated in place). Named
    /// `_resource_usage` to match the historical private attribute.
    #[getter(_resource_usage)]
    fn get_resource_usage_attr(&self, py: Python<'_>) -> Py<ContextResourceUsage> {
        self.resource_usage.clone_ref(py)
    }

    fn __str__(&self) -> String {
        self.name.clone()
    }

    /// Enter this logging context, making it the current context.
    fn __enter__<'py>(slf: Bound<'py, Self>) -> PyResult<Bound<'py, Self>> {
        let py = slf.py();
        let (name, previous) = {
            let this = slf.borrow();
            (
                this.name.clone(),
                this.previous_context.as_ref().map(|p| p.clone_ref(py)),
            )
        };

        debug!(target: DEBUG_LOGGER_NAME, "LoggingContext({name}).__enter__");

        // Call the native `set_current_context` directly rather than resolving it
        // through the module: it is re-exported unchanged as the module-level name
        // and nothing patches it, so the round-trip would just circle back here.
        let old_context = set_current_context(py, slf.as_any().clone())?.into_bound(py);

        let previous = previous.unwrap_or_else(|| py.None());
        if previous.bind(py).ne(&old_context)? {
            let previous_repr: String = previous.bind(py).repr()?.extract()?;
            let old_repr: String = old_context.repr()?.extract()?;
            logcontext_error(
                py,
                format!("Expected previous context {previous_repr}, found {old_repr}"),
            )?;
        }

        Ok(slf)
    }

    /// Restore the previous logging context. Returns `None` (does not suppress
    /// exceptions).
    fn __exit__(
        slf: Bound<'_, Self>,
        _exc_type: Bound<'_, PyAny>,
        _exc_value: Bound<'_, PyAny>,
        _traceback: Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();

        let (name, previous) = {
            let this = slf.borrow();
            (
                this.name.clone(),
                this.previous_context.as_ref().map(|p| p.clone_ref(py)),
            )
        };
        let previous = previous.unwrap_or_else(|| py.None());

        if log_enabled!(target: DEBUG_LOGGER_NAME, Level::Debug) {
            // Match the Python `%s`: the str() of the previous context. Computed
            // only when the opt-in debug logger is actually enabled.
            let previous_str: String = previous.bind(py).str()?.extract()?;
            debug!(
                target: DEBUG_LOGGER_NAME,
                "LoggingContext({name}).__exit__ --> {previous_str}"
            );
        }

        let current = set_current_context(py, previous.bind(py).clone())?.into_bound(py);

        if !current.is(&slf) {
            if current.is(sentinel(py).bind(py)) {
                logcontext_error(py, format!("Expected logging context {name} was lost"))?;
            } else {
                let current_str: String = current.str()?.extract()?;
                logcontext_error(
                    py,
                    format!("Expected logging context {name} but found {current_str}"),
                )?;
            }
        }

        // the fact that we are here suggests that the caller thinks everything is
        // done and dusted for this logcontext, and further activity will not get
        // recorded against the correct metrics.
        slf.borrow_mut().finished = true;

        Ok(())
    }

    /// Record that this logcontext is currently running.
    ///
    /// This should not be called directly: use `set_current_context`. `rusage` is
    /// the thread CPU usage `(ru_utime, ru_stime)` at the point of switching to
    /// this context (`None` if the platform doesn't track it).
    fn start(slf: Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        Self::start_inner(&slf, rusage)
    }

    /// Record that this logcontext is no longer running.
    ///
    /// This should not be called directly: use `set_current_context`.
    fn stop(slf: Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        Self::stop_inner(&slf, rusage)
    }

    /// Get a *copy* of the resources used by this logcontext so far.
    fn get_resource_usage(slf: Bound<'_, Self>) -> PyResult<ContextResourceUsage> {
        let py = slf.py();

        // we always return a copy, for consistency
        let mut res = slf.borrow().resource_usage.borrow(py).clone();

        let (usage_start, main_thread) = {
            let this = slf.borrow();
            (this.usage_start, this.main_thread)
        };

        // If we are on the correct thread and we're currently running then we can
        // include resource usage so far.
        if let Some(start) = usage_start {
            if get_thread_id(py)? == main_thread {
                if let Some(current) = get_thread_rusage() {
                    let (utime_delta, stime_delta) = cputime_delta(current, start)?;
                    res.ru_utime += utime_delta;
                    res.ru_stime += stime_delta;
                }
            }
        }

        Ok(res)
    }

    /// Update the CPU time usage of this context (and any parents, recursively).
    fn add_cputime(&self, py: Python<'_>, utime_delta: f64, stime_delta: f64) -> PyResult<()> {
        {
            let mut usage = self.resource_usage.borrow_mut(py);
            usage.ru_utime += utime_delta;
            usage.ru_stime += stime_delta;
        }
        forward_to_parent(
            &self.parent_context,
            py,
            "add_cputime",
            (utime_delta, stime_delta),
        )
    }

    /// Record the use of a database transaction and how long it took.
    fn add_database_transaction(&self, py: Python<'_>, duration_sec: f64) -> PyResult<()> {
        if duration_sec < 0.0 {
            return Err(PyValueError::new_err(
                "DB txn time can only be non-negative",
            ));
        }
        {
            let mut usage = self.resource_usage.borrow_mut(py);
            usage.db_txn_count += 1;
            usage.db_txn_duration_sec += duration_sec;
        }
        forward_to_parent(
            &self.parent_context,
            py,
            "add_database_transaction",
            (duration_sec,),
        )
    }

    /// Record a use of the database pool (the time taken to get a connection).
    fn add_database_scheduled(&self, py: Python<'_>, sched_sec: f64) -> PyResult<()> {
        if sched_sec < 0.0 {
            return Err(PyValueError::new_err(
                "DB scheduling time can only be non-negative",
            ));
        }
        {
            let mut usage = self.resource_usage.borrow_mut(py);
            usage.db_sched_duration_sec += sched_sec;
        }
        forward_to_parent(
            &self.parent_context,
            py,
            "add_database_scheduled",
            (sched_sec,),
        )
    }

    /// Record a number of events being fetched from the db.
    fn record_event_fetch(&self, py: Python<'_>, event_count: i64) -> PyResult<()> {
        {
            let mut usage = self.resource_usage.borrow_mut(py);
            usage.evt_db_fetch_count += event_count;
        }
        forward_to_parent(
            &self.parent_context,
            py,
            "record_event_fetch",
            (event_count,),
        )
    }

    /// Traverse referenced Python objects for the cyclic garbage collector.
    /// `scope` and the context can reference each other, forming a real cycle.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(previous_context) = &self.previous_context {
            visit.call(previous_context)?;
        }
        if let Some(parent_context) = &self.parent_context {
            visit.call(parent_context)?;
        }
        if let Some(request) = &self.request {
            visit.call(request)?;
        }
        if let Some(scope) = &self.scope {
            visit.call(scope)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.previous_context = None;
        self.parent_context = None;
        self.request = None;
        self.scope = None;
    }
}

impl LoggingContext {
    /// Whether `__exit__` has run, for crate-internal callers (Python code reads
    /// the `finished` attribute instead).
    pub(crate) fn is_finished(&self) -> bool {
        self.finished
    }

    /// Native body of the `start` pymethod. Shared with the switch fast path in
    /// [`set_current_context`], which calls this directly for a base
    /// `LoggingContext` rather than dispatching through Python. Runs the same
    /// thread-affinity and abuse checks on both paths.
    fn start_inner(slf: &Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        let py = slf.py();
        let name = slf.borrow().name.clone();
        let main_thread = slf.borrow().main_thread;

        if get_thread_id(py)? != main_thread {
            logcontext_error(py, format!("Started logcontext {name} on different thread"))?;
            return Ok(());
        }

        if slf.borrow().finished {
            logcontext_error(py, format!("Re-starting finished log context {name}"))?;
        }

        // If we haven't already started, record the thread resource usage so far.
        if slf.borrow().usage_start.is_some() {
            logcontext_error(py, format!("Re-starting already-active log context {name}"))?;
        } else {
            slf.borrow_mut().usage_start = rusage;
        }

        Ok(())
    }

    /// Native body of the `stop` pymethod. Shared with the switch fast path.
    fn stop_inner(slf: &Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        let py = slf.py();
        let name = slf.borrow().name.clone();
        let main_thread = slf.borrow().main_thread;

        // Mirror Python's `try: ... finally: self.usage_start = None`.
        let result = (|| -> PyResult<()> {
            if get_thread_id(py)? != main_thread {
                logcontext_error(py, format!("Stopped logcontext {name} on different thread"))?;
                return Ok(());
            }

            // `if not rusage: return` — no rusage means this platform doesn't
            // track per-thread CPU, so there is nothing to account.
            let Some(current) = rusage else {
                return Ok(());
            };

            // Record the cpu used since we started.
            let Some(start) = slf.borrow().usage_start else {
                logcontext_error(
                    py,
                    format!("Called stop on logcontext {name} without recording a start rusage"),
                )?;
                return Ok(());
            };

            let (utime_delta, stime_delta) = cputime_delta(current, start)?;
            slf.borrow().add_cputime(py, utime_delta, stime_delta)?;
            Ok(())
        })();

        slf.borrow_mut().usage_start = None;
        result
    }
}

/// Dispatch a `stop`/`start` to a context during a switch, respecting subclass
/// overrides.
///
/// For a base `LoggingContext` we call the native accounting directly (the hot
/// path — no Python dispatch, no `struct_rusage` allocation). The sentinel is a
/// no-op. Anything else is a Python subclass (e.g.
/// `BackgroundProcessLoggingContext`), so we go through Python — materialising
/// the rusage as a `(utime, stime)` tuple only here, off the hot path — so its
/// overrides run.
fn switch_context(
    obj: &Bound<'_, PyAny>,
    method: &str,
    rusage: Option<(f64, f64)>,
) -> PyResult<()> {
    let py = obj.py();
    if let Ok(base) = obj.cast_exact::<LoggingContext>() {
        match method {
            "start" => LoggingContext::start_inner(base, rusage),
            _ => LoggingContext::stop_inner(base, rusage),
        }
    } else if obj.is(sentinel(py).bind(py)) {
        Ok(())
    } else {
        obj.call_method1(method, (rusage,))?;
        Ok(())
    }
}

/// Set the current logging context, returning the context that was previously
/// current.
///
/// The native replacement for the former Python `set_current_context`: it keeps
/// the accounting policy (read the thread rusage once, `stop` the old context and
/// `start` the new one) but does it all natively — `getrusage` via libc and, for
/// the common base-`LoggingContext` case, the `stop`/`start` bookkeeping inline
/// with no per-switch Python allocation.
#[pyfunction]
pub fn set_current_context(py: Python<'_>, context: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    // everything blows up if we allow current_context to be set to None, so
    // sanity-check that now.
    if context.is_none() {
        return Err(PyTypeError::new_err("'context' argument may not be None"));
    }

    let current = current_context(py);

    if !current.bind(py).is(&context) {
        let rusage = get_thread_rusage();
        switch_context(current.bind(py), "stop", rusage)?;
        // Raw slot write; we already hold `current`, so ignore the previous value
        // it returns.
        swap_current_context(py, context.clone().unbind());
        switch_context(&context, "start", rusage)?;
    }

    Ok(current)
}

/// Get a reference to the sentinel logcontext singleton, creating it on first use.
///
/// The instance is owned here (not pushed in from Python), so its identity is
/// stable and equal to the `SENTINEL_CONTEXT` exported by [`register_module`],
/// preserving `context is SENTINEL_CONTEXT` and `bool(context)` semantics.
fn sentinel(py: Python<'_>) -> Py<PyAny> {
    SENTINEL
        .get_or_init(|| {
            Py::new(py, Sentinel::instance())
                .expect("failed to create the sentinel logcontext")
                .into_any()
        })
        .clone_ref(py)
}

/// Get the current logging context.
///
/// Resolves the tokio task-local first (so logging emitted while a task is being
/// polled is attributed to the context that was current when the task was
/// spawned), then this OS thread's slot, then the sentinel.
#[pyfunction]
pub fn current_context(py: Python<'_>) -> Py<PyAny> {
    if let Some(ctx) = LogContext::current() {
        return ctx.context.clone_ref(py);
    }

    THREAD_LOCAL_CONTEXT.with(|slot| match &*slot.borrow() {
        Some(ctx) => ctx.clone_ref(py),
        None => sentinel(py),
    })
}

/// Set this OS thread's current logging context, returning the context that was
/// previously current *on this thread*.
///
/// This is the raw slot write only — it does **not** do any resource-usage
/// accounting or thread-affinity checks; `synapse.logging.context.set_current_context`
/// wraps this with the `getrusage` start/stop bookkeeping.
///
/// Note this deliberately only touches the thread-local slot, never the tokio
/// task-local: the switch primitive is only ever driven on reactor/threadpool
/// threads (Python code), while the task-local is populated once at spawn time by
/// [`LogContext::scope`].
#[pyfunction]
pub fn swap_current_context(py: Python<'_>, context: Py<PyAny>) -> Py<PyAny> {
    let previous = THREAD_LOCAL_CONTEXT.with(|slot| slot.borrow_mut().replace(context));
    match previous {
        Some(ctx) => ctx,
        None => sentinel(py),
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "logcontext")?;
    child_module.add_class::<ContextResourceUsage>()?;
    child_module.add_class::<LoggingContext>()?;
    child_module.add_class::<Sentinel>()?;
    child_module.add_function(wrap_pyfunction!(current_context, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(swap_current_context, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(set_current_context, &child_module)?)?;
    child_module.add("DEBUG_LOGGER_NAME", DEBUG_LOGGER_NAME)?;
    // The sentinel singleton is owned by Rust; export the one instance so Python's
    // `SENTINEL_CONTEXT` is that exact object (identity preserved).
    child_module.add("SENTINEL_CONTEXT", sentinel(py))?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import logcontext` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.logcontext", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use pyo3::types::PyString;

    use super::*;

    /// The native sentinel singleton (created lazily on first use), used for
    /// identity assertions.
    fn registered_sentinel(py: Python<'_>) -> Py<PyAny> {
        sentinel(py)
    }

    #[test]
    fn thread_local_defaults_to_sentinel() {
        Python::initialize();
        Python::attach(|py| {
            let sentinel = registered_sentinel(py);
            // Nothing set on this (fresh) test thread → sentinel.
            assert!(current_context(py).bind(py).is(sentinel.bind(py)));
        });
    }

    #[test]
    fn swap_returns_previous_and_updates_thread_local() {
        Python::initialize();
        Python::attach(|py| {
            let sentinel = registered_sentinel(py);
            let a = PyString::new(py, "A").into_any().unbind();
            let b = PyString::new(py, "B").into_any().unbind();

            // Swapping in A returns the previous (sentinel) and makes A current.
            let prev = swap_current_context(py, a.clone_ref(py));
            assert!(prev.bind(py).is(sentinel.bind(py)));
            assert!(current_context(py).bind(py).is(a.bind(py)));

            // Swapping in B returns A.
            let prev = swap_current_context(py, b.clone_ref(py));
            assert!(prev.bind(py).is(a.bind(py)));
            assert!(current_context(py).bind(py).is(b.bind(py)));

            // Restore the sentinel so we don't leak into any other test that
            // happens to reuse this OS thread from the test harness pool.
            swap_current_context(py, sentinel);
        });
    }

    #[test]
    fn task_local_takes_precedence_over_thread_local() {
        Python::initialize();
        Python::attach(|py| {
            let sentinel = registered_sentinel(py);
            let task_ctx = PyString::new(py, "TASKCTX").into_any().unbind();

            // Outside any scoped task, `current_context` resolves the
            // thread-local (here the sentinel).
            assert!(current_context(py).bind(py).is(sentinel.bind(py)));
            assert!(LogContext::current().is_none());

            let log_context = LogContext {
                context: Arc::new(task_ctx.clone_ref(py)),
            };

            let rt = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();

            rt.block_on(log_context.scope(async {
                // Inside the scope, both the Rust handle and the pyfunction (the
                // thing the log filter calls) resolve the task-local context —
                // even though the thread-local is still the sentinel.
                assert!(LogContext::current().is_some());
                Python::attach(|py| {
                    assert!(current_context(py).bind(py).is(task_ctx.bind(py)));
                });
            }));

            // Once the scope ends, we fall back to the thread-local again.
            assert!(LogContext::current().is_none());
            assert!(current_context(py).bind(py).is(sentinel.bind(py)));
        });
    }
}
