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

//! The federation request layer: Python's `MatrixFederationHttpClient`
//! `_send_request` and its retry, backoff and signing rules.
//!
//! [`FederationClient::send`] validates the destination, checks the backoff,
//! signs and sends the request through a [`FederationTransport`] with the
//! retry schedule, and records the outcome for the backoff. It returns the
//! head of a 2xx response with the body unread, as `_send_request` does.
//! [`FederationClient::read_body`] then reads the body with the size cap and
//! timeout `_handle_response` applies. Turning the body into Python objects is
//! [`python`]'s job.
//!
//! - [`limiter`]: the per-destination backoff.
//! - [`sleeper`]: the sleep between retries that `wake_destination` ends.

use std::{
    collections::HashSet,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};

use anyhow::Context as _;
use bytes::{Bytes, BytesMut};
use http::{header, HeaderMap, HeaderValue, Method, Request, StatusCode};
use http_body_util::BodyExt;
use rand::RngExt;
use serde_json::Value;
use tracing::Instrument;

use crate::logging::opentracing::layer::inject_headers;
use crate::logging::opentracing::{record_error, whitelisted_homeserver};
use crate::{
    clock::Sleeper,
    event_auth::is_valid_server_name,
    federation::{
        agent::Now,
        signing::{make_auth_header, CanonicalBody, Destination, FederationSigningKey},
        transport::{FederationTransport, ResponseBody, TransportError},
    },
};

pub mod limiter;
pub mod python;
pub mod sleeper;

use limiter::{
    BackoffConfig, LimiterOptions, NotRetrying, Outcome, RetryLimiter, RetryTimingsStore,
};
use sleeper::AwakenableSleeper;

/// The most of an error response's body we keep. Python reads the whole
/// thing. The body only feeds the error message and the `errcode` check.
const MAX_ERROR_BODY_SIZE: usize = 1024 * 1024;

/// Everything the client needs to know, from the homeserver.
#[derive(Clone)]
pub struct ClientConfig {
    pub server_name: String,
    pub signing_key: FederationSigningKey,
    /// The `User-Agent` header. Python sends the bare version string here,
    /// without `user_agent_suffix`.
    pub user_agent: HeaderValue,
    pub default_timeout: Duration,
    pub max_long_retries: u32,
    pub max_short_retries: u32,
    pub max_long_retry_delay: Duration,
    pub max_short_retry_delay: Duration,
    /// If set, the only destinations we may send to.
    pub domain_whitelist: Option<HashSet<String>>,
    pub backoff: BackoffConfig,
}

/// One request, as the Python facade describes it.
#[derive(Clone, Debug)]
pub struct FederationRequest {
    pub method: Method,
    pub destination: String,
    /// The path, already percent-encoded.
    pub path: String,
    /// The query string without the `?`, already encoded. Empty for none.
    pub query: String,
    /// The JSON body. As in Python, a value that is falsy in Python (`None`,
    /// `{}`) means no body.
    pub body: Option<Value>,
    /// Rewrite each PDU's `age_ts` into `unsigned.age` on every attempt, as
    /// the transaction manager's `json_data_callback` does.
    pub rewrite_age_ts: bool,
    /// Extra headers, such as the tracing span.
    pub extra_headers: Vec<(String, String)>,
}

/// How to send it. The names are those of the Python keyword arguments.
#[derive(Clone, Copy, Debug)]
pub struct RequestOptions {
    pub retry_on_dns_fail: bool,
    pub timeout: Option<Duration>,
    pub long_retries: bool,
    pub ignore_backoff: bool,
    pub backoff_on_404: bool,
    pub backoff_on_all_error_codes: bool,
    pub try_trailing_slash_on_400: bool,
    /// The caller made the `outgoing-federation-request` span and injected
    /// its context, as Python's `MatrixFederationHttpClient` does when it
    /// sends through this client: make neither again.
    pub traced_by_caller: bool,
}

