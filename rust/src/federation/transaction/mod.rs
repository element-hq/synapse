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

//! Transactions: Python's `TransactionManager.send_new_transaction` and the
//! `Transaction` unit of `synapse.federation.units`.
//!
//! [`TransactionSender::build`] gives a transaction its id and timestamp.
//! [`TransactionSender::send`] PUTs it to `/send/<txn_id>` through the
//! [`FederationClient`] with the options Python's transport layer uses, reads
//! the JSON response and works out which PDUs the remote accepted. None of
//! it needs the GIL. [`python`] is the facade the Python `TransactionManager`
//! calls.

use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};

use bytes::{Bytes, BytesMut};
use http::Method;
use serde_json::{Map, Value};

use super::client::{
    check_content_type,
    python::{python_type_name, MAX_JSON_RESPONSE_SIZE},
    ClientError, FailureCause, FederationClient, FederationRequest, RequestOptions, RequestStats,
    SentResponse,
};

pub mod python;

#[cfg(test)]
mod tests;

/// `CANONICALJSON_MAX_INT`: the largest integer canonical JSON allows.
const CANONICALJSON_MAX_INT: i64 = (1 << 53) - 1;
/// `CANONICALJSON_MIN_INT`.
const CANONICALJSON_MIN_INT: i64 = -CANONICALJSON_MAX_INT;

/// The logger of Python's `TX [dest] {txn}` lines. Rust logs them under the
/// same name, so log configs that set its level still apply.
const TX_LOG: &str = "synapse.federation.sender.transaction_manager";
/// The logger of Python's lines about reading the response.
const CLIENT_LOG: &str = "synapse.http.matrixfederationclient";

/// The EDU types whose contents the issue 8631 debug log records.
const DEVICE_UPDATE_EDUS: [&str; 2] = ["m.device_list_update", "m.signing_key_update"];

/// A transaction to send: `synapse.federation.units.Transaction`.
#[derive(Clone, Debug)]
pub struct Transaction {
    pub transaction_id: String,
    pub origin: String,
    pub destination: String,
    pub origin_server_ts: u64,
    /// The PDUs, as `get_pdu_json` gives them.
    pub pdus: Vec<Value>,
    /// The EDUs, as `Edu.get_dict` gives them.
    pub edus: Vec<Value>,
}

impl Transaction {
    /// The request body: `Transaction.get_dict`. PDUs with a depth canonical
    /// JSON cannot hold are dropped, and `edus` is left out when empty.
    pub fn body(&self) -> Value {
        self.clone().into_body()
    }

    /// As [`Transaction::body`], moving the PDUs and EDUs into it.
    pub fn into_body(self) -> Value {
        let mut pdus = self.pdus;
        pdus.retain(has_valid_depth);

        let mut body = Map::new();
        body.insert("origin".to_owned(), Value::from(self.origin));
        body.insert(
            "origin_server_ts".to_owned(),
            Value::from(self.origin_server_ts),
        );
        body.insert("pdus".to_owned(), Value::Array(pdus));
        if !self.edus.is_empty() {
            body.insert("edus".to_owned(), Value::Array(self.edus));
        }
        Value::Object(body)
    }

    /// `/_matrix/federation/v1/send/<txn_id>`. The id is a number, so the
    /// quoting `_create_v1_path` applies changes nothing.
    pub fn path(&self) -> String {
        format!("/_matrix/federation/v1/send/{}", self.transaction_id)
    }

    /// The prefix of the transaction's log lines, `TX [dest] {txn}`.
    pub(crate) fn log_prefix(&self) -> String {
        log_prefix(&self.destination, &self.transaction_id)
    }
}

/// `TX [dest] {txn}`.
fn log_prefix(destination: &str, transaction_id: &str) -> String {
    format!("TX [{destination}] {{{transaction_id}}}")
}

/// `filter_pdus_for_valid_depth`: keep the PDUs whose `depth` is an integer
/// that canonical JSON allows.
pub fn filter_pdus_for_valid_depth(pdus: &[Value]) -> Vec<Value> {
    pdus.iter()
        .filter(|pdu| has_valid_depth(pdu))
        .cloned()
        .collect()
}

