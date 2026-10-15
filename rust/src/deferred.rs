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
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use once_cell::sync::OnceCell;
use pyo3::{
    create_exception,
    exceptions::PyException,
    exceptions::PyRuntimeError,
    intern,
    prelude::*,
    types::{PyCFunction, PyModule, PyModuleMethods},
};
use tokio::sync::oneshot;

use crate::logging::context::with_logcontext;
use crate::runtime::RustRuntime;

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

/// An already-fired Twisted deferred holding `value`.
pub(crate) fn succeed<'py, V: IntoPyObject<'py>>(
    py: Python<'py>,
    value: V,
) -> PyResult<Bound<'py, PyAny>> {
    defer(py)?.call_method1("succeed", (value,))
}

/// What cancelling a deferred from [`create_deferred_with`] does to the
/// tokio task behind it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OnCancel {
    /// The task runs on and its result is discarded. The deferred fails with
    /// `CancelledError` at once, as `stop_cancellation` arranges for a Python
    /// awaitable.
    ///
    /// This is right for work that other callers share, such as a cache
    /// load: one caller giving up must not take the load away from the rest.
    RunOn,
    /// The task is aborted: its future is dropped where it stands, on a tokio
    /// thread, and never polled again. Anything the future holds is released
    /// by its `Drop`, which is where a pooled database connection goes back.
    ///
    /// This is right for work only this caller wants, such as a `/sync`
    /// request's per-room loop once the client has gone.
    Abort,
}

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Does not handle contextvars.
///
/// Cancelling the deferred leaves the task running ([`OnCancel::RunOn`]), so
/// the task can outlive the request's logcontext (`run_python_awaitable`
/// defends against the resulting finished-context case). Use
/// [`create_deferred_with`] and [`OnCancel::Abort`] for work that should stop
/// with its caller.
pub fn create_deferred<'py, F, O>(
    py: Python<'py>,
    runtime: &RustRuntime,
    fut: F,
) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<O>> + Send + 'static,
    for<'a> O: IntoPyObject<'a> + Send + 'static,
{
    create_deferred_with(py, runtime, fut, OnCancel::RunOn)
}

/// As [`create_deferred`], choosing what cancelling the deferred does to the
/// task.
pub fn create_deferred_with<'py, F, O>(
    py: Python<'py>,
    runtime: &RustRuntime,
    fut: F,
    on_cancel: OnCancel,
) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<O>> + Send + 'static,
    for<'a> O: IntoPyObject<'a> + Send + 'static,
{
    // Capture the caller's logcontext here (on the reactor thread) and record
    // it in the spawned task. Log records emitted while the future is polled
    // (on the tokio threads) are then attributed to the context that was
    // current when the caller invoked us. See `crate::logging::context`.
    let logcontext = crate::logging::context::LogContextHandle::capture(py);

    let handle = runtime.tokio_handle()?;
    let task = handle.spawn(logcontext.scope(fut));

    let deferred = match on_cancel {
        OnCancel::RunOn => defer(py)?.call_method0("Deferred")?,
        OnCancel::Abort => {
            // Twisted calls the canceller before it fails the deferred, so
            // the abort is requested before the caller sees `CancelledError`.
            // The task's own completion then finds the deferred already
            // failed, below, and leaves it alone.
            let abort = task.abort_handle();
            let canceller = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    abort.abort();
                    Ok(args.py().None())
                },
            )?;
            defer(py)?.call_method1("Deferred", (canceller,))?
        }
    };
    let deferred_callback = deferred.getattr("callback")?.unbind();
    let deferred_errback = deferred.getattr("errback")?.unbind();

    // Keep the runtime state (and, through it, the reactor) alive while the
    // task is in flight.
    let runtime = runtime.clone();
    handle.spawn(async move {
        let res = task.await;

        // Once done pass the result to the Twisted reactor thread for handling.
        runtime.dispatch_to_twisted(move |py| {
            // Flatten the panic into standard python error
            let res = match res {
                Ok(r) => r,
                Err(join_err) => match join_err.try_into_panic() {
                    Ok(panic_err) => Err(RustPanicError::from_panic(&panic_err)),
                    // The only way the task is cancelled is through the
                    // canceller above, by which point Twisted has already
                    // failed the deferred with `CancelledError`. (If the task
                    // finished first, firing the deferred here is harmless:
                    // Twisted swallows the one late result of a cancelled
                    // deferred.)
                    Err(_) => return,
                },
            };

            // Send the result to the deferred, via `.callback(..)` or `.errback(..)`
            let fired = match res {
                Ok(obj) => deferred_callback.call1(py, (obj,)),
                Err(err) => deferred_errback.call1(py, (err,)),
            };

            if let Err(err) = fired {
                // There is nowhere to propagate this to. The closure runs from
                // the dispatch reader's `doRead`, and an exception out of that
                // makes Twisted drop the reader. Log it instead.
                log::error!("Failed to fire a deferred from a Rust future: {err}");
            }
        })
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
    runtime: &RustRuntime,
    make_awaitable: F,
) -> PyResult<Py<PyAny>>
where
    F: for<'py> Fn(Python<'py>) -> PyResult<Bound<'py, PyAny>> + Send + Sync + 'static,
{
    run_python_awaitable_observed(
        runtime,
        make_awaitable,
        |value: &Bound<'_, PyAny>| Ok(value.clone().unbind()),
        None,
    )
    .await
}

