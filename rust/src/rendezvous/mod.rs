/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2024 New Vector, Ltd
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

use std::{
    collections::HashMap,
    time::{Duration, SystemTime},
};

use bytes::Bytes;
use headers::{
    AccessControlAllowOrigin, AccessControlExposeHeaders, CacheControl, ContentLength, ContentType,
    HeaderMapExt, IfMatch, IfNoneMatch, Pragma,
};
use http::{header::ETAG, HeaderMap, Response, StatusCode, Uri};
use log::info;
use mime::Mime;
use pyo3::{
    exceptions::PyValueError, pyclass, pymethods, types::PyModule, Py, PyAny, PyObject, PyResult,
    Python, ToPyObject,
};
use ulid::Ulid;

use self::session::Session;
use crate::{
    errors::{NotFoundError, SynapseError},
    http::{http_request_from_twisted, http_response_to_twisted, HeaderMapPyExt},
};

mod session;

const MAX_CONTENT_LENGTH: u64 = 1024 * 100;

// n.b. Because OPTIONS requests are handled by the Python code, we don't need to set Access-Control-Allow-Headers.
fn prepare_headers(headers: &mut HeaderMap, session: &Session) {
    headers.typed_insert(AccessControlAllowOrigin::ANY);
    headers.typed_insert(AccessControlExposeHeaders::from_iter([ETAG]));
    headers.typed_insert(Pragma::no_cache());
    headers.typed_insert(CacheControl::new().with_no_store());
    headers.typed_insert(session.etag());
    headers.typed_insert(session.expires());
    headers.typed_insert(session.last_modified());
}

fn check_input_headers(headers: &HeaderMap) -> PyResult<Mime> {
    let ContentLength(content_length) = headers.typed_get_required()?;

    if content_length > MAX_CONTENT_LENGTH {
        return Err(SynapseError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "Payload too large".to_owned(),
            "M_TOO_LARGE",
            None,
            None,
        ));
    }

    let content_type: ContentType = headers.typed_get_required()?;

    Ok(content_type.into())
}

// TODO: handle eviction
#[pyclass]
struct RendezvousHandler {
    base: Uri,
    clock: PyObject,
    sessions: HashMap<Ulid, Session>,
}

#[pymethods]
impl RendezvousHandler {
    #[new]
    fn new(py: Python<'_>, homeserver: &PyAny) -> PyResult<Py<Self>> {
        let base: String = homeserver
            .getattr("config")?
            .getattr("server")?
            .getattr("public_baseurl")?
            .extract()?;
        let base = Uri::try_from(format!(
            "{base}_matrix/client/unstable/org.matrix.msc4108/rendezvous"
        ))
        .map_err(|_| PyValueError::new_err("Invalid base URI"))?;

        let clock = homeserver.call_method0("get_clock")?.to_object(py);

        // Construct a Python object so that we can get a reference to the
        // evict method and schedule it to run.
        let self_ = Py::new(
            py,
            Self {
                base,
                clock,
                sessions: HashMap::new(),
            },
        )?;

        let evict = self_.getattr(py, "_evict")?;
        homeserver
            .call_method0("get_clock")?
            .call_method("looping_call", (evict, 500), None)?;

        Ok(self_)
    }

    fn _evict(&mut self) {
        info!("Evicting sessions");
    }

    fn handle_post(&mut self, py: Python<'_>, twisted_request: &PyAny) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        let content_type = check_input_headers(request.headers())?;

        // Generate a new ULID for the session from the current time.
        let clock = self.clock.as_ref(py);
        let now: u64 = clock.call_method0("time_msec")?.extract()?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);
        let id = Ulid::from_datetime(now);

        let uri = format!("{base}/{id}", base = self.base);

        let body = request.into_body();

        let session = Session::new(body, content_type, now, Duration::from_secs(5 * 60));

        let response = serde_json::json!({
            "url": uri,
        })
        .to_string();

        let mut response = Response::new(response.as_bytes());
        *response.status_mut() = StatusCode::CREATED;
        response.headers_mut().typed_insert(ContentType::json());
        prepare_headers(response.headers_mut(), &session);
        http_response_to_twisted(twisted_request, response)?;

        self.sessions.insert(id, session);

        Ok(())
    }

    fn handle_get(&mut self, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        let if_none_match: Option<IfNoneMatch> = request.headers().typed_get_optional()?;

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self.sessions.get(&id).ok_or_else(NotFoundError::new)?;

        if let Some(if_none_match) = if_none_match {
            if !if_none_match.precondition_passes(&session.etag()) {
                let mut response = Response::new(Bytes::new());
                *response.status_mut() = StatusCode::NOT_MODIFIED;
                prepare_headers(response.headers_mut(), session);
                http_response_to_twisted(twisted_request, response)?;
                return Ok(());
            }
        }

        let mut response = Response::new(session.data());
        *response.status_mut() = StatusCode::OK;
        let headers = response.headers_mut();
        prepare_headers(headers, session);
        headers.typed_insert(session.content_type());
        headers.typed_insert(session.content_length());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_put(&mut self, py: Python<'_>, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        let content_type = check_input_headers(request.headers())?;

        let if_match: IfMatch = request.headers().typed_get_required()?;

        let data = request.into_body();

        let now: u64 = self.clock.call_method0(py, "time_msec")?.extract(py)?;
        let now = SystemTime::UNIX_EPOCH + Duration::from_millis(now);

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self.sessions.get_mut(&id).ok_or_else(NotFoundError::new)?;

        if !if_match.precondition_passes(&session.etag()) {
            let mut headers = HeaderMap::new();
            prepare_headers(&mut headers, session);

            return Err(SynapseError::new(
                StatusCode::PRECONDITION_FAILED,
                "ETag does not match".to_owned(),
                "M_CONCURRENT_WRITE",
                None,
                Some(headers),
            ));
        }

        session.update(data, content_type, now);

        let mut response = Response::new(Bytes::new());
        *response.status_mut() = StatusCode::ACCEPTED;
        prepare_headers(response.headers_mut(), session);
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_delete(&mut self, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let _request = http_request_from_twisted(twisted_request)?;

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let _session = self.sessions.remove(&id).ok_or_else(NotFoundError::new)?;

        let mut response = Response::new(Bytes::new());
        *response.status_mut() = StatusCode::NO_CONTENT;
        response
            .headers_mut()
            .typed_insert(AccessControlAllowOrigin::ANY);
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }
}

pub fn register_module(py: Python<'_>, m: &PyModule) -> PyResult<()> {
    let child_module = PyModule::new(py, "rendezvous")?;

    child_module.add_class::<RendezvousHandler>()?;

    m.add_submodule(child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import rendezvous` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.rendezvous", child_module)?;

    Ok(())
}
