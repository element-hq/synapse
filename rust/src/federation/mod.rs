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
 *
 */

//! Outbound federation.
//!
//! This is being built up from the network end, one layer at a time.
//!
//! - [`agent`]: sends requests: server name resolution, the IP range and
//!   TLS policies, and the connection pool.
//! - [`client`]: the request layer: signing, retries and the backoff.
//! - [`gil`]: accounting for every time this code takes the GIL.
//! - [`queue`]: the per-destination queues and their transmission loops.
//! - [`signing`]: the `X-Matrix` request signature.
//! - [`transaction`]: builds and sends `/send` transactions.
//! - [`transport`]: the seam between the request layer and the network, with
//!   a Python-backed implementation for tests.

use pyo3::prelude::*;

pub mod agent;
pub mod client;
pub mod queue;
pub mod signing;
pub mod transaction;
pub mod transport;

use signing::{make_auth_header, CanonicalBody, Destination, FederationSigningKey};

/// Build the `Authorization` header for a request, as
/// `MatrixFederationHttpClient.build_auth_headers` does. For tests, which
/// check the two produce the same header.
#[pyfunction]
#[pyo3(signature = (signing_key, origin, method, uri, destination, destination_is = None, content = None))]
fn build_auth_header_for_test(
    signing_key: FederationSigningKey,
    origin: &str,
    method: &str,
    uri: &str,
    destination: Option<&str>,
    destination_is: Option<&str>,
    content: Option<Bound<'_, PyAny>>,
) -> PyResult<String> {
    let destination = match (destination, destination_is) {
        (Some(server), None) => Destination::Server(server),
        (None, Some(identity_server)) => Destination::IdentityServer(identity_server),
        (Some(server), Some(identity_server)) => Destination::Both {
            server,
            identity_server,
        },
        (None, None) => {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "one of destination and destination_is is required",
            ))
        }
    };

    let content = content
        .map(|c| -> PyResult<_> {
            let value: serde_json::Value = pythonize::depythonize(&c)?;
            Ok(CanonicalBody::new(&value)?)
        })
        .transpose()?;

    Ok(make_auth_header(
        &signing_key,
        origin,
        method,
        uri,
        destination,
        content.as_ref(),
    )?)
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "federation")?;
    child_module.add_function(wrap_pyfunction!(build_auth_header_for_test, &child_module)?)?;
    child_module.add_function(wrap_pyfunction!(
        crate::gil::federation_gil_stats,
        &child_module
    )?)?;
    child_module.add_class::<agent::python::RustFederationAgentForTest>()?;
    child_module.add_class::<client::python::RustFederationClient>()?;
    child_module.add_class::<transaction::python::RustTransaction>()?;
    child_module.add_class::<transaction::python::RustTransactionManager>()?;
    child_module.add_class::<queue::python::RustDestinationQueues>()?;
    child_module.add_class::<queue::python::RustDestinationQueue>()?;
    child_module.add_class::<crate::storage::federation_store::PyFederationStoreForTest>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import federation` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.federation", child_module)?;

    Ok(())
}
