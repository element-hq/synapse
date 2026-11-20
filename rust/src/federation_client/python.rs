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

//! The Python bindings

use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

use crate::federation_client::{
    server_name::{Host, ServerName},
    signing::{RequestSigner, SigningInput},
};

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "federation_client")?;
    child_module.add_class::<PyRequestSigner>()?;
    child_module.add_function(wrap_pyfunction!(parse_server_name, &child_module)?)?;
    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import federation_client` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.federation_client", child_module)?;

    Ok(())
}

/// Python wrapper for [`RequestSigner`].
#[pyclass(frozen, name = "RequestSigner")]
struct PyRequestSigner {
    signer: RequestSigner,
}

#[pymethods]
impl PyRequestSigner {
    /// See [`RequestSigner::new`].
    #[new]
    fn py_new(server_name: String, key_id: String, seed: &[u8]) -> PyResult<Self> {
        let signer = RequestSigner::new(server_name, key_id, seed)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(Self { signer })
    }

    /// See [`RequestSigner::authorization`].
    fn authorization<'py>(
        &self,
        py: Python<'py>,
        method: &[u8],
        uri: &[u8],
        destination: Option<&str>,
        destination_is: Option<&str>,
        content: Option<&[u8]>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let input = SigningInput {
            method: to_ascii("method", method)?,
            uri: to_ascii("uri", uri)?,
            destination,
            destination_is,
            content,
        };
        // Other Python threads can run while the signature is computed.
        let header = py
            .detach(|| self.signer.authorization(&input))
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(PyBytes::new(py, header.as_bytes()))
    }
}

fn to_ascii<'a>(name: &str, bytes: &'a [u8]) -> PyResult<&'a str> {
    std::str::from_utf8(bytes)
        .ok()
        .filter(|s| s.is_ascii())
        .ok_or_else(|| PyValueError::new_err(format!("{name} is not ASCII")))
}

/// For tests: `(host as written, port, "dns" | "ipv4" | "ipv6")`.
#[pyfunction(name = "_parse_server_name")]
fn parse_server_name(name: &str) -> PyResult<(String, Option<u16>, &'static str)> {
    let parsed = ServerName::parse(name).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let kind = match parsed.host() {
        Host::Dns => "dns",
        Host::Ipv4(_) => "ipv4",
        Host::Ipv6(_) => "ipv6",
    };
    Ok((parsed.host_str().to_owned(), parsed.port(), kind))
}
