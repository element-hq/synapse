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

use std::{collections::HashMap, future::Future};

use anyhow::Context;
use futures::TryStreamExt;
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

/// The tokio runtime that we're using to run async Rust libs.
static RUNTIME: OnceCell<Runtime> = OnceCell::new();

/// A reference to the `twisted.internet.defer` module.
static DEFER: OnceCell<PyObject> = OnceCell::new();

/// Access the tokio runtime.
fn runtime() -> PyResult<&'static Runtime> {
    RUNTIME.get_or_try_init(|| {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .context("building tokio runtime")?;

        Ok(runtime)
    })
}

/// Access to the `twisted.internet.defer` module.
fn defer(py: Python<'_>) -> PyResult<&Bound<PyAny>> {
    Ok(DEFER
        .get_or_try_init(|| py.import("twisted.internet.defer").map(Into::into))?
        .bind(py))
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "http_client")?;
    child_module.add_class::<HttpClient>()?;

    // Make sure we fail early if we can't build the lazy statics.
    runtime()?;
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
    reactor: PyObject,
}

#[pymethods]
impl HttpClient {
    #[new]
    pub fn py_new(reactor: PyObject, user_agent: &str) -> PyResult<HttpClient> {
        Ok(HttpClient {
            client: reqwest::Client::builder()
                .user_agent(user_agent)
                .build()
                .context("building reqwest client")?,
            reactor,
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
        create_deferred(py, self.reactor.clone_ref(py), async move {
            let response = builder.send().await.context("sending request")?;

            let status = response.status();

            let mut stream = response.bytes_stream();
            let mut buffer = Vec::new();
            while let Some(chunk) = stream.try_next().await.context("reading body")? {
                if buffer.len() + chunk.len() > response_limit {
                    Err(anyhow::anyhow!("Response size too large"))?;
                }

                buffer.extend_from_slice(&chunk);
            }

            if !status.is_success() {
                return Err(HttpResponseException::new(status, buffer));
            }

            let r = Python::with_gil(|py| buffer.into_pyobject(py).map(|o| o.unbind()))?;

            Ok(r)
        })
    }
}

/// Creates a twisted deferred from the given future, spawning the task on the
/// tokio runtime.
///
/// Does not handle deferred cancellation or contextvars.
fn create_deferred<F, O>(py: Python, reactor: PyObject, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = PyResult<O>> + Send + 'static,
    for<'a> O: IntoPyObject<'a> + Send + 'static,
{
    let deferred = defer(py)?.call_method0("Deferred")?;
    let deferred_callback = deferred.getattr("callback")?.unbind();
    let deferred_errback = deferred.getattr("errback")?.unbind();

    let rt = runtime()?;
    let task = rt.spawn(fut);

    rt.spawn(async move {
        let res = task.await;

        Python::with_gil(move |py| {
            // Flatten the panic into standard python error
            let res = match res {
                Ok(r) => r,
                Err(join_err) => match join_err.try_into_panic() {
                    Ok(panic_err) => Err(RustPanicError::from_panic(&panic_err)),
                    Err(err) => Err(PyException::new_err(format!("Task cancelled: {err}"))),
                },
            };

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

    Ok(deferred)
}
