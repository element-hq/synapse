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

//! Pseudonymisation of identifiers.
//!
//! Every identifier that leaves the database goes through a keyed hash
//! (HMAC-SHA256 under a random per-run key, truncated). The key is never
//! written to the output, so the pseudonyms cannot be reversed or linked
//! back to the source without it, but the same input always maps to the
//! same pseudonym within a run, which preserves graph structure.

use std::collections::HashMap;

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Number of hex characters kept from the HMAC output. 64 bits is far more
/// than enough to avoid collisions for the entity counts involved.
const PSEUDONYM_HEX_LEN: usize = 16;

/// Short suffix used when an event/account-data type is not a spec (`m.*`)
/// type and must be hidden.
const TYPE_HASH_HEX_LEN: usize = 8;

pub struct Anonymiser {
    key: Vec<u8>,
    /// The homeserver's own server name, so that user IDs can be split into
    /// local and remote without the server name leaving the box.
    local_server: String,
}

impl Anonymiser {
    pub fn new(key: Vec<u8>, local_server: String) -> Self {
        Self { key, local_server }
    }

    pub fn local_server(&self) -> &str {
        &self.local_server
    }

    fn mac(&self, domain: &str, value: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(&self.key).expect("HMAC accepts any key length");
        mac.update(domain.as_bytes());
        mac.update(b"\0");
        mac.update(value.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    /// Pseudonym for an opaque identifier (room ID, event ID, device ID...).
    pub fn id(&self, domain: &str, value: &str) -> String {
        let mut s = self.mac(domain, value);
        s.truncate(PSEUDONYM_HEX_LEN);
        s
    }

    /// Pseudonym for a server name. The local server gets a fixed, obviously
    /// synthetic name so the generator can tell it apart.
    pub fn server(&self, server: &str) -> String {
        if server == self.local_server {
            "local".to_owned()
        } else {
            format!("s{}", self.id("server", server))
        }
    }

    /// Whether a Matrix ID (`@user:server`, `!room:server`) is on the local
    /// server. Server names may contain `:` (a port), so split on the first
    /// colon after the sigil, not the last.
    pub fn is_local(&self, matrix_id: &str) -> bool {
        server_of(matrix_id) == Some(self.local_server.as_str())
    }

    /// Pseudonym for a user ID, keeping the local/remote split visible.
    pub fn user(&self, user_id: &str) -> String {
        let server = server_of(user_id).unwrap_or("");
        format!("@{}:{}", self.id("user", user_id), self.server(server))
    }

    pub fn room(&self, room_id: &str) -> String {
        format!("!{}", self.id("room", room_id))
    }

    /// Event / account-data / room types. Spec (`m.*`) types are low-entropy
    /// and needed verbatim for the synthetic data to behave the same way;
    /// anything else is replaced by a hash so that cardinality survives but
    /// the name does not.
    pub fn type_name(&self, t: &str) -> String {
        if t.starts_with("m.") {
            t.to_owned()
        } else {
            let mut s = self.mac("type", t);
            s.truncate(TYPE_HASH_HEX_LEN);
            format!("x.{s}")
        }
    }
}

/// The server part of a Matrix ID, i.e. everything after the first `:`.
pub fn server_of(matrix_id: &str) -> Option<&str> {
    matrix_id.split_once(':').map(|(_, s)| s)
}

/// Dense integer indexes for rooms and users, so that the bulk sections
/// (memberships, rates) can reference them in a few bytes. Keyed by the raw
/// database ID, which stays in memory only.
#[derive(Default)]
pub struct Indexer {
    map: HashMap<String, u32>,
}

impl Indexer {
    pub fn get(&self, raw_id: &str) -> Option<u32> {
        self.map.get(raw_id).copied()
    }

    /// Insert `raw_id` with the next free index, or return the existing one.
    pub fn insert(&mut self, raw_id: &str) -> u32 {
        let next = self.map.len() as u32;
        *self.map.entry(raw_id.to_owned()).or_insert(next)
    }

    /// Insert with a specific index (used when resuming from an earlier run).
    pub fn insert_at(&mut self, raw_id: String, idx: u32) {
        self.map.insert(raw_id, idx);
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }
}

/// Log2 histogram bucket for a size or count: 0 for 0, else floor(log2(n))+1.
pub fn log2_bucket(n: u64) -> usize {
    if n == 0 {
        0
    } else {
        (64 - n.leading_zeros()) as usize
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn anon() -> Anonymiser {
        Anonymiser::new(b"test-key".to_vec(), "example.org".to_owned())
    }

    #[test]
    fn pseudonyms_are_stable_and_distinct() {
        let a = anon();
        assert_eq!(a.room("!abc:example.org"), a.room("!abc:example.org"));
        assert_ne!(a.room("!abc:example.org"), a.room("!abd:example.org"));
        // Different domains never collide for the same input.
        assert_ne!(a.id("room", "x"), a.id("user", "x"));
    }

    #[test]
    fn user_pseudonym_keeps_locality() {
        let a = anon();
        assert!(a.user("@alice:example.org").ends_with(":local"));
        assert!(!a.user("@bob:other.org").ends_with(":local"));
        assert!(a.is_local("@alice:example.org"));
        assert!(!a.is_local("@alice:example.org.evil"));
        assert_eq!(server_of("@a:host:8448"), Some("host:8448"));
    }

    #[test]
    fn spec_types_survive_custom_types_do_not() {
        let a = anon();
        assert_eq!(a.type_name("m.room.message"), "m.room.message");
        let t = a.type_name("io.element.secret");
        assert!(t.starts_with("x."));
        assert!(!t.contains("element"));
    }

    #[test]
    fn log2_buckets() {
        assert_eq!(log2_bucket(0), 0);
        assert_eq!(log2_bucket(1), 1);
        assert_eq!(log2_bucket(2), 2);
        assert_eq!(log2_bucket(3), 2);
        assert_eq!(log2_bucket(1024), 11);
    }
}
