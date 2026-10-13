/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! The cache of resolved conflicted sets, the Rust counterpart of
//! `StateResolutionHandler._conflict_resolution_cache`.
//!
//! Keyed on the digest from `_conflict_cache_key` (see
//! [`super::v2::resolve`]). Bounded by the total number of state entries
//! across the cached maps, evicting the least recently used entry first.
//! Entries also expire after a period without use.

use std::{
    collections::{BTreeMap, HashMap},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use super::StateMap;

/// A cache key: the sha256 digest of the conflicted set and base state.
pub type CacheKey = [u8; 32];

struct Entry {
    value: Arc<StateMap>,
    /// Position in the recency order.
    seq: u64,
    last_used: Instant,
    size: usize,
}

#[derive(Default)]
struct Inner {
    entries: HashMap<CacheKey, Entry>,
    /// Recency order: sequence number to key, oldest first.
    order: BTreeMap<u64, CacheKey>,
    next_seq: u64,
    total_size: usize,
}

impl Inner {
    fn remove(&mut self, key: &CacheKey) {
        if let Some(entry) = self.entries.remove(key) {
            self.order.remove(&entry.seq);
            self.total_size -= entry.size;
        }
    }

    fn bump(&mut self) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        seq
    }
}

pub struct ConflictCache {
    inner: Mutex<Inner>,
    max_size: usize,
    expiry: Duration,
}

impl ConflictCache {
    /// A cache holding at most `max_size` state entries in total, dropping
    /// entries unused for `expiry`.
    pub fn new(max_size: usize, expiry: Duration) -> Self {
        ConflictCache {
            inner: Mutex::default(),
            max_size,
            expiry,
        }
    }

    pub fn get(&self, key: &CacheKey) -> Option<Arc<StateMap>> {
        let mut inner = self.inner.lock().expect("not poisoned");
        let now = Instant::now();
        let expired = now.duration_since(inner.entries.get(key)?.last_used) > self.expiry;
        if expired {
            inner.remove(key);
            return None;
        }

        let seq = inner.bump();
        let entry = inner.entries.get_mut(key).expect("checked above");
        let old_seq = std::mem::replace(&mut entry.seq, seq);
        entry.last_used = now;
        let value = Arc::clone(&entry.value);
        inner.order.remove(&old_seq);
        inner.order.insert(seq, *key);
        Some(value)
    }

    pub fn insert(&self, key: CacheKey, value: Arc<StateMap>) {
        // As `_state_map_size`, but never 0.
        let size = value.len().max(1);
        if size > self.max_size {
            return;
        }

        let mut inner = self.inner.lock().expect("not poisoned");
        inner.remove(&key);

        while inner.total_size + size > self.max_size {
            let Some((_, oldest)) = inner.order.pop_first() else {
                break;
            };
            if let Some(entry) = inner.entries.remove(&oldest) {
                inner.total_size -= entry.size;
            }
        }

        let seq = inner.bump();
        inner.order.insert(seq, key);
        inner.total_size += size;
        inner.entries.insert(
            key,
            Entry {
                value,
                seq,
                last_used: Instant::now(),
                size,
            },
        );
    }

    /// The number of cached resolutions.
    pub fn len(&self) -> usize {
        self.inner.lock().expect("not poisoned").entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn clear(&self) {
        *self.inner.lock().expect("not poisoned") = Inner::default();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::StateKey;

    fn map(n: usize) -> Arc<StateMap> {
        Arc::new(
            (0..n)
                .map(|i| (StateKey::new("t", &i.to_string()), Arc::from("$e")))
                .collect(),
        )
    }

    #[test]
    fn evicts_least_recently_used_by_size() {
        let cache = ConflictCache::new(10, Duration::from_secs(60));
        cache.insert([1; 32], map(4));
        cache.insert([2; 32], map(4));
        // Use 1, so 2 is the oldest.
        assert!(cache.get(&[1; 32]).is_some());
        cache.insert([3; 32], map(4));
        assert!(cache.get(&[1; 32]).is_some());
        assert!(cache.get(&[2; 32]).is_none());
        assert!(cache.get(&[3; 32]).is_some());

        // Too big to cache at all.
        cache.insert([4; 32], map(11));
        assert!(cache.get(&[4; 32]).is_none());
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn expires_unused_entries() {
        let cache = ConflictCache::new(10, Duration::ZERO);
        cache.insert([1; 32], map(1));
        std::thread::sleep(Duration::from_millis(2));
        assert!(cache.get(&[1; 32]).is_none());
        assert!(cache.is_empty());
    }
}