/// Told, on the reactor thread, when the awaitable of
/// [`run_python_awaitable_observed`] starts and when it finishes.
pub trait AwaitableObserver: Send + Sync + 'static {
    /// The awaitable has been made and is running on the reactor.
    fn started(&self);
    /// The awaitable has a result, which the caller has yet to see. Also
    /// called if it failed to start, without `started`.
    fn finished(&self);
}

/// As [`run_python_awaitable`], with two additions.
///
/// - `convert` turns the result into a Rust value on the reactor thread, in
///   the GIL hold that receives it. The caller then needs no GIL hop of its
///   own to read the result.
/// - `observer` hears when the awaitable starts and finishes. A test can then
///   tell a task waiting on the reactor from one with work still to do.
pub(crate) async fn run_python_awaitable_observed<F, C, T>(
    runtime: &RustRuntime,
    make_awaitable: F,
    convert: C,
    observer: Option<Arc<dyn AwaitableObserver>>,
) -> PyResult<T>
where
    F: for<'py> Fn(Python<'py>) -> PyResult<Bound<'py, PyAny>> + Send + Sync + 'static,
    C: for<'py> Fn(&Bound<'py, PyAny>) -> PyResult<T> + Send + Sync + 'static,
    T: Send + 'static,
{
    // Resolves when the awaitable completes; carries the resolved value or error.
    let (tx, rx) = oneshot::channel::<PyResult<T>>();
    // Shared between the success and error callbacks (only one ever fires).
    // The observer hears of the result before the channel carries it.
    let sender = Arc::new(Mutex::new(Some((tx, observer))));

    // Capture the logcontext of the calling tokio task (if any). We restore it
    // on the reactor thread before driving the awaitable, so Python code
    // invoked from Rust (e.g. `DatabasePool.runInteraction`) runs in the same
    // logcontext that was current when Python originally called into Rust. Its
    // logging and DB-metrics accounting are then attributed to the right
    // request. If we are not inside a scoped task, this is `None` and the
    // awaitable runs in the sentinel.
    let logcontext = crate::logging::context::LogContextHandle::task_current();

    runtime.dispatch_to_twisted(move |py| {
        let started = (|| -> PyResult<()> {
            // Create some deferred success/error callback functions that we will use to get
            // the result from Python to Rust.
            let success_sender = Arc::clone(&sender);
            let on_success = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    let value = convert(&args.get_item(0)?);
                    if let Some((tx, observer)) = success_sender
                        .lock()
                        .map_err(|err| {
                            anyhow::anyhow!("Failed to acquire lock on `success_sender`: {:#}", err)
                        })?
                        .take()
                    {
                        if let Some(observer) = observer {
                            observer.finished();
                        }
                        let _ = tx.send(value);
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
                    if let Some((tx, observer)) = error_sender
                        .lock()
                        .map_err(|err| {
                            anyhow::anyhow!("Failed to acquire lock on `error_sender`: {:#}", err)
                        })?
                        .take()
                    {
                        if let Some(observer) = observer {
                            observer.finished();
                        }
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

            // Drive the awaitable in the captured logcontext. Restored here
            // as we're on the reactor thread (the only thread where the
            // context's `main_thread` check passes).
            let context = match &logcontext {
                Some(handle) => handle.logging_context().map(|ctx| ctx.clone_ref(py)),
                // Called from outside any scoped task: the sentinel.
                None => None,
            };

            // Kick off the awaitable, fire-and-forget, via
            // `run_in_background`. It calls the factory in the current
            // logcontext and follows the logcontext rules from there. In
            // particular, it arranges for the reactor to be back at the
            // sentinel when the awaitable later completes.
            let started_observer = sender
                .lock()
                .ok()
                .and_then(|slot| slot.as_ref().and_then(|(_, observer)| observer.clone()));
            if let Some(observer) = started_observer {
                observer.started();
            }
            with_logcontext(py, context, || {
                let deferred = run_in_background(py, awaitable_factory.bind(py))?;
                deferred.call_method1(
                    intern!(py, "addCallbacks"),
                    (on_success.bind(py), on_error.bind(py)),
                )?;
                Ok(())
            })
        })();

        if let Err(err) = started {
            // We failed before the awaitable's callbacks were attached, so
            // nothing else will ever resolve the channel. Do it ourselves.
            if let Some((tx, observer)) = sender.lock().ok().and_then(|mut slot| slot.take()) {
                if let Some(observer) = observer {
                    observer.finished();
                }
                let _ = tx.send(Err(err));
            }
        }
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

/// Call `synapse.logging.context.run_in_background(f)`, which calls `f` in the
/// current logcontext and drives the awaitable it returns to completion,
/// following the logcontext rules. Returns the resulting `Deferred`.
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

/// How many [`sleep_for_test`] futures have been dropped, finished or not.
static DROPPED_SLEEPS: AtomicU64 = AtomicU64::new(0);

/// Counts its drop in [`DROPPED_SLEEPS`], so a test can see a future being
/// dropped on abort.
struct CountDrop;

impl Drop for CountDrop {
    fn drop(&mut self) {
        DROPPED_SLEEPS.fetch_add(1, Ordering::SeqCst);
    }
}

/// A deferred that fires with `"slept"` after `seconds`, with the task
/// aborted on cancellation if `abort_on_cancel`. For tests of
/// [`create_deferred_with`].
#[pyfunction]
fn sleep_for_test<'py>(
    py: Python<'py>,
    runtime: &RustRuntime,
    seconds: f64,
    abort_on_cancel: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let on_cancel = if abort_on_cancel {
        OnCancel::Abort
    } else {
        OnCancel::RunOn
    };
    let guard = CountDrop;
    create_deferred_with(
        py,
        runtime,
        async move {
            tokio::time::sleep(Duration::from_secs_f64(seconds)).await;
            drop(guard);
            Ok("slept")
        },
        on_cancel,
    )
}

/// How many [`sleep_for_test`] futures have been dropped so far, whether
/// they ran to the end or were aborted.
#[pyfunction]
fn dropped_sleeps_for_test() -> u64 {
    DROPPED_SLEEPS.load(Ordering::SeqCst)
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Make sure we fail early if we can't load some modules
    defer(py)?;
    // We can't check this here because of circular import issues
    // logging_context_module(py)?;

    let child_module = PyModule::new(py, "deferred")?;
    child_module.add_function(wrap_pyfunction!(sleep_for_test, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(dropped_sleeps_for_test, &child_module)?)?;
    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import deferred` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.deferred", child_module)?;

    Ok(())
}