impl Default for RequestOptions {
    fn default() -> Self {
        RequestOptions {
            retry_on_dns_fail: true,
            timeout: None,
            long_retries: false,
            ignore_backoff: false,
            backoff_on_404: false,
            backoff_on_all_error_codes: false,
            try_trailing_slash_on_400: false,
            traced_by_caller: false,
        }
    }
}

/// An error response, as `HttpResponseException` carries it.
#[derive(Clone, Debug)]
pub struct HttpError {
    pub code: u16,
    pub phrase: String,
    pub body: Bytes,
}

impl HttpError {
    /// The `errcode` of a Matrix error body, as `to_synapse_error` finds it.
    pub fn errcode(&self) -> Option<String> {
        let body: Value = serde_json::from_slice(&self.body).ok()?;
        body.get("errcode")?.as_str().map(str::to_owned)
    }
}

/// Why a request failed in a way that `RequestSendFailed` reports.
#[derive(Debug)]
pub enum FailureCause {
    /// No response.
    Transport(TransportError),
    /// A 5xx or 429 response.
    Http(HttpError),
    /// Reading the body took longer than the timeout.
    BodyTimeout,
    /// Reading the body failed.
    BodyRead(String),
    /// The body was larger than the limit.
    TooLarge { limit: usize },
    /// The `Content-Type` was missing or wrong.
    ContentType(String),
    /// The body did not parse.
    InvalidBody(String),
}

impl std::fmt::Display for FailureCause {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FailureCause::Transport(err) => write!(f, "{err}"),
            FailureCause::Http(err) => write!(f, "{}: {}", err.code, err.phrase),
            FailureCause::BodyTimeout => write!(f, "timed out reading the response"),
            FailureCause::BodyRead(err) => write!(f, "reading the response failed: {err}"),
            FailureCause::TooLarge { limit } => write!(f, "response larger than {limit} bytes"),
            FailureCause::ContentType(err) => write!(f, "{err}"),
            FailureCause::InvalidBody(err) => write!(f, "invalid response: {err}"),
        }
    }
}

/// Why a request failed. Each maps to the Python exception that
/// `MatrixFederationHttpClient` raises in the same case.
#[derive(Debug)]
pub enum ClientError {
    /// `FederationDeniedError`: the destination is invalid, or not in the
    /// whitelist.
    Denied,
    /// `NotRetryingDestination`.
    NotRetrying(NotRetrying),
    /// `RequestSendFailed`.
    SendFailed {
        cause: FailureCause,
        can_retry: bool,
    },
    /// `HttpResponseException`, for an error response that is not retried.
    Http(HttpError),
    /// `ValueError`: the request could not be built.
    InvalidRequest(String),
    /// Reading the backoff state failed.
    Store(anyhow::Error),
    /// The client was shut down before the request was sent.
    Shutdown,
}

impl ClientError {
    fn send_failed(cause: FailureCause, can_retry: bool) -> Self {
        ClientError::SendFailed { cause, can_retry }
    }
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::Denied => write!(f, "federation denied"),
            ClientError::NotRetrying(nr) => write!(
                f,
                "not retrying: retry_last_ts={} retry_interval={}",
                nr.retry_last_ts, nr.retry_interval
            ),
            ClientError::SendFailed { cause, .. } => write!(f, "{cause}"),
            ClientError::Http(err) => write!(f, "{}: {}", err.code, err.phrase),
            ClientError::InvalidRequest(err) => write!(f, "invalid request: {err}"),
            ClientError::Store(err) => write!(f, "reading the backoff state: {err:#}"),
            ClientError::Shutdown => write!(f, "the federation client is shut down"),
        }
    }
}

/// What happened on each attempt, for the request and response counters.
/// They are Python counters, so the caller bumps them when it next holds
/// the GIL anyway.
#[derive(Debug, Default)]
pub struct RequestStats {
    pub attempts: u32,
    /// The status of each response received.
    pub responses: Vec<u16>,
}

