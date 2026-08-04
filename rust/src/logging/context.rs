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

//! Rust storage for the Synapse "current logcontext".
//!
//! The storage lives in Rust rather than in a Python `threading.local` because
//! a Python thread-local is invisible to the Rust tokio threads.
//!
//! There are two pieces of storage, and [`current_context`] combines them so
//! that it answers correctly from both Python and Rust:
//!
//! 1. [`THREAD_LOCAL_CONTEXT`], one value per OS thread. Used by the Python
//!    reactor thread and any reactor-managed threadpool threads.
//! 2. [`TASK_LOCAL_CONTEXT`], one value per tokio task, set when the task is
//!    spawned. It stays with the task as the task moves between worker threads
//!    across `.await` points.
//!
//! Both hold an `Option<Py<LoggingContext>>`, where `None` means "no context",
//! i.e. the sentinel. The `_Sentinel` object itself stays pure Python
//! (`synapse.logging.context.SENTINEL_CONTEXT`): the wrappers there convert
//! between it and `None`, so Rust code never sees the sentinel object.
//!
//! [`current_context`] returns the task-local value when called from inside a
//! tokio task, and the thread-local value otherwise. `LoggingContextFilter`
//! (and therefore `pyo3-log`) calls [`current_context`] for each log record, so
//! records emitted while a task runs are attributed to that task's context
//! without having to call into Python.
//!
//! [`set_current_context`] also does the CPU accounting. It reads the thread
//! rusage via libc, calls `stop` on the old context and `start` on the new one,
//! and writes the new context with [`swap_current_context`]. It is only ever
//! called on reactor or threadpool threads, never on tokio worker threads, so
//! it always writes the thread-local. The task-local is written exactly once,
//! at spawn time, by [`LogContextHandle::scope`]. [`swap_current_context`]
//! checks that invariant rather than trusting it.

use std::{cell::RefCell, future::Future};

use log::{debug, error, log_enabled, Level};
use pyo3::call::PyCallArgs;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use pyo3::{intern, PyTraverseError, PyVisit};

/// Name of the opt-in logger for debugging when the logcontext switches.
///
/// Used as the `debug!` `target:` for the messages emitted below, and exported
/// to Python via [`register_module`] so that `synapse.logging.context` builds
/// exactly this logger (see `logcontext_debug_logger` there). Sharing one
/// constant keeps the two names in sync. The messages only appear when this
/// logger is explicitly configured: `ExplicitlyConfiguredLogger` on the Python
/// side implements that, and pyo3-log honours its `isEnabledFor`, so the
/// opt-in applies to messages from Rust too.
pub const DEBUG_LOGGER_NAME: &str = "synapse.logging.context.debug";

thread_local! {
    /// The current logcontext for this OS thread, a [`LoggingContext`]
    /// (possibly a Python subclass instance), or `None` for the sentinel.
    static THREAD_LOCAL_CONTEXT: RefCell<Option<Py<LoggingContext>>> = const { RefCell::new(None) };
}

tokio::task_local! {
    /// The logcontext captured for the current tokio task, set by
    /// [`LogContextHandle::scope`] when the task is spawned. Only present
    /// inside such a task. Readable during any poll of the task, whichever
    /// worker thread runs it.
    static TASK_LOCAL_CONTEXT: LogContextHandle;
}

/// A captured logcontext that can be cloned and read without the GIL. Holds a
/// [`LoggingContext`] (possibly a Python subclass instance), or `None` for the
/// sentinel.
#[derive(Clone)]
pub struct LogContextHandle {
    context: std::sync::Arc<Option<Py<LoggingContext>>>,
}

impl LogContextHandle {
    /// Capture the calling thread's current logcontext.
    pub fn capture(py: Python<'_>) -> Self {
        LogContextHandle {
            context: std::sync::Arc::new(current_context(py)),
        }
    }

    /// Create a handle to the logcontext of the current tokio task, if we are
    /// running inside one that was spawned through [`LogContextHandle::scope`].
    pub fn task_current() -> Option<LogContextHandle> {
        TASK_LOCAL_CONTEXT.try_with(|c| c.clone()).ok()
    }

