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

/// The maximum number of transaction IDs we remember per session, so that the
/// memory used by a session stays bounded even if a client sends a large number
/// of requests. MSC4388 exchanges only need a handful of sends, so in practice
/// this is never reached.
const MAX_RECORDED_TRANSACTIONS: usize = 100;

/// The recorded outcome of a `PUT` request, so that a retry using the same
/// transaction ID can be answered without re-evaluating the compare-and-swap.
#[derive(Clone)]
pub enum PutOutcome {
    /// The payload was accepted, advancing the session to this sequence token.
    Accepted(String),
    /// The supplied `sequence_token` did not match, so the payload was
    /// rejected as a concurrent write.
    ConcurrentWrite,
}

/// A single session, containing data, metadata, and expiry information.
pub struct Session {
    id: Ulid,
    data: String,
    /// Counter incremented on each successful write, whose decimal
    /// representation is the `sequence_token`, as recommended by MSC4388.
    sequence: u64,
    /// The outcome of each `PUT` seen for this session, keyed by the
    /// transaction ID used, oldest first. Used to make retries idempotent.
    transactions: VecDeque<(String, PutOutcome)>,
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
    /// against the supplied `sequence_token`, recording the outcome against
    /// the transaction ID so that a retry using the same transaction ID is
    /// answered with the same response rather than being re-evaluated.
    pub fn send(
        &mut self,
        txn_id: &str,
        sequence_token: &str,
        data: String,
        now: SystemTime,
    ) -> PutOutcome {
        if let Some(outcome) = self.transaction_outcome(txn_id) {
            return outcome.clone();
        }

        let outcome = if self.sequence_token() == sequence_token {
            self.update(data, now);
            PutOutcome::Accepted(self.sequence_token())
        } else {
            PutOutcome::ConcurrentWrite
        };

        self.record_transaction(txn_id.to_owned(), outcome.clone());

        outcome
    }

    /// Update the session with new data and last modified time.
    fn update(&mut self, data: String, now: SystemTime) {
        self.sequence += 1;
        self.data = data;
        self.last_modified = now;
    }

    /// The outcome of a previous `PUT` made with the given transaction ID, if
    /// we have seen it for this session.
    fn transaction_outcome(&self, txn_id: &str) -> Option<&PutOutcome> {
        self.transactions
            .iter()
            .find(|(id, _)| id == txn_id)
            .map(|(_, outcome)| outcome)
    }

    /// Record the outcome of a `PUT` against the transaction ID it used, so
    /// that a retry with the same transaction ID gets the same response.
    fn record_transaction(&mut self, txn_id: String, outcome: PutOutcome) {
        self.transactions.push_back((txn_id, outcome));
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
