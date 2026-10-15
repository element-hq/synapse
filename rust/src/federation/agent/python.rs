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

//! A Python handle on the agent, for tests only. Nothing in Synapse sends
//! through the agent yet: the request layer that will is the next step.

use std::{collections::HashMap, net::IpAddr, sync::Arc};

use http::{HeaderName, HeaderValue, Method, Request};
use http_body_util::{BodyExt, Full};
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
};
use tokio::runtime::Handle;

use super::{
    resolve::{InMemoryResolver, SrvRecord},
    Agent, AgentConfig,
};
use crate::{clock::Clock, homeserver::HomeServer};

/// What `request_blocking` returns: status, headers and body.
type BlockingResponse = (u16, Vec<(String, Vec<u8>)>, Vec<u8>);

/// An [`Agent`] built from the homeserver's config, resolving names from
/// fixed tables, with a blocking `request` method.
///
/// `request_blocking` blocks the calling thread (the reactor thread, under
/// trial) until the response arrives, so the server it talks to must run on
/// another thread. The agent sleeps on the virtual clock, so a request that
/// needs to sleep (a `.well-known` retry) would never finish. Tests avoid
/// those paths.
#[pyclass(frozen)]
pub struct RustFederationAgentForTest {
    agent: Arc<Agent>,
    tokio: Handle,
}

#[pymethods]
impl RustFederationAgentForTest {
    /// `srv_records` maps an SRV name to `(priority, weight, port, target)`
    /// tuples, `host_records` maps a host to its addresses, and
    /// `port_overrides` maps the ports resolution picks to the ports to
    /// really connect to.
    #[new]
    #[pyo3(signature = (hs, srv_records, host_records, port_overrides, user_agent = "Synapse-test".to_owned()))]
    fn py_new(
        py: Python<'_>,
        hs: HomeServer,
        srv_records: HashMap<String, Vec<(u16, u16, u16, String)>>,
        host_records: HashMap<String, Vec<String>>,
        port_overrides: HashMap<u16, u16>,
        user_agent: String,
    ) -> PyResult<Self> {
        let config = hs.config(py)?;
        let tokio = hs.get_rust_runtime(py)?.tokio_handle()?;

        let dns = InMemoryResolver::new();
        for (name, records) in srv_records {
            dns.add_srv(
                &name,
                records
                    .into_iter()
                    .map(|(priority, weight, port, target)| SrvRecord {
                        priority,
                        weight,
                        port,
                        target,
                        ttl: 3600,
                    })
                    .collect(),
            );
        }
        for (host, ips) in host_records {
            let ips = ips
                .iter()
                .map(|ip| ip.parse::<IpAddr>())
                .collect::<Result<Vec<_>, _>>()
                .map_err(|err| PyValueError::new_err(err.to_string()))?;
            dns.add_host(&host, ips);
        }

        let clock = Clock::new();
        let agent = Agent::new(
            AgentConfig::from_homeserver_config(&config, user_agent),
            Arc::new(dns),
            Arc::new(Clock::new()),
            Arc::new(move || clock.now_millis()),
            port_overrides,
        )?;

        Ok(RustFederationAgentForTest {
            agent: Arc::new(agent),
            tokio,
        })
    }

    /// Send a request and wait for the whole response. A failure raises
    /// `RuntimeError("<kind>: <message>")`, where `kind` is one of the
    /// `TransportError` kinds.
    #[pyo3(signature = (method, uri, headers = Vec::new(), body = Vec::new()))]
    fn request_blocking(
        &self,
        py: Python<'_>,
        method: &str,
        uri: &str,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    ) -> PyResult<BlockingResponse> {
        let mut request = Request::builder()
            .method(
                Method::from_bytes(method.as_bytes())
                    .map_err(|err| PyValueError::new_err(err.to_string()))?,
            )
            .uri(uri)
            .body(Full::new(bytes::Bytes::from(body)))
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
        for (name, value) in headers {
            request.headers_mut().append(
                HeaderName::from_bytes(name.as_bytes())
                    .map_err(|err| PyValueError::new_err(err.to_string()))?,
                HeaderValue::from_str(&value)
                    .map_err(|err| PyValueError::new_err(err.to_string()))?,
            );
        }

        let agent = self.agent.clone();
        let result = py.detach(|| {
            self.tokio.block_on(async move {
                let response = agent
                    .request(request)
                    .await
                    .map_err(|err| format!("{}: {err}", err.kind()))?;
                let (parts, body) = response.into_parts();
                let body = body
                    .collect()
                    .await
                    .map_err(|err| format!("request: reading body: {err}"))?
                    .to_bytes();
                Ok::<_, String>((parts, body))
            })
        });

        let (parts, body) = result.map_err(PyRuntimeError::new_err)?;
        let headers = parts
            .headers
            .iter()
            .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
            .collect();

        Ok((parts.status.as_u16(), headers, body.to_vec()))
    }
}
