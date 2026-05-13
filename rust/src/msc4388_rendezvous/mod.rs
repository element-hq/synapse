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
 */

use std::{
    collections::BTreeMap,
    time::{Duration, SystemTime},
};

use http::StatusCode;
use pyo3::{
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, IntoPyObject, Py, PyAny, PyResult, Python,
};
use serde::Deserialize;
use ulid::Ulid;

use self::session::Session;
use crate::{
    duration::SynapseDuration,
    errors::{NotFoundError, SynapseError},
    http::http_request_from_twisted,
    msc4388_rendezvous::session::{GetResponse, PostResponse, PutResponse},
    UnwrapInfallible,
};

mod session;

#[pyclass]
struct MSC4388RendezvousHandler {
    clock: Py<PyAny>,
    sessions: BTreeMap<Ulid, Session>,
    soft_limit: usize,
    hard_limit: usize,
    max_content_length: u64,
    ttl: Duration,
}

impl MSC4388RendezvousHandler {
    /// Check the length of the data parameter and throw error if invalid.
    fn check_data_length(&self, data: &str) -> PyResult<()> {
        let data_length = data.len() as u64;
        if data_length > self.max_content_length {
            return Err(SynapseError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Payload too large".to_owned(),
                "M_TOO_LARGE",
                None,
                None,
            ));
        }
        Ok(())
    }

    /// Evict expired sessions and remove the oldest sessions until we're under the capacity.
    fn evict(&mut self, now: SystemTime) {
        // First remove all the entries which expired
        self.sessions.retain(|_, session| !session.expired(now));

        // Then we remove the oldest entries until we're under the soft limit
        while self.sessions.len() > self.soft_limit {
            self.sessions.pop_first();
        }
    }
}

#[derive(Deserialize)]
pub struct PostRequest {
    data: String,
}

#[derive(Deserialize)]
pub struct PutRequest {
    sequence_token: String,
    data: String,
}

#[pymethods]
impl MSC4388RendezvousHandler {
    #[new]
    #[pyo3(signature = (homeserver, /, soft_limit=100, hard_limit=200,max_content_length=4*1024, eviction_interval=60*1000, ttl=2*60*1000))]
    fn new(
        py: Python<'_>,
        homeserver: &Bound<'_, PyAny>,
        soft_limit: usize,
        hard_limit: usize,
        max_content_length: u64,
        eviction_interval: u64,
        ttl: u64,
    ) -> PyResult<Py<Self>> {
        let clock = homeserver
            .call_method0("get_clock")?
            .into_pyobject(py)
            .unwrap_infallible()
            .unbind();

        // Construct a Python object so that we can get a reference to the
        // evict method and schedule it to run.
        let self_ = Py::new(
            py,
            Self {
                clock,
                sessions: BTreeMap::new(),
                soft_limit,
                hard_limit,
                max_content_length,
                ttl: Duration::from_millis(ttl),
            },
        )?;

        let eviction_duration = SynapseDuration::from_milliseconds(eviction_interval);

        let evict = self_.getattr(py, "_evict")?;
        homeserver.call_method0("get_clock")?.call_method(
            "looping_call",
            (evict, &eviction_duration),
            None,
        )?;

        Ok(self_)
    }

    fn _evict(&mut self, py: Python<'_>) -> PyResult<()> {
        let clock = self.clock.bind(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);
        self.evict(now);

        Ok(())
    }