/// A 2xx response, with the body still to read.
pub struct SentResponse {
    /// The request's id in the logs.
    pub txn_id: String,
    /// `METHOD matrix-federation://destination/path?query`, for logs.
    pub description: String,
    pub status: StatusCode,
    pub phrase: String,
    pub headers: HeaderMap,
    pub body: ResponseBody,
    /// When the request started, in milliseconds since the epoch.
    pub start_ms: u64,
}

pub struct FederationClient {
    config: ClientConfig,
    transport: Arc<dyn FederationTransport>,
    retry_timings: Arc<dyn RetryTimingsStore>,
    /// For timeouts.
    sleeper: Arc<dyn Sleeper>,
    /// For the sleep between retries.
    retry_sleeper: AwakenableSleeper,
    now: Now,
    /// The jitter for retry delays and backoff growth: U(0.8, 1.4).
    jitter: Box<dyn Fn() -> f64 + Send + Sync>,
    is_shutdown: AtomicBool,
    next_id: AtomicU64,
}

impl FederationClient {
    pub fn new(
        config: ClientConfig,
        transport: Arc<dyn FederationTransport>,
        retry_timings: Arc<dyn RetryTimingsStore>,
        sleeper: Arc<dyn Sleeper>,
        now: Now,
    ) -> Self {
        FederationClient {
            config,
            transport,
            retry_timings,
            retry_sleeper: AwakenableSleeper::new(Arc::clone(&sleeper)),
            sleeper,
            now,
            jitter: Box::new(|| rand::rng().random_range(0.8..1.4)),
            is_shutdown: AtomicBool::new(false),
            next_id: AtomicU64::new(1),
        }
    }

    /// Fix the jitter, for tests.
    #[cfg(test)]
    fn with_jitter(mut self, jitter: f64) -> Self {
        self.jitter = Box::new(move || jitter);
        self
    }

    pub fn config(&self) -> &ClientConfig {
        &self.config
    }

    pub fn now_ms(&self) -> u64 {
        (self.now)()
    }

    /// Stop retrying. Requests already sleeping before a retry fail, and new
    /// ones are not sent.
    pub fn shutdown(&self) {
        self.is_shutdown.store(true, Ordering::Relaxed);
    }

    /// Whether we are backing off from `destination`: the check of
    /// `get_retry_limiter`, which the transmission loop makes before it does
    /// anything.
    pub async fn backoff(&self, destination: &str) -> Result<Option<NotRetrying>, ClientError> {
        let checked = RetryLimiter::check(
            &*self.retry_timings,
            destination,
            self.now_ms(),
            LimiterOptions::default(),
        )
        .await
        .map_err(ClientError::Store)?;
        Ok(checked.err())
    }

    /// The destination may be back: end any sleep before a retry to it.
    pub fn wake_destination(&self, destination: &str) {
        self.retry_sleeper.wake(destination);
    }

    /// Send a request, with the trailing slash retry if asked for:
    /// `_send_request_with_optional_trailing_slash`.
    pub async fn send(
        &self,
        request: &FederationRequest,
        options: &RequestOptions,
        stats: &mut RequestStats,
    ) -> Result<SentResponse, ClientError> {
        match self.send_request(request, options, stats).await {
            // Synapse 0.99.3 and older answer some endpoints with a 400
            // `M_UNRECOGNIZED` when the path lacks a trailing slash.
            Err(ClientError::Http(err))
                if options.try_trailing_slash_on_400
                    && err.code == 400
                    && err.errcode().as_deref() == Some("M_UNRECOGNIZED") =>
            {
                log::info!("Retrying request with trailing slash");
                let mut request = request.clone();
                request.path.push('/');
                self.send_request(&request, options, stats).await
            }
            other => other,
        }
    }

