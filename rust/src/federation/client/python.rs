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

//! The client as `MatrixFederationHttpClient` uses it.
//!
//! [`RustFederationClient::request`] takes the request in Python objects,
//! converts it once, and returns a Deferred. Everything until the response
//! is parsed runs on tokio without the GIL. The response body is parsed
//! there too, for the JSON parsers, and only the conversion of the result to
//! Python objects takes the GIL. A custom `ByteParser` is Python code, so it
//! is fed the body under the GIL, in chunks of at least 64 KiB.
//!
//! Errors become the exceptions `MatrixFederationHttpClient` raises, built in
//! the same GIL hop as a result would be.

use std::{collections::HashSet, sync::Arc, time::Duration};

use async_trait::async_trait;
use bytes::{Bytes, BytesMut};
use http::{header, HeaderMap, HeaderValue, Method};
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    intern,
    prelude::*,
    types::{PyBytes, PyDict, PyList},
};
use serde_json::Value;

use super::{
    limiter::{BackoffConfig, RetryTimings, RetryTimingsStore, TimingsUpdate},
    ClientConfig, ClientError, FailureCause, FederationClient, FederationRequest, HttpError,
    RequestOptions, RequestStats, SentResponse,
};
use crate::{
    clock::Clock,
    deferred::{create_deferred_with, OnCancel},
    federation::{
        agent::{resolve::SystemResolver, Agent, AgentConfig},
        gil::{account, with_gil},
        transport::{FederationTransport, PyTransport, TransportError},
    },
    homeserver::HomeServer,
    runtime::RustRuntime,
    storage::store::Store,
};

/// The response size limit of Python's JSON parsers.
const MAX_JSON_RESPONSE_SIZE: usize = 100 * 1024 * 1024;

/// How much of the body to collect before handing it to a Python parser.
const PARSER_CHUNK_SIZE: usize = 64 * 1024;

/// How to parse a 2xx response body.
enum Parser {
    /// `JsonParser`: a JSON object.
    Json,
    /// `LegacyJsonSendParser`: `[int, object]`.
    LegacyJsonSend,
    /// Any other `ByteParser`: Python code.
    Custom {
        parser: Py<PyAny>,
        content_type: String,
        max_size: usize,
    },
}

impl Parser {
    fn content_type(&self) -> &str {
        match self {
            Parser::Json | Parser::LegacyJsonSend => "application/json",
            Parser::Custom { content_type, .. } => content_type,
        }
    }

    fn max_size(&self) -> usize {
        match self {
            Parser::Json | Parser::LegacyJsonSend => MAX_JSON_RESPONSE_SIZE,
            Parser::Custom { max_size, .. } => *max_size,
        }
    }
}

/// The retry timings, read through the Rust store's mirror of
/// `get_destination_retry_timings`, and written by a Python callable.
///
/// The writer is Python because the write invalidates the cache across
/// workers, and because it also tells the notifier and the other workers
/// when a server comes back. It only runs when the backoff changes.
struct StoreRetryTimings {
    store: Arc<Store>,
    runtime: RustRuntime,
    /// `write(destination, failure_ts, retry_last_ts, retry_interval,
    /// server_up)`, which starts a background process.
    write: Arc<Py<PyAny>>,
}

#[async_trait]
impl RetryTimingsStore for StoreRetryTimings {
    async fn get(&self, destination: &str) -> anyhow::Result<Option<RetryTimings>> {
        self.store.get_destination_retry_timings(destination).await
    }

    fn set(&self, destination: &str, update: TimingsUpdate) {
        let write = Arc::clone(&self.write);
        let destination = destination.to_owned();
        let dispatched = self.runtime.dispatch_to_twisted(move |py| {
            account("retry_timings_write", py, |py| {
                let result = write.bind(py).call1((
                    destination,
                    update.failure_ts,
                    update.retry_last_ts,
                    update.retry_interval,
                    update.server_up,
                ));
                if let Err(err) = result {
                    log::error!("Failed to store destination_retry_timings: {err}");
                }
            })
        });
        if let Err(err) = dispatched {
            log::warn!("Failed to store destination_retry_timings: {err:#}");
        }
    }
}

