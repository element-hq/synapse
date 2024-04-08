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

use headers::{Header, HeaderMapExt};
use http::{HeaderName, StatusCode};
use pyo3::{import_exception, PyResult};

import_exception!(synapse.api.errors, SynapseError);

impl SynapseError {
    pub fn new(code: StatusCode, message: &'static str) -> pyo3::PyErr {
        SynapseError::new_err((code.as_u16(), message))
    }
}

import_exception!(synapse.api.errors, NotFoundError);

impl NotFoundError {
    pub fn new() -> pyo3::PyErr {
        NotFoundError::new_err(())
    }
}

import_exception!(synapse.api.errors, MissingHeaderError);

impl MissingHeaderError {
    pub fn new(header: &HeaderName) -> pyo3::PyErr {
        let header = header.as_str().to_owned();
        Self::new_err(header)
    }
}

import_exception!(synapse.api.errors, InvalidHeaderError);

impl InvalidHeaderError {
    pub fn new(header: &HeaderName) -> pyo3::PyErr {
        let header = header.as_str().to_owned();
        Self::new_err(header)
    }
}

import_exception!(synapse.api.errors, PayloadTooLargeError);

impl PayloadTooLargeError {
    pub fn new() -> pyo3::PyErr {
        Self::new_err(())
    }
}

/// An extension trait for [`HeaderMap`] that provides typed access to headers, and throws the
/// right python exceptions when the header is missing or fails to parse.
pub(crate) trait HeaderMapPyExt: HeaderMapExt {
    /// Get a header from the map, returning an error if it is missing or invalid.
    fn typed_get_required<H>(&self) -> PyResult<H>
    where
        H: Header,
    {
        self.typed_get_optional::<H>()?
            .ok_or_else(|| MissingHeaderError::new(H::name()))
    }

    /// Get a header from the map, returning `None` if it is missing and an error if it is invalid.
    fn typed_get_optional<H>(&self) -> PyResult<Option<H>>
    where
        H: Header,
    {
        self.typed_try_get::<H>()
            .map_err(|_| InvalidHeaderError::new(H::name()))
    }
}

impl<T: HeaderMapExt> HeaderMapPyExt for T {}