    /// Run `fut` with this logcontext active for the duration of the task.
    pub fn scope<F>(self, fut: F) -> impl Future<Output = F::Output>
    where
        F: Future,
    {
        TASK_LOCAL_CONTEXT.scope(self, fut)
    }

    /// The captured [`LoggingContext`], or `None` if the sentinel was captured.
    pub fn logging_context(&self) -> Option<&Py<LoggingContext>> {
        self.context.as_ref().as_ref()
    }
}

/// Tracks the resources used by a log context.
///
/// The attributes, operators and `repr` format are relied on by Python callers
/// (`Measure`, request and background-process metrics, the task scheduler,
/// etc). Implemented in Rust so that [`set_current_context`] can update the
/// counters without allocating a Python object each time.
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
    /// If `copy_from` is given, copy its stats; otherwise start at zero.
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
        // `Measure` logs this string in its "Failed to save metrics!" warning,
        // and log scrapers may match on it, so keep the format stable.
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

    /// `self += other`, mutating in place.
    fn __iadd__(&mut self, other: &ContextResourceUsage) {
        self.add_assign(other);
    }

    /// `self -= other`, mutating in place.
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

/// Call `logcontext_error(msg)` in `synapse.logging.context`.
///
/// Looked up at call time rather than cached, so tests can patch it.
fn logcontext_error(py: Python<'_>, msg: String) -> PyResult<()> {
    let module = py.import("synapse.logging.context")?;
    module.getattr("logcontext_error")?.call1((msg,))?;
    Ok(())
}

extern "C" {
    /// CPython's thread identifier (`pythread.h`) — the exact value
    /// `threading.get_ident()` returns. Part of the stable ABI, but not bound
    /// by pyo3-ffi.
    fn PyThread_get_thread_ident() -> std::os::raw::c_ulong;
}

/// This thread's `threading.get_ident()` value, read without calling into
/// Python.
///
/// Note that `get_ident` is *not* an OS-level tid. On Linux it returns the same
/// value either side of a `fork()` call. Synapse forks in exactly one place, so
/// contexts created before the fork still pass the `main_thread` check after
/// it.
fn get_thread_id() -> u64 {
    // SAFETY: no preconditions; returns an identifier for the calling thread.
    (unsafe { PyThread_get_thread_ident() }) as u64
}

