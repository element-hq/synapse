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

use std::{collections::HashMap, time::Duration};

use bytes::Bytes;
use headers::{ContentLength, ContentType, HeaderMapExt};
use http::{Response, StatusCode};
use pyo3::{pyclass, pymethods, types::PyModule, PyAny, PyResult, Python};
use ulid::Ulid;

use crate::{
    errors::{NotFoundError, SynapseError},
    http::{http_request_from_twisted, http_response_to_twisted, HeaderMapPyExt},
};

mod session;

use self::session::Session;

// TODO: handle eviction
#[derive(Default)]
#[pyclass]
struct Rendezvous {
    sessions: HashMap<Ulid, Session>,
}

#[pymethods]
impl Rendezvous {
    #[new]
    fn new() -> Self {
        Rendezvous::default()
    }

    fn handle_post(&mut self, twisted_request: &PyAny) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        let ContentLength(content_length) = request.headers().typed_get_required()?;

        if content_length > 1024 * 100 {
            return Err(SynapseError::new(
                StatusCode::BAD_REQUEST,
                "Content-Length too large".to_owned(),
                "M_INVALID_PARAM",
                None,
                None,
            ));
        }

        let content_type: ContentType = request.headers().typed_get_required()?;

        let id = Ulid::new();

        // XXX: this is lazy
        let source_uri = request.uri();
        let uri = format!("{source_uri}/{id}");

        let body = request.into_body();

        let session = Session::new(body, content_type.into(), Duration::from_secs(5 * 60));

        let response = serde_json::json!({
            "uri": uri,
        })
        .to_string();

        let mut response = Response::new(response.as_bytes());
        *response.status_mut() = StatusCode::CREATED;
        response.headers_mut().typed_insert(ContentType::json());
        response.headers_mut().typed_insert(session.etag());
        response.headers_mut().typed_insert(session.expires());
        response.headers_mut().typed_insert(session.last_modified());
        http_response_to_twisted(twisted_request, response)?;

        self.sessions.insert(id, session);

        Ok(())
    }

    fn handle_get(&mut self, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let _request = http_request_from_twisted(twisted_request)?;

        // TODO: handle If-None-Match

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self.sessions.get(&id).ok_or_else(NotFoundError::new)?;

        let mut response = Response::new(session.data());
        *response.status_mut() = StatusCode::OK;
        response.headers_mut().typed_insert(session.content_type());
        response.headers_mut().typed_insert(session.etag());
        response.headers_mut().typed_insert(session.expires());
        response.headers_mut().typed_insert(session.last_modified());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_put(&mut self, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let request = http_request_from_twisted(twisted_request)?;

        // TODO: handle If-Match

        let content_type: ContentType = request.headers().typed_get_required()?;
        let data = request.into_body();

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let session = self.sessions.get_mut(&id).ok_or_else(NotFoundError::new)?;
        session.update(data, content_type.into());

        let mut response = Response::new(Bytes::new());
        *response.status_mut() = StatusCode::ACCEPTED;
        response.headers_mut().typed_insert(session.etag());
        response.headers_mut().typed_insert(session.expires());
        response.headers_mut().typed_insert(session.last_modified());
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }

    fn handle_delete(&mut self, twisted_request: &PyAny, id: &str) -> PyResult<()> {
        let _request = http_request_from_twisted(twisted_request)?;

        let id: Ulid = id.parse().map_err(|_| NotFoundError::new())?;
        let _session = self.sessions.remove(&id).ok_or_else(NotFoundError::new)?;

        let mut response = Response::new(Bytes::new());
        *response.status_mut() = StatusCode::NO_CONTENT;
        http_response_to_twisted(twisted_request, response)?;

        Ok(())
    }
}

pub fn register_module(py: Python<'_>, m: &PyModule) -> PyResult<()> {
    let child_module = PyModule::new(py, "rendezvous")?;

    child_module.add_class::<Rendezvous>()?;

    m.add_submodule(child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import rendezvous` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.rendezvous", child_module)?;

    Ok(())
}
