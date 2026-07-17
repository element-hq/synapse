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
//! The storage lives in Rust rather than in a Python `threading.local` because a
//! Python thread-local is invisible to Rust: each tokio worker thread would see
//! its own slot, permanently at the sentinel, and logging emitted from Rust —
//! including from spawned tokio tasks — could not be attributed to the request
//! that caused it.
//!
//! This module holds that storage — a per-OS-thread slot
//! ([`THREAD_LOCAL_CONTEXT`]) used by the reactor thread and any
//! reactor-managed threadpool threads — along with the logcontext classes
//! themselves. `LoggingContextFilter` (and therefore `pyo3-log`) resolves the
//! context by calling [`current_context`] at log-record time.
//!
//! The slot holds an `Option<Py<LoggingContext>>`: `None` means "no context" —
//! what Synapse calls the sentinel. The `_Sentinel` marker object itself is pure
//! Python (`synapse.logging.context.SENTINEL_CONTEXT`); the wrappers there
//! convert between it and `None` at the boundary, so no Rust code ever sees or
//! produces the sentinel object.
//!
//! The accounting policy is native too: [`set_current_context`] reads the thread
//! rusage via libc, runs the `stop`/`start` bookkeeping, and uses
//! [`swap_current_context`] for the raw slot write.
//!
//! TODO: tokio tasks do not yet see a logcontext — a worker thread's slot is
//! always empty, so Rust-emitted log records still land in the sentinel. A
//! task-scoped capture (carried with the task across `.await` points and
//! consulted by [`current_context`] ahead of the thread slot) follows in the
//! next change.

use std::cell::RefCell;

use log::{debug, error, log_enabled, Level};
use pyo3::call::PyCallArgs;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};
use pyo3::{intern, PyTraverseError, PyVisit};

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
    /// The current logcontext for this OS thread. The slot is typed: it holds a
    /// [`LoggingContext`] (possibly a Python subclass instance), and `None` means
    /// "no context set on this thread", i.e. the sentinel — a fresh thread (e.g.
    /// a new threadpool worker) therefore starts in the sentinel.
    static THREAD_LOCAL_CONTEXT: RefCell<Option<Py<LoggingContext>>> = const { RefCell::new(None) };
}

/// Tracks the resources used by a log context.
///
/// The public attribute surface, operators and `repr` are a compatibility
/// contract with the Python callers (Measure, request/background-process
/// metrics, the task scheduler, ...) — change both sides together. Native so the
/// switch machinery can do its rusage accounting without allocating a Python
/// object per operation.
// `skip_from_py_object`: pyo3 0.28 requires `Clone` pyclasses to explicitly
// opt in or out of a generated extract-by-clone `FromPyObject` (a bare
// `#[pyclass]` is a deprecation warning, and we build with `-D warnings`).
// Nothing extracts this type by value, so opt out.
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
        // The single-quoted, `repr()`-style value formatting is the shape
        // this class logs in, and scrapers may match on it — keep it stable.
        // Rust's `{:?}` renders exponent-form floats as e.g. `1e-7` (Python
        // `repr` writes `1e-07`); the only consumer of the string form is
        // Measure's "Failed to save metrics!" warning, so we don't chase exact
        // parity with Python there.
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

extern "C" {
    /// CPython's thread identifier (`pythread.h`) — the exact value
    /// `threading.get_ident()` returns. Part of the stable ABI, but not bound
    /// by pyo3-ffi, so declared here.
    fn PyThread_get_thread_ident() -> std::os::raw::c_ulong;
}

/// This thread's `threading.get_ident()` value, read natively (`get_ident` is
/// a thin wrapper around `PyThread_get_thread_ident`, which is `pthread_self()`
/// on POSIX) rather than by calling into Python.
///
/// Note that `get_ident` is *not* an OS-level tid: on Linux it returns the same
/// value either side of a `fork()` call. Synapse forks in exactly one place, so
/// contexts created before the fork still pass the `main_thread` affinity check
/// after it.
fn get_thread_id() -> u64 {
    // SAFETY: no preconditions; returns an identifier for the calling thread.
    (unsafe { PyThread_get_thread_ident() }) as u64
}

