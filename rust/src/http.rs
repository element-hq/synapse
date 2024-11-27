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

use bytes::{Buf, BufMut, Bytes, BytesMut};
use headers::{Header, HeaderMapExt};
use http::{HeaderName, HeaderValue, Method, Request, Response, StatusCode, Uri};
use pyo3::{
    exceptions::PyValueError,
    types::{PyAnyMethods, PyBytes, PyBytesMethods, PySequence, PyTuple},
    Bound, PyAny, PyResult,
};

use crate::errors::SynapseError;

/// Read a file-like Python object by chunks
///
/// # Errors
///
/// Returns an error if calling the `read` on the Python object failed
fn read_io_body(body: &Bound<'_, PyAny>, chunk_size: usize) -> PyResult<Bytes> {
    let mut buf = BytesMut::new();
    loop {
        let bound = &body.call_method1("read", (chunk_size,))?;
        let bytes: &Bound<'_, PyBytes> = bound.downcast()?;
        if bytes.as_bytes().is_empty() {
            return Ok(buf.into());
        }
        buf.put(bytes.as_bytes());
    }
}

/// Transform a Twisted `IRequest` to an [`http::Request`]
///
/// It uses the following members of `IRequest`:
///   - `content`, which is expected to be a file-like object with a `read` method
///   - `uri`, which is expected to be a valid URI as `bytes`
///   - `method`, which is expected to be a valid HTTP method as `bytes`
///   - `requestHeaders`, which is expected to have a `getAllRawHeaders` method
///
/// # Errors
///
/// Returns an error if the Python object doesn't properly implement `IRequest`
pub fn http_request_from_twisted(request: &Bound<'_, PyAny>) -> PyResult<Request<Bytes>> {
    let content = request.getattr("content")?;
    let body = read_io_body(&content, 4096)?;

    let mut req = Request::new(body);

    let bound = &request.getattr("uri")?;
    let uri: &Bound<'_, PyBytes> = bound.downcast()?;
    *req.uri_mut() =
        Uri::try_from(uri.as_bytes()).map_err(|_| PyValueError::new_err("invalid uri"))?;

    let bound = &request.getattr("method")?;
    let method: &Bound<'_, PyBytes> = bound.downcast()?;
    *req.method_mut() = Method::from_bytes(method.as_bytes())
        .map_err(|_| PyValueError::new_err("invalid method"))?;

    let headers_iter = request
        .getattr("requestHeaders")?
        .call_method0("getAllRawHeaders")?
        .try_iter()?;

    for header in headers_iter {
        let header = header?;
        let header: &Bound<'_, PyTuple> = header.downcast()?;
        let bound = &header.get_item(0)?;
        let name: &Bound<'_, PyBytes> = bound.downcast()?;
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| PyValueError::new_err("invalid header name"))?;

        let bound = &header.get_item(1)?;
        let values: &Bound<'_, PySequence> = bound.downcast()?;
        for index in 0..values.len()? {
            let bound = &values.get_item(index)?;
            let value: &Bound<'_, PyBytes> = bound.downcast()?;
            let value = HeaderValue::from_bytes(value.as_bytes())
                .map_err(|_| PyValueError::new_err("invalid header value"))?;
            req.headers_mut().append(name.clone(), value);
        }
    }

    Ok(req)
}

/// Send an [`http::Response`] through a Twisted `IRequest`
///
/// It uses the following members of `IRequest`:
///
///  - `responseHeaders`, which is expected to have a `addRawHeader(bytes, bytes)` method
///  - `setResponseCode(int)` method
///  - `write(bytes)` method
///  - `finish()` method
///
///  # Errors
///
/// Returns an error if the Python object doesn't properly implement `IRequest`
pub fn http_response_to_twisted<B>(
    request: &Bound<'_, PyAny>,
    response: Response<B>,
) -> PyResult<()>
where
    B: Buf,
{
    let (parts, mut body) = response.into_parts();

    request.call_method1("setResponseCode", (parts.status.as_u16(),))?;

    let response_headers = request.getattr("responseHeaders")?;
    for (name, value) in parts.headers.iter() {
        response_headers.call_method1("addRawHeader", (name.as_str(), value.as_bytes()))?;
    }

    while body.remaining() != 0 {
        let chunk = body.chunk();
        request.call_method1("write", (chunk,))?;
        body.advance(chunk.len());
    }

    request.call_method0("finish")?;

    Ok(())
}

/// An extension trait for [`HeaderMap`] that provides typed access to headers, and throws the
/// right python exceptions when the header is missing or fails to parse.
///
/// [`HeaderMap`]: headers::HeaderMap
pub trait HeaderMapPyExt: HeaderMapExt {
    /// Get a header from the map, returning an error if it is missing or invalid.
    fn typed_get_required<H>(&self) -> PyResult<H>
    where
        H: Header,
    {
        self.typed_get_optional::<H>()?.ok_or_else(|| {
            SynapseError::new(
                StatusCode::BAD_REQUEST,
                format!("Missing required header: {}", H::name()),
                "M_MISSING_PARAM",
                None,
                None,
            )
        })
    }

    /// Get a header from the map, returning `None` if it is missing and an error if it is invalid.
    fn typed_get_optional<H>(&self) -> PyResult<Option<H>>
    where
        H: Header,
    {
        self.typed_try_get::<H>().map_err(|_| {
            SynapseError::new(
                StatusCode::BAD_REQUEST,
                format!("Invalid header: {}", H::name()),
                "M_INVALID_PARAM",
                None,
                None,
            )
        })
    }
}

impl<T: HeaderMapExt> HeaderMapPyExt for T {}
