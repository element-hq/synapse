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

//! The transaction sender as the Python `TransactionManager` uses it.
//!
//! [`RustTransactionManager::send_transaction`] takes the events and EDU
//! dicts, and returns a Deferred. The PDU JSON is built from the Rust events
//! on tokio, so the only GIL hold after the call is the one that resolves
//! the Deferred.

use std::sync::Arc;

use pyo3::{intern, prelude::*, types::PyDict};
use serde_json::Value;

use super::{
    acknowledged_pdus, log_prefix, Transaction, TransactionResponse, TransactionSender, CLIENT_LOG,
    TX_LOG,
};
use crate::{
    deferred::{create_deferred_with, OnCancel},
    events::Event,
    federation::client::{
        python::{client_error_to_py, request_send_failed, RustFederationClient},
        RequestStats,
    },
    gil::with_gil,
    runtime::RustRuntime,
};

/// A built transaction. Tests read it as they read a Python `Transaction`.
#[pyclass(frozen, name = "RustTransaction")]
pub struct RustTransaction {
    pub(crate) inner: Transaction,
}

#[pymethods]
impl RustTransaction {
    #[getter]
    fn transaction_id(&self) -> &str {
        &self.inner.transaction_id
    }

    #[getter]
    fn origin(&self) -> &str {
        &self.inner.origin
    }

    #[getter]
    fn destination(&self) -> &str {
        &self.inner.destination
    }

    #[getter]
    fn origin_server_ts(&self) -> u64 {
        self.inner.origin_server_ts
    }

    #[getter]
    fn pdus<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize::pythonize(py, &self.inner.pdus)?)
    }

    #[getter]
    fn edus<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize::pythonize(py, &self.inner.edus)?)
    }

    /// The request body, as `Transaction.get_dict` gives it.
    fn get_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize::pythonize(py, &self.inner.body())?)
    }
}

/// Builds and sends transactions through a [`RustFederationClient`].
#[pyclass(frozen, name = "RustTransactionManager")]
pub struct RustTransactionManager {
    pub(crate) sender: Arc<TransactionSender>,
    pub(crate) runtime: RustRuntime,
    pub(crate) record_metrics: Arc<Py<PyAny>>,
}

#[pymethods]
impl RustTransactionManager {
    #[new]
    fn py_new(client: &Bound<'_, RustFederationClient>) -> Self {
        let client = client.get();
        RustTransactionManager {
            sender: Arc::new(TransactionSender::new(Arc::clone(&client.client))),
            runtime: client.runtime.clone(),
            record_metrics: Arc::clone(&client.record_metrics),
        }
    }

    /// Send `pdus` and `edus` to `destination` in one transaction. Returns a
    /// Deferred of the ids of the PDUs the remote accepted, or of `None`
    /// unless `want_acknowledged`. Raises what `put_json` raises.
    ///
    /// `check`, for tests, is called with the `RustTransaction` before it is
    /// sent, and may raise to stop the send.
    #[pyo3(signature = (destination, pdus, edus, *, extra_headers = Vec::new(), want_acknowledged = false, check = None))]
    #[allow(clippy::too_many_arguments)]
    fn send_transaction<'py>(
        &self,
        py: Python<'py>,
        destination: String,
        pdus: Vec<Py<Event>>,
        edus: Vec<Bound<'py, PyAny>>,
        extra_headers: Vec<(String, String)>,
        want_acknowledged: bool,
        check: Option<Py<PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let edus = edus
            .iter()
            .map(|edu| pythonize::depythonize::<Value>(edu))
            .collect::<Result<Vec<_>, _>>()?;

        let sender = Arc::clone(&self.sender);
        let record_metrics = Arc::clone(&self.record_metrics);
        let fut = async move {
            // The events are frozen, so their JSON can be read without the
            // GIL.
            let pdus = pdus
                .iter()
                .map(|pdu| pdu.get().to_pdu_value())
                .collect::<Result<Vec<_>, _>>()
                .map_err(|err| {
                    pyo3::exceptions::PyValueError::new_err(format!(
                        "Failed to serialize event: {err}"
                    ))
                })?;
            let transaction = sender.build(&destination, pdus, edus);

            if let Some(check) = check {
                with_gil("transaction_check", |py| {
                    let built = RustTransaction {
                        inner: transaction.clone(),
                    };
                    check.bind(py).call1((built,)).map(drop)
                })?;
            }

            let transaction_id = transaction.transaction_id.clone();
            let mut stats = RequestStats::default();
            let result = sender.send(transaction, extra_headers, &mut stats).await;

            with_gil("transaction_response", |py| -> PyResult<Py<PyAny>> {
                let recorded =
                    record_metrics
                        .bind(py)
                        .call1(("PUT", stats.attempts, stats.responses.clone()));
                if let Err(err) = recorded {
                    log::warn!("Failed to record federation request metrics: {err}");
                }

                let acknowledged = match result {
                    Ok(TransactionResponse::Acknowledged(ids)) => ids,
                    Ok(TransactionResponse::Unparsed(body)) => {
                        python_acknowledged(py, &destination, &transaction_id, &body)?
                    }
                    Err(err) => return Err(client_error_to_py(py, err, &destination)),
                };
                if want_acknowledged {
                    Ok(acknowledged.into_pyobject(py)?.into_any().unbind())
                } else {
                    Ok(py.None())
                }
            })
        };