/// Whether the PDU has a `depth` that canonical JSON allows.
fn has_valid_depth(pdu: &Value) -> bool {
    let Some(depth) = pdu.get("depth") else {
        return false;
    };
    // Python compares whatever is there. An event's depth is always an
    // integer, but compare a float as Python would.
    if let Some(depth) = depth.as_i64() {
        (CANONICALJSON_MIN_INT..=CANONICALJSON_MAX_INT).contains(&depth)
    } else if let Some(depth) = depth.as_f64() {
        (CANONICALJSON_MIN_INT as f64..=CANONICALJSON_MAX_INT as f64).contains(&depth)
    } else {
        false
    }
}

/// The remote's answer to a transaction.
#[derive(Debug)]
pub enum TransactionResponse {
    /// The ids of the PDUs the remote echoed back without an error, in the
    /// order of the response.
    Acknowledged(Vec<String>),
    /// A 2xx body that serde_json refused, such as one with a lone surrogate
    /// in a string. Python's decoder may accept it, so the caller decides,
    /// and logs the `got 200 response` line if it does.
    Unparsed(Bytes),
}

/// Log the PDUs the remote reported errors for, and return the ids of the
/// ones it accepted: the response handling of `send_new_transaction`.
pub fn acknowledged_pdus(log_prefix: &str, response: &Map<String, Value>) -> Vec<String> {
    let Some(pdus) = response.get("pdus") else {
        return Vec::new();
    };
    let Some(pdus) = pdus.as_object() else {
        log::warn!(target: TX_LOG, "{log_prefix} Remote returned invalid type for `pdus`");
        return Vec::new();
    };

    let mut acknowledged = Vec::with_capacity(pdus.len());
    for (event_id, result) in pdus {
        match result.as_object() {
            Some(result) if !result.contains_key("error") => {
                acknowledged.push(event_id.clone());
            }
            _ => log::warn!(
                target: TX_LOG,
                "{log_prefix} Remote returned error for {event_id}: {result}"
            ),
        }
    }
    acknowledged
}

/// Builds and sends transactions.
pub struct TransactionSender {
    client: Arc<FederationClient>,
    /// The next transaction id. Seeded with the time, as Python does, so ids
    /// do not repeat across restarts.
    next_txn_id: AtomicU64,
}

impl TransactionSender {
    pub fn new(client: Arc<FederationClient>) -> Self {
        let next_txn_id = AtomicU64::new(client.now_ms());
        TransactionSender {
            client,
            next_txn_id,
        }
    }

    pub fn client(&self) -> &FederationClient {
        &self.client
    }

    /// Give the PDUs and EDUs for `destination` an id and a timestamp.
    pub fn build(&self, destination: &str, pdus: Vec<Value>, edus: Vec<Value>) -> Transaction {
        let transaction_id = self.next_txn_id.fetch_add(1, Ordering::Relaxed).to_string();
        log::debug!(
            target: TX_LOG,
            "TX [{destination}] {{{transaction_id}}} Attempting new transaction (pdus: {}, edus: {})",
            pdus.len(),
            edus.len()
        );
        Transaction {
            transaction_id,
            origin: self.client.config().server_name.clone(),
            destination: destination.to_owned(),
            origin_server_ts: self.client.now_ms(),
            pdus,
            edus,
        }
    }

