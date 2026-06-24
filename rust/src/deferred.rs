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
    sync::{Arc, Mutex, OnceLock},
};

use once_cell::sync::OnceCell;
use pyo3::{
    create_exception, exceptions::PyException, exceptions::PyRuntimeError, intern, prelude::*,
    types::PyCFunction,
};
use tokio::sync::oneshot;

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

/// Set the Synapse logcontext active on the current (reactor) thread, returning
/// the context that was previously active so it can be restored afterwards.
fn set_current_logging_context<'py>(
    py: Python<'py>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    logging_context_module(py)?.call_method1(intern!(py, "set_current_context"), (context,))
}

tokio::task_local! {
    /// The Synapse `LoggingContext` that was active on the reactor thread when
    /// the Rust future was created (see [`create_deferred`]).
    ///
    /// Synapse attributes per-request CPU/DB usage via a thread-local
    /// `LoggingContext`, but our work runs on a Tokio worker thread that is
    /// detached from it. We stash the originating context here so that whenever
    /// we hop back onto the reactor thread to drive Python work (see
    /// [`run_python_awaitable`]) we can re-activate it; otherwise that work runs in
    /// the sentinel context and its resource usage (e.g. database time) is lost
    /// instead of being charged to the request.
    static LOGGING_CONTEXT: Py<PyAny>;
}

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Captures the Synapse logcontext active on the reactor thread (so work driven
/// by the future is attributed to the originating request, see
/// [`LOGGING_CONTEXT`]), but does not handle deferred cancellation or
/// contextvars.
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

    // Capture the logcontext active on the reactor thread now, while we're still
    // on it, so the spawned task can re-apply it when it hops back to drive
    // Python work. If there's no real context this is the sentinel, and
    // re-applying it later is a no-op.
    let logging_context = logging_context_module(py)?
        .call_method0(intern!(py, "current_context"))?
        .unbind();

    let rt = runtime(reactor)?;
    let handle = rt.handle()?;
    let task = handle.spawn(LOGGING_CONTEXT.scope(logging_context, fut));

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
/// `make_awaitable` is invoked on the reactor thread to produce the awaitable.
/// In Synapse that's typically a coroutine (e.g. from calling an `async def`
/// like `runInteraction`), though a `Deferred` works too — both are awaitable,
/// but a coroutine is not itself a `Deferred`.
///
/// Despite returning a future, the awaitable is kicked off in the background and
/// runs to completion regardless of whether the returned Rust future is ever
/// polled; awaiting it only observes the result.
///
/// We're called on a Tokio worker thread, but Python awaitables and Synapse's
/// logcontext (thread-local) are not thread-safe, so the awaitable must be both
/// started and driven on the reactor thread. The `callFromThread` hop is what
/// gets us there for the kickoff; the resulting deferred's callbacks then fire
/// on the reactor thread too. (Note this is unrelated to offloading the DB work
/// onto a thread — that's handled internally by whatever `make_awaitable`
/// produces, e.g. `runInteraction`.)
///
/// If a logcontext was captured when the task was created (see
/// [`create_deferred`]), it is re-activated while the awaitable is kicked off so
/// that its resource usage (e.g. database time) is attributed to the originating
/// request. We drive it with `run_in_background` so this follows the Synapse
/// logcontext rules (see `docs/log_contexts.md`): the awaitable runs in the
/// request context but the logcontext is reset back to the sentinel once the
/// work completes, so the request context doesn't leak into the reactor.
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

    // The logcontext that is active for this async task
    let current_rust_task_logging_context = LOGGING_CONTEXT
        .try_with(|ctx| Python::attach(|py| ctx.clone_ref(py)))
        .ok();

    Python::attach(|py| -> PyResult<()> {
        // Create some deferred success/error callback functions that we will use to get
        // the result from Python to Rust.
        let success_sender = Arc::clone(&sender);
        let on_success = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let value = args.get_item(0)?.unbind();
                if let Some(tx) = success_sender.lock().unwrap().take() {
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
                if let Some(tx) = error_sender.lock().unwrap().take() {
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

                // Activate the captured Rust task-local logging context. This way we
                // properly record metrics/logging for the thing being run.
                let calling_logging_context = current_rust_task_logging_context
                    .as_ref()
                    .map(|ctx| set_current_logging_context(py, ctx.bind(py)))
                    .transpose()?
                    .expect("No `LoggingContext` returned from `set_current_logging_context(...)`. This is a Synapse programming error.");

                // We fire-and-forget using `run_in_background`. Re-using
                // `run_in_background` also makes sure the awaitable gets run with the
                // current logcontext (the one we just activated) while following the
                // logcontext rules.
                let deferred = logging_context_module(py)?.call_method1(
                    intern!(py, "run_in_background"),
                    (awaitable_factory.bind(py),),
                );

                // Restore the `calling_logging_context` after we kick off the
                // background task.
                //
                // Our goal is to have the caller logcontext unchanged after firing off
                // the background task and returning.
                set_current_logging_context(py, &calling_logging_context)?;

                let deferred = deferred?;
                deferred.call_method1(
                    intern!(py, "addCallbacks"),
                    (on_success.bind(py), on_error.bind(py)),
                )?;
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

/// Convert a Twisted `Failure` (as passed to an errback) into a [`PyErr`].
///
/// A `Failure` carries the original exception instance in its `.value`
/// attribute, which we re-raise so callers see the real error. If that can't be
/// reached, fall back to the `Failure`'s textual representation.
fn failure_to_pyerr(failure: &Bound<'_, PyAny>) -> PyErr {
    match failure.getattr(intern!(failure.py(), "value")) {
        Ok(value) => PyErr::from_value(value),
        Err(_) => PyRuntimeError::new_err(
            failure
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|_| "<unknown failure>".to_owned()),
        ),
    }
}

static MAKE_DEFERRED_YIELDABLE: OnceLock<pyo3::Py<pyo3::PyAny>> = OnceLock::new();

/// Given a deferred, make it follow the Synapse logcontext rules
fn make_deferred_yieldable<'py>(
    py: Python<'py>,
    deferred: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let make_deferred_yieldable = MAKE_DEFERRED_YIELDABLE.get_or_init(|| {
        let sys = PyModule::import(py, "synapse.logging.context").unwrap();
        let func = sys.getattr("make_deferred_yieldable").unwrap().unbind();
        func
    });

    make_deferred_yieldable
        .call1(py, (deferred,))?
        .extract(py)
        .map_err(Into::into)
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, _m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Make sure we fail early if we can't load some modules
    defer(py)?;

    Ok(())
}
