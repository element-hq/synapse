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
    capacity: usize,
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

        // Then we remove the oldest entries until we're under the limit
        while self.sessions.len() > self.capacity {
            self.sessions.pop_first();
        }
    }
}

#[pymethods]
impl MSC4388RendezvousHandler {
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

        // We trigger an immediate eviction if we're at 2x the capacity
        if self.sessions.len() >= self.capacity * 2 {
            self.evict(now);
        }

        // Generate a new ULID for the session from the current time.
        let id = Ulid::from_datetime(now);

        let request = http_request_from_twisted(twisted_request)?;
        // parse JSON body
        let json: serde_json::Value =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    None,
                )
            })?;

        let data: String = json["data"].as_str().map(|s| s.to_owned()).ok_or_else(|| {
            SynapseError::new(
                StatusCode::BAD_REQUEST,
                "Missing 'data' field in JSON body".to_owned(),
                "M_INVALID_PARAM",
                None,
                None,
            )
        })?;

        self.check_data_length(&data)?;

        let session = Session::new(id, data, now, self.ttl);
        let response = session.post_response(now);
        self.sessions.insert(id, session);

        Ok((200, response))
    }

    fn handle_get(&mut self, py: Python<'_>, id: &str) -> PyResult<(u8, GetResponse)> {
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
        let json: serde_json::Value =
            serde_json::from_slice(&request.into_body()).map_err(|_| {
                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Invalid JSON in request body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    None,
                )
            })?;

        let sequence_token: String = json["sequence_token"]
            .as_str()
            .map(|s| s.to_owned())
            .ok_or_else(|| {
                SynapseError::new(
                    StatusCode::BAD_REQUEST,
                    "Missing 'sequence_token' field in JSON body".to_owned(),
                    "M_INVALID_PARAM",
                    None,
                    None,
                )
            })?;

        let data: String = json["data"].as_str().map(|s| s.to_owned()).ok_or_else(|| {
            SynapseError::new(
                StatusCode::BAD_REQUEST,
                "Missing 'data' field in JSON body".to_owned(),
                "M_INVALID_PARAM",
                None,
                None,
            )
        })?;

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
