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
 */

#![allow(clippy::new_ret_no_self)]

use std::collections::HashMap;

use http::{HeaderMap, StatusCode};
use pyo3::{exceptions::PyValueError, import_exception};

import_exception!(synapse.api.errors, SynapseError);

impl SynapseError {
    pub fn new(
        code: StatusCode,
        message: String,
        errcode: &'static str,
        additional_fields: Option<HashMap<String, String>>,
        headers: Option<HeaderMap>,
    ) -> pyo3::PyErr {
        // Transform the HeaderMap into a HashMap<String, String>
        let headers = if let Some(headers) = headers {
            let mut map = HashMap::with_capacity(headers.len());
            for (key, value) in headers.iter() {
                let Ok(value) = value.to_str() else {
                    // This should never happen, but we don't want to panic in case it does
                    return PyValueError::new_err(
                        "Could not construct SynapseError: header value is not valid ASCII",
                    );
                };

                map.insert(key.as_str().to_owned(), value.to_owned());
            }
            Some(map)
        } else {
            None
        };

        SynapseError::new_err((code.as_u16(), message, errcode, additional_fields, headers))
    }
}

import_exception!(synapse.api.errors, NotFoundError);

impl NotFoundError {
    pub fn new() -> pyo3::PyErr {
        NotFoundError::new_err(())
    }
}

import_exception!(synapse.api.errors, HttpResponseException);

impl HttpResponseException {
    pub fn new(status: StatusCode, bytes: Vec<u8>) -> pyo3::PyErr {
        HttpResponseException::new_err((
            status.as_u16(),
            status.canonical_reason().unwrap_or_default(),
            bytes,
        ))
    }
}
