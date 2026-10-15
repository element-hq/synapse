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

//! The outbound federation agent: Python's `MatrixFederationAgent` and its
//! `HTTPConnectionPool`.
//!
//! [`Agent::request`] takes a `matrix-federation://<server name>/<path>`
//! request, applies `.well-known` delegation (rewriting the URI and the
//! `Host` header), and sends it through a hyper connection pool whose
//! [`connect::FederationConnector`] does SRV resolution, the IP range policy,
//! the proxy and TLS. It does no retries, signing or response parsing: those
//! belong to the request layer above.
//!
//! - [`resolve`]: SRV lookups and the order to try servers in.
//! - [`well_known`]: `.well-known` delegation and its cache.
//! - [`policy`]: the IP range lists and the proxy.
//! - [`tls`]: certificate verification.
//! - [`connect`]: opening connections for the pool.

use std::{collections::HashMap, sync::Arc, time::Duration};

use anyhow::{anyhow, Context as _};
use async_trait::async_trait;
use bytes::Bytes;
use http::{header, HeaderValue, Request, Response, Uri};
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Incoming;
use hyper_util::{
    client::legacy::Client,
    rt::{TokioExecutor, TokioTimer},
};

use crate::{
    clock::Sleeper,
    config::SynapseHomeServerConfig,
    federation::transport::{FederationTransport, ResponseBody, TransportError},
};

pub mod connect;
pub mod policy;
pub mod python;
pub mod resolve;
pub mod tls;
pub mod well_known;

use connect::{ConnectorInner, FederationConnector, MATRIX_FEDERATION_SCHEME};
use policy::{IpPolicy, Proxy};
use resolve::{parse_ip_literal, DnsResolver, ServerResolver};
use tls::TlsPolicy;
use well_known::{FetchError, FetchResponse, WellKnownFetcher, WellKnownResolver};

/// The current time in milliseconds since the epoch. Production reads the
/// homeserver's [`Clock`](crate::clock::Clock). Rust tests use their own,
/// since the virtual clock is process-global.
pub type Now = Arc<dyn Fn() -> u64 + Send + Sync>;

/// Idle connections kept per host, as Python's pool.
const MAX_IDLE_PER_HOST: usize = 5;
/// How long an idle connection is kept, as Python's pool.
const IDLE_TIMEOUT: Duration = Duration::from_secs(120);

/// Everything the agent needs to know, taken from the homeserver config.
#[derive(Clone)]
pub struct AgentConfig {
    pub user_agent: String,
    pub ip_policy: IpPolicy,
    pub tls: crate::config::TlsConfig,
    pub proxy: crate::config::types::ProxyConfig,
    /// The timeout for each `.well-known` fetch attempt.
    pub well_known_timeout: Duration,
}

impl AgentConfig {
    pub fn from_homeserver_config(config: &SynapseHomeServerConfig, user_agent: String) -> Self {
        AgentConfig {
            user_agent,
            ip_policy: IpPolicy::new(
                config.server.federation_ip_range_blocklist.clone(),
                config.server.federation_ip_range_allowlist.clone(),
            ),
            tls: config.tls.clone(),
            proxy: config.server.proxy_config.clone(),
            well_known_timeout: Duration::from_millis(config.federation.client_timeout_ms),
        }
    }
}

type PoolClient = Client<FederationConnector, Full<Bytes>>;

pub struct Agent {
    client: PoolClient,
    well_known: Arc<WellKnownResolver>,
    user_agent: HeaderValue,
}

impl Agent {
    /// Build an agent. `port_overrides` is for tests (see
    /// [`ConnectorInner::port_overrides`]) and is empty in production.
    pub fn new(
        config: AgentConfig,
        dns: Arc<dyn DnsResolver>,
        sleeper: Arc<dyn Sleeper>,
        now: Now,
        port_overrides: HashMap<u16, u16>,
    ) -> anyhow::Result<Self> {
        let user_agent = HeaderValue::from_str(&config.user_agent).context("invalid user agent")?;
        let ip_policy = config.ip_policy.clone();

        let connector = FederationConnector {
            inner: Arc::new(ConnectorInner {
                servers: ServerResolver::new(dns),
                ip_policy: config.ip_policy,
                tls: TlsPolicy::new(&config.tls)?,
                proxy: Proxy::from_config(&config.proxy)?,
                now: now.clone(),
                port_overrides,
            }),
        };

        let client = Client::builder(TokioExecutor::new())
            .pool_timer(TokioTimer::new())
            .pool_idle_timeout(IDLE_TIMEOUT)
            .pool_max_idle_per_host(MAX_IDLE_PER_HOST)
            // Python's pool does not retry either: the request layer decides.
            .retry_canceled_requests(false)
            .build(connector);

        // The `.well-known` fetch shares the pool, as in Python.
        let fetcher = Arc::new(PoolFetcher {
            client: client.clone(),
            user_agent: user_agent.clone(),
            ip_policy,
        });
        let well_known = Arc::new(WellKnownResolver::new(
            fetcher,
            sleeper,
            now,
            config.well_known_timeout,
        ));

        Ok(Agent {
            client,
            well_known,
            user_agent,
        })
    }