    /// `_send_request`: one request, with retries and the backoff.
    async fn send_request(
        &self,
        request: &FederationRequest,
        options: &RequestOptions,
        stats: &mut RequestStats,
    ) -> Result<SentResponse, ClientError> {
        let destination = request.destination.as_str();
        if !is_valid_server_name(destination) {
            log::error!("Invalid destination: {destination}.");
            return Err(ClientError::Denied);
        }
        if let Some(whitelist) = &self.config.domain_whitelist {
            if !whitelist.contains(destination) {
                return Err(ClientError::Denied);
            }
        }

        let limiter = RetryLimiter::check(
            &*self.retry_timings,
            destination,
            self.now_ms(),
            LimiterOptions {
                ignore_backoff: options.ignore_backoff,
                backoff_on_404: options.backoff_on_404,
                backoff_on_all_error_codes: options.backoff_on_all_error_codes,
            },
        )
        .await
        .map_err(ClientError::Store)?
        .map_err(ClientError::NotRetrying)?;

        // Python encodes these as ASCII after the backoff check and before
        // entering the limiter, so this failure records nothing.
        if !request.path.is_ascii() || !request.query.is_ascii() {
            return Err(ClientError::InvalidRequest(
                "the path and query must be ASCII".to_owned(),
            ));
        }

        let result = self.send_with_retries(request, options, stats).await;

        let outcome = match &result {
            Ok(_) => Outcome::Success,
            Err(ClientError::Http(err)) => Outcome::HttpError(err.code),
            // Python fails with an `UnboundLocalError` here, which counts as
            // a failure. Nothing will write the result during shutdown, so
            // there is no point recording one.
            Err(ClientError::Shutdown) => return result,
            Err(_) => Outcome::Failure,
        };
        let now = self.now_ms();
        if let Some(update) = limiter.finish(outcome, now, &self.config.backoff, (self.jitter)()) {
            if update.retry_interval == 0 {
                log::debug!("Connection to {destination} was successful; clearing backoff");
            } else {
                let err = result.as_ref().err().map(ToString::to_string);
                log::info!(
                    "Connection to {destination} was unsuccessful ({}); backoff now {}",
                    err.unwrap_or_default(),
                    update.retry_interval,
                );
            }
            self.retry_timings.set(destination, update);
        }

        result
    }

    /// The retry loop inside the limiter.
    /// Python's `outgoing-federation-request` span covers the request, all
    /// its attempts included.
    async fn send_with_retries(
        &self,
        request: &FederationRequest,
        options: &RequestOptions,
        stats: &mut RequestStats,
    ) -> Result<SentResponse, ClientError> {
        if options.traced_by_caller {
            return self
                .send_with_retries_in_span(request, options, stats, &[])
                .await;
        }
        let span = tracing::info_span!(
            "outgoing-federation-request",
            span.kind = "client",
            peer.address = request.destination.as_str(),
            http.method = request.method.as_str(),
            http.url = request.path.as_str(),
            http.status_code = tracing::field::Empty,
            error = tracing::field::Empty,
        );
        // As `inject_header_dict`: only to whitelisted servers, and not if
        // the caller sent Python's context already.
        let trace_headers = if whitelisted_homeserver(&request.destination)
            && !request
                .extra_headers
                .iter()
                .any(|(name, _)| name.eq_ignore_ascii_case("uber-trace-id"))
        {
            inject_headers(&span)
        } else {
            Vec::new()
        };
        let result = self
            .send_with_retries_in_span(request, options, stats, &trace_headers)
            .instrument(span.clone())
            .await;
        if let Err(err) = &result {
            record_error(&span, err);
        }
        result
    }

