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

use std::collections::HashMap;

use anyhow::Context;
use http_body_util::BodyExt;
use pyo3::prelude::*;
use reqwest::RequestBuilder;

use crate::deferred::create_deferred;
use crate::errors::HttpResponseException;
use crate::tokio_runtime::runtime;

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "http_client")?;
    child_module.add_class::<HttpClient>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import http_client` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.http_client", child_module)?;

    Ok(())
}

#[pyclass]
struct RequestOptions {
    response_limit: Option<usize>,
    headers: Option<HashMap<String, String>>,
    request_body: Option<String>,
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
        request_options: RequestOptions,
    ) -> PyResult<Bound<'a, PyAny>> {
        self.send_request(py, reqwest::Method::GET, url, request_options)
    }

    pub fn post<'a>(
        &self,
        py: Python<'a>,
        url: String,
        request_options: RequestOptions,
    ) -> PyResult<Bound<'a, PyAny>> {
        self.send_request(py, reqwest::Method::POST, url, request_options)
    }
}

impl HttpClient {
    fn send_request<'a>(
        &self,
        py: Python<'a>,
        method: reqwest::Method,
        url: String,
        request_options: RequestOptions,
    ) -> PyResult<Bound<'a, PyAny>> {
        // Create the request
        let builder = {
            let mut builder = self.client.request(method, url);
            if let Some(headers) = request_options.headers {
                for (name, value) in headers {
                    builder = builder.header(name, value);
                }
            }
            if let Some(request_body) = request_options.request_body {
                builder = builder.body(request_body);
            }

            builder
        };

        // Fire-off the request
        create_deferred(py, self.reactor.bind(py), async move {
            let response = builder.send().await.context("sending request")?;

            let status = response.status();

            // A light-weight way to read the response up until the `response_limit`. We
            // want to avoid allocating a giant response object on the server above our
            // expected `response_limit` to avoid out-of-memory DOS problems.
            let bytes = if let Some(response_limit) = request_options.response_limit {
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
                bytes
            } else {
                response.bytes().await.map_err(anyhow::Error::from)?
            };

            if !status.is_success() {
                return Err(HttpResponseException::new(status, bytes));
            }

            // Because of the `pyo3` `bytes` feature, we can pass this back to Python
            // land efficiently
            Ok(bytes)
        })
    }
}