/// Propagate a usage update to the parent context, if there is a (truthy) one.
///
/// A plain base `LoggingContext` parent — the overwhelmingly common case, e.g.
/// every `Measure`-created nested context — is dispatched via `native` directly,
/// skipping the Python method call (attribute lookup, args tuple, call frame)
/// that `add_cputime` would otherwise pay on every switch away from a parented
/// context and `add_database_*` per DB operation. Anything else truthy is a
/// Python subclass, dispatched via `call_method1` so its overrides are
/// respected — the same split as `switch_context`. The truthiness guard matches
/// Python's `if self.parent_context:` and cannot be relaxed to `is_some()`: the
/// sentinel is falsy and implements no `add_cputime`, so that would call a
/// nonexistent method on it.
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
        if let Ok(base) = parent.cast_exact::<LoggingContext>() {
            return native(base);
        }
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
/// CPU accounting). Reading it natively avoids allocating a Python
/// `resource.struct_rusage` object on every switch.
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
/// Guards against the clock going backwards
/// (clamping to zero and logging, as the accounting must never go negative).
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
/// belongs to and accounting CPU/DB usage against it. Contexts are scoped within
/// a `with` block.
///
/// The attribute surface, methods, error-message wording and abuse-detection
/// behaviour are a compatibility contract with Python callers and subclasses
/// (notably `BackgroundProcessLoggingContext`) — change both sides together.
///
/// Construction is split between `__new__` (which allocates a blank
/// instance) and `__init__` (which does the real initialisation), matching how a
/// pure-Python class behaves. This lets Python subclasses — in particular
/// `synapse.metrics.background_process_metrics.BackgroundProcessLoggingContext`,
/// which composes a name and then calls `super().__init__(name=..., ...)` — work.
#[pyclass(subclass)]
pub struct LoggingContext {
    /// Name for the context, used in logging. Stored as a Python string:
    /// `LoggingContextFilter` calls `str(context)` on every log record, and a
    /// `Py<PyString>` getter is INCREF-only where a `String` getter would
    /// allocate a fresh `str` per record. Rust-side reads only happen on cold
    /// error/debug paths (see [`Self::name_string`]).
    #[pyo3(get, set)]
    name: Py<PyString>,
    /// The homeserver name this context is associated with. Stored as a Python
    /// string for the same reason as `name` (read per log record).
    #[pyo3(get, set)]
    server_name: Py<PyString>,
    /// The `threading.get_ident()` value of the thread this context was created
    /// on (see [`get_thread_id`] for why it is not a real OS tid); activity on
    /// any other thread is an error. Settable only so tests can simulate
    /// activity on the wrong thread.
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
    /// A short human-readable tag (e.g. the sync type). Initialised to `""` and
    /// treated as a `str` by everything in-tree, but `Option` so that assigning
    /// `None` (which the sentinel's `tag` reports, and which out-of-tree callers
    /// may assign) is accepted rather than raising `TypeError`.
    #[pyo3(get, set)]
    tag: Option<String>,
    /// The resources used by this context so far; mutated in place. Not
    /// exposed as an attribute: Python reads it via `get_resource_usage()`,
    /// which returns a copy.
    resource_usage: Py<ContextResourceUsage>,
    /// The context that was current when this one was created; restored on exit.
    /// `None` means the sentinel was current (or — only before `__init__` has
    /// run — that nothing has been recorded yet; both restore to the sentinel),
    /// so the getter reports `None` to Python where the old pure-Python
    /// attribute held `SENTINEL_CONTEXT`.
    #[pyo3(get, set)]
    previous_context: Option<Py<LoggingContext>>,
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

        // The thread resource usage when the logcontext became active. None if
        // the context is not currently active.
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

    /// Returns the stored name object itself (INCREF-only): this runs per log
    /// record via `LoggingContextFilter`.
    fn __str__(&self, py: Python<'_>) -> Py<PyString> {
        self.name.clone_ref(py)
    }

    /// Enter this logging context, making it the current context.
    fn __enter__<'py>(slf: Bound<'py, Self>) -> PyResult<Bound<'py, Self>> {
        let py = slf.py();
        // An owned handle rather than a held borrow: `set_current_context`
        // re-enters `slf` (borrow_mut in `start_inner`).
        let previous = slf
            .borrow()
            .previous_context
            .as_ref()
            .map(|p| p.clone_ref(py));

        if log_enabled!(target: DEBUG_LOGGER_NAME, Level::Debug) {
            // The name is only materialised when the opt-in debug logger is
            // actually enabled: this runs on every context entry.
            debug!(
                target: DEBUG_LOGGER_NAME,
                "LoggingContext({}).__enter__",
                slf.borrow().name_string(py)
            );
        }

        let old_context = set_current_context(py, Some(slf.clone().unbind()))?;

        if !slots_identical(&previous, &old_context) {
            let previous_repr = slot_repr(py, &previous)?;
            let old_repr = slot_repr(py, &old_context)?;
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
            // Match the Python `%s`: the str() of the previous context
            // (`"sentinel"` for an empty slot). Computed (along with the name)
            // only when the opt-in debug logger is actually enabled: this runs
            // on every context exit.
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
            // Cold path: the name is only materialised for the error message.
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
    /// This copies the string data, so it is for cold error/debug paths only —
    /// the switch fast path must not allocate.
    fn name_string(&self, py: Python<'_>) -> String {
        self.name.bind(py).to_string_lossy().into_owned()
    }

    /// Native body of the `start` pymethod. Shared with the switch fast path in
    /// [`set_current_context`], which calls this directly for a base
    /// `LoggingContext` rather than dispatching through Python. Runs the same
    /// thread-affinity and abuse checks on both paths.
    ///
    /// This (like [`Self::stop_inner`]) runs on every context switch: the error
    /// branches materialise the name themselves so the fast path stays
    /// allocation-free.
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

    /// Native body of the `stop` pymethod. Shared with the switch fast path.
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

            // `if not rusage: return` — no rusage means this platform doesn't
            // track per-thread CPU, so there is nothing to account.
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

/// Dispatch a `stop`/`start` to a slot value during a switch, respecting
/// subclass overrides.
///
/// The sentinel (an empty slot) is a no-op. A base `LoggingContext` takes the
/// native accounting directly (the hot path — no Python dispatch, no
/// `struct_rusage` allocation). A Python subclass (e.g.
/// `BackgroundProcessLoggingContext`) goes through Python — materialising the
/// rusage as a `(utime, stime)` tuple only here, off the hot path — so its
/// overrides run. Both arms match on the same [`SwitchDirection`], so the
/// native and Python paths provably dispatch the same operation.
fn switch_context(
    py: Python<'_>,
    slot: Option<&Py<LoggingContext>>,
    direction: SwitchDirection,
    rusage: Option<(f64, f64)>,
) -> PyResult<()> {
    let Some(ctx) = slot else {
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

/// Whether two slot values are the same context (or both the sentinel).
/// `LoggingContext` defines no `__eq__`, so identity is the comparison Python
/// callers got too.
fn slots_identical(a: &Option<Py<LoggingContext>>, b: &Option<Py<LoggingContext>>) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(a), Some(b)) => a.is(b),
        _ => false,
    }
}

