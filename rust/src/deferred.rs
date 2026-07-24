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

use std::{
    future::Future,
    sync::{Arc, Mutex},
};

use log::{debug, log_enabled, Level};
use once_cell::sync::OnceCell;
use pyo3::{
    create_exception, exceptions::PyException, exceptions::PyRuntimeError, intern, prelude::*,
    types::PyCFunction,
};
use tokio::sync::oneshot;

use crate::logging::context::{with_logcontext, DEBUG_LOGGER_NAME};
use crate::tokio_runtime::runtime;

create_exception!(
    synapse.synapse_rust.http_client,
    RustPanicError,
    PyException,
    "A panic which happened in a Rust future"
);

impl RustPanicError {
    fn from_panic(panic_err: &(dyn std::any::Any + Send + 'static)) -> PyErr {
        // Apparently this is how you extract the panic message from a panic
        let panic_message = if let Some(str_slice) = panic_err.downcast_ref::<&str>() {
            str_slice
        } else if let Some(string) = panic_err.downcast_ref::<String>() {
            string
        } else {
            "unknown error"
        };
        Self::new_err(panic_message.to_owned())
    }
}

/// A reference to the `twisted.internet.defer` module.
static DEFER: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `twisted.internet.defer` module.
fn defer(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(DEFER
        .get_or_try_init(|| py.import("twisted.internet.defer").map(Into::into))?
        .bind(py))
}

/// A reference to the `synapse.logging.context` module.
static LOGGING_CONTEXT_MODULE: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `synapse.logging.context` module.
fn logging_context_module(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(LOGGING_CONTEXT_MODULE
        .get_or_try_init(|| py.import("synapse.logging.context").map(Into::into))?
        .bind(py))
}

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Does not handle contextvars.
///
/// TODO: propagate deferred cancellation to the tokio task (via
/// `JoinHandle::abort`). Until then a cancelled request leaves its task
/// running, so the task can outlive the request's logcontext —
/// `run_python_awaitable` defends against the resulting finished-context case,
/// but the work itself is wasted.
pub fn create_deferred<'py, F, O>(
    py: Python<'py>,
    reactor: &Bound<'py, PyAny>,
    fut: F,
) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<O>> + Send + 'static,
    for<'a> O: IntoPyObject<'a> + Send + 'static,
{
    let deferred = defer(py)?.call_method0("Deferred")?;
    let deferred_callback = deferred.getattr("callback")?.unbind();
    let deferred_errback = deferred.getattr("errback")?.unbind();

    // Capture the caller's logcontext at the boundary (GIL held, on the reactor
    // thread) and scope it onto the spawned task, so that logging emitted while
    // the future is polled — and any `run_python_awaitable` callbacks back into
    // Python — are attributed to the context that was current when the caller
    // invoked us. See `crate::logging::context`.
    let logcontext = crate::logging::context::LogContextHandle::capture(py);

    let rt = runtime(reactor)?;
    let handle = rt.handle()?;
    let task = handle.spawn(logcontext.scope(fut));

    // Unbind the reactor so that we can pass it to the task
    let reactor = reactor.clone().unbind();
    handle.spawn(async move {
        let res = task.await;

        Python::attach(move |py| {
            // Flatten the panic into standard python error
            let res = match res {
                Ok(r) => r,
                Err(join_err) => match join_err.try_into_panic() {
                    Ok(panic_err) => Err(RustPanicError::from_panic(&panic_err)),
                    Err(err) => Err(PyException::new_err(format!("Task cancelled: {err}"))),
                },
            };

            // Re-bind the reactor
            let reactor = reactor.bind(py);

            // Send the result to the deferred, via `.callback(..)` or `.errback(..)`
            match res {
                Ok(obj) => {
                    reactor
                        .call_method("callFromThread", (deferred_callback, obj), None)
                        .expect("callFromThread should not fail"); // There's nothing we can really do with errors here
                }
                Err(err) => {
                    reactor
                        .call_method("callFromThread", (deferred_errback, err), None)
                        .expect("callFromThread should not fail"); // There's nothing we can really do with errors here
                }
            }
        });
    });

    // Make the deferred follow the Synapse logcontext rules
    make_deferred_yieldable(py, &deferred)
}