    /// Send one request and return the response head, with the body still
    /// to stream. The response is returned whatever its status.
    pub async fn request(
        &self,
        mut request: Request<Full<Bytes>>,
    ) -> Result<Response<Incoming>, TransportError> {
        let uri = request.uri().clone();
        let host = uri
            .host()
            .ok_or_else(|| TransportError::InvalidRequest(anyhow!("URI {uri} has no host")))?;

        // `.well-known` applies to a federation server name with no port
        // that is not an IP literal. It has to happen here rather than in
        // the connector, since it changes the `Host` header.
        if uri.scheme_str() == Some(MATRIX_FEDERATION_SCHEME)
            && uri.port().is_none()
            && parse_ip_literal(host).is_none()
        {
            if let Some(delegated) = self.well_known.get_well_known(host).await {
                let mut parts = uri.into_parts();
                parts.authority = Some(delegated.parse().map_err(|err| {
                    TransportError::InvalidRequest(anyhow!(
                        "bad delegated server {delegated:?}: {err}"
                    ))
                })?);
                *request.uri_mut() = Uri::from_parts(parts)
                    .map_err(|err| TransportError::InvalidRequest(anyhow!(err)))?;
            }
        }

        // The `Host` header is the (delegated) server name, with its port
        // if it has one. hyper would leave a non-default port off.
        if !request.headers().contains_key(header::HOST) {
            let authority = request
                .uri()
                .authority()
                .expect("checked above")
                .as_str()
                .to_owned();
            let value = HeaderValue::from_str(&authority)
                .map_err(|err| TransportError::InvalidRequest(anyhow!(err)))?;
            request.headers_mut().insert(header::HOST, value);
        }
        if !request.headers().contains_key(header::USER_AGENT) {
            request
                .headers_mut()
                .insert(header::USER_AGENT, self.user_agent.clone());
        }

        self.client
            .request(request)
            .await
            .map_err(from_client_error)
    }
}

#[async_trait]
impl FederationTransport for Agent {
    async fn send(
        &self,
        request: Request<Bytes>,
    ) -> Result<Response<ResponseBody>, TransportError> {
        let response = self.request(request.map(Full::new)).await?;
        Ok(response.map(|body| body.map_err(Into::into).boxed_unsync()))
    }
}

/// Recover our own error from a failed connect, else wrap hyper's.
fn from_client_error(err: hyper_util::client::legacy::Error) -> TransportError {
    let mut source = std::error::Error::source(&err);
    while let Some(inner) = source {
        if let Some(ours) = inner.downcast_ref::<TransportError>() {
            return ours.duplicate();
        }
        source = inner.source();
    }

    if err.is_connect() {
        TransportError::Connect(anyhow!(err))
    } else {
        TransportError::Request(anyhow!(err))
    }
}

/// Fetches `.well-known` files through the agent's pool.
struct PoolFetcher {
    client: PoolClient,
    user_agent: HeaderValue,
    ip_policy: IpPolicy,
}

#[async_trait]
impl WellKnownFetcher for PoolFetcher {
    async fn fetch(&self, uri: &Uri) -> Result<FetchResponse, FetchError> {
        // Python wraps this fetch in a `BlocklistingAgentWrapper` that is
        // given only the blocklist, so an IP literal (reachable through a
        // redirect) is blocked even if the allowlist covers it.
        if let Some(ip) = uri.host().and_then(parse_ip_literal) {
            if self.ip_policy.is_blocked_ignoring_allowlist(&ip) {
                return Err(FetchError::Other(anyhow!("IP address {ip} blocked")));
            }
        }

        let request = Request::get(uri.clone())
            .header(header::USER_AGENT, self.user_agent.clone())
            .body(Full::new(Bytes::new()))
            .map_err(|err| FetchError::Other(anyhow!(err)))?;

        let response = self
            .client
            .request(request)
            .await
            .map_err(|err| FetchError::Other(anyhow!(from_client_error(err))))?;

        let (parts, body) = response.into_parts();
        let body = match Limited::new(body, well_known::MAX_SIZE).collect().await {
            Ok(collected) => collected.to_bytes(),
            Err(err) if err.is::<http_body_util::LengthLimitError>() => {
                return Err(FetchError::TooLarge)
            }
            Err(err) => return Err(FetchError::Other(anyhow!(err))),
        };

        Ok(FetchResponse {
            status: parts.status,
            headers: parts.headers,
            body,
        })
    }
}
