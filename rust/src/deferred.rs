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

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Does not handle deferred cancellation or contextvars.
pub(crate) fn create_deferred<'py, F, O>(
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

    let rt = runtime(reactor)?;
    let handle = rt.handle()?;
    let task = handle.spawn(fut);

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

/// Runs `make_deferred` on the Twisted reactor thread to obtain a Deferred (or
/// coroutine), then resolves once that Deferred fires.
///
/// This is the inverse of [`create_deferred`]: where that turns a Rust future
/// into a Twisted Deferred, this turns a Twisted Deferred into an awaitable Rust
/// future.
///
/// We're called on a Tokio worker thread, but Twisted `Deferred`s (and the
/// coroutine that `ensureDeferred` drives) are not thread-safe and Synapse's
/// logcontext is thread-local, so the coroutine must both start and resume on
/// the reactor thread. The `callFromThread` hop is what gets us there for the
/// kickoff; the deferred's own callbacks then fire on the reactor thread too.
/// (Note this is unrelated to offloading the DB work onto a thread — that's
/// handled internally by whatever `make_deferred` calls, e.g. `runInteraction`.)
///
/// `make_deferred` is invoked on the reactor thread and may return either a
/// coroutine or a `Deferred`; `ensureDeferred` normalises both to a `Deferred`.
pub(crate) async fn await_deferred<F>(reactor: Py<PyAny>, make_deferred: F) -> PyResult<Py<PyAny>>
where
    F: for<'py> Fn(Python<'py>) -> PyResult<Bound<'py, PyAny>> + Send + 'static,
{
    // Resolves when the deferred fires; carries the resolved value or the error.
    let (tx, rx) = oneshot::channel::<PyResult<Py<PyAny>>>();
    // Shared between the success and error callbacks (only one ever fires).
    let sender = Arc::new(Mutex::new(Some(tx)));

    Python::attach(|py| -> PyResult<()> {
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

        let starter = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args, _kwargs| -> PyResult<Py<PyAny>> {
                let py = args.py();
                let deferred = defer(py)?
                    .call_method1(intern!(py, "ensureDeferred"), (make_deferred(py)?,))?;
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
            "await_deferred channel closed before the deferred fired",
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
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    _ = m;

    // Make sure we fail early if we can't load some modules
    defer(py)?;

    Ok(())
}
