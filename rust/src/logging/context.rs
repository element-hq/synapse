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

use once_cell::sync::OnceCell;
use pyo3::prelude::*;

/// The Python sentinel logcontext (`synapse.logging.context.SENTINEL_CONTEXT`).
///
/// Pushed in from Python at import time via [`register_sentinel`] rather than
/// imported here, to avoid a circular import at module-registration time (Rust
/// must not import `synapse.logging.context`; see [`crate::deferred`]).
static SENTINEL: OnceCell<Py<PyAny>> = OnceCell::new();

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

/// Register the Python sentinel logcontext.
///
/// Called once from `synapse.logging.context` at import time. Registering twice
/// is a no-op (the first registration wins); this keeps the identity of the
/// sentinel object we return from [`current_context`] equal to Python's
/// `SENTINEL_CONTEXT` singleton, preserving `context is SENTINEL_CONTEXT` and
/// `bool(context)` semantics.
#[pyfunction]
pub fn register_sentinel(sentinel: Py<PyAny>) {
    let _ = SENTINEL.set(sentinel);
}

/// Get a fresh reference to the sentinel logcontext.
fn sentinel(py: Python<'_>) -> Py<PyAny> {
    SENTINEL
        .get()
        .expect(
            "synapse.logging.context sentinel not registered with the Rust logcontext slot; \
             synapse.logging.context must call register_sentinel() at import",
        )
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
    child_module.add_function(wrap_pyfunction!(current_context, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(swap_current_context, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(register_sentinel, &child_module)?)?;

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

    /// Register a sentinel exactly once (the `OnceCell` keeps the first) and
    /// return whichever object is actually registered, so identity assertions
    /// hold regardless of which test ran first.
    fn registered_sentinel(py: Python<'_>) -> Py<PyAny> {
        register_sentinel(PyString::new(py, "SENTINEL").into_any().unbind());
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