    /// Send `transaction` and read the remote's answer.
    ///
    /// The PDUs and EDUs move into the request body, so the transaction holds
    /// one copy of them while it is retried, besides the canonical JSON.
    /// `extra_headers` carries the tracing span. The request's attempts and
    /// response codes go to `stats`, for the Python counters.
    pub async fn send(
        &self,
        transaction: Transaction,
        extra_headers: Vec<(String, String)>,
        stats: &mut RequestStats,
    ) -> Result<TransactionResponse, ClientError> {
        let log_prefix = transaction.log_prefix();
        log::info!(
            target: TX_LOG,
            "{log_prefix} Sending transaction [{}], (PDUs: {}, EDUs: {})",
            transaction.transaction_id,
            transaction.pdus.len(),
            transaction.edus.len(),
        );
        log_device_list_updates(&transaction);

        let transaction_id = transaction.transaction_id.clone();
        let destination = transaction.destination.clone();
        let request = FederationRequest {
            method: Method::PUT,
            destination: destination.clone(),
            path: transaction.path(),
            query: String::new(),
            body: Some(transaction.into_body()),
            rewrite_age_ts: false,
            extra_headers,
        };
        // `TransportLayerClient.send_transaction`'s options.
        let options = RequestOptions {
            long_retries: true,
            try_trailing_slash_on_400: true,
            // A transaction should always succeed. If it does not, something
            // is wrong, so back off.
            backoff_on_all_error_codes: true,
            ..RequestOptions::default()
        };

        let response = match self.client.send(&request, &options, stats).await {
            Ok(response) => response,
            Err(err) => {
                if let ClientError::Http(http) = &err {
                    log::info!(target: TX_LOG, "{log_prefix} got {} response", http.code);
                }
                return Err(err);
            }
        };

        let body = self
            .read_json(response, &destination, self.client.config().default_timeout)
            .await?;

        let value = match serde_json::from_slice::<Value>(&body) {
            Ok(value) => value,
            Err(_) => return Ok(TransactionResponse::Unparsed(body)),
        };
        let Value::Object(value) = value else {
            log::warn!(
                target: CLIENT_LOG,
                "{{{transaction_id}}} [{destination}] Failed to parse response"
            );
            return Err(ClientError::SendFailed {
                cause: FailureCause::InvalidBody(format!(
                    "Received incorrect JSON value: {}",
                    python_type_name(&value)
                )),
                can_retry: false,
            });
        };
        log::info!(target: TX_LOG, "{log_prefix} got 200 response");
        Ok(TransactionResponse::Acknowledged(acknowledged_pdus(
            &log_prefix,
            &value,
        )))
    }

    /// Read a 2xx response's JSON body, as `_handle_response` does with a
    /// `JsonParser`.
    async fn read_json(
        &self,
        response: SentResponse,
        destination: &str,
        timeout: Duration,
    ) -> Result<Bytes, ClientError> {
        let log_prefix = format!("{{{}}} [{destination}]", response.txn_id);
        let description = response.description.clone();
        let start_ms = response.start_ms;
        let status = response.status.as_u16();
        let phrase = response.phrase.clone();

        if let Err(err) = check_content_type(&response.headers, "application/json") {
            log::warn!(
                target: CLIENT_LOG,
                "{log_prefix} Error reading response {description}: {err}"
            );
            return Err(err);
        }

        let mut collected = BytesMut::new();
        let read = self
            .client
            .read_body(
                response.body,
                Some(MAX_JSON_RESPONSE_SIZE),
                timeout,
                |chunk| {
                    collected.extend_from_slice(&chunk);
                    Ok(())
                },
            )
            .await;
        let length = match read {
            Ok(length) => length,
            Err(err) => {
                match &err {
                    ClientError::SendFailed {
                        cause: FailureCause::TooLarge { limit },
                        ..
                    } => log::warn!(
                        target: CLIENT_LOG,
                        "{log_prefix} JSON response exceeded max size {limit} - {description}"
                    ),
                    ClientError::SendFailed {
                        cause: FailureCause::BodyTimeout,
                        ..
                    } => log::warn!(
                        target: CLIENT_LOG,
                        "{log_prefix} Timed out reading response - {description}"
                    ),
                    _ => log::warn!(
                        target: CLIENT_LOG,
                        "{log_prefix} Failed to read response - {description}"
                    ),
                }
                return Err(err);
            }
        };

        let secs = self.client.now_ms().saturating_sub(start_ms) as f64 / 1000.0;
        log::info!(
            target: CLIENT_LOG,
            "{log_prefix} Completed request: {status} {phrase} in {secs:.2} secs, got {length} bytes - {description}"
        );
        Ok(collected.freeze())
    }
}

/// The debug log for issue 8631: the device list updates a transaction
/// carries.
fn log_device_list_updates(transaction: &Transaction) {
    const TARGET: &str = "synapse.8631_debug";
    if !log::log_enabled!(target: TARGET, log::Level::Debug) {
        return;
    }
    let updates: Vec<&Value> = transaction
        .edus
        .iter()
        .filter(|edu| {
            edu.get("edu_type")
                .and_then(Value::as_str)
                .is_some_and(|edu_type| DEVICE_UPDATE_EDUS.contains(&edu_type))
        })
        .filter_map(|edu| edu.get("content"))
        .collect();
    if !updates.is_empty() {
        log::debug!(
            target: TARGET,
            "about to send txn [{}] including device list updates: {}",
            transaction.transaction_id,
            Value::from(updates.into_iter().cloned().collect::<Vec<_>>()),
        );
    }
}