/// `repr()` of a slot value, for error messages (cold paths only). An empty
/// slot renders as `None`, matching what the `previous_context` getter exposes
/// to Python.
fn slot_repr(py: Python<'_>, slot: &Option<Py<LoggingContext>>) -> PyResult<String> {
    match slot {
        Some(ctx) => Ok(ctx.bind(py).repr()?.extract()?),
        None => Ok("None".to_owned()),
    }
}

/// Set the current logging context, returning the context that was previously
/// current. `None` means the sentinel, in both directions.
///
/// `context` must be a [`LoggingContext`] (or subclass) or `None` — anything
/// else fails extraction with a `TypeError`, so the storage slots stay typed.
/// This is not the Python-facing API: `synapse.logging.context.set_current_context`
/// wraps this with the `SENTINEL_CONTEXT` <-> `None` mapping (and it, not this,
/// rejects `None` from callers — here `None` legitimately means the sentinel).
///
/// Reads the thread rusage once (`getrusage` via libc), `stop`s the old context
/// and `start`s the new one; for the common base-`LoggingContext` case the
/// bookkeeping runs inline, with no per-switch Python dispatch or allocation.
#[pyfunction]
#[pyo3(signature = (context))]
pub fn set_current_context(
    py: Python<'_>,
    context: Option<Py<LoggingContext>>,
) -> PyResult<Option<Py<LoggingContext>>> {
    let current = current_context(py);

    if !slots_identical(&current, &context) {
        let rusage = get_thread_rusage();
        switch_context(py, current.as_ref(), SwitchDirection::Stop, rusage)?;
        // Raw slot write; we already hold `current`, so ignore the previous value
        // it returns. The clone_ref keeps a reference for the `start` below.
        let new_ref = context.as_ref().map(|ctx| ctx.clone_ref(py));
        swap_current_context(context);
        switch_context(py, new_ref.as_ref(), SwitchDirection::Start, rusage)?;
    }

    Ok(current)
}

/// Get the current logging context, or `None` for the sentinel.
///
/// Resolves this OS thread's slot. This is not the Python-facing API:
/// `synapse.logging.context.current_context` wraps this and returns
/// `SENTINEL_CONTEXT` instead of `None`.
#[pyfunction]
pub fn current_context(py: Python<'_>) -> Option<Py<LoggingContext>> {
    THREAD_LOCAL_CONTEXT.with(|slot| slot.borrow().as_ref().map(|ctx| ctx.clone_ref(py)))
}

/// Set this OS thread's current logging context slot, returning the previous
/// slot value (`None` is the sentinel, in both directions).
///
/// This is the raw slot write only — it does **not** do any resource-usage
/// accounting or thread-affinity checks; [`set_current_context`] wraps this with
/// the `getrusage` start/stop bookkeeping.
///
/// Crate-internal: a raw slot write that bypasses the rusage accounting and
/// thread-affinity checks has no Python caller, so it is not exported (Python
/// uses [`set_current_context`]).
fn swap_current_context(context: Option<Py<LoggingContext>>) -> Option<Py<LoggingContext>> {
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
    use pyo3::types::PyString;

    use super::*;

    /// A minimal `LoggingContext` for slot tests, built directly (bypassing
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
            // Nothing set on this (fresh) test thread → empty slot (the sentinel).
            assert!(current_context(py).is_none());
        });
    }

    #[test]
    fn swap_returns_previous_and_updates_thread_local() {
        Python::initialize();
        Python::attach(|py| {
            let a = test_context(py, "A");
            let b = test_context(py, "B");

            // Swapping in A returns the previous slot (empty ⇒ sentinel) and
            // makes A current.
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

            // Restore the empty slot (sentinel) so we don't leak into any other
            // test that happens to reuse this OS thread from the harness pool.
            swap_current_context(None);
        });
    }
}