/// The Rust federation client.
#[pyclass(frozen, name = "RustFederationClient")]
pub struct RustFederationClient {
    client: Arc<FederationClient>,
    runtime: RustRuntime,
    /// `record(method, attempts, response_codes)`, which bumps the request
    /// and response counters.
    record_metrics: Arc<Py<PyAny>>,
}

#[pymethods]
impl RustFederationClient {
    /// `user_agent` is the `User-Agent` header, the bare version string as
    /// Python sends it. `agent_user_agent` is the agent's, with the suffix,
    /// which only applies to the `.well-known` fetch. `transport`, for tests,
    /// is a `PyTransport` callable to send through instead of the network.
    #[new]
    #[pyo3(signature = (hs, user_agent, agent_user_agent, write_retry_timings, record_metrics, transport = None))]
    fn py_new(
        py: Python<'_>,
        hs: HomeServer,
        user_agent: &str,
        agent_user_agent: String,
        write_retry_timings: Py<PyAny>,
        record_metrics: Py<PyAny>,
        transport: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let config = hs.config(py)?;
        let runtime = hs.get_rust_runtime(py)?;
        let store = hs.get_rust_store(py)?;
        let federation = &config.federation;

        let clock = Clock::new();
        let now = Arc::new(move || clock.now_millis());

        let transport: Arc<dyn FederationTransport> = match transport {
            Some(callable) => Arc::new(PyTransport::new(callable)),
            None => {
                let dns = SystemResolver::new()
                    .map_err(|err| PyRuntimeError::new_err(format!("{err:#}")))?;
                let agent = Agent::new(
                    AgentConfig::from_homeserver_config(&config, agent_user_agent),
                    Arc::new(dns),
                    Arc::new(Clock::new()),
                    now.clone(),
                    Default::default(),
                )
                .map_err(|err| PyValueError::new_err(format!("{err:#}")))?;
                Arc::new(agent)
            }
        };

        let client_config = ClientConfig {
            server_name: hs.hostname(py)?,
            signing_key: hs.signing_key(py)?,
            user_agent: HeaderValue::from_str(user_agent)
                .map_err(|err| PyValueError::new_err(err.to_string()))?,
            default_timeout: Duration::from_millis(federation.client_timeout_ms),
            max_long_retries: federation.max_long_retries,
            max_short_retries: federation.max_short_retries,
            max_long_retry_delay: Duration::from_millis(federation.max_long_retry_delay_ms),
            max_short_retry_delay: Duration::from_millis(federation.max_short_retry_delay_ms),
            domain_whitelist: federation
                .federation_domain_whitelist
                .as_ref()
                .map(|whitelist| whitelist.keys().cloned().collect::<HashSet<_>>()),
            backoff: BackoffConfig {
                min_retry_interval_ms: federation.destination_min_retry_interval_ms,
                retry_multiplier: federation.destination_retry_multiplier,
                max_retry_interval_ms: federation.destination_max_retry_interval_ms,
            },
        };

        let retry_timings = Arc::new(StoreRetryTimings {
            store,
            runtime: runtime.clone(),
            write: Arc::new(write_retry_timings),
        });

        let client = FederationClient::new(
            client_config,
            transport,
            retry_timings,
            Arc::new(Clock::new()),
            now,
        );

        Ok(RustFederationClient {
            client: Arc::new(client),
            runtime,
            record_metrics: Arc::new(record_metrics),
        })
    }

    /// Send a request and parse the response. Returns a Deferred of
    /// `(body, headers)`, where `headers` is `None` unless `want_headers`.
    ///
    /// `parser_kind` is `"json"`, `"legacy_json_send"` or `"custom"`, for
    /// which `parser` is the `ByteParser`.
    #[pyo3(signature = (
        method,
        destination,
        path,
        query,
        body,
        parser_kind,
        parser = None,
        *,
        rewrite_age_ts = false,
        extra_headers = Vec::new(),
        retry_on_dns_fail = true,
        timeout_ms = None,
        long_retries = false,
        ignore_backoff = false,
        backoff_on_404 = false,
        backoff_on_all_error_codes = false,
        try_trailing_slash_on_400 = false,
        want_headers = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn request<'py>(
        &self,
        py: Python<'py>,
        method: &str,
        destination: String,
        path: String,
        query: String,
        body: Option<Bound<'py, PyAny>>,
        parser_kind: &str,
        parser: Option<Bound<'py, PyAny>>,
        rewrite_age_ts: bool,
        extra_headers: Vec<(String, String)>,
        retry_on_dns_fail: bool,
        timeout_ms: Option<u64>,
        long_retries: bool,
        ignore_backoff: bool,
        backoff_on_404: bool,
        backoff_on_all_error_codes: bool,
        try_trailing_slash_on_400: bool,
        want_headers: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let method = Method::from_bytes(method.as_bytes())
            .map_err(|err| PyValueError::new_err(err.to_string()))?;