        // Cancelling stops the send, as cancelling the Python one does.
        create_deferred_with(py, &self.runtime, fut, OnCancel::Abort)
    }

    /// Build the transaction `send_transaction` would send, without sending
    /// it. For tests.
    fn build_transaction(
        &self,
        destination: &str,
        pdus: Vec<PyRef<'_, Event>>,
        edus: Vec<Bound<'_, PyAny>>,
    ) -> PyResult<RustTransaction> {
        let pdus = pdus
            .iter()
            .map(|pdu| pdu.to_pdu_value())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|err| {
                pyo3::exceptions::PyValueError::new_err(format!("Failed to serialize event: {err}"))
            })?;
        let edus = edus
            .iter()
            .map(|edu| pythonize::depythonize::<Value>(edu))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(RustTransaction {
            inner: self.sender.build(destination, pdus, edus),
        })
    }

    /// The ids of the PDUs a response to `transaction` accepts, logging the
    /// ones it rejects. For tests, which answer transactions themselves.
    fn acknowledged_pdus(
        &self,
        transaction: &RustTransaction,
        response: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<String>> {
        let response: Value = pythonize::depythonize(response)?;
        let Value::Object(response) = response else {
            return Ok(Vec::new());
        };
        Ok(acknowledged_pdus(
            &transaction.inner.log_prefix(),
            &response,
        ))
    }
}

/// The response handling for a body serde_json refused: decode it with
/// Python's decoder, as `JsonParser` would, and read the `pdus` map from the
/// result.
pub(crate) fn python_acknowledged(
    py: Python<'_>,
    destination: &str,
    transaction_id: &str,
    body: &[u8],
) -> PyResult<Vec<String>> {
    let log_prefix = log_prefix(destination, transaction_id);
    let failed = |err: PyErr| -> PyErr {
        log::warn!(
            target: CLIENT_LOG,
            "{{{transaction_id}}} [{destination}] Failed to parse response"
        );
        request_send_failed(py, err.into_value(py).into_bound(py).into_any(), false)
    };

    let decoded = (|| {
        let text =
            pyo3::types::PyBytes::new(py, body).call_method1(intern!(py, "decode"), ("utf-8",))?;
        py.import("synapse.util.json")?
            .getattr("json_decoder")?
            .call_method1(intern!(py, "decode"), (text,))
    })();
    let value = match decoded {
        Ok(value) => value,
        Err(err) if err.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
            return Err(failed(err))
        }
        Err(err) => return Err(err),
    };
    let Ok(response) = value.cast::<PyDict>() else {
        let name = value.get_type().name()?.to_string();
        return Err(failed(pyo3::exceptions::PyValueError::new_err(format!(
            "Received incorrect JSON value: {name}"
        ))));
    };
    log::info!(target: TX_LOG, "{log_prefix} got 200 response");

    let Some(pdus) = response.get_item("pdus")? else {
        return Ok(Vec::new());
    };
    let Ok(pdus) = pdus.cast::<PyDict>() else {
        log::warn!(target: TX_LOG, "{log_prefix} Remote returned invalid type for `pdus`");
        return Ok(Vec::new());
    };
    let mut acknowledged = Vec::with_capacity(pdus.len());
    for (event_id, result) in pdus.iter() {
        let accepted = match result.cast::<PyDict>() {
            Ok(result) => !result.contains("error")?,
            Err(_) => false,
        };
        // An id Rust cannot hold is not one of ours.
        match (accepted, event_id.extract::<String>()) {
            (true, Ok(event_id)) => acknowledged.push(event_id),
            _ => log::warn!(
                target: TX_LOG,
                "{log_prefix} Remote returned error for {event_id}: {result}"
            ),
        }
    }
    Ok(acknowledged)
}
