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

use std::{
    collections::VecDeque,
    time::{Duration, SystemTime},
};

use pyo3::{Bound, IntoPyObject, PyAny, Python};
use pythonize::{pythonize, PythonizeError};
use serde::Serialize;
use ulid::Ulid;

use crate::identifier::OpaqueIdentifier;

/// The maximum number of transaction IDs we remember per session, so that the
/// memory used by a session stays bounded even if a client sends a large number
/// of requests. MSC4388 exchanges only need a handful of sends, so in practice
/// this is never reached.
const MAX_RECORDED_TRANSACTIONS: usize = 100;

/// The supplied `sequence_token` did not match the session's current one, so
/// the payload was rejected as a concurrent write.
#[derive(Debug)]
pub struct ConcurrentWrite;

/// A single session, containing data, metadata, and expiry information.
pub struct Session {
    id: Ulid,
    data: String,
    /// Counter incremented on each successful write, whose decimal
    /// representation is the `sequence_token`, as recommended by MSC4388.
    sequence: u64,
    /// The sequence token returned by each successful `PUT` to this session,
    /// keyed by the transaction ID used, oldest first. Used to make retries
    /// idempotent.
    ///
    /// Ideally we would keep only the latest transaction ID and response per
    /// sender. However, we cannot tell the senders apart, so we keep a bounded
    /// history instead. A retry is only wrongly rejected if more than
    /// `MAX_RECORDED_TRANSACTIONS` successful writes happen in between.
    ///
    /// Rejected writes are not recorded: the counter only ever advances, so a
    /// retry of a rejected `PUT` would be rejected again anyway, and leaving
    /// them out means a stream of failed writes cannot evict the record of a
    /// successful one.
    transactions: VecDeque<(OpaqueIdentifier, String)>,
    last_modified: SystemTime,
    expires: SystemTime,
}

#[derive(Serialize)]
pub struct PostResponse {
    id: String,
    sequence_token: String,
    expires_in_ms: u64,
}

impl<'source> IntoPyObject<'source> for PostResponse {
    type Target = PyAny;
    type Output = Bound<'source, Self::Target>;
    type Error = PythonizeError;

    fn into_pyobject(self, py: Python<'source>) -> Result<Self::Output, Self::Error> {
        pythonize(py, &self)
    }
}

#[derive(Serialize)]
pub struct GetResponse {
    data: String,
    sequence_token: String,
    expires_in_ms: u64,
}

impl<'source> IntoPyObject<'source> for GetResponse {
    type Target = PyAny;
    type Output = Bound<'source, Self::Target>;
    type Error = PythonizeError;

    fn into_pyobject(self, py: Python<'source>) -> Result<Self::Output, Self::Error> {
        pythonize(py, &self)
    }
}

#[derive(Serialize)]
pub struct PutResponse {
    sequence_token: String,
}

impl PutResponse {
    pub fn new(sequence_token: String) -> Self {
        Self { sequence_token }
    }
}

impl<'source> IntoPyObject<'source> for PutResponse {
    type Target = PyAny;
    type Output = Bound<'source, Self::Target>;
    type Error = PythonizeError;

    fn into_pyobject(self, py: Python<'source>) -> Result<Self::Output, Self::Error> {
        pythonize(py, &self)
    }
}

impl Session {
    /// Create a new session with the given data and time-to-live.
    pub fn new(id: Ulid, data: String, now: SystemTime, ttl: Duration) -> Self {
        Self {
            id,
            data,
            sequence: 0,
            transactions: VecDeque::new(),
            expires: now + ttl,
            last_modified: now,
        }
    }

    /// Returns true if the session has expired at the given time.
    pub fn expired(&self, now: SystemTime) -> bool {
        self.expires <= now
    }

    /// Handle a send (`PUT`) for this session: perform the compare-and-swap
    /// against the supplied `sequence_token`, recording the resulting sequence
    /// token against the transaction ID so that a retry using the same
    /// transaction ID is answered with the same response rather than being
    /// re-evaluated.
    ///
    /// Returns the new sequence token if the payload was accepted.
    pub fn send(
        &mut self,
        txn_id: &OpaqueIdentifier,
        sequence_token: &str,
        data: String,
        now: SystemTime,
    ) -> Result<String, ConcurrentWrite> {
        if let Some(recorded) = self.transaction_outcome(txn_id) {
            return Ok(recorded.clone());
        }

        if self.sequence_token() != sequence_token {
            return Err(ConcurrentWrite);
        }

        self.update(data, now);
        let new_sequence_token = self.sequence_token();
        self.record_transaction(txn_id.clone(), new_sequence_token.clone());

        Ok(new_sequence_token)
    }

    /// Update the session with new data and last modified time.
    fn update(&mut self, data: String, now: SystemTime) {
        self.sequence += 1;
        self.data = data;
        self.last_modified = now;
    }

    /// The sequence token returned by a previous successful `PUT` made with
    /// the given transaction ID, if we have seen one for this session.
    fn transaction_outcome(&self, txn_id: &OpaqueIdentifier) -> Option<&String> {
        self.transactions
            .iter()
            .find(|(id, _)| id == txn_id)
            .map(|(_, sequence_token)| sequence_token)
    }

    /// Record the sequence token returned by a successful `PUT` against the
    /// transaction ID it used, so that a retry with the same transaction ID
    /// gets the same response.
    fn record_transaction(&mut self, txn_id: OpaqueIdentifier, sequence_token: String) {
        self.transactions.push_back((txn_id, sequence_token));
        while self.transactions.len() > MAX_RECORDED_TRANSACTIONS {
            self.transactions.pop_front();
        }
    }

    /// The sequence token for the session, which as recommended by MSC4388 is
    /// the decimal representation of the write counter.
    pub fn sequence_token(&self) -> String {
        self.sequence.to_string()
    }

    pub fn get_response(&self, now: SystemTime) -> GetResponse {
        GetResponse {
            data: self.data.clone(),
            sequence_token: self.sequence_token(),
            expires_in_ms: self
                .expires
                .duration_since(now)
                .unwrap_or_default()
                .as_millis() as u64,
        }
    }

    pub fn post_response(&self, now: SystemTime) -> PostResponse {
        PostResponse {
            id: self.id.to_string(),
            sequence_token: self.sequence_token(),
            expires_in_ms: self
                .expires
                .duration_since(now)
                .unwrap_or_default()
                .as_millis() as u64,
        }
    }
}