        let body: Option<Value> = match body {
            Some(body) if !body.is_none() => Some(pythonize::depythonize(&body)?),
            _ => None,
        };

        let parser = match parser_kind {
            "json" => Parser::Json,
            "legacy_json_send" => Parser::LegacyJsonSend,
            "custom" => {
                let parser =
                    parser.ok_or_else(|| PyValueError::new_err("a custom parser is required"))?;
                Parser::Custom {
                    content_type: parser.getattr(intern!(py, "CONTENT_TYPE"))?.extract()?,
                    max_size: parser
                        .getattr(intern!(py, "MAX_RESPONSE_SIZE"))?
                        .extract()?,
                    parser: parser.unbind(),
                }
            }
            other => {
                return Err(PyValueError::new_err(format!(
                    "unknown parser kind {other:?}"
                )))
            }
        };

        let request = FederationRequest {
            method,
            destination,
            path,
            query,
            body,
            rewrite_age_ts,
            extra_headers,
        };
        let options = RequestOptions {
            retry_on_dns_fail,
            timeout: timeout_ms.map(Duration::from_millis),
            long_retries,
            ignore_backoff,
            backoff_on_404,
            backoff_on_all_error_codes,
            try_trailing_slash_on_400,
        };

        let client = Arc::clone(&self.client);
        let record_metrics = Arc::clone(&self.record_metrics);
        let fut = async move {
            let mut stats = RequestStats::default();
            let result = client.send(&request, &options, &mut stats).await;
            let record = |py: Python<'_>| {
                let recorded = record_metrics.bind(py).call1((
                    request.method.as_str(),
                    stats.attempts,
                    stats.responses.clone(),
                ));
                if let Err(err) = recorded {
                    log::warn!("Failed to record federation request metrics: {err}");
                }
            };

            let response = match result {
                Ok(response) => response,
                Err(err) => {
                    return Err(with_gil("request_error", |py| {
                        record(py);
                        client_error_to_py(py, err, &request.destination)
                    }))
                }
            };

            let timeout = options.timeout.unwrap_or(client.config().default_timeout);
            read_and_parse(
                &client,
                response,
                parser,
                timeout,
                want_headers,
                &request.destination,
                record,
            )
            .await
        };

        // Cancelling the request stops it, as cancelling the Python one
        // does.
        create_deferred_with(py, &self.runtime, fut, OnCancel::Abort)
    }

    /// Stop retrying requests.
    fn shutdown(&self) {
        self.client.shutdown();
    }

    /// End the sleep before any retry to `destination`.
    fn wake_destination(&self, destination: &str) {
        self.client.wake_destination(destination);
    }
}

