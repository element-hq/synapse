/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2025 New Vector, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

use std::{collections::HashMap, future::Future, sync::OnceLock};

use anyhow::Context;
use http_body_util::BodyExt;
use once_cell::sync::OnceCell;
use pyo3::{create_exception, exceptions::PyException, prelude::*};
use reqwest::RequestBuilder;
use tokio::runtime::Runtime;

use crate::errors::HttpResponseException;

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

/// This is the name of the attribute where we store the runtime on the reactor
static TOKIO_RUNTIME_ATTR: &str = "__synapse_rust_tokio_runtime";

/// A Python wrapper around a Tokio runtime.
///
/// This allows us to 'store' the runtime on the reactor instance, starting it
/// when the reactor starts, and stopping it when the reactor shuts down.
#[pyclass]
struct PyTokioRuntime {
    runtime: Option<Runtime>,
}

#[pymethods]
impl PyTokioRuntime {
    fn start(&mut self) -> PyResult<()> {
        // TODO: allow customization of the runtime like the number of threads
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()?;

        self.runtime = Some(runtime);

        Ok(())
    }

    fn shutdown(&mut self) -> PyResult<()> {
        let runtime = self
            .runtime
            .take()
            .context("Runtime was already shutdown")?;

        // Dropping the runtime will shut it down
        drop(runtime);

        Ok(())
    }
}

impl PyTokioRuntime {
    /// Get the handle to the Tokio runtime, if it is running.
    fn handle(&self) -> PyResult<&tokio::runtime::Handle> {
        let handle = self
            .runtime
            .as_ref()
            .context("Tokio runtime is not running")?
            .handle();

        Ok(handle)
    }
}

/// Get a handle to the Tokio runtime stored on the reactor instance, or create
/// a new one.
fn runtime<'a>(reactor: &Bound<'a, PyAny>) -> PyResult<PyRef<'a, PyTokioRuntime>> {
    if !reactor.hasattr(TOKIO_RUNTIME_ATTR)? {
        install_runtime(reactor)?;
    }

    get_runtime(reactor)
}

/// Install a new Tokio runtime on the reactor instance.
fn install_runtime(reactor: &Bound<PyAny>) -> PyResult<()> {
    let py = reactor.py();
    let runtime = PyTokioRuntime { runtime: None };
    let runtime = runtime.into_pyobject(py)?;

    // Attach the runtime to the reactor, starting it when the reactor is
    // running, stopping it when the reactor is shutting down
    reactor.call_method1("callWhenRunning", (runtime.getattr("start")?,))?;
    reactor.call_method1(
        "addSystemEventTrigger",
        ("after", "shutdown", runtime.getattr("shutdown")?),
    )?;
    reactor.setattr(TOKIO_RUNTIME_ATTR, runtime)?;

    Ok(())
}

/// Get a reference to a Tokio runtime handle stored on the reactor instance.
fn get_runtime<'a>(reactor: &Bound<'a, PyAny>) -> PyResult<PyRef<'a, PyTokioRuntime>> {
    // This will raise if `TOKIO_RUNTIME_ATTR` is not set or if it is
    // not a `Runtime`. Careful that this could happen if the user sets it
    // manually, or if multiple versions of `pyo3-twisted` are used!
    let runtime: Bound<PyTokioRuntime> = reactor.getattr(TOKIO_RUNTIME_ATTR)?.extract()?;
    Ok(runtime.borrow())
}

/// A reference to the `twisted.internet.defer` module.
static DEFER: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `twisted.internet.defer` module.
fn defer(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(DEFER
        .get_or_try_init(|| py.import("twisted.internet.defer").map(Into::into))?
        .bind(py))
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "http_client")?;
    child_module.add_class::<HttpClient>()?;

    // Make sure we fail early if we can't load some modules
    defer(py)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import http_client` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.http_client", child_module)?;

    Ok(())
}

#[pyclass]
struct HttpClient {
    client: reqwest::Client,
    reactor: Py<PyAny>,
}

#[pymethods]
impl HttpClient {
    #[new]
    #[pyo3(signature = (reactor, user_agent, http2_only = false))]
    pub fn py_new(
        reactor: Bound<PyAny>,
        user_agent: &str,
        http2_only: bool,
    ) -> PyResult<HttpClient> {
        // Make sure the runtime gets installed
        let _ = runtime(&reactor)?;

        let mut builder = reqwest::Client::builder().user_agent(user_agent);

        if http2_only {
            // Create the client with 'HTTP/2 prior knowledge' enabled, which
            // means it will always use HTTP/2 for unencrypted connections
            builder = builder.http2_prior_knowledge();
        }

        let client = builder.build().context("building reqwest client")?;

        Ok(HttpClient {
            client,
            reactor: reactor.unbind(),
        })
    }

    pub fn get<'a>(
        &self,
        py: Python<'a>,
        url: String,
        response_limit: usize,
    ) -> PyResult<Bound<'a, PyAny>> {
        self.send_request(py, self.client.get(url), response_limit)
    }

    pub fn post<'a>(
        &self,
        py: Python<'a>,
        url: String,
        response_limit: usize,
        headers: HashMap<String, String>,
        request_body: String,
    ) -> PyResult<Bound<'a, PyAny>> {
        let mut builder = self.client.post(url);
        for (name, value) in headers {
            builder = builder.header(name, value);
        }
        builder = builder.body(request_body);

        self.send_request(py, builder, response_limit)
    }
}

impl HttpClient {
    fn send_request<'a>(
        &self,
        py: Python<'a>,
        builder: RequestBuilder,
        response_limit: usize,
    ) -> PyResult<Bound<'a, PyAny>> {
        create_deferred(py, self.reactor.bind(py), async move {
            let response = builder.send().await.context("sending request")?;

            let status = response.status();

            // A light-weight way to read the response up until the `response_limit`. We
            // want to avoid allocating a giant response object on the server above our
            // expected `response_limit` to avoid out-of-memory DOS problems.
            let body = reqwest::Body::from(response);
            let limited_body = http_body_util::Limited::new(body, response_limit);
            let collected = limited_body
                .collect()
                .await
                .map_err(anyhow::Error::from_boxed)
                .with_context(|| {
                    format!(
                        "Response body exceeded response limit ({} bytes)",
                        response_limit
                    )
                })?;
            let bytes: bytes::Bytes = collected.to_bytes();

            if !status.is_success() {
                return Err(HttpResponseException::new(status, bytes));
            }

            // Because of the `pyo3` `bytes` feature, we can pass this back to Python
            // land efficiently
            Ok(bytes)
        })
    }
}

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Does not handle deferred cancellation or contextvars.
fn create_deferred<'py, F, O>(
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
