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
use tracing::Instrument;

use crate::deferred::create_deferred;
use crate::errors::HttpResponseException;
use crate::logging::opentracing::record_error;
use crate::runtime::RustRuntime;

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
struct HttpClient {
    client: reqwest::Client,
    runtime: RustRuntime,
}

#[pymethods]
impl HttpClient {
    #[new]
    #[pyo3(signature = (runtime, user_agent, http2_only = false))]
    pub fn py_new(
        runtime: &RustRuntime,
        user_agent: &str,
        http2_only: bool,
    ) -> PyResult<HttpClient> {
        let mut builder = reqwest::Client::builder().user_agent(user_agent);

        if http2_only {
            // Create the client with 'HTTP/2 prior knowledge' enabled, which
            // means it will always use HTTP/2 for unencrypted connections
            builder = builder.http2_prior_knowledge();
        }

        let client = builder.build().context("building reqwest client")?;

        Ok(HttpClient {
            client,
            runtime: runtime.clone(),
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
        let client = self.client.clone();
        create_deferred(py, &self.runtime, async move {
            let request = builder.build().context("building request")?;
            traced_request(client, request, response_limit).await
        })
    }
}

/// Send `request` in Python's `SimpleHttpClient` span, created in the
/// task so that the caller's Python span is its parent.
async fn traced_request(
    client: reqwest::Client,
    request: reqwest::Request,
    response_limit: usize,
) -> PyResult<bytes::Bytes> {
    // Without credentials or the query, which may carry secrets.
    let mut url = request.url().clone();
    let _ = url.set_password(None);
    let _ = url.set_username("");
    url.set_query(None);
    let span = tracing::info_span!(
        "outgoing-client-request",
        span.kind = "client",
        http.method = request.method().as_str(),
        http.url = url.as_str(),
        http.status_code = tracing::field::Empty,
        error = tracing::field::Empty,
    );
    let result = read_response(client, request, response_limit)
        .instrument(span.clone())
        .await;
    match result {
        Ok(bytes) => Ok(bytes),
        // As Python's span, an error status is not a failure of the
        // request: the caller decides.
        Err(ReadError::Status(status, bytes)) => Err(HttpResponseException::new(status, bytes)),
        Err(ReadError::Other(err)) => {
            // Before the conversion to a Python error, which needs the GIL.
            record_error(&span, &format!("{err:#}"));
            Err(err.into())
        }
    }
}

/// Why [`read_response`] failed.
enum ReadError {
    /// The server answered with an error status.
    Status(reqwest::StatusCode, bytes::Bytes),
    Other(anyhow::Error),
}

impl From<anyhow::Error> for ReadError {
    fn from(err: anyhow::Error) -> Self {
        ReadError::Other(err)
    }
}

/// Send `request` and read its response, up to `response_limit` bytes.
async fn read_response(
    client: reqwest::Client,
    request: reqwest::Request,
    response_limit: usize,
) -> Result<bytes::Bytes, ReadError> {
    let response = client.execute(request).await.context("sending request")?;

    let status = response.status();
    tracing::Span::current().record("http.status_code", status.as_u16());

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
        return Err(ReadError::Status(status, bytes));
    }

    // Because of the `pyo3` `bytes` feature, we can pass this back to Python
    // land efficiently
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    use super::*;
    use crate::logging::opentracing::layer::testing::{tag, traced_until_dropped};
    use crate::logging::opentracing::thrift::TagValue;
    use crate::logging::opentracing::SamplerConfig;

    /// The request is Python's `outgoing-client-request` span, with its
    /// status, and a URL without credentials or query.
    #[tokio::test]
    async fn traced_request_span() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buf = [0; 4096];
            let _ = socket.read(&mut buf).await.unwrap();
            socket
                .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
                .await
                .unwrap();
        });

        let (guard, sink) = traced_until_dropped(SamplerConfig::Const(true));
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let request = client
            .get(format!(
                "http://user:secret@127.0.0.1:{port}/introspect?token=abc"
            ))
            .build()
            .unwrap();
        let body = traced_request(client, request, 1024).await.unwrap();
        drop(guard);
        server.await.unwrap();

        assert_eq!(&body[..], b"ok");
        let spans = sink.take();
        let [span] = &spans[..] else {
            panic!("expected one span: {spans:#?}")
        };
        assert_eq!(span.operation, "outgoing-client-request");
        assert_eq!(tag(span, "http.method"), Some(&TagValue::Str("GET".into())));
        assert_eq!(
            tag(span, "http.url"),
            Some(&TagValue::Str(
                format!("http://127.0.0.1:{port}/introspect").into()
            ))
        );
        assert_eq!(tag(span, "http.status_code"), Some(&TagValue::Long(200)));
        assert_eq!(tag(span, "error"), None);
    }
}