/// Read a 2xx response's body, parse it and convert it for Python, as
/// `_handle_response` does.
async fn read_and_parse(
    client: &FederationClient,
    response: SentResponse,
    parser: Parser,
    timeout: Duration,
    want_headers: bool,
    destination: &str,
    record: impl Fn(Python<'_>),
) -> PyResult<Py<PyAny>> {
    let log_prefix = format!("{{{}}} [{destination}]", response.txn_id);
    let description = response.description.clone();
    let status = response.status.as_u16();
    let phrase = response.phrase.clone();
    let start_ms = response.start_ms;
    let headers = response.headers;
    let max_size = parser.max_size();

    let failed = |py: Python<'_>, err: ClientError| -> PyErr {
        record(py);
        if let Parser::Custom { parser, .. } = &parser {
            // Let the parser free its resources. It has already failed.
            let _ = parser.bind(py).call_method0(intern!(py, "finish"));
        }
        client_error_to_py(py, err, destination)
    };

    if let Err(err) = check_content_type(&headers, parser.content_type()) {
        log::warn!("{log_prefix} Error reading response {description}: {err}");
        return Err(with_gil("response_error", |py| failed(py, err)));
    }

    // Read the body. A JSON body is collected here. A custom parser is fed
    // as it arrives.
    let mut collected = BytesMut::new();
    let mut python_error: Option<PyErr> = None;
    let read = client
        .read_body(response.body, Some(max_size), timeout, |chunk| {
            collected.extend_from_slice(&chunk);
            if let Parser::Custom { parser, .. } = &parser {
                if collected.len() >= PARSER_CHUNK_SIZE {
                    let chunk = collected.split().freeze();
                    feed_parser(parser, chunk).map_err(|err| {
                        python_error = Some(err);
                        // Stops the read. Replaced by the Python error below.
                        ClientError::Shutdown
                    })?;
                }
            }
            Ok(())
        })
        .await;

    let length = match read {
        Ok(length) => length,
        Err(err) => {
            if let Some(py_err) = python_error {
                return Err(with_gil("response_error", |py| {
                    record(py);
                    if let Parser::Custom { parser, .. } = &parser {
                        // As Python, let the parser free its resources.
                        let _ = parser.bind(py).call_method0(intern!(py, "finish"));
                    }
                    parser_error(py, py_err)
                }));
            }
            match &err {
                ClientError::SendFailed {
                    cause: FailureCause::TooLarge { limit },
                    ..
                } => log::warn!(
                    "{log_prefix} JSON response exceeded max size {limit} - {description}"
                ),
                ClientError::SendFailed {
                    cause: FailureCause::BodyTimeout,
                    ..
                } => log::warn!("{log_prefix} Timed out reading response - {description}"),
                _ => log::warn!("{log_prefix} Failed to read response - {description}"),
            }
            return Err(with_gil("response_error", |py| failed(py, err)));
        }
    };

    // Parse a JSON body off the GIL.
    let parsed = match &parser {
        Parser::Json | Parser::LegacyJsonSend => Some(serde_json::from_slice::<Value>(&collected)),
        Parser::Custom { .. } => None,
    };

    let secs = (client.now_ms().saturating_sub(start_ms)) as f64 / 1000.0;

    let result = with_gil("response", |py| -> PyResult<Py<PyAny>> {
        record(py);

        let value = match (&parser, parsed) {
            (Parser::Custom { parser, .. }, _) => {
                let parser = parser.bind(py);
                if !collected.is_empty() {
                    parser
                        .call_method1(intern!(py, "write"), (PyBytes::new(py, &collected),))
                        .map_err(|err| parser_error(py, err))?;
                }
                parser
                    .call_method0(intern!(py, "finish"))
                    .map_err(|err| parser_error(py, err))?
            }
            (_, Some(Ok(value))) => {
                validate_json(&parser, &value).map_err(|err| {
                    log::warn!("{log_prefix} Failed to parse response - {description}");
                    invalid_body(py, err)
                })?;
                pythonize::pythonize(py, &value)?
            }
            (_, _) => {
                // serde_json refuses some JSON that Python accepts, such as
                // lone surrogates in strings. Let Python decide.
                python_json(py, &collected, &parser).inspect_err(|_| {
                    log::warn!("{log_prefix} Failed to parse response - {description}");
                })?
            }
        };

        let headers = if want_headers {
            headers_to_py(py, &headers)?.into_any()
        } else {
            py.None().into_bound(py)
        };

        Ok((value, headers).into_pyobject(py)?.into_any().unbind())
    });

    if result.is_ok() {
        log::info!(
            "{log_prefix} Completed request: {status} {phrase} in {secs:.2} secs, got {length} bytes - {description}"
        );
    }
    result
}

/// Hand a chunk of the body to a Python parser.
fn feed_parser(parser: &Py<PyAny>, chunk: Bytes) -> PyResult<()> {
    with_gil("parser_write", |py| {
        parser
            .bind(py)
            .call_method1(intern!(py, "write"), (PyBytes::new(py, &chunk),))
            .map(drop)
    })
}

