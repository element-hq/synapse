/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2025 Element Creations, Ltd
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

use bytes::Bytes;
use headers::{
    AccessControlAllowHeaders, AccessControlAllowMethods, AccessControlAllowOrigin, HeaderMapExt,
};
use http::header::HeaderName;
use http::{header, HeaderMap, Method, Response, StatusCode};
use pyo3::{
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, IntoPyObject, Py, PyAny, PyObject, PyResult, Python,
};
use ulid::Ulid;

use self::session::Session;
use crate::{
    errors::{NotFoundError, SynapseError},
    http::{http_request_from_twisted, http_response_to_twisted},
    UnwrapInfallible,
};

mod session;

// Annoyingly we need to set the normal CORS headers on every response as the Python layer doesn't do it for us.
// List is taken from https://spec.matrix.org/v1.16/client-server-api/#web-browser-clients
fn prepare_headers(headers: &mut HeaderMap) {
    headers.typed_insert(AccessControlAllowOrigin::ANY);
    headers.typed_insert(AccessControlAllowMethods::from_iter([
        Method::POST,
        Method::GET,
        Method::PUT,
        Method::DELETE,
        Method::OPTIONS,
    ]));
    headers.typed_insert(AccessControlAllowHeaders::from_iter([
        HeaderName::from_static("x-requested-with"),
        header::CONTENT_TYPE,
        header::AUTHORIZATION,
    ]));
}

#[pyclass]
struct MSC4108v2025RendezvousHandler {
    clock: PyObject,
    sessions: BTreeMap<Ulid, Session>,
    capacity: usize,
    max_content_length: u64,
    ttl: Duration,
}

impl MSC4108v2025RendezvousHandler {
    /// Check the length of the data parameter and throw error if invalid.
    fn check_data_length(&self, data: &str) -> PyResult<()> {
        let data_length = data.len() as u64;
        if data_length > self.max_content_length {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            return Err(SynapseError::new(
                StatusCode::PAYLOAD_TOO_LARGE,
                "Payload too large".to_owned(),
                "M_TOO_LARGE",
                None,
                Some(headers),
            ));
        }
        Ok(())
    }

    /// Evict expired sessions and remove the oldest sessions until we're under the capacity.
    fn evict(&mut self, now: SystemTime) {
        // First remove all the entries which expired
        self.sessions.retain(|_, session| !session.expired(now));

        // Then we remove the oldest entries until we're under the limit
        while self.sessions.len() > self.capacity {
            self.sessions.pop_first();
        }
    }
}

