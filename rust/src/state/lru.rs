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

//! The caches on the Rust state resolution handler.
//!
//! [`SizedLru`] is a bounded, least recently used cache whose bound is the
//! sum of the sizes its caller assigns to the entries. Entries also expire
//! after a period without use, so a room that goes quiet does not pin its
//! events in memory for ever.
//!
//! - [`ConflictCache`]: resolved conflicted sets, the Rust counterpart of
//!   `StateResolutionHandler._conflict_resolution_cache`. Keyed on the digest
//!   from `_conflict_cache_key` (see [`super::v2::resolve`]), sized by the
//!   number of state entries in each cached map.
//! - [`EventCache`]: events loaded from the database for state resolution,
//!   keyed on event ID and sized by count. The state resolution reads events
//!   as they are (unredacted, rejected ones included), so a cached event only
//!   goes stale when its rejection status or internal metadata changes.
//!   `EventsWorkerStore` invalidates it alongside its own event cache, by
//!   event ID and on a room purge, so the two stay in step.

use std::{
    collections::{BTreeMap, HashMap},
    hash::Hash,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use super::{StateMap, StateResEvent};

/// A conflict cache key: the sha256 digest of the conflicted set and base state.
pub type CacheKey = [u8; 32];

pub type ConflictCache = SizedLru<CacheKey, Arc<StateMap>>;

pub type EventCache = SizedLru<Arc<str>, StateResEvent>;

struct Entry<V> {
    value: V,
    /// Position in the recency order.
    seq: u64,
    last_used: Instant,
    size: usize,
}

struct Inner<K, V> {
    entries: HashMap<K, Entry<V>>,
    /// Recency order: sequence number to key, oldest first.
    order: BTreeMap<u64, K>,
    next_seq: u64,
    total_size: usize,
}

impl<K, V> Default for Inner<K, V> {
    fn default() -> Self {
        Inner {
            entries: HashMap::new(),
            order: BTreeMap::new(),
            next_seq: 0,
            total_size: 0,
        }
    }
}

impl<K: Hash + Eq + Clone, V> Inner<K, V> {
    fn remove(&mut self, key: &K) {
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

/// A bounded cache evicting the least recently used entry first. See the
/// module docs.
pub struct SizedLru<K, V> {
    inner: Mutex<Inner<K, V>>,
    max_size: usize,
    expiry: Duration,
}

impl<K: Hash + Eq + Clone, V: Clone> SizedLru<K, V> {
    /// A cache whose entries' sizes sum to at most `max_size`, dropping
    /// entries unused for `expiry`.
    pub fn new(max_size: usize, expiry: Duration) -> Self {
        SizedLru {
            inner: Mutex::new(Inner::default()),
            max_size,
            expiry,
        }
    }

    pub fn get(&self, key: &K) -> Option<V> {
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
        let value = entry.value.clone();
        inner.order.remove(&old_seq);
        inner.order.insert(seq, key.clone());
        Some(value)
    }

    /// Insert `value` under `key`, counting `size` (at least 1) towards the
    /// bound. A value bigger than the whole cache is not cached.
    pub fn insert(&self, key: K, value: V, size: usize) {
        let size = size.max(1);
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
        inner.order.insert(seq, key.clone());
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

    /// Drop the entry under `key`, if any.
    pub fn remove(&self, key: &K) {
        self.inner.lock().expect("not poisoned").remove(key);
    }

    /// The number of cached entries.
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

impl ConflictCache {
    /// Cache a resolved conflicted set, sized by its number of state entries
    /// as `_state_map_size` does.
    pub fn insert_state(&self, key: CacheKey, value: Arc<StateMap>) {
        let size = value.len();
        self.insert(key, value, size);
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
        cache.insert_state([1; 32], map(4));
        cache.insert_state([2; 32], map(4));
        // Use 1, so 2 is the oldest.
        assert!(cache.get(&[1; 32]).is_some());
        cache.insert_state([3; 32], map(4));
        assert!(cache.get(&[1; 32]).is_some());
        assert!(cache.get(&[2; 32]).is_none());
        assert!(cache.get(&[3; 32]).is_some());

        // Too big to cache at all.
        cache.insert_state([4; 32], map(11));
        assert!(cache.get(&[4; 32]).is_none());
        assert_eq!(cache.len(), 2);
    }

    #[test]
    fn expires_unused_entries() {
        let cache = ConflictCache::new(10, Duration::ZERO);
        cache.insert_state([1; 32], map(1));
        std::thread::sleep(Duration::from_millis(2));
        assert!(cache.get(&[1; 32]).is_none());
        assert!(cache.is_empty());
    }

    #[test]
    fn counts_entries_by_one() {
        let cache: SizedLru<u8, u8> = SizedLru::new(2, Duration::from_secs(60));
        cache.insert(1, 1, 1);
        cache.insert(2, 2, 1);
        cache.insert(3, 3, 1);
        assert!(cache.get(&1).is_none());
        assert_eq!(cache.get(&2), Some(2));
        assert_eq!(cache.get(&3), Some(3));
    }
}
