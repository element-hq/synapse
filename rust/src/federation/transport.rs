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

//! The seam between the federation request layer and the network.
//!
//! The request layer (signing, retries, backoff) sends through a
//! [`FederationTransport`]. In production that is the
//! [`Agent`](super::agent::Agent), which resolves Matrix server names and
//! pools connections. Tests install a [`PyTransport`] instead, which hands
//! each request to a Python callable. That is the Rust-side replacement for
//! the `FakeTransport` and `reactor.tcpClients` plumbing the Python tests use.

use std::fmt;

use async_trait::async_trait;
use bytes::Bytes;
use http::{HeaderName, HeaderValue, Request, Response, StatusCode};
use http_body_util::{combinators::UnsyncBoxBody, BodyExt, Full};
use pyo3::{
    prelude::*,
    types::{PyBytes, PyList, PyTuple},
};

/// A boxed error, as body streams produce.
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// A streaming response body.
pub type ResponseBody = UnsyncBoxBody<Bytes, BoxError>;

/// Sends one federation request and returns the response head with a
/// streaming body.
///
/// The request URI is `matrix-federation://<server name>/<path>`, or an
/// `https://` URI for anything that is not server-to-server federation.
#[async_trait]
pub trait FederationTransport: Send + Sync + 'static {
    async fn send(&self, request: Request<Bytes>)
        -> Result<Response<ResponseBody>, TransportError>;
}

/// Why a request got no response.
///
/// The request layer needs to tell these apart: a DNS failure is not
/// retried when the caller asked for `retry_on_dns_fail=False`.
#[derive(Debug)]
pub enum TransportError {
    /// Looking up the server's addresses (SRV or A/AAAA) failed, or found
    /// nothing we may connect to. An address the IP range blocklist covers
    /// counts as not found, as in Python.
    Dns(anyhow::Error),
    /// The server publishes a single SRV record with target `.`, which means
    /// it offers no federation service.
    ServiceUnavailable(String),
    /// Every TCP connection attempt failed, or the proxy refused the tunnel.
    Connect(anyhow::Error),
    /// The TLS handshake or certificate verification failed.
    Tls(anyhow::Error),
    /// The request could not be built.
    InvalidRequest(anyhow::Error),
    /// Sending the request or reading the response head failed.
    Request(anyhow::Error),
    /// The request took longer than the transport's timeout.
    Timeout,
    /// A Python test transport raised.
    Python(PyErr),
}

impl TransportError {
    /// Whether this is a name resolution failure, which Python reports as a
    /// `DNSLookupError`.
    pub fn is_dns(&self) -> bool {
        matches!(self, TransportError::Dns(_))
    }

    /// A short name for the kind of failure.
    pub fn kind(&self) -> &'static str {
        match self {
            TransportError::Dns(_) => "dns",
            TransportError::ServiceUnavailable(_) => "service_unavailable",
            TransportError::Connect(_) => "connect",
            TransportError::Tls(_) => "tls",
            TransportError::InvalidRequest(_) => "invalid_request",
            TransportError::Request(_) => "request",
            TransportError::Timeout => "timeout",
            TransportError::Python(_) => "python",
        }
    }

    /// A copy of this error of the same kind, with the source chain
    /// flattened into the message. hyper only lends us the error our
    /// connector returned, so this is how we get it back out.
    pub fn duplicate(&self) -> TransportError {
        let flat = |e: &anyhow::Error| anyhow::anyhow!("{e:#}");
        match self {
            TransportError::Dns(e) => TransportError::Dns(flat(e)),
            TransportError::ServiceUnavailable(name) => {
                TransportError::ServiceUnavailable(name.clone())
            }
            TransportError::Connect(e) => TransportError::Connect(flat(e)),
            TransportError::Tls(e) => TransportError::Tls(flat(e)),
            TransportError::InvalidRequest(e) => TransportError::InvalidRequest(flat(e)),
            TransportError::Request(e) => TransportError::Request(flat(e)),
            TransportError::Timeout => TransportError::Timeout,
            // Cloning a `PyErr` needs the GIL, which we may not hold.
            TransportError::Python(e) => TransportError::Request(anyhow::anyhow!("{e}")),
        }
    }
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TransportError::Dns(e) => write!(f, "DNS lookup failed: {e:#}"),
            TransportError::ServiceUnavailable(name) => write!(f, "service {name} unavailable"),
            TransportError::Connect(e) => write!(f, "connection failed: {e:#}"),
            TransportError::Tls(e) => write!(f, "TLS failed: {e:#}"),
            TransportError::InvalidRequest(e) => write!(f, "invalid request: {e:#}"),
            TransportError::Request(e) => write!(f, "request failed: {e:#}"),
            TransportError::Timeout => write!(f, "request timed out"),
            TransportError::Python(e) => write!(f, "test transport raised: {e}"),
        }
    }
}