    async fn send_with_retries_in_span(
        &self,
        request: &FederationRequest,
        options: &RequestOptions,
        stats: &mut RequestStats,
        trace_headers: &[(String, String)],
    ) -> Result<SentResponse, ClientError> {
        let destination = request.destination.as_str();
        let txn_id = format!(
            "{}-O-{}",
            request.method,
            self.next_id.fetch_add(1, Ordering::Relaxed)
        );
        let path_and_query = if request.query.is_empty() {
            request.path.clone()
        } else {
            format!("{}?{}", request.path, request.query)
        };
        let uri = format!("matrix-federation://{destination}{path_and_query}");
        let description = format!("{} {uri}", request.method);
        let timeout = options.timeout.unwrap_or(self.config.default_timeout);
        let start_ms = self.now_ms();

        let mut retries_left = if options.long_retries {
            self.config.max_long_retries
        } else {
            self.config.max_short_retries
        };

        // Without the age rewrite, the body is the same on every attempt.
        let fixed_body = if request.rewrite_age_ts {
            None
        } else {
            Some(canonical_body(request.body.as_ref())?)
        };

        loop {
            if self.is_shutdown.load(Ordering::Relaxed) {
                return Err(ClientError::Shutdown);
            }

            let body = match &fixed_body {
                Some(body) => body.clone(),
                None => {
                    let body = request.body.clone().map(|mut body| {
                        rewrite_age_ts(&mut body, self.now_ms());
                        body
                    });
                    canonical_body(body.as_ref())?
                }
            };

            let http_request =
                self.build_request(request, &uri, &path_and_query, body, trace_headers)?;

            log::debug!(
                "{{{txn_id}}} [{destination}] Sending request: {description}; timeout {}s",
                timeout.as_secs_f64()
            );

            let log_prefix = format!("{{{txn_id}}} [{destination}]");
            match self
                .attempt(
                    http_request,
                    timeout,
                    options.retry_on_dns_fail,
                    &log_prefix,
                    stats,
                )
                .await
            {
                Ok((status, phrase, headers, body)) => {
                    log::debug!(
                        "{{{txn_id}}} [{destination}] Got response headers: {} {phrase}",
                        status.as_u16()
                    );
                    return Ok(SentResponse {
                        txn_id,
                        description,
                        status,
                        phrase,
                        headers,
                        body,
                        start_ms,
                    });
                }
                Err(ClientError::SendFailed { cause, can_retry }) => {
                    log::info!(
                        "{{{txn_id}}} [{destination}] Request failed: {description}: {cause}"
                    );
                    tracing::info!(message = "request failed", error = %cause, retries_left);

                    let err = ClientError::SendFailed { cause, can_retry };
                    // Python retries only when the caller did not set a
                    // timeout, and treats a zero timeout as unset here.
                    let explicit_timeout = options.timeout.is_some_and(|t| !t.is_zero());
                    if !can_retry || retries_left == 0 || explicit_timeout {
                        return Err(err);
                    }

                    let delay = self.retry_delay(options.long_retries, retries_left);
                    log::debug!(
                        "{{{txn_id}}} [{destination}] Waiting {}s before re-sending...",
                        delay.as_secs_f64()
                    );

                    // Fail at once rather than start a long sleep after
                    // shutdown was asked for.
                    if self.is_shutdown.load(Ordering::Relaxed) {
                        return Err(err);
                    }

                    // Sleep, or wake early if the server comes back.
                    self.retry_sleeper.sleep(destination, delay).await;
                    retries_left -= 1;
                }
                Err(err) => {
                    log::warn!("{{{txn_id}}} [{destination}] Request failed: {description}: {err}");
                    return Err(err);
                }
            }
        }
    }

    /// The delay before the next retry, as Python computes it: long retries
    /// grow as 4^n up to `max_long_retry_delay`, short ones as 0.5 * 2^n up
    /// to `max_short_retry_delay`, then jitter.
    fn retry_delay(&self, long_retries: bool, retries_left: u32) -> Duration {
        let seconds = if long_retries {
            let n = self.config.max_long_retries + 1 - retries_left;
            4f64.powi(n as i32)
                .min(self.config.max_long_retry_delay.as_secs_f64())
        } else {
            let n = self.config.max_short_retries - retries_left;
            (0.5 * 2f64.powi(n as i32)).min(self.config.max_short_retry_delay.as_secs_f64())
        };
        Duration::from_secs_f64(seconds * (self.jitter)())
    }

