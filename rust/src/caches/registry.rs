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

//! Every Rust cache of a homeserver, and age-based expiry across them.
//!
//! Synapse drops cache entries that haven't been touched for a while. It is on
//! by default, with a 30 minute TTL (`expire_caches` / `cache_entry_ttl`), so
//! caches here have to take part too, or a Rust cache would sit on memory where
//! its Python equivalent wouldn't.
//!
//! Python drives this from the existing 30 second looping call in
//! `synapse.util.caches.lrucache`, which calls `RustRuntime.expire_caches`
//! alongside its own walk. One tick, one set of config knobs.
//!
//! Unlike Python, which threads every entry of every cache onto one global
//! linked list, this expiry decomposes per cache: each cache's LRU order is
//! also its age order, so each can find its own stale entries and stop at the
//! first live one. (Memory-pressure eviction, which *does* need a global
//! coldest-first order, isn't implemented yet. It will need a k-way merge over
//! the per-cache LRU tails rather than a global list.)

use std::{
    hash::Hash,
    sync::{Arc, Mutex, MutexGuard, Weak},
    time::{Duration, SystemTime},
};

use crate::caches::{
    lock_ignoring_poison,
    python::{CacheHandle, PyCacheKey},
    Cache,
};

/// Every cache built for one homeserver, weakly.
///
/// Weakly, because a cache is owned by the Rust `Store` that reads through it,
/// and it should go when that does.
///
/// One of these lives on each `RustRuntimeInner`, so there is nothing
/// process-wide here: a homeserver's expiry tick only ever reaches its own
/// caches. That matters in tests, which run several homeservers in one
/// process.
pub struct CacheRegistry {
    caches: Mutex<Vec<Weak<dyn CacheHandle>>>,
}

impl Default for CacheRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl CacheRegistry {
    pub fn new() -> Self {
        CacheRegistry {
            caches: Mutex::new(Vec::new()),
        }
    }

    /// Build a cache holding at most `max_size` entries, enrolled for this
    /// homeserver's age-based expiry.
    ///
    /// `name` is the name Synapse knows the cache by: the datastore attribute
    /// it is bound under, the metrics label, and the `cache_func` of its rows
    /// on the cache invalidation stream.
    pub fn new_cache<K, V>(&self, name: &'static str, max_size: usize) -> Arc<Cache<K, V>>
    where
        K: PyCacheKey + Eq + Hash + Clone + Send + Sync + 'static,
        V: Clone + Send + Sync + 'static,
    {
        let cache = Cache::new(name, max_size);

        // Via a second `Arc` to the same allocation, because the unsizing
        // coercion to `dyn CacheHandle` has to happen before we downgrade.
        let handle: Arc<dyn CacheHandle> = cache.clone();
        self.lock().push(Arc::downgrade(&handle));

        cache
    }

    /// Evict entries that haven't been read for `max_age` as of `now`, across
    /// every cache in this registry.
    ///
    /// `now` comes from the owning runtime's clock, so that the registry has
    /// no time source of its own. Returns the number of entries evicted.
    pub fn expire_caches(&self, now: SystemTime, max_age: Duration) -> usize {
        // The cutoff is before the epoch only under the virtual clock, which
        // tests start at the epoch. Nothing can be older than the clock
        // itself, so there is nothing to expire.
        match now.checked_sub(max_age) {
            Some(cutoff) => self.expire_before(cutoff),
            None => 0,
        }
    }

    /// Evict every entry not read since `cutoff`, across every cache in this
    /// registry. Returns the number of entries evicted.
    fn expire_before(&self, cutoff: SystemTime) -> usize {
        self.live()
            .iter()
            .map(|cache| cache.expire_entries_before(cutoff))
            .sum()
    }

    /// Every cache that is still alive, pruning the ones that aren't.
    ///
    /// A copy, so that callers work outside the registry lock: expiring takes
    /// each cache's own lock and drops the evicted values, and holding a
    /// second lock across that is how deadlocks get written.
    fn live(&self) -> Vec<Arc<dyn CacheHandle>> {
        let mut caches = self.lock();
        caches.retain(|cache| cache.strong_count() > 0);
        caches.iter().filter_map(Weak::upgrade).collect()
    }

    fn lock(&self) -> MutexGuard<'_, Vec<Weak<dyn CacheHandle>>> {
        lock_ignoring_poison(&self.caches)
    }
}

#[cfg(test)]
mod tests {
    use pyo3::{types::PyTuple, Bound};

    use super::*;
    use crate::caches::tests::at;

    /// Registered caches have to be invalidatable from Python. The tests here
    /// never do, so a key that decodes from nothing will do.
    impl PyCacheKey for &'static str {
        fn from_py_key(_key: &Bound<'_, PyTuple>) -> Option<Self> {
            None
        }
    }

    const THIRTY_SECS: Duration = Duration::from_secs(30);

    #[test]
    fn expiry_reaches_registered_caches_and_forgets_dead_ones() {
        let registry = CacheRegistry::new();

        let cache: Arc<Cache<&'static str, u32>> = registry.new_cache("registry_test", 10);
        cache.insert_at("a", 1, at(60));
        assert_eq!(cache.len(), 1);

        // Nothing is stale yet.
        assert_eq!(registry.expire_caches(at(80), THIRTY_SECS), 0);

        assert_eq!(registry.expire_caches(at(120), THIRTY_SECS), 1);
        assert_eq!(cache.len(), 0);

        // A dropped cache is pruned rather than left behind for ever.
        drop(cache);
        assert!(registry
            .lock()
            .iter()
            .any(|cache| cache.strong_count() == 0));
        registry.expire_caches(at(120), THIRTY_SECS);
        assert!(registry.lock().is_empty());
    }

    #[test]
    fn each_registry_only_reaches_its_own_caches() {
        let first = CacheRegistry::new();
        let second = CacheRegistry::new();

        let in_first: Arc<Cache<&'static str, u32>> = first.new_cache("first", 10);
        let in_second: Arc<Cache<&'static str, u32>> = second.new_cache("second", 10);
        in_first.insert_at("a", 1, at(60));
        in_second.insert_at("a", 1, at(60));

        assert_eq!(first.expire_caches(at(120), THIRTY_SECS), 1);
        assert_eq!(in_first.len(), 0);
        assert_eq!(in_second.len(), 1);
    }

    #[test]
    fn expiry_before_the_epoch_expires_nothing() {
        // The virtual clock starts at zero, so an expiry window longer than
        // the clock has been running must not count as "everything".
        let registry = CacheRegistry::new();
        let cache: Arc<Cache<&'static str, u32>> = registry.new_cache("epoch", 10);
        cache.insert_at("a", 1, at(0));
        assert_eq!(registry.expire_caches(at(10), THIRTY_SECS), 0);
        assert_eq!(cache.len(), 1);
    }
}