/// Runs a Python awaitable to completion on the Twisted reactor and resolves
/// with its result.
///
/// This is the inverse of [`create_deferred`]: where that turns a Rust future
/// into a Twisted `Deferred`, this turns a Python awaitable into a Rust future.
///
/// Despite returning a future, the awaitable is kicked off in the background running in
/// the Twisted reactor and runs to completion regardless of whether the returned Rust
/// future is ever polled; awaiting it only observes the result.
pub(crate) async fn run_python_awaitable<F>(
    reactor: Py<PyAny>,
    make_awaitable: F,
) -> PyResult<Py<PyAny>>
where
    F: for<'py> Fn(Python<'py>) -> PyResult<Bound<'py, PyAny>> + Send + 'static,
{
    // Resolves when the awaitable completes; carries the resolved value or error.
    let (tx, rx) = oneshot::channel::<PyResult<Py<PyAny>>>();
    // Shared between the success and error callbacks (only one ever fires).
    let sender = Arc::new(Mutex::new(Some(tx)));

    // Capture the logcontext of the calling tokio task (if any). We restore it on
    // the reactor thread before driving the awaitable, so Python code invoked from
    // Rust (e.g. `DatabasePool.runInteraction`) runs in the same logcontext that was
    // current when Python originally called into Rust — its logging and DB-metrics
    // accounting are then attributed to the right request. `None` (called outside a
    // scoped task) falls back to the sentinel.
    let logcontext = crate::logging::context::LogContextHandle::current();

    Python::attach(move |py| -> PyResult<()> {
        // Create some deferred success/error callback functions that we will use to get
        // the result from Python to Rust.
        let success_sender = Arc::clone(&sender);
        let on_success = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let value = args.get_item(0)?.unbind();
                if let Some(tx) = success_sender
                    .lock()
                    .map_err(|err| {
                        anyhow::anyhow!("Failed to acquire lock on `success_sender`: {:#}", err)
                    })?
                    .take()
                {
                    let _ = tx.send(Ok(value));
                }
                Ok(args.py().None())
            },
        )?
        .unbind();

        let error_sender = Arc::clone(&sender);
        let on_error = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let err = failure_to_pyerr(&args.get_item(0)?);
                if let Some(tx) = error_sender
                    .lock()
                    .map_err(|err| {
                        anyhow::anyhow!("Failed to acquire lock on `error_sender`: {:#}", err)
                    })?
                    .take()
                {
                    let _ = tx.send(Err(err));
                }
                Ok(args.py().None())
            },
        )?
        .unbind();

        // Wrap `make_awaitable` as a Python callable so we can hand it to
        // `run_in_background`, which calls it (in the active logcontext) to produce
        // the awaitable it then drives.
        let awaitable_factory = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let py = args.py();
                Ok(make_awaitable(py)?.unbind())
            },
        )?
        .unbind();

        // Create a function that we will run with the Twisted reactor that will drive
        // the Python awaitable.
        let starter = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let py = args.py();

                // Choose the logcontext to drive the awaitable in: the captured
                // one, restored on the reactor thread — the one thread where the
                // context's `main_thread` affinity check passes.
                //
                // Never re-start a context that has already finished: the request may
                // have completed (or been cancelled — `create_deferred` does not
                // propagate cancellation) while this task was still running. Restoring
                // it would trip the "Re-starting finished log context" abuse check and
                // account our work against a context whose metrics are already
                // finalised, so such work runs in the sentinel instead. Both
                // `__exit__` (which sets `finished`) and this check run on the
                // reactor thread, so the check cannot race.
                let context = match &logcontext {
                    Some(handle) => {
                        let finished = handle
                            .logging_context()
                            .is_some_and(|ctx| ctx.borrow(py).is_finished());
                        if finished {
                            if log_enabled!(target: DEBUG_LOGGER_NAME, Level::Debug) {
                                // Only a real context can be finished, so
                                // `logging_context()` is `Some` here.
                                if let Some(ctx) = handle.logging_context() {
                                    debug!(
                                        target: DEBUG_LOGGER_NAME,
                                        "run_python_awaitable: captured logcontext {} has \
                                         finished; running in the sentinel",
                                        ctx.bind(py).str()?
                                    );
                                }
                            }
                            None
                        } else {
                            handle.logging_context().map(|ctx| ctx.clone_ref(py))
                        }
                    }
                    // Called from outside any scoped task: the sentinel. (The
                    // reactor thread is normally at the sentinel already, in which
                    // case the switch below is a no-op.)
                    None => None,
                };

                // Kick off the awaitable, fire-and-forget, via `run_in_background`:
                // it calls the factory in the current logcontext and follows the
                // logcontext rules from there — in particular, it arranges for the
                // reactor to be back at the sentinel when the awaitable later
                // completes.
                with_logcontext(py, context, || {
                    let deferred = run_in_background(py, awaitable_factory.bind(py))?;
                    deferred.call_method1(
                        intern!(py, "addCallbacks"),
                        (on_success.bind(py), on_error.bind(py)),
                    )?;
                    Ok(())
                })?;

                Ok(py.None())
            },
        )?;

        reactor
            .bind(py)
            .call_method1(intern!(py, "callFromThread"), (starter,))?;

        Ok(())
    })?;

    match rx.await {
        Ok(result) => result,
        Err(_) => Err(PyRuntimeError::new_err(
            "run_python_awaitable channel closed before the awaitable completed",
        )),
    }
}