    fn build_request(
        &self,
        request: &FederationRequest,
        uri: &str,
        path_and_query: &str,
        body: Option<CanonicalBody>,
        trace_headers: &[(String, String)],
    ) -> Result<Request<Bytes>, ClientError> {
        let invalid = |err: anyhow::Error| ClientError::InvalidRequest(format!("{err:#}"));

        let auth = make_auth_header(
            &self.config.signing_key,
            &self.config.server_name,
            request.method.as_str(),
            path_and_query,
            Destination::Server(&request.destination),
            body.as_ref(),
        )
        .map_err(invalid)?;

        let mut builder = Request::builder()
            .method(request.method.clone())
            .uri(uri)
            .header(header::USER_AGENT, self.config.user_agent.clone())
            .header(header::AUTHORIZATION, auth);
        for (name, value) in request.extra_headers.iter().chain(trace_headers) {
            builder = builder.header(name.as_str(), value.as_str());
        }
        let body = match body {
            Some(body) => {
                builder = builder.header(header::CONTENT_TYPE, "application/json");
                body.into_bytes()
            }
            None => Bytes::new(),
        };

        builder
            .body(body)
            .context("building the request")
            .map_err(invalid)
    }

    /// One attempt: send, and turn an error response into the error Python
    /// raises for it.
    async fn attempt(
        &self,
        request: Request<Bytes>,
        timeout: Duration,
        retry_on_dns_fail: bool,
        log_prefix: &str,
        stats: &mut RequestStats,
    ) -> Result<(StatusCode, String, HeaderMap, ResponseBody), ClientError> {
        stats.attempts += 1;

        let response = match self
            .with_timeout(timeout, self.transport.send(request))
            .await
        {
            Some(Ok(response)) => response,
            Some(Err(err)) => {
                let can_retry = retry_on_dns_fail || !err.is_dns();
                return Err(ClientError::send_failed(
                    FailureCause::Transport(err),
                    can_retry,
                ));
            }
            None => {
                return Err(ClientError::send_failed(
                    FailureCause::Transport(TransportError::Timeout),
                    true,
                ))
            }
        };

        let (parts, body) = response.into_parts();
        stats.responses.push(parts.status.as_u16());
        // On the request's span, for every response, as Python sets it
        // before it raises for an error.
        tracing::Span::current().record("http.status_code", parts.status.as_u16());
        let phrase = reason_phrase(&parts);

        if parts.status.is_success() {
            return Ok((parts.status, phrase, parts.headers, body));
        }

        log::info!(
            "{log_prefix} Got response headers: {} {phrase}",
            parts.status.as_u16()
        );

        // Read the error body. If that fails we raise the error anyway, so a
        // failure here only loses the body.
        let mut error_body = BytesMut::new();
        let read = self
            .read_body(body, None, timeout, |chunk| {
                let room = MAX_ERROR_BODY_SIZE.saturating_sub(error_body.len());
                error_body.extend_from_slice(&chunk[..chunk.len().min(room)]);
                Ok(())
            })
            .await;
        if let Err(err) = read {
            log::warn!("{log_prefix} Failed to get error response: {err}");
            error_body.clear();
        }

        let err = HttpError {
            code: parts.status.as_u16(),
            phrase,
            body: error_body.freeze(),
        };

        // Retry a 5xx or a 429. Raise anything else as it is.
        if parts.status.is_server_error() || parts.status == StatusCode::TOO_MANY_REQUESTS {
            Err(ClientError::send_failed(FailureCause::Http(err), true))
        } else {
            Err(ClientError::Http(err))
        }
    }

