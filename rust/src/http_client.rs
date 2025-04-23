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

use std::{collections::HashMap, future::Future, panic::AssertUnwindSafe};

use anyhow::Context;
use futures::{FutureExt, TryStreamExt};
use lazy_static::lazy_static;
use pyo3::{exceptions::PyException, prelude::*, types::PyString};
use reqwest::RequestBuilder;
use tokio::runtime::Runtime;

use crate::errors::HttpResponseException;

lazy_static! {
    static ref RUNTIME: Runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .enable_all()
        .build()
        .unwrap();
    static ref DEFERRED_CLASS: PyObject = {
        Python::with_gil(|py| {
            py.import("twisted.internet.defer")
                .expect("module 'twisted.internet.defer' should be importable")
                .getattr("Deferred")
                .expect("module 'twisted.internet.defer' should have a 'Deferred' class")
                .unbind()
        })
    };
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "http_client")?;
    child_module.add_class::<HttpClient>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import acl` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.http_client", child_module)?;

    Ok(())
}

#[pyclass]
#[derive(Clone)]
struct HttpClient {
    client: reqwest::Client,
}

#[pymethods]
impl HttpClient {
    #[new]
    pub fn py_new() -> HttpClient {
        HttpClient {
            client: reqwest::Client::new(),
        }
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
        create_deferred(py, async move {
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
fn create_deferred<F, O>(py: Python, fut: F) -> PyResult<Bound<'_, PyAny>>
where
    F: Future<Output = PyResult<O>> + Send + 'static,
    for<'a> O: IntoPyObject<'a>,
{
    let deferred = DEFERRED_CLASS.bind(py).call0()?;
    let deferred_callback = deferred.getattr("callback")?.unbind();
    let deferred_errback = deferred.getattr("errback")?.unbind();

    let reactor = py.import("twisted.internet")?.getattr("reactor")?.unbind();

    RUNTIME.spawn(async move {
        // TODO: Is it safe to assert unwind safety here? I think so, as we
        // don't use anything that could be tainted by the panic afterwards.
        // Note that `.spawn(..)` asserts unwind safety on the future too.
        let res = AssertUnwindSafe(fut).catch_unwind().await;

        Python::with_gil(move |py| {
            // Flatten the panic into standard python error
            let res = match res {
                Ok(r) => r,
                Err(panic_err) => {
                    let panic_message = get_panic_message(&panic_err);
                    Err(PyException::new_err(
                        PyString::new(py, panic_message).unbind(),
                    ))
                }
            };

            // Send the result to the deferred, via `.callback(..)` or `.errback(..)`
            match res {
                Ok(obj) => {
                    reactor
                        .call_method(py, "callFromThread", (deferred_callback, obj), None)
                        .expect("callFromThread should not fail"); // There's nothing we can really do with errors here
                }
                Err(err) => {
                    reactor
                        .call_method(py, "callFromThread", (deferred_errback, err), None)
                        .expect("callFromThread should not fail"); // There's nothing we can really do with errors here
                }
            }
        });
    });

    Ok(deferred)
}

/// Try and get the panic message out of the
fn get_panic_message<'a>(panic_err: &'a (dyn std::any::Any + Send + 'static)) -> &'a str {
    // Apparently this is how you extract the panic message from a panic
    if let Some(str_slice) = panic_err.downcast_ref::<&str>() {
        str_slice
    } else if let Some(string) = panic_err.downcast_ref::<String>() {
        string
    } else {
        "unknown error"
    }
}