#[pymethods]
impl MSC4108v2025RendezvousHandler {
    #[new]
    #[pyo3(signature = (homeserver, /, capacity=100, max_content_length=4*1024, eviction_interval=60*1000, ttl=2*60*1000))]
    fn new(
        py: Python<'_>,
        homeserver: &Bound<'_, PyAny>,
        capacity: usize,
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
                capacity,
                max_content_length,
                ttl: Duration::from_millis(ttl),
            },
        )?;

        let evict = self_.getattr(py, "_evict")?;
        homeserver.call_method0("get_clock")?.call_method(
            "looping_call",
            (evict, eviction_interval),
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

    fn handle_post(&mut self, py: Python<'_>, twisted_request: &Bound<'_, PyAny>) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        let clock = self.clock.bind(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        // We trigger an immediate eviction if we're at 2x the capacity
        if self.sessions.len() >= self.capacity * 2 {
            self.evict(now);
        }

        // Generate a new ULID for the session from the current time.
        let id = Ulid::from_datetime(now);

        // parse JSON body out of request
        let json: serde_json::Value =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                let mut headers = HeaderMap::new();
                prepare_headers(&mut headers);

                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    Some(headers),
                )
            })?;

        let data: String = json["data"].as_str().map(|s| s.to_owned()).ok_or_else(|| {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            SynapseError::new(
                StatusCode::BAD_REQUEST,
                "Missing 'data' field in JSON body".to_owned(),
                "M_INVALID_PARAM",
                None,
                Some(headers),
            )
        })?;

        self.check_data_length(&data)?;

        let session = Session::new(id, data, now, self.ttl);

        let response_body = serde_json::to_string(&session.post_response()).map_err(|_| {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            SynapseError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to serialize response".to_owned(),
                "M_UNKNOWN",
                None,
                Some(headers),
            )
        })?;
        let mut response = Response::new(response_body.as_bytes());
        *response.status_mut() = StatusCode::OK;
        let headers = response.headers_mut();
        prepare_headers(headers);
        http_response_to_twisted(twisted_request, response)?;

        self.sessions.insert(id, session);

        Ok(())
    }

    fn handle_get(
        &mut self,
        py: Python<'_>,
        twisted_request: &Bound<'_, PyAny>,
        id: &str,
    ) -> PyResult<()> {
        let now: u64 = self.clock.call_method0(py, "time_msec")?.extract(py)?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self
            .sessions
            .get(&id)
            .filter(|s| !s.expired(now))
            .ok_or_else(NotFoundError::new)?;

        let response_body = serde_json::to_string(&session.get_response()).map_err(|_| {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            SynapseError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to serialize response".to_owned(),
                "M_UNKNOWN",
                None,
                Some(headers),
            )
        })?;
        let mut response = Response::new(response_body.as_bytes());
        *response.status_mut() = StatusCode::OK;
        prepare_headers(response.headers_mut());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_put(
        &mut self,
        py: Python<'_>,
        twisted_request: &Bound<'_, PyAny>,
        id: &str,
    ) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        // parse JSON body out of request
        let json: serde_json::Value =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                let mut headers = HeaderMap::new();
                prepare_headers(&mut headers);

                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    Some(headers),
                )
            })?;

        let sequence_token: String = json["sequence_token"]
            .as_str()
            .map(|s| s.to_owned())
            .ok_or_else(|| {
                let mut headers = HeaderMap::new();
                prepare_headers(&mut headers);

                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Missing 'sequence_token' field in JSON body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    Some(headers),
                )
            })?;

        let data: String = json["data"].as_str().map(|s| s.to_owned()).ok_or_else(|| {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            SynapseError::new(
                StatusCode::BAD_REQUEST,
                "Missing 'data' field in JSON body".to_owned(),
                "M_INVALID_PARAM",
                None,
                Some(headers),
            )
        })?;

        self.check_data_length(&data)?;

        let now: u64 = self.clock.call_method0(py, "time_msec")?.extract(py)?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self
            .sessions
            .get_mut(&id)
            .filter(|s| !s.expired(now))
            .ok_or_else(NotFoundError::new)?;

        if !session.sequence_token().eq(&sequence_token) {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers);

            return Err(SynapseError::new(
                StatusCode::CONFLICT,
                "sequence_token does not match".to_owned(),
                "IO_ELEMENT_MSC4108_CONCURRENT_WRITE",
                None,
                Some(headers),
            ));
        }

        session.update(data, now);

        let response_body = serde_json::to_string(&session.put_response()).map_err(|_| {
            SynapseError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Failed to serialize response".to_owned(),
                "M_UNKNOWN",
                None,
                None,
            )
        })?;
        let mut response = Response::new(response_body.as_bytes());
        *response.status_mut() = StatusCode::OK;
        prepare_headers(response.headers_mut());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_delete(&mut self, twisted_request: &Bound<'_, PyAny>, id: &str) -> PyResult<()> {
        let _request = http_request_from_twisted(twisted_request)?;

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let _session = self.sessions.remove(&id).ok_or_else(NotFoundError::new)?;

        let mut response = Response::new(Bytes::new());
        *response.status_mut() = StatusCode::OK;
        prepare_headers(response.headers_mut());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "msc4108v2025_rendezvous")?;

    child_module.add_class::<MSC4108v2025RendezvousHandler>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import rendezvous` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.msc4108v2025_rendezvous", child_module)?;

    Ok(())
}
