/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2025 Element Creations, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
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
    expires_ts: u64,
}

#[derive(Serialize)]
pub struct GetResponse {
    data: String,
    sequence_token: String,
    expires_ts: u64,
}

#[derive(Serialize)]
pub struct PutResponse {
    sequence_token: String,
}

impl Session {
    /// Create a new session with the given data and time-to-live.
    pub fn new(id: Ulid, data: String, now: SystemTime, ttl: Duration) -> Self {
        let hash = Sha256::digest(&data).into();
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
        self.hash = Sha256::digest(&data).into();
        self.data = data;
        self.last_modified = now;
    }

    /// The sequence token for the session.
    pub fn sequence_token(&self) -> String {
        let encoded = URL_SAFE_NO_PAD.encode(self.hash);
        format!("\"{encoded}\"")
            .parse()
            .expect("base64-encoded hash as sequence token should be valid")
    }

    pub fn get_response(&self) -> GetResponse {
        GetResponse {
            data: self.data.clone(),
            sequence_token: self.sequence_token(),
            expires_ts: self
                .expires
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64,
        }
    }

    pub fn post_response(&self) -> PostResponse {
        PostResponse {
            id: self.id.to_string(),
            sequence_token: self.sequence_token(),
            expires_ts: self
                .expires
                .duration_since(UNIX_EPOCH)
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