/// Propagate a usage update to the parent context, if there is a (truthy, i.e.
/// non-sentinel) one.
///
/// Handles the parent being a [`LoggingContext`] subclass (a Python object),
/// but has a fast-path for the common case that the parent is a base
/// [`LoggingContext`].
fn forward_to_parent<'py, N>(
    parent: &Option<Py<PyAny>>,
    py: Python<'py>,
    method: &str,
    args: impl PyCallArgs<'py>,
    native: N,
) -> PyResult<()>
where
    N: FnOnce(&Bound<'py, LoggingContext>) -> PyResult<()>,
{
    if let Some(parent) = parent {
        let parent = parent.bind(py);

        // Check if the parent is a base LoggingContext, and if so call the
        // native Rust method rather than going through Python.
        if let Ok(base) = parent.cast_exact::<LoggingContext>() {
            return native(base);
        }

        // Otherwise call the method on the parent, if it is truthy. This
        // handles Python subclasses of `LoggingContext`.
        if parent.is_truthy()? {
            parent.call_method1(method, args)?;
        }
    }
    Ok(())
}

/// The current thread's CPU usage as `(ru_utime, ru_stime)` in seconds, read
/// via `getrusage(RUSAGE_THREAD)`.
///
/// Returns `None` where per-thread rusage isn't available, which we take to
/// be any non-Linux target: `RUSAGE_THREAD` is Linux-only, so e.g. macOS gets
/// no per-context CPU accounting.
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
/// Guards against the clock going backwards (clamping to zero and logging, as
/// the accounting must never go negative).
fn cputime_delta(current: (f64, f64), start: (f64, f64)) -> (f64, f64) {
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

    (utime_delta, stime_delta)
}

/// Additional context for log formatting, tracking which request a unit of work
/// belongs to and accounting CPU/DB usage against it. Contexts are scoped
/// within a `with` block.
///
/// The attributes and methods are relied on by Python callers and subclasses
/// (notably `BackgroundProcessLoggingContext`).
///
/// Construction is split between `__new__`, which allocates a blank instance,
/// and `__init__`, which does the real initialisation, to support subclasses.
#[pyclass(subclass)]
pub struct LoggingContext {
    /// Name for the context, used in logging. Stored as a Python string.
    /// `LoggingContextFilter` calls `str(context)` on every log record, and
    /// returning the stored `Py<PyString>` only bumps a reference count where a
    /// `String` field would allocate a fresh `str` per record.
    #[pyo3(get, set)]
    name: Py<PyString>,
    /// The homeserver name this context is associated with. Stored as a Python
    /// string for the same reason as `name` (read per log record).
    #[pyo3(get, set)]
    server_name: Py<PyString>,
    /// The `threading.get_ident()` value of the thread this context was
    /// created on (see [`get_thread_id`] for why it is not a real OS tid).
    /// Activity on any other thread is an error. Settable only so tests can
    /// simulate activity on the wrong thread.
    #[pyo3(get, set)]
    main_thread: u64,
    /// Whether `__exit__` has run. Re-activating a finished context is an error.
    #[pyo3(get, set)]
    finished: bool,
    /// The thread CPU usage `(ru_utime, ru_stime)` in seconds captured when
    /// this context became active, or `None` if it is not currently active.
    /// Kept as a plain `(f64, f64)` rather than a Python `struct_rusage` so
    /// that switching contexts allocates no Python object. Nothing outside
    /// this module reads it, so it is not exposed to Python.
    usage_start: Option<(f64, f64)>,
    /// A short human-readable tag (e.g. the sync type). Initialised to `""` and
    /// treated as a `str` by everything in-tree, but `Option` so that assigning
    /// `None` (which the sentinel's `tag` reports, and which out-of-tree
    /// callers may assign) is allowed.
    #[pyo3(get, set)]
    tag: Option<String>,
    /// The resources used by this context so far.
    resource_usage: Py<ContextResourceUsage>,
    /// The context that was current when this one was created. Restored on
    /// exit.
    #[pyo3(get, set)]
    previous_context: Option<Py<LoggingContext>>,
    /// The parent context, if any.
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
    /// Allocate a blank context. The real initialisation happens in `__init__`,
    /// the same as for a normal Python class. Extra positional/keyword
    /// arguments are accepted and ignored. `type.__call__` feeds the same
    /// arguments to both `__new__` and `__init__`, so a subclass constructor's
    /// arguments must not be rejected here.
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    fn __new__(
        py: Python<'_>,
        _args: &Bound<'_, PyTuple>,
        _kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        Ok(LoggingContext {
            name: intern!(py, "").clone().unbind(),
            server_name: intern!(py, "").clone().unbind(),
            main_thread: 0,
            finished: false,
            usage_start: None,
            tag: Some(String::new()),
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
        name: Bound<'_, PyString>,
        server_name: Bound<'_, PyString>,
        parent_context: Option<Py<PyAny>>,
        request: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        self.previous_context = current_context(py);

        // The resource-usage tracker was already allocated (zeroed) by `__new__`,
        // which `type.__call__` runs immediately before this.

        self.usage_start = None;

        self.name = name.unbind();
        self.server_name = server_name.unbind();
        self.main_thread = get_thread_id();
        self.request = None;
        self.tag = Some(String::new());
        self.scope = None;

        // keep track of whether we have hit the __exit__ block for this context
        self.finished = false;

        // Inherit some fields from the parent context (read before we move it
        // into `self`, so no borrow of `self.parent_context` is held).
        if let Some(parent) = &parent_context {
            let parent = parent.bind(py);
            // which request this corresponds to
            self.request = parent.getattr("request")?.extract()?;
            // we also track the current scope
            self.scope = parent.getattr("scope")?.extract()?;
        }

        if let Some(request) = request {
            // the request param overrides the request from the parent context
            self.request = Some(request);
        }

        self.parent_context = parent_context;

        Ok(())
    }

    /// Returns the stored name object itself, without copying the string.
    /// This runs for every log record via `LoggingContextFilter`.
    fn __str__(&self, py: Python<'_>) -> Py<PyString> {
        self.name.clone_ref(py)
    }

    /// Enter this logging context, making it the current context.
    fn __enter__<'py>(slf: Bound<'py, Self>) -> PyResult<Bound<'py, Self>> {
        let py = slf.py();
        // Clone the reference rather than holding a borrow across the call.
        // `set_current_context` re-borrows `slf` (borrow_mut in `start_inner`).
        let previous = slf
            .borrow()
            .previous_context
            .as_ref()
            .map(|p| p.clone_ref(py));

        if log_enabled!(target: DEBUG_LOGGER_NAME, Level::Debug) {
            // Only build the name string when the opt-in debug logger is
            // enabled. This runs on every context entry.
            debug!(
                target: DEBUG_LOGGER_NAME,
                "LoggingContext({}).__enter__",
                slf.borrow().name_string(py)
            );
        }

        let old_context = set_current_context(py, Some(slf.clone().unbind()))?;

        if !are_contexts_identical(&previous, &old_context) {
            let previous_repr = context_repr(py, &previous)?;
            let old_repr = context_repr(py, &old_context)?;
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

        let previous = slf
            .borrow()
            .previous_context
            .as_ref()
            .map(|p| p.clone_ref(py));

        if log_enabled!(target: DEBUG_LOGGER_NAME, Level::Debug) {
            // The `str()` of the previous context, or `"sentinel"` for
            // `None`. Only built (along with the name) when the opt-in debug
            // logger is enabled. This runs on every context exit.
            let previous_str = match &previous {
                Some(p) => p.bind(py).str()?.extract::<String>()?,
                None => "sentinel".to_owned(),
            };
            debug!(
                target: DEBUG_LOGGER_NAME,
                "LoggingContext({}).__exit__ --> {previous_str}",
                slf.borrow().name_string(py)
            );
        }

        let current = set_current_context(py, previous)?;

        let restored_self = current.as_ref().is_some_and(|c| c.bind(py).is(&slf));
        if !restored_self {
            // Error path: the name string is only built for the message.
            let name = slf.borrow().name_string(py);
            match &current {
                None => logcontext_error(py, format!("Expected logging context {name} was lost"))?,
                Some(current) => {
                    let current_str: String = current.bind(py).str()?.extract()?;
                    logcontext_error(
                        py,
                        format!("Expected logging context {name} but found {current_str}"),
                    )?;
                }
            }
        }

        slf.borrow_mut().finished = true;

        Ok(())
    }

    /// Record that this logcontext is currently running.
    ///
    /// This should not be called directly, use `set_current_context`. `rusage` is
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

        let mut res = slf.borrow().resource_usage.borrow(py).clone();

        let (usage_start, main_thread) = {
            let this = slf.borrow();
            (this.usage_start, this.main_thread)
        };

        // If we are on the correct thread and we're currently running then we can
        // include resource usage so far.
        if let Some(start) = usage_start {
            if get_thread_id() == main_thread {
                if let Some(current) = get_thread_rusage() {
                    let (utime_delta, stime_delta) = cputime_delta(current, start);
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
            |p| p.borrow().add_cputime(py, utime_delta, stime_delta),
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
            |p| p.borrow().add_database_transaction(py, duration_sec),
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
            |p| p.borrow().add_database_scheduled(py, sched_sec),
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
            |p| p.borrow().record_event_fetch(py, event_count),
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
    /// The context name as an owned Rust string.
    ///
    /// This copies the string data, so it is for error/debug paths only.
    /// Switching contexts should not allocate in the common case.
    fn name_string(&self, py: Python<'_>) -> String {
        self.name.bind(py).to_string_lossy().into_owned()
    }

    /// Rust body of the `start` pymethod. [`set_current_context`] calls this
    /// directly for a base `LoggingContext` rather than dispatching through
    /// Python.
    ///
    /// This (like [`Self::stop_inner`]) runs on every context switch. The
    /// error branches build the name string themselves so that the common
    /// path does not allocate.
    fn start_inner(slf: &Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        let py = slf.py();
        let main_thread = slf.borrow().main_thread;

        if get_thread_id() != main_thread {
            let name = slf.borrow().name_string(py);
            logcontext_error(py, format!("Started logcontext {name} on different thread"))?;
            return Ok(());
        }

        if slf.borrow().finished {
            let name = slf.borrow().name_string(py);
            logcontext_error(py, format!("Re-starting finished log context {name}"))?;
        }

        // If we haven't already started, record the thread resource usage so far.
        if slf.borrow().usage_start.is_some() {
            let name = slf.borrow().name_string(py);
            logcontext_error(py, format!("Re-starting already-active log context {name}"))?;
        } else {
            slf.borrow_mut().usage_start = rusage;
        }

        Ok(())
    }

    /// Rust body of the `stop` pymethod; see [`Self::start_inner`].
    fn stop_inner(slf: &Bound<'_, Self>, rusage: Option<(f64, f64)>) -> PyResult<()> {
        let py = slf.py();
        let main_thread = slf.borrow().main_thread;

        // `finally`-style: `usage_start` must be cleared however we exit.
        let result = (|| -> PyResult<()> {
            if get_thread_id() != main_thread {
                let name = slf.borrow().name_string(py);
                logcontext_error(py, format!("Stopped logcontext {name} on different thread"))?;
                return Ok(());
            }

            // No rusage means this platform doesn't track per-thread CPU, so
            // there is nothing to account.
            let Some(current) = rusage else {
                return Ok(());
            };

            // Record the cpu used since we started.
            let Some(start) = slf.borrow().usage_start else {
                let name = slf.borrow().name_string(py);
                logcontext_error(
                    py,
                    format!("Called stop on logcontext {name} without recording a start rusage"),
                )?;
                return Ok(());
            };

            let (utime_delta, stime_delta) = cputime_delta(current, start);
            slf.borrow().add_cputime(py, utime_delta, stime_delta)?;
            Ok(())
        })();

        slf.borrow_mut().usage_start = None;
        result
    }
}

/// Which way a [`switch_context`] dispatch goes.
#[derive(Clone, Copy)]
enum SwitchDirection {
    Start,
    Stop,
}

/// Call `stop` or `start` on a context as the current context changes,
/// respecting subclass overrides.
///
/// `None` (the sentinel) is a no-op.
///
/// Handles the context being a Python subclass of [`LoggingContext`], but has
/// a fast-path for the common case that it is a base [`LoggingContext`].
fn switch_context(
    py: Python<'_>,
    ctx: Option<&Py<LoggingContext>>,
    direction: SwitchDirection,
    rusage: Option<(f64, f64)>,
) -> PyResult<()> {
    let Some(ctx) = ctx else {
        return Ok(());
    };
    let ctx = ctx.bind(py);
    if ctx.as_any().is_exact_instance_of::<LoggingContext>() {
        match direction {
            SwitchDirection::Start => LoggingContext::start_inner(ctx, rusage),
            SwitchDirection::Stop => LoggingContext::stop_inner(ctx, rusage),
        }
    } else {
        let method = match direction {
            SwitchDirection::Start => intern!(py, "start"),
            SwitchDirection::Stop => intern!(py, "stop"),
        };
        ctx.call_method1(method, (rusage,))?;
        Ok(())
    }
}

/// Whether two stored values are the same context, or both the sentinel.
/// `LoggingContext` has no `__eq__`, so contexts compare by identity.
fn are_contexts_identical(a: &Option<Py<LoggingContext>>, b: &Option<Py<LoggingContext>>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(a), Some(b)) => a.is(b),
        _ => false,
    }
}

/// `repr()` of a stored context, for error messages only. `None` renders as
/// `"None"`.
fn context_repr(py: Python<'_>, slot: &Option<Py<LoggingContext>>) -> PyResult<String> {
    match slot {
        Some(ctx) => Ok(ctx.bind(py).repr()?.extract()?),
        None => Ok("None".to_owned()),
    }
}

/// Set the current logging context, returning the context that was previously
/// current. `None` means the sentinel, in both directions.
///
/// `context` must be a [`LoggingContext`] (or subclass) or `None`.
///
/// This is not the Python-facing API.
/// `synapse.logging.context.set_current_context` wraps this, converting between
/// `SENTINEL_CONTEXT` and `None`.
///
/// Reads the thread rusage once via libc, calls `stop` on the old context and
/// `start` on the new one.
#[pyfunction]
#[pyo3(signature = (context))]
pub fn set_current_context(
    py: Python<'_>,
    context: Option<Py<LoggingContext>>,
) -> PyResult<Option<Py<LoggingContext>>> {
    let current = current_context(py);

    if !are_contexts_identical(&current, &context) {
        let rusage = get_thread_rusage();
        switch_context(py, current.as_ref(), SwitchDirection::Stop, rusage)?;
        // We already hold `current`, so ignore the previous value the swap
        // returns. The clone_ref keeps a reference for the `start` below.
        let new_ref = context.as_ref().map(|ctx| ctx.clone_ref(py));
        swap_current_context(context);
        switch_context(py, new_ref.as_ref(), SwitchDirection::Start, rusage)?;
    }

    Ok(current)
}

/// Run `f` with `context` as the current logcontext (`None` meaning the
/// sentinel), restoring the previously-current context afterwards. This is the
/// Rust equivalent of Python's `with PreserveLoggingContext(context):`.
///
/// The restore runs whether or not `f` fails. If both `f` and the restore fail,
/// `f`'s error is reported.
pub(crate) fn with_logcontext<R>(
    py: Python<'_>,
    context: Option<Py<LoggingContext>>,
    f: impl FnOnce() -> PyResult<R>,
) -> PyResult<R> {
    let previous = set_current_context(py, context)?;
    let result = f();
    let restored = set_current_context(py, previous);

    let value = result?;
    restored?;
    Ok(value)
}

/// Get the current logging context, or `None` for the sentinel.
///
/// Returns the tokio task-local value when called from inside a task that has
/// one (even if what it captured was `None`/the sentinel) and this OS thread's
/// value otherwise. Logging emitted while a task runs is therefore attributed
/// to the context that was current when the task was spawned.
///
/// This is not the Python-facing API.
/// `synapse.logging.context.current_context` wraps this and returns
/// `SENTINEL_CONTEXT` instead of `None`.
#[pyfunction]
pub fn current_context(py: Python<'_>) -> Option<Py<LoggingContext>> {
    if let Some(handle) = LogContextHandle::task_current() {
        return handle.logging_context().map(|ctx| ctx.clone_ref(py));
    }

    THREAD_LOCAL_CONTEXT.with(|slot| slot.borrow().as_ref().map(|ctx| ctx.clone_ref(py)))
}

/// Replace this OS thread's current logging context, returning the previous one
/// (`None` is the sentinel, in both directions).
///
/// This is the raw write only. It does no resource-usage accounting and no
/// thread checks; [`set_current_context`] wraps it with the `getrusage`
/// `stop`/`start` bookkeeping.
///
/// It never touches the tokio task-local. The current context is only ever
/// changed on reactor/threadpool threads, while the task-local is written
/// exactly once, at spawn time, by [`LogContextHandle::scope`]. If this is
/// nonetheless called while a task-local is in scope, it logs an error, leaves
/// the thread-local unchanged and returns `None`.
fn swap_current_context(context: Option<Py<LoggingContext>>) -> Option<Py<LoggingContext>> {
    // A task-local in scope would shadow the write (`current_context` prefers
    // it), so refuse rather than misattribute later reads.
    if TASK_LOCAL_CONTEXT.try_with(|_| ()).is_ok() {
        error!(
            "swap_current_context called during a tokio-scoped poll; the switch is \
             invisible to current_context() and will misattribute logs and metrics"
        );
        return None;
    }

    THREAD_LOCAL_CONTEXT.with(|slot| std::mem::replace(&mut *slot.borrow_mut(), context))
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "logcontext")?;
    child_module.add_class::<ContextResourceUsage>()?;
    child_module.add_class::<LoggingContext>()?;
    child_module.add_function(wrap_pyfunction!(current_context, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(set_current_context, &child_module)?)?;
    child_module.add("DEBUG_LOGGER_NAME", DEBUG_LOGGER_NAME)?;

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

    /// A minimal `LoggingContext` for these tests, built directly (bypassing
    /// `__init__`, which would capture the current context and thread id).
    fn test_context(py: Python<'_>, name: &str) -> Py<LoggingContext> {
        Py::new(
            py,
            LoggingContext {
                name: PyString::new(py, name).unbind(),
                server_name: PyString::new(py, "test_server").unbind(),
                main_thread: 0,
                finished: false,
                usage_start: None,
                tag: Some(String::new()),
                resource_usage: Py::new(py, ContextResourceUsage::default())
                    .expect("failed to allocate ContextResourceUsage"),
                previous_context: None,
                parent_context: None,
                request: None,
                scope: None,
            },
        )
        .expect("failed to allocate LoggingContext")
    }

    #[test]
    fn thread_local_defaults_to_sentinel() {
        Python::initialize();
        Python::attach(|py| {
            // Nothing has been set on this fresh test thread, so the current
            // context is `None`: the sentinel.
            assert!(current_context(py).is_none());
        });
    }

    #[test]
    fn swap_returns_previous_and_updates_thread_local() {
        Python::initialize();
        Python::attach(|py| {
            let a = test_context(py, "A");
            let b = test_context(py, "B");

            // Swapping in A returns the previous value (`None` / the sentinel)
            // and makes A current.
            let prev = swap_current_context(Some(a.clone_ref(py)));
            assert!(prev.is_none());
            assert!(current_context(py)
                .expect("expected a current context")
                .bind(py)
                .is(a.bind(py)));

            // Swapping in B returns A.
            let prev = swap_current_context(Some(b.clone_ref(py)));
            assert!(prev
                .expect("expected previous context")
                .bind(py)
                .is(a.bind(py)));
            assert!(current_context(py)
                .expect("expected a current context")
                .bind(py)
                .is(b.bind(py)));

            // Reset to the sentinel so we don't leak into another test that
            // reuses this OS thread from the test harness's pool.
            swap_current_context(None);
        });
    }

    #[test]
    fn task_local_takes_precedence_over_thread_local() {
        Python::initialize();
        Python::attach(|py| {
            let task_ctx = test_context(py, "TASKCTX");

            // Outside any task, `current_context` reads the thread-local, which
            // here is `None` / the sentinel.
            assert!(current_context(py).is_none());
            assert!(LogContextHandle::task_current().is_none());

            let log_context = LogContextHandle {
                context: Arc::new(Some(task_ctx.clone_ref(py))),
            };

            let rt = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();

            rt.block_on(log_context.scope(async {
                // Inside the scope, both the Rust handle and the
                // `current_context` (what the log filter calls) return the
                // task-local context, even though the thread-local is still
                // unset.
                assert!(LogContextHandle::task_current().is_some());
                Python::attach(|py| {
                    assert!(current_context(py)
                        .expect("expected a current context")
                        .bind(py)
                        .is(task_ctx.bind(py)));
                });
            }));

            // Once the scope ends, we fall back to the thread-local again.
            assert!(LogContextHandle::task_current().is_none());
            assert!(current_context(py).is_none());
        });
    }

    #[test]
    fn swap_during_scoped_poll_leaves_thread_local_untouched() {
        Python::initialize();
        Python::attach(|py| {
            let thread_ctx = test_context(py, "THREAD");
            let task_ctx = test_context(py, "TASK");
            let stray = test_context(py, "STRAY");

            // Give this thread a current context.
            swap_current_context(Some(thread_ctx.clone_ref(py)));

            let log_context = LogContextHandle {
                context: Arc::new(Some(task_ctx.clone_ref(py))),
            };

            let rt = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();

            rt.block_on(log_context.scope(async {
                Python::attach(|py| {
                    // Swapping while a task-local is in scope is refused: it
                    // returns `None` and leaves the thread-local unchanged.
                    assert!(swap_current_context(Some(stray.clone_ref(py))).is_none());

                    // The task-local still governs reads inside the scope.
                    assert!(current_context(py)
                        .expect("expected a current context")
                        .bind(py)
                        .is(task_ctx.bind(py)));
                });
            }));

            // The scope has ended, the thread-local still holds the original
            // context, not the stray value from the invalid swap.
            assert!(current_context(py)
                .expect("expected a current context")
                .bind(py)
                .is(thread_ctx.bind(py)));

            // Reset to the sentinel so we don't leak into another test that
            // reuses this OS thread from the test harness's pool.
            swap_current_context(None);
        });
    }
}