/// `check_content_type_is`.
fn check_content_type(headers: &HeaderMap, expected: &str) -> Result<(), ClientError> {
    let failed = |message: String| ClientError::SendFailed {
        cause: FailureCause::ContentType(message),
        can_retry: false,
    };
    let Some(value) = headers.get(header::CONTENT_TYPE) else {
        return Err(failed(
            "No Content-Type header received from remote server".to_owned(),
        ));
    };
    let value = String::from_utf8_lossy(value.as_bytes());
    let essence = value.split(';').next().unwrap_or_default().trim();
    if essence != expected {
        return Err(failed(format!(
            "Remote server sent Content-Type header of '{value}', not '{expected}'"
        )));
    }
    Ok(())
}

/// The shape check of `JsonParser` and `LegacyJsonSendParser`.
fn validate_json(parser: &Parser, value: &Value) -> Result<(), String> {
    let valid = match parser {
        Parser::Json => value.is_object(),
        Parser::LegacyJsonSend => match value.as_array().map(Vec::as_slice) {
            Some([code, body]) => {
                (code.is_i64() || code.is_u64()) && !code.is_f64() && body.is_object()
            }
            _ => false,
        },
        Parser::Custom { .. } => true,
    };
    if valid {
        Ok(())
    } else {
        Err(format!(
            "Received incorrect JSON value: {}",
            python_type_name(value)
        ))
    }
}

/// The name of the Python type a JSON value decodes to.
fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Parse with Python's decoder, for JSON that serde_json refused.
fn python_json<'py>(py: Python<'py>, body: &[u8], parser: &Parser) -> PyResult<Bound<'py, PyAny>> {
    let parsed = (|| {
        let text = PyBytes::new(py, body).call_method1(intern!(py, "decode"), ("utf-8",))?;
        py.import("synapse.util.json")?
            .getattr("json_decoder")?
            .call_method1(intern!(py, "decode"), (text,))
    })();
    let value = match parsed {
        Ok(value) => value,
        Err(err) if err.is_instance_of::<PyValueError>(py) => {
            return Err(request_send_failed(
                py,
                err.into_value(py).into_bound(py).into_any(),
                false,
            ))
        }
        Err(err) => return Err(err),
    };

    let valid = match parser {
        Parser::Json => value.is_instance_of::<PyDict>(),
        Parser::LegacyJsonSend => value
            .cast::<PyList>()
            .ok()
            .filter(|list| list.len() == 2)
            .is_some_and(|list| {
                let code_is_int = list
                    .get_item(0)
                    .and_then(|code| code.get_type().name().map(|n| n == "int"))
                    .unwrap_or(false);
                let body_is_dict = list
                    .get_item(1)
                    .is_ok_and(|body| body.is_instance_of::<PyDict>());
                code_is_int && body_is_dict
            }),
        Parser::Custom { .. } => true,
    };
    if !valid {
        let name = value.get_type().name()?.to_string();
        return Err(invalid_body(
            py,
            format!("Received incorrect JSON value: {name}"),
        ));
    }
    Ok(value)
}

/// A response body that failed to parse: `RequestSendFailed(ValueError)`.
fn invalid_body(py: Python<'_>, message: String) -> PyErr {
    let inner = PyValueError::new_err(message)
        .into_value(py)
        .into_bound(py)
        .into_any();
    request_send_failed(py, inner, false)
}

/// What Python does with an exception from a custom parser: a `ValueError`
/// becomes `RequestSendFailed`, anything else is raised as it is.
fn parser_error(py: Python<'_>, err: PyErr) -> PyErr {
    if err.is_instance_of::<PyValueError>(py) {
        request_send_failed(py, err.into_value(py).into_bound(py).into_any(), false)
    } else {
        err
    }
}

/// The headers as Twisted's `getAllRawHeaders` gives them: canonical names
/// to lists of values, as bytes.
fn headers_to_py<'py>(py: Python<'py>, headers: &HeaderMap) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for name in headers.keys() {
        let values = PyList::empty(py);
        for value in headers.get_all(name) {
            values.append(PyBytes::new(py, value.as_bytes()))?;
        }
        dict.set_item(
            PyBytes::new(py, canonical_header_name(name.as_str()).as_bytes()),
            values,
        )?;
    }
    Ok(dict)
}