    /// Read a response body, passing each chunk to `on_chunk`, with a
    /// timeout on the whole read and, if given, a size limit. As Python's
    /// `read_body_with_max_size`, a body that reaches the limit fails.
    pub async fn read_body(
        &self,
        body: ResponseBody,
        max_size: Option<usize>,
        timeout: Duration,
        mut on_chunk: impl FnMut(Bytes) -> Result<(), ClientError> + Send,
    ) -> Result<usize, ClientError> {
        let read = async {
            let mut body = body;
            let mut length = 0usize;
            while let Some(frame) = body.frame().await {
                let frame = frame.map_err(|err| {
                    ClientError::send_failed(FailureCause::BodyRead(err.to_string()), true)
                })?;
                let Ok(chunk) = frame.into_data() else {
                    // Trailers.
                    continue;
                };
                length += chunk.len();
                if let Some(limit) = max_size {
                    if length >= limit {
                        return Err(ClientError::send_failed(
                            FailureCause::TooLarge { limit },
                            false,
                        ));
                    }
                }
                on_chunk(chunk)?;
            }
            Ok(length)
        };

        match self.with_timeout(timeout, read).await {
            Some(result) => result,
            None => Err(ClientError::send_failed(FailureCause::BodyTimeout, true)),
        }
    }

    /// `fut`, or `None` if it takes longer than `timeout` on the client's
    /// clock.
    async fn with_timeout<T>(
        &self,
        timeout: Duration,
        fut: impl std::future::Future<Output = T>,
    ) -> Option<T> {
        tokio::select! {
            result = fut => Some(result),
            () = self.sleeper.sleep(timeout) => None,
        }
    }
}

/// `check_content_type_is`.
pub(crate) fn check_content_type(headers: &HeaderMap, expected: &str) -> Result<(), ClientError> {
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

/// The reason phrase the server sent, else the standard one.
fn reason_phrase(parts: &http::response::Parts) -> String {
    if let Some(phrase) = parts.extensions.get::<hyper::ext::ReasonPhrase>() {
        return String::from_utf8_lossy(phrase.as_bytes()).into_owned();
    }
    parts
        .status
        .canonical_reason()
        .unwrap_or_default()
        .to_owned()
}

/// The body to send, as canonical JSON, or `None` for a body that Python
/// would treat as false.
fn canonical_body(body: Option<&Value>) -> Result<Option<CanonicalBody>, ClientError> {
    let Some(body) = body.filter(|body| is_truthy(body)) else {
        return Ok(None);
    };
    CanonicalBody::new(body)
        .map(Some)
        .map_err(|err| ClientError::InvalidRequest(format!("{err:#}")))
}

/// Python's truthiness of a JSON value.
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The transaction manager's `json_data_callback`: move each PDU's top-level
/// `age_ts` into `unsigned.age`, relative to now. Python would raise on an
/// `age_ts` that `int()` rejects. We leave such a PDU alone.
fn rewrite_age_ts(body: &mut Value, now_ms: u64) {
    let Some(pdus) = body.get_mut("pdus").and_then(Value::as_array_mut) else {
        return;
    };
    for pdu in pdus {
        let Some(pdu) = pdu.as_object_mut() else {
            continue;
        };
        let Some(age_ts) = pdu.get("age_ts").and_then(|ts| {
            ts.as_i64()
                .or_else(|| ts.as_f64().map(|f| f.trunc() as i64))
        }) else {
            continue;
        };
        pdu.remove("age_ts");
        let unsigned = pdu
            .entry("unsigned")
            .or_insert_with(|| Value::Object(Default::default()));
        if let Some(unsigned) = unsigned.as_object_mut() {
            unsigned.insert("age".to_owned(), Value::from(now_ms as i64 - age_ts));
        }
    }
}

#[cfg(test)]
pub(crate) mod tests;
