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
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by New Vector Limited]
 *
 */

#![allow(clippy::new_ret_no_self)]

use std::collections::HashMap;

use http::{HeaderMap, StatusCode};
use pyo3::import_exception;

import_exception!(synapse.api.errors, SynapseError);

impl SynapseError {
    pub fn new(code: StatusCode, message: &'static str) -> pyo3::PyErr {
        SynapseError::new_err((code.as_u16(), message))
    }

    pub fn new_with_headers(
        code: StatusCode,
        message: &'static str,
        headers: HeaderMap,
    ) -> pyo3::PyErr {
        let headers = headers
            .iter()
            .map(|(key, value)| {
                (
                    key.to_string(),
                    value
                        .to_str()
                        // XXX: will that ever throw?
                        .expect("header value is valid ASCII")
                        .to_owned(),
                )
            })
            .collect::<HashMap<String, String>>();

        SynapseError::new_err((code.as_u16(), message, None::<()>, headers))
    }
}

import_exception!(synapse.api.errors, NotFoundError);

impl NotFoundError {
    pub fn new() -> pyo3::PyErr {
        NotFoundError::new_err(())
    }
}