/// `Content-Type` from `content-type`.
fn canonical_header_name(name: &str) -> String {
    name.split('-')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                Some(first) => first.to_ascii_uppercase().to_string() + chars.as_str(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join("-")
}

/// `RequestSendFailed(inner, can_retry)`.
fn request_send_failed(py: Python<'_>, inner: Bound<'_, PyAny>, can_retry: bool) -> PyErr {
    let built = (|| {
        py.import("synapse.api.errors")?
            .getattr("RequestSendFailed")?
            .call1((inner, can_retry))
    })();
    match built {
        Ok(exc) => PyErr::from_value(exc),
        Err(err) => err,
    }
}

/// Build an exception from `module.name(*args)`.
fn build_exception<'py, A: pyo3::call::PyCallArgs<'py>>(
    py: Python<'py>,
    module: &str,
    name: &str,
    args: A,
) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)?.call1(args)
}

/// `HttpResponseException(code, msg, response)`.
fn http_exception<'py>(py: Python<'py>, err: &HttpError) -> PyResult<Bound<'py, PyAny>> {
    build_exception(
        py,
        "synapse.api.errors",
        "HttpResponseException",
        (err.code, err.phrase.as_str(), PyBytes::new(py, &err.body)),
    )
}

/// The exception Python raises for `err`.
fn client_error_to_py(py: Python<'_>, err: ClientError, destination: &str) -> PyErr {
    let built = (|| -> PyResult<PyErr> {
        Ok(match err {
            ClientError::Denied => PyErr::from_value(build_exception(
                py,
                "synapse.api.errors",
                "FederationDeniedError",
                (destination,),
            )?),
            ClientError::NotRetrying(nr) => {
                let kwargs = PyDict::new(py);
                kwargs.set_item("retry_last_ts", nr.retry_last_ts)?;
                kwargs.set_item("retry_interval", nr.retry_interval)?;
                kwargs.set_item("destination", destination)?;
                PyErr::from_value(
                    py.import("synapse.util.retryutils")?
                        .getattr("NotRetryingDestination")?
                        .call((), Some(&kwargs))?,
                )
            }
            ClientError::SendFailed { cause, can_retry } => {
                let inner = failure_cause_to_py(py, cause)?;
                request_send_failed(py, inner, can_retry)
            }
            ClientError::Http(err) => PyErr::from_value(http_exception(py, &err)?),
            ClientError::InvalidRequest(message) => PyValueError::new_err(message),
            ClientError::Store(err) => PyRuntimeError::new_err(format!("{err:#}")),
            ClientError::Shutdown => {
                PyRuntimeError::new_err("the federation client has been shut down")
            }
        })
    })();
    built.unwrap_or_else(|err| err)
}

/// The exception `RequestSendFailed` wraps, as close to Python's as we can
/// get. Only `HttpResponseException` is looked at by callers.
fn failure_cause_to_py(py: Python<'_>, cause: FailureCause) -> PyResult<Bound<'_, PyAny>> {
    let twisted_error = |name: &str, message: String| {
        build_exception(py, "twisted.internet.error", name, (message,))
    };
    match cause {
        FailureCause::Transport(err) => match err {
            TransportError::Dns(_) | TransportError::ServiceUnavailable(_) => {
                twisted_error("DNSLookupError", err.to_string())
            }
            TransportError::Connect(_) | TransportError::Tls(_) => {
                twisted_error("ConnectError", err.to_string())
            }
            TransportError::Timeout => build_exception(
                py,
                "twisted.internet.defer",
                "TimeoutError",
                (err.to_string(),),
            ),
            TransportError::Python(err) => Ok(err.into_value(py).into_bound(py).into_any()),
            TransportError::InvalidRequest(_) | TransportError::Request(_) => {
                Ok(PyRuntimeError::new_err(err.to_string())
                    .into_value(py)
                    .into_bound(py)
                    .into_any())
            }
        },
        FailureCause::Http(err) => http_exception(py, &err),
        FailureCause::BodyTimeout => build_exception(
            py,
            "twisted.internet.defer",
            "TimeoutError",
            ("timed out reading the response",),
        ),
        FailureCause::TooLarge { .. } => {
            build_exception(py, "synapse.http.client", "BodyExceededMaxSize", ())
        }
        FailureCause::BodyRead(message) | FailureCause::ContentType(message) => {
            Ok(PyRuntimeError::new_err(message)
                .into_value(py)
                .into_bound(py)
                .into_any())
        }
        FailureCause::InvalidBody(message) => Ok(PyValueError::new_err(message)
            .into_value(py)
            .into_bound(py)
            .into_any()),
    }
}
