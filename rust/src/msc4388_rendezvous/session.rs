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

use std::time::{Duration, SystemTime};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use pyo3::{Bound, IntoPyObject, PyAny, Python};
use pythonize::{pythonize, PythonizeError};
use serde::Serialize;
use sha2::{Digest, Sha256};
use ulid::Ulid;

/// A single session, containing data, metadata, and expiry information.
pub struct Session {
    id: Ulid,
    hash: [u8; 32],
    data: String,
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
        let hash = Self::compute_hash(&data, now);
        Self {
            id,
            hash,
            data,
            expires: now + ttl,
            last_modified: now,
        }
    }

    /// Returns true if the session has expired at the given time.
    pub fn expired(&self, now: SystemTime) -> bool {
        self.expires <= now
    }

    /// Update the session with new data and last modified time.
    pub fn update(&mut self, data: String, now: SystemTime) {
        self.hash = Self::compute_hash(&data, now);
        self.data = data;
        self.last_modified = now;
    }

    /// Compute the hash of the data and timestamp.
    fn compute_hash(data: &str, now: SystemTime) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(data);
        let now_millis = now
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        hasher.update(now_millis.to_be_bytes());
        hasher.finalize().into()
    }

    /// The sequence token for the session.
    pub fn sequence_token(&self) -> String {
        URL_SAFE_NO_PAD.encode(self.hash)
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

    pub fn put_response(&self) -> PutResponse {
        PutResponse {
            sequence_token: self.sequence_token(),
        }
    }
}