    fn handle_post(
        &mut self,
        py: Python<'_>,
        twisted_request: &Bound<'_, PyAny>,
    ) -> PyResult<(u8, PostResponse)> {
        let clock = self.clock.bind(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        // We trigger an immediate eviction if we're at the hard limit
        if self.sessions.len() >= self.hard_limit {
            self.evict(now);
        }

        // Generate a new ULID for the session from the current time.
        let id = Ulid::from_datetime(now);

        let request = http_request_from_twisted(twisted_request)?;
        // parse JSON body
        let post_request: PostRequest =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    None,
                )
            })?;

        let data: String = post_request.data;
        self.check_data_length(&data)?;

        let session = Session::new(id, data, now, self.ttl);
        let response = session.post_response(now);
        self.sessions.insert(id, session);

        Ok((200, response))
    }

    fn handle_get(
        &mut self,
        py: Python<'_>,
        id: &str,
        twisted_request: &Bound<'_, PyAny>,
    ) -> PyResult<(u8, GetResponse)> {
        let request = http_request_from_twisted(twisted_request)?;

        // As per the MSC, we check the Sec-Fetch-* headers to ensure this request did not come from somewhere that will
        // be rendered directly to the user, as the response may contain sensitive data. These headers are added by
        // well behaved browsers so are helpful for protecting regular users.

        // Sec-Fetch-Dest: https://www.w3.org/TR/fetch-metadata/#sec-fetch-dest-header
        //
        // If the header is present then this must be "empty". All other values such as document, image etc.
        // are considered potentially dangerous as they might be rendered to the user.
        //
        // Note that because we only ever return JSON, so it is unlikely that it could somehow be rendered as an image,
        // video or other media.
        let sec_fetch_dest: Option<String> = request
            .headers()
            .get("sec-fetch-dest")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_owned());
        if sec_fetch_dest.is_some() && sec_fetch_dest.as_deref() != Some("empty") {
            return Err(SynapseError::new(
                StatusCode::FORBIDDEN,
                "Rendezvous content is not accessible from the request destination".to_owned(),
                "M_FORBIDDEN",
                None,
                None,
            ));
        }

        // Sec-Fetch-Mode: https://www.w3.org/TR/fetch-metadata/#sec-fetch-mode-header
        //
        // A request mode of "navigate" is not allowed as this indicates the request is being made by the
        // browser to navigate to a URL, which could lead to the response being rendered directly to the user.
        //
        // Note that usually Sec-Fetch-Dest would be "document" in this case and so the request would be rejected earlier,
        // but we check the mode just in case the destination is not set correctly.
        let sec_fetch_mode: Option<String> = request
            .headers()
            .get("sec-fetch-mode")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_owned());
        if sec_fetch_mode.as_deref() == Some("navigate") {
            return Err(SynapseError::new(
                StatusCode::FORBIDDEN,
                "Rendezvous content is not accessible via top-level navigation".to_owned(),
                "M_FORBIDDEN",
                None,
                None,
            ));
        }

        // Sec-Fetch-User: https://www.w3.org/TR/fetch-metadata/#sec-fetch-user-header
        //
        // If the request has a Sec-Fetch-User header with a value of "?1", this indicates that the
        // request was triggered by user activation, such as a click.
        //
        // Note that usually Sec-Fetch-Mode would be "navigate" or the Sec-Fetch-Dest would be "document" in this case
        // and so the request would be rejected earlier, but we check the user activation just in case those headers are
        // not set correctly.
        let sec_fetch_user: Option<String> = request
            .headers()
            .get("sec-fetch-user")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_owned());
        if sec_fetch_user.as_deref() == Some("?1") {
            return Err(SynapseError::new(
                StatusCode::FORBIDDEN,
                "Rendezvous content is not accessible from requests with user activation"
                    .to_owned(),
                "M_FORBIDDEN",
                None,
                None,
            ));
        }

        // Sec-Fetch-Site: https://www.w3.org/TR/fetch-metadata/#sec-fetch-site-header
        //
        // "none" indicates the request did not originate from a web page
        // (e.g. typed URL, bookmark, or browser extension), so we disallow it.
        let sec_fetch_site: Option<String> = request
            .headers()
            .get("sec-fetch-site")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_owned());
        if sec_fetch_site.as_deref() == Some("none") {
            return Err(SynapseError::new(
                StatusCode::FORBIDDEN,
                "Rendezvous content is not accessible from requests from user interaction"
                    .to_owned(),
                "M_FORBIDDEN",
                None,
                None,
            ));
        }

        let clock = self.clock.bind(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self
            .sessions
            .get(&id)
            .filter(|s| !s.expired(now))
            .ok_or_else(NotFoundError::new)?;

        Ok((200, session.get_response(now)))
    }

    fn handle_put(
        &mut self,
        py: Python<'_>,
        id: &str,
        twisted_request: &Bound<'_, PyAny>,
    ) -> PyResult<(u8, PutResponse)> {
        let request = http_request_from_twisted(twisted_request)?;
        // parse JSON body
        let put_request: PutRequest =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    None,
                )
            })?;

        let sequence_token: String = put_request.sequence_token;

        let data: String = put_request.data;

        self.check_data_length(&data)?;

        let clock = self.clock.bind(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self
            .sessions
            .get_mut(&id)
            .filter(|s| !s.expired(now))
            .ok_or_else(NotFoundError::new)?;

        if !session.sequence_token().eq(&sequence_token) {
            return Err(SynapseError::new(
                StatusCode::CONFLICT,
                "sequence_token does not match".to_owned(),
                "IO_ELEMENT_MSC4388_CONCURRENT_WRITE",
                None,
                None,
            ));
        }

        session.update(data, now);

        Ok((200, session.put_response()))
    }

    fn handle_delete(&mut self, id: &str) -> PyResult<(u8, ())> {
        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let _session = self.sessions.remove(&id).ok_or_else(NotFoundError::new)?;

        Ok((200, ()))
    }
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "msc4388_rendezvous")?;

    child_module.add_class::<MSC4388RendezvousHandler>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import rendezvous` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.msc4388_rendezvous", child_module)?;

    Ok(())
}