/// Convert a Twisted `Failure` (as passed to an Deferred errback) into a [`PyErr`].
///
/// A Twisted `Failure` carries the original exception instance in its `.value`
/// attribute, which we re-raise so callers see the real error. If the `Failure` is
/// mangled, we fallback to raising a generic [`PyRuntimeError`] explaining what we saw
/// instead.
fn failure_to_pyerr(failure: &Bound<'_, PyAny>) -> PyErr {
    match failure.getattr(intern!(failure.py(), "value")) {
        Ok(value) => PyErr::from_value(value),
        Err(_) => PyRuntimeError::new_err(format!(
            "Expected Python object passed here to be a Twisted `Failure` with a `value` attribute \
            but saw something else: {}",
            failure
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|_| "<failed to stringify Python object>".to_owned()),
        )),
    }
}

/// A reference to `synapse.logging.context.run_in_background`.
static RUN_IN_BACKGROUND: OnceCell<Py<PyAny>> = OnceCell::new();

/// Call `synapse.logging.context.run_in_background(f)`: call `f` in the current
/// logcontext and drive the awaitable it returns to completion, following the
/// logcontext rules. Returns the resulting `Deferred`.
fn run_in_background<'py>(py: Python<'py>, f: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let run_in_background = RUN_IN_BACKGROUND.get_or_try_init(|| {
        logging_context_module(py)?
            .getattr("run_in_background")
            .map(Into::into)
    })?;

    run_in_background
        .call1(py, (f,))?
        .extract(py)
        .map_err(Into::into)
}

/// A reference to `synapse.logging.context.make_deferred_yieldable`.
static MAKE_DEFERRED_YIELDABLE: OnceCell<Py<PyAny>> = OnceCell::new();

/// Given a deferred, make it follow the Synapse logcontext rules
fn make_deferred_yieldable<'py>(
    py: Python<'py>,
    deferred: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let make_deferred_yieldable = MAKE_DEFERRED_YIELDABLE.get_or_try_init(|| {
        logging_context_module(py)?
            .getattr("make_deferred_yieldable")
            .map(Into::into)
    })?;

    make_deferred_yieldable
        .call1(py, (deferred,))?
        .extract(py)
        .map_err(Into::into)
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, _m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Make sure we fail early if we can't load some modules
    defer(py)?;
    // We can't check this here because of circular import issues
    // logging_context_module(py)?;

    Ok(())
}