impl std::error::Error for TransportError {}

/// What a [`PyTransport`] callable returns: status, headers and body.
type PyResponse = (u16, Vec<(Vec<u8>, Vec<u8>)>, Vec<u8>);

/// A [`FederationTransport`] that calls a Python callable, for tests.
///
/// The callable is called with the GIL held, on a tokio worker thread, as
/// `callable(destination: str, method: str, path: str,
/// headers: list[tuple[bytes, bytes]], body: bytes)` and returns
/// `(status: int, headers: list[tuple[bytes, bytes]], body: bytes)`.
/// `path` includes the query string. If it raises, the request fails with
/// [`TransportError::Python`].
pub struct PyTransport {
    callable: Py<PyAny>,
}

impl PyTransport {
    pub fn new(callable: Py<PyAny>) -> Self {
        PyTransport { callable }
    }

    fn call(&self, py: Python<'_>, request: &Request<Bytes>) -> PyResult<Response<Bytes>> {
        let uri = request.uri();
        let destination = uri.authority().map(|a| a.as_str()).unwrap_or("");
        let path = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");

        let headers = PyList::empty(py);
        for (name, value) in request.headers() {
            headers.append((
                PyBytes::new(py, name.as_str().as_bytes()),
                PyBytes::new(py, value.as_bytes()),
            ))?;
        }

        let result = self.callable.bind(py).call1((
            destination,
            request.method().as_str(),
            path,
            headers,
            PyBytes::new(py, request.body()),
        ))?;

        let result = result.cast::<PyTuple>()?;
        let (status, headers, body): PyResponse = result.extract()?;

        let mut response = Response::new(Bytes::from(body));
        *response.status_mut() = StatusCode::from_u16(status)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        for (name, value) in headers {
            let name = HeaderName::from_bytes(&name)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            let value = HeaderValue::from_bytes(&value)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            response.headers_mut().append(name, value);
        }

        Ok(response)
    }
}

#[async_trait]
impl FederationTransport for PyTransport {
    async fn send(
        &self,
        request: Request<Bytes>,
    ) -> Result<Response<ResponseBody>, TransportError> {
        let response =
            Python::attach(|py| self.call(py, &request)).map_err(TransportError::Python)?;

        Ok(response.map(|body| {
            Full::new(body)
                .map_err(|never| match never {})
                .boxed_unsync()
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The test transport passes the request through to Python and builds the
    /// response from what it returns.
    #[tokio::test]
    async fn py_transport_round_trip() {
        Python::initialize();

        let callable = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let locals = pyo3::types::PyDict::new(py);
            py.run(
                c"
def handler(destination, method, path, headers, body):
    assert destination == 'remote.example', destination
    assert method == 'PUT', method
    assert path == '/_matrix/federation/v1/send/1?a=b', path
    assert (b'content-type', b'application/json') in headers, headers
    return (201, [(b'x-seen', body)], b'{\"ok\":true}')
",
                None,
                Some(&locals),
            )?;
            Ok(locals.get_item("handler")?.unwrap().unbind())
        })
        .unwrap();

        let transport = PyTransport::new(callable);
        let request =
            Request::put("matrix-federation://remote.example/_matrix/federation/v1/send/1?a=b")
                .header("content-type", "application/json")
                .body(Bytes::from_static(b"{}"))
                .unwrap();

        let response = transport.send(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::CREATED);
        assert_eq!(response.headers()["x-seen"], "{}");
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(&body[..], b"{\"ok\":true}");
    }

    #[tokio::test]
    async fn py_transport_raises() {
        Python::initialize();

        let callable = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let locals = pyo3::types::PyDict::new(py);
            py.run(
                c"
def handler(*args):
    raise ConnectionError('boom')
",
                None,
                Some(&locals),
            )?;
            Ok(locals.get_item("handler")?.unwrap().unbind())
        })
        .unwrap();

        let transport = PyTransport::new(callable);
        let request = Request::get("matrix-federation://remote.example/x")
            .body(Bytes::new())
            .unwrap();

        let err = transport.send(request).await.unwrap_err();
        assert!(matches!(err, TransportError::Python(_)), "{err}");
    }
}
