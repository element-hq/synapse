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

//! Caching for database functions that live in Rust.
//!
//! This is the Rust counterpart to `synapse.util.caches.descriptors.cached`:
//! a bounded LRU of loaded values, with in-flight requests for the same key
//! coalesced into one database round trip. A cache can also be filled
//! directly, with [`Cache::insert`], by code that loads values its own way.
//!
//! The bound is a weight: the sum of what a [`Weigher`] says each cached value
//! weighs. The default weigher counts every value as one, which makes the
//! bound a number of entries, as for `@cached`. A cache of state maps weighs
//! each by its entries instead, as Python's `size_callback` caches do, so a
//! few huge maps cannot pin more memory than many small ones.
//!
//! Invalidation deliberately stays in Python. Synapse invalidates caches by
//! *name*, resolving `getattr(store, cache_name)` and calling `.invalidate(key)`
//! on whatever it finds (`synapse/storage/_base.py`), so a cache here only has
//! to be bound onto the datastore under the right name and expose that method.
//! See [`python`]. Everything else (the replication stream, `txn.call_after`,
//! the `*_fake` names) then works unchanged.
//!
//! # Locking
//!
//! **Never hold [`Cache`]'s mutex across an `.await` or a GIL acquisition.**
//!
//! Unlike Python's caches, which are single-threaded by assertion, this one is
//! reached both from Tokio workers (which hold no GIL) and from the reactor
//! thread by way of invalidation (which does). Taking the GIL under the cache
//! lock would invert the order against those Tokio readers and deadlock. The
//! rule holds for value drops too. A cached value may own a `Py<PyAny>`, whose
//! drop needs the GIL, which is why every eviction path here *returns* the
//! values it removed rather than dropping them in place.
//!
//! Keys get no such treatment: they are dropped under the lock. So a key type
//! must not own Python objects. Every key so far is built from strings, and a
//! key that needs to hold a `Py<PyAny>` should extract what it hashes on
//! instead.

pub mod python;
pub mod registry;

use std::{
    collections::{HashMap, HashSet},
    fmt,
    future::Future,
    hash::Hash,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, MutexGuard,
    },
    time::SystemTime,
};

use futures::channel::oneshot;

use crate::clock::Clock;

/// An error from a load, shared with everyone who was waiting on it.
///
/// `anyhow::Error` isn't `Clone`, and a single failed load has to be handed to
/// an arbitrary number of waiters, so we share one behind an `Arc`.
#[derive(Debug, Clone)]
pub struct SharedError(Arc<anyhow::Error>);

impl SharedError {
    fn new(error: anyhow::Error) -> Self {
        SharedError(Arc::new(error))
    }

    /// The load's error, for a caller that needs to tell errors apart.
    pub fn inner(&self) -> &anyhow::Error {
        &self.0
    }
}

impl fmt::Display for SharedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // `{:#}` so the whole context chain survives. Otherwise wrapping this
        // back up in an `anyhow::Error` would lose everything but the outermost
        // message.
        write!(f, "{:#}", self.0)
    }
}

impl std::error::Error for SharedError {}

/// What a waiter on an in-flight load gets: the value, `None` if a batch
/// load did not find the key, or the load's error.
type LoadAnswer<V> = Result<Option<V>, SharedError>;

/// How much a cached value counts towards the cache's bound.
pub type Weigher<V> = fn(&V) -> usize;

/// The default [`Weigher`]: every value weighs one, so the bound is a number
/// of entries.
pub fn weigh_one<V>(_: &V) -> usize {
    1
}

/// What a cache indexes its values by, as well as their key: a string the
/// value holds. See [`Cache::new_indexed`].
pub type Indexer<V> = fn(&V) -> &str;

/// A cached value, and when it was last read.
struct Entry<V> {
    value: V,
    /// From [`Cache::clock`]. Used by the age-based expiry in [`registry`].
    last_read: SystemTime,
    /// What the value counted for when it was inserted.
    weight: usize,
}

struct Inner<K, V> {
    /// Unbounded as far as `lru` is concerned: the bound is [`Inner::max_weight`],
    /// which this module enforces itself.
    entries: lru::LruCache<K, Entry<V>>,

    /// The sum of the weights of the entries.
    total_weight: usize,

    /// The bound on `total_weight`, after any cache factor.
    max_weight: usize,

    /// Keys with a load in flight, and the id of that load.
    ///
    /// The id is what makes invalidating a key mid-load safe: a load only gets
    /// to populate the cache if the id it started with is still the one here.
    pending: HashMap<K, u64>,

    /// Callers waiting on an in-flight load, keyed by that load's id. A
    /// batch load ([`Cache::get_or_load_many`]) answers `None` for a key it
    /// did not find.
    waiters: HashMap<u64, Vec<oneshot::Sender<LoadAnswer<V>>>>,

    next_load_id: u64,

    /// The bound this cache was declared with, before any cache factor is
    /// applied. `set_cache_factor` scales this rather than the current bound,
    /// so that repeated resizes don't compound.
    original_max_weight: usize,

    /// The secondary index, for a cache built with [`Cache::new_indexed`].
    index: Option<SecondaryIndex<K, V>>,
}

/// The keys of a cache's entries, grouped by what [`Indexer`] says of each
/// value. Kept in step with `entries` by [`Inner::insert`],
/// [`Inner::remove`] and [`Inner::pop_lru`], which every path that adds or
/// removes an entry goes through.
struct SecondaryIndex<K, V> {
    index_of: Indexer<V>,
    keys: HashMap<Arc<str>, HashSet<K>>,
}

/// A bounded, LRU cache of values loaded from somewhere slow.
///
/// Create one with [`registry::CacheRegistry::new_cache`] and read through it
/// with [`Cache::get_or_load`]. See the module docs for the locking rule.
pub struct Cache<K, V> {
    name: &'static str,

    /// Stamps entries with their last access time. Read without the GIL, from
    /// under the cache lock. See [`crate::clock`].
    clock: Clock,

    weigher: Weigher<V>,

    hits: AtomicU64,
    misses: AtomicU64,
    evicted_size: AtomicU64,
    evicted_time: AtomicU64,
    evicted_invalidation: AtomicU64,

    inner: Mutex<Inner<K, V>>,
}

impl<K, V> Cache<K, V>
where
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    /// Build a cache holding at most `max_size` entries.
    ///
    /// This does not enrol the cache for age-based expiry. Go through
    /// [`registry::CacheRegistry::new_cache`] for that.
    ///
    /// `name` is the name Synapse knows the cache by: the datastore attribute,
    /// the metrics label, and what travels over the cache invalidation stream.
    pub fn new(name: &'static str, max_size: usize) -> Arc<Self> {
        Self::new_weighted(name, max_size, weigh_one)
    }

    /// Build a cache whose values' weights, as `weigher` measures them, sum
    /// to at most `max_weight`. A value heavier than the whole cache is never
    /// cached. Otherwise as [`Cache::new`].
    pub fn new_weighted(name: &'static str, max_weight: usize, weigher: Weigher<V>) -> Arc<Self> {
        Self::build(name, max_weight, weigher, None)
    }

    /// Build a cache of at most `max_size` entries that also indexes its
    /// entries by `index_of` of their values, so that
    /// [`Cache::invalidate_index`] can drop every entry under one index key
    /// without walking the whole cache. The event cache indexes by room, for
    /// a room purge.
    pub fn new_indexed(name: &'static str, max_size: usize, index_of: Indexer<V>) -> Arc<Self> {
        Self::build(
            name,
            max_size,
            weigh_one,
            Some(SecondaryIndex {
                index_of,
                keys: HashMap::new(),
            }),
        )
    }

    fn build(
        name: &'static str,
        max_weight: usize,
        weigher: Weigher<V>,
        index: Option<SecondaryIndex<K, V>>,
    ) -> Arc<Self> {
        let max_weight = non_zero(max_weight);
        Arc::new(Cache {
            name,
            clock: Clock::new(),
            weigher,
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
            evicted_size: AtomicU64::new(0),
            evicted_time: AtomicU64::new(0),
            evicted_invalidation: AtomicU64::new(0),
            inner: Mutex::new(Inner {
                entries: lru::LruCache::unbounded(),
                total_weight: 0,
                max_weight,
                pending: HashMap::new(),
                waiters: HashMap::new(),
                next_load_id: 0,
                original_max_weight: max_weight,
                index,
            }),
        })
    }

    /// Look `key` up, loading and caching it if it isn't here.
    ///
    /// Concurrent callers for the same key share a single load: the first
    /// caller runs `load`, the rest wait for its result.
    ///
    /// Failures are never cached. Every waiter on a failed load gets the
    /// error, and the next caller tries again. This matches
    /// `DeferredCache.set`, which evicts the key when the deferred errbacks.
    pub async fn get_or_load<F, Fut>(&self, key: K, load: F) -> Result<V, SharedError>
    where
        F: Fn() -> Fut,
        Fut: Future<Output = Result<V, anyhow::Error>>,
    {
        loop {
            let load_id = match self.start_load(&key) {
                Started::Lead(load_id) => load_id,
                Started::Cached(value) => return Ok(value),
                Started::Waiting(receiver) => match receiver.await {
                    // Someone else's load answered for us, so this is a hit,
                    // as joining an in-flight load is in `DeferredCache.get`.
                    // It is counted here rather than when we joined, because
                    // a leader can go away before it answers, and then we go
                    // round again and get charged as whatever we become.
                    Ok(Ok(Some(value))) => {
                        self.hits.fetch_add(1, Ordering::Relaxed);
                        return Ok(value);
                    }
                    Ok(Err(e)) => {
                        self.hits.fetch_add(1, Ordering::Relaxed);
                        return Err(e);
                    }
                    // A batch load did not find the key. This load may
                    // know better, so go round again.
                    Ok(Ok(None)) => continue,
                    // The sender went away, which means the caller that was
                    // loading this key was dropped or panicked before it could
                    // finish. Go round again. One of us will pick the work up.
                    Err(_) => continue,
                },
            };

            // From here until `finish_load` we own the pending marker for this
            // key, and must clear it however we leave. The guard covers the
            // paths we can't: a panic in `load`, or this future being dropped.
            let guard = LoadGuard {
                cache: self,
                key: &key,
                load_id,
                armed: true,
            };

            let result = load().await.map_err(SharedError::new);

            // We made it, so hand the bookkeeping over to `finish_load`.
            guard.finish();

            return self.finish_load(key, load_id, result);
        }
    }

    /// Look `keys` up, loading those that aren't here with one call to
    /// `load`. The answer leaves out the keys that `load` did not find, and
    /// may hold extra keys `load` returned, which are not cached.
    ///
    /// Each key coalesces with the loads in flight as in
    /// [`Cache::get_or_load`], whether those are single or batched: a key
    /// that another caller is loading is waited for rather than loaded
    /// again, and the rest are loaded together. A key invalidated while its
    /// load is in flight is answered but not cached.
    ///
    /// `load` runs again, for the keys still missing, only if a caller we
    /// were waiting on went away before answering.
    pub async fn get_or_load_many<F, Fut>(
        &self,
        keys: &[K],
        mut load: F,
    ) -> Result<HashMap<K, V>, SharedError>
    where
        F: FnMut(Vec<K>) -> Fut,
        Fut: Future<Output = Result<HashMap<K, V>, anyhow::Error>>,
    {
        let mut found: HashMap<K, V> = HashMap::with_capacity(keys.len());
        let mut wanted: Vec<K> = Vec::with_capacity(keys.len());
        let mut seen: HashSet<&K> = HashSet::with_capacity(keys.len());
        for key in keys {
            if seen.insert(key) {
                wanted.push(key.clone());
            }
        }

        while !wanted.is_empty() {
            let StartedLoads { leads, waiting } = self.start_loads(&wanted, &mut found);
            wanted.clear();

            if !leads.is_empty() {
                // As in `get_or_load`, the guard clears our pending markers
                // if `load` panics or this future is dropped.
                let guard = BatchLoadGuard {
                    cache: self,
                    leads: &leads,
                    armed: true,
                };
                let lead_keys = leads.iter().map(|(key, _)| key.clone()).collect();
                let result = load(lead_keys).await.map_err(SharedError::new);
                guard.finish();
                found.extend(self.finish_loads(leads, result)?);
            }

            for (key, receiver) in waiting {
                match receiver.await {
                    Ok(Ok(Some(value))) => {
                        self.hits.fetch_add(1, Ordering::Relaxed);
                        found.insert(key, value);
                    }
                    // The load we joined did not find it either.
                    Ok(Ok(None)) => {}
                    Ok(Err(e)) => return Err(e),
                    // The caller loading it went away. Go round again.
                    Err(_) => wanted.push(key),
                }
            }
        }

        Ok(found)
    }

    /// Drop `key` from the cache, and stop any in-flight load from caching what
    /// it finds.
    ///
    /// Returns the value that was cached, for the caller to drop.
    ///
    /// Callers already waiting on an in-flight load still get their result.
    /// It just doesn't get cached. That matches Python, where invalidating a
    /// key drops the pending deferred from the cache without disturbing the
    /// `ObservableDeferred` its observers are waiting on.
    pub fn invalidate(&self, key: &K) -> Option<V> {
        let mut inner = self.lock();

        // Any load in flight for this key started before we knew the value had
        // changed, so whatever it returns may already be stale.
        inner.pending.remove(key);

        let removed = inner.remove(key);
        if removed.is_some() {
            self.evicted_invalidation.fetch_add(1, Ordering::Relaxed);
        }

        removed
    }

    /// Drop every entry whose key satisfies `matches`, and stop in-flight
    /// loads of such keys from caching what they find. Returns the values
    /// that were cached, for the caller to drop.
    ///
    /// This walks the whole cache, as there is no index by prefix. It is
    /// what a `tree=True` Python cache does with a partial key, for a cache
    /// such as the unread counts, which is invalidated by room as well as
    /// by room and user. See [`python::PyCacheKey::prefix_matcher`].
    pub fn invalidate_matching(&self, matches: impl Fn(&K) -> bool) -> Vec<V> {
        let mut inner = self.lock();

        inner.pending.retain(|key, _| !matches(key));

        let keys: Vec<K> = inner
            .entries
            .iter()
            .filter(|(key, _)| matches(key))
            .map(|(key, _)| key.clone())
            .collect();
        let mut removed = Vec::with_capacity(keys.len());
        for key in &keys {
            if let Some(value) = inner.remove(key) {
                removed.push(value);
            }
        }
        self.evicted_invalidation
            .fetch_add(removed.len() as u64, Ordering::Relaxed);

        removed
    }

    /// Drop every entry whose value the cache's [`Indexer`] maps to
    /// `index_key`. Returns the values that were cached, for the caller to
    /// drop. Does nothing for a cache built without an index.
    ///
    /// In-flight loads are left alone, as their values, and so their index
    /// keys, aren't known yet. A caller that needs those stopped too must
    /// invalidate their keys, or the whole cache.
    pub fn invalidate_index(&self, index_key: &str) -> Vec<V> {
        let mut inner = self.lock();

        let keys = match &mut inner.index {
            Some(index) => index.keys.remove(index_key).unwrap_or_default(),
            None => return Vec::new(),
        };

        let mut removed = Vec::with_capacity(keys.len());
        for key in &keys {
            if let Some(value) = inner.remove(key) {
                removed.push(value);
            }
        }
        self.evicted_invalidation
            .fetch_add(removed.len() as u64, Ordering::Relaxed);

        removed
    }

    /// Stop every in-flight load from caching what it finds, leaving the
    /// cached entries alone. Callers waiting on those loads still get their
    /// results.
    ///
    /// For an invalidation that cannot name the keys of the loads it
    /// affects, such as [`Cache::invalidate_index`].
    pub fn abandon_loads(&self) {
        self.lock().pending.clear();
    }

    /// Empty the cache, and stop every in-flight load from caching what it
    /// finds. Returns the values that were cached, for the caller to drop.
    pub fn invalidate_all(&self) -> Vec<V> {
        let mut inner = self.lock();

        inner.pending.clear();

        let mut removed = Vec::with_capacity(inner.entries.len());
        while let Some(value) = inner.pop_lru() {
            removed.push(value);
        }

        self.evicted_invalidation
            .fetch_add(removed.len() as u64, Ordering::Relaxed);

        removed
    }

    /// The cached value for `key`, counted as a hit, or `None`.
    ///
    /// `None` is not counted as a miss. This is the synchronous fast path for
    /// a caller that will go on to [`Cache::get_or_load`], which counts that
    /// lookup, if the value isn't here.
    pub fn get(&self, key: &K) -> Option<V> {
        self.lookup(key, Counted::HitsOnly)
    }

    /// Look `key` up, as [`Cache::get`], and count `None` as a miss. For a
    /// caller that loads what it doesn't find its own way and fills the cache
    /// with [`Cache::insert`].
    pub fn get_or_count_miss(&self, key: &K) -> Option<V> {
        self.lookup(key, Counted::HitsAndMisses)
    }

    /// Look `key` up, as [`Cache::get`], without touching the hit or miss
    /// counts. The entry is still marked as read. For Python's
    /// `update_metrics=False`.
    pub fn get_uncounted(&self, key: &K) -> Option<V> {
        self.lookup(key, Counted::Nothing)
    }

    /// Mark `key`'s entry as read, as a hit would, without cloning its value.
    /// Returns whether the key was cached.
    ///
    /// Not counted: the caller already has the value from somewhere else,
    /// and decides whether that counts as a hit, with [`Cache::count_hit`].
    pub fn touch(&self, key: &K) -> bool {
        self.lock().touch(key, self.clock.now())
    }

    /// Mark `key`'s entry as read, as [`Cache::touch`] does, or if the key
    /// isn't cached, cache `value()` under it. One lock, so an entry another
    /// thread inserts meanwhile is never overwritten. Returns the values an
    /// insert evicted, for the caller to drop.
    pub fn touch_or_insert_with(&self, key: K, value: impl FnOnce() -> V) -> Vec<V> {
        let now = self.clock.now();
        let mut inner = self.lock();
        if inner.touch(&key, now) {
            return Vec::new();
        }
        let value = value();
        let weight = (self.weigher)(&value);
        let displaced = inner.insert(key, value, weight, now);
        self.evicted_size
            .fetch_add(displaced.evicted.len() as u64, Ordering::Relaxed);
        // The key was absent, so nothing was under it.
        debug_assert!(displaced.old.is_none());
        displaced.evicted
    }

    /// Count a hit answered outside the cache, such as from a caller's own
    /// memo of values it handed out.
    pub fn count_hit(&self) {
        self.hits.fetch_add(1, Ordering::Relaxed);
    }

    /// Count a miss that happened outside the cache: the caller looked
    /// somewhere else first and found nothing there either.
    pub fn count_miss(&self) {
        self.misses.fetch_add(1, Ordering::Relaxed);
    }

    fn lookup(&self, key: &K, counted: Counted) -> Option<V> {
        let value = self.lock().get(key, self.clock.now());
        match (&value, counted) {
            (Some(_), Counted::HitsOnly | Counted::HitsAndMisses) => {
                self.hits.fetch_add(1, Ordering::Relaxed);
            }
            (None, Counted::HitsAndMisses) => {
                self.misses.fetch_add(1, Ordering::Relaxed);
            }
            _ => {}
        }
        value
    }

    /// Cache `value` under `key`, replacing what was there. Returns the
    /// values this displaced (the old value, and any cold entries evicted to
    /// make room) for the caller to drop.
    ///
    /// For code that loads values its own way, as `prefill` is for a Python
    /// cache. It leaves any in-flight load for the key alone: that load
    /// still answers its waiters, and caches what it found if nothing
    /// invalidated the key meanwhile, which is at least as fresh as `value`.
    pub fn insert(&self, key: K, value: V) -> Vec<V> {
        let weight = (self.weigher)(&value);
        let mut displaced = self.lock().insert(key, value, weight, self.clock.now());
        self.evicted_size
            .fetch_add(displaced.evicted.len() as u64, Ordering::Relaxed);
        if let Some(old) = displaced.old.take() {
            self.evicted_invalidation.fetch_add(1, Ordering::Relaxed);
            displaced.evicted.push(old);
        }
        displaced.evicted
    }

    /// Evict every entry that hasn't been read since `cutoff`. Returns the
    /// evicted values, for the caller to drop.
    pub fn expire_before(&self, cutoff: SystemTime) -> Vec<V> {
        let expired = self.lock().expire_before(cutoff);

        self.evicted_time
            .fetch_add(expired.len() as u64, Ordering::Relaxed);

        expired
    }

    /// When the coldest entry was last read, or `None` if the cache is empty.
    pub fn oldest_access(&self) -> Option<SystemTime> {
        self.lock().oldest_access()
    }

    /// Evict up to `max` of the coldest entries, as long as they were last
    /// read before `before`. Returns the evicted values, for the caller to
    /// drop.
    ///
    /// For memory-pressure eviction, which works across every cache from the
    /// coldest entry up. See [`registry::CacheRegistry::evict_coldest`].
    /// Counted as evictions for size, as the cache is too big for the memory
    /// it is allowed.
    pub fn evict_coldest(&self, before: SystemTime, max: usize) -> Vec<V> {
        let evicted = self.lock().evict_coldest(before, max);

        self.evicted_size
            .fetch_add(evicted.len() as u64, Ordering::Relaxed);

        evicted
    }

    /// Rescale the cache's bound to `original * factor`, evicting from the
    /// cold end if it shrank. Returns the evicted values, for the caller to
    /// drop.
    ///
    /// This is what `SYNAPSE_CACHE_FACTOR_*` and `caches.per_cache_factors`
    /// drive, via `synapse.config.cache.add_resizable_cache`.
    pub fn set_cache_factor(&self, factor: f64) -> Vec<V> {
        let mut inner = self.lock();

        let new_max = non_zero((inner.original_max_weight as f64 * factor) as usize);
        if new_max == inner.max_weight {
            return Vec::new();
        }
        inner.max_weight = new_max;

        let evicted = inner.evict_to_fit(0);

        self.evicted_size
            .fetch_add(evicted.len() as u64, Ordering::Relaxed);

        evicted
    }

    /// The number of cached entries.
    pub fn len(&self) -> usize {
        self.lock().entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The sum of the cached values' weights. The same as [`Cache::len`] for
    /// a cache built with [`Cache::new`].
    pub fn size(&self) -> usize {
        self.lock().total_weight
    }

    /// The bound on [`Cache::size`].
    pub fn max_size(&self) -> usize {
        self.lock().max_weight
    }

    pub fn name(&self) -> &'static str {
        self.name
    }

    /// `(hits, misses)`.
    pub fn hit_counts(&self) -> (u64, u64) {
        (
            self.hits.load(Ordering::Relaxed),
            self.misses.load(Ordering::Relaxed),
        )
    }

    /// Eviction counts as `(size, time, invalidation)`, matching Python's
    /// `EvictionReason`.
    pub fn eviction_counts(&self) -> (u64, u64, u64) {
        (
            self.evicted_size.load(Ordering::Relaxed),
            self.evicted_time.load(Ordering::Relaxed),
            self.evicted_invalidation.load(Ordering::Relaxed),
        )
    }

    fn lock(&self) -> MutexGuard<'_, Inner<K, V>> {
        lock_ignoring_poison(&self.inner)
    }

    /// Decide what this caller has to do to get a value for `key`: nothing (it
    /// is cached), wait for someone else's load, or run the load itself.
    fn start_load(&self, key: &K) -> Started<V> {
        let mut inner = self.lock();

        if let Some(value) = inner.get(key, self.clock.now()) {
            self.hits.fetch_add(1, Ordering::Relaxed);
            return Started::Cached(value);
        }

        if let Some(&load_id) = inner.pending.get(key) {
            // Counted as a hit once the load answers, in `get_or_load`.
            let (sender, receiver) = oneshot::channel();
            inner.waiters.entry(load_id).or_default().push(sender);
            return Started::Waiting(receiver);
        }

        self.misses.fetch_add(1, Ordering::Relaxed);

        let load_id = inner.next_load_id;
        inner.next_load_id += 1;
        inner.pending.insert(key.clone(), load_id);

        Started::Lead(load_id)
    }

    /// [`Cache::start_load`] for each of `keys`, under one lock: hits go
    /// into `found`, and the rest come back as the keys this caller loads,
    /// with their load ids, and the keys it waits on someone else for.
    fn start_loads(&self, keys: &[K], found: &mut HashMap<K, V>) -> StartedLoads<K, V> {
        let mut leads = Vec::new();
        let mut waiting = Vec::new();
        let mut inner = self.lock();
        let now = self.clock.now();
        for key in keys {
            if let Some(value) = inner.get(key, now) {
                self.hits.fetch_add(1, Ordering::Relaxed);
                found.insert(key.clone(), value);
                continue;
            }
            if let Some(&load_id) = inner.pending.get(key) {
                // Counted as a hit once the load answers.
                let (sender, receiver) = oneshot::channel();
                inner.waiters.entry(load_id).or_default().push(sender);
                waiting.push((key.clone(), receiver));
                continue;
            }
            self.misses.fetch_add(1, Ordering::Relaxed);
            let load_id = inner.next_load_id;
            inner.next_load_id += 1;
            inner.pending.insert(key.clone(), load_id);
            leads.push((key.clone(), load_id));
        }
        StartedLoads { leads, waiting }
    }

    /// [`Cache::finish_load`] for a batch: cache each loaded key that is
    /// still wanted, and answer everyone waiting on one, with `None` for a
    /// key the load did not find. Returns everything the load returned.
    fn finish_loads(
        &self,
        leads: Vec<(K, u64)>,
        result: Result<HashMap<K, V>, SharedError>,
    ) -> Result<HashMap<K, V>, SharedError> {
        let mut answers = Vec::new();
        let mut displaced_values = Vec::new();
        {
            let mut inner = self.lock();
            let now = self.clock.now();
            for (key, load_id) in leads {
                let still_current = inner.pending.get(&key) == Some(&load_id);
                if still_current {
                    inner.pending.remove(&key);
                }
                let waiters = inner.waiters.remove(&load_id).unwrap_or_default();
                let answer = match &result {
                    Ok(loaded) => {
                        let value = loaded.get(&key).cloned();
                        if let (Some(value), true) = (&value, still_current) {
                            let weight = (self.weigher)(value);
                            let mut displaced = inner.insert(key, value.clone(), weight, now);
                            // As in `finish_load`: what `insert` put there
                            // meanwhile is no fresher than this.
                            displaced.evicted.extend(displaced.old.take());
                            self.evicted_size
                                .fetch_add(displaced.evicted.len() as u64, Ordering::Relaxed);
                            displaced_values.extend(displaced.evicted);
                        }
                        Ok(value)
                    }
                    Err(e) => Err(e.clone()),
                };
                if !waiters.is_empty() {
                    answers.push((waiters, answer));
                }
            }
        }

        // With the lock released. See the module docs.
        drop(displaced_values);

        for (waiters, answer) in answers {
            for waiter in waiters {
                // An error means the waiter gave up, which is fine.
                let _ = waiter.send(answer.clone());
            }
        }

        result
    }

    /// Publish the result of a load: cache it if it is still wanted, and hand
    /// it to everyone who was waiting.
    fn finish_load(
        &self,
        key: K,
        load_id: u64,
        result: Result<V, SharedError>,
    ) -> Result<V, SharedError> {
        let (waiters, displaced) = {
            let mut inner = self.lock();

            // If this is no longer the current load for the key, something
            // invalidated it while we were away and what we have may be stale.
            // Hand it to our waiters, but don't cache it.
            let still_current = inner.pending.get(&key) == Some(&load_id);
            if still_current {
                inner.pending.remove(&key);
            }

            let waiters = inner.waiters.remove(&load_id).unwrap_or_default();

            let displaced = match &result {
                Ok(value) if still_current => {
                    let weight = (self.weigher)(value);
                    let mut displaced = inner.insert(key, value.clone(), weight, self.clock.now());
                    // `Cache::insert` may have filled the key while this load
                    // was in flight. What it put there is no fresher than
                    // this, so replacing it is not an invalidation.
                    displaced.evicted.extend(displaced.old.take());
                    self.evicted_size
                        .fetch_add(displaced.evicted.len() as u64, Ordering::Relaxed);
                    displaced.evicted
                }
                _ => Vec::new(),
            };

            (waiters, displaced)
        };

        // With the lock released, so that dropping a value that owns Python
        // objects can take the GIL. See the module docs.
        drop(displaced);

        for waiter in waiters {
            // An error means the waiter gave up, which is fine.
            let _ = waiter.send(result.clone().map(Some));
        }

        result
    }
}

/// The parts of the cache that depend on the time take it as an argument,
/// rather than reading [`Cache::clock`], so that the tests can drive them with
/// explicit timestamps and stay clear of the process-wide virtual clock.
impl<K, V> Inner<K, V>
where
    K: Eq + Hash + Clone,
    V: Clone,
{
    /// Look `key` up, stamping its entry as read at `now` and moving it to the
    /// head of the LRU.
    fn get(&mut self, key: &K, now: SystemTime) -> Option<V> {
        let entry = self.entries.get_mut(key)?;
        entry.last_read = now;
        Some(entry.value.clone())
    }

    /// [`Inner::get`] without the clone. Returns whether the key was here.
    fn touch(&mut self, key: &K, now: SystemTime) -> bool {
        match self.entries.get_mut(key) {
            Some(entry) => {
                entry.last_read = now;
                true
            }
            None => false,
        }
    }

    /// Insert an entry of `weight` read at `now`, evicting cold entries to
    /// make room. Returns whatever it displaced for the caller to drop.
    ///
    /// A value heavier than the whole cache is not inserted, and any entry
    /// under its key is removed, so that a key never maps to a value older
    /// than the one its caller just loaded.
    fn insert(&mut self, key: K, value: V, weight: usize, now: SystemTime) -> Displaced<V> {
        let weight = weight.max(1);
        let old = self.remove(&key);
        if weight > self.max_weight {
            return Displaced {
                old,
                evicted: Vec::new(),
            };
        }

        let evicted = self.evict_to_fit(weight);
        self.total_weight += weight;
        if let Some(index) = &mut self.index {
            // Allocates the index key only for the first entry under it.
            let index_key = (index.index_of)(&value);
            match index.keys.get_mut(index_key) {
                Some(keys) => {
                    keys.insert(key.clone());
                }
                None => {
                    index
                        .keys
                        .insert(Arc::from(index_key), HashSet::from([key.clone()]));
                }
            }
        }
        let entry = Entry {
            value,
            last_read: now,
            weight,
        };
        // The key was removed above, so nothing is pushed out here.
        self.entries.push(key, entry);

        Displaced { old, evicted }
    }

    /// Remove the entry under `key`, returning its value for the caller to
    /// drop.
    fn remove(&mut self, key: &K) -> Option<V> {
        let entry = self.entries.pop(key)?;
        self.total_weight -= entry.weight;
        self.unindex(key, &entry.value);
        Some(entry.value)
    }

    /// Pop the coldest entry, returning its value for the caller to drop.
    fn pop_lru(&mut self) -> Option<V> {
        let (key, entry) = self.entries.pop_lru()?;
        self.total_weight -= entry.weight;
        self.unindex(&key, &entry.value);
        Some(entry.value)
    }

    /// Take a removed entry out of the secondary index, if there is one.
    fn unindex(&mut self, key: &K, value: &V) {
        let Some(index) = &mut self.index else {
            return;
        };
        let index_key = (index.index_of)(value);
        if let Some(keys) = index.keys.get_mut(index_key) {
            keys.remove(key);
            if keys.is_empty() {
                index.keys.remove(index_key);
            }
        }
    }

    /// Pop cold entries until `extra` more weight fits under the bound.
    /// Returns the values for the caller to drop.
    fn evict_to_fit(&mut self, extra: usize) -> Vec<V> {
        let mut evicted = Vec::new();
        while self.total_weight + extra > self.max_weight {
            match self.pop_lru() {
                Some(value) => evicted.push(value),
                None => break,
            }
        }
        evicted
    }

    fn oldest_access(&self) -> Option<SystemTime> {
        self.entries.peek_lru().map(|(_, entry)| entry.last_read)
    }

    /// Pop up to `max` of the coldest entries read before `before`, returning
    /// the values for the caller to drop.
    fn evict_coldest(&mut self, before: SystemTime, max: usize) -> Vec<V> {
        let mut evicted = Vec::new();
        while evicted.len() < max && self.oldest_access().is_some_and(|read| read < before) {
            evicted.push(self.pop_lru().expect("oldest_access just found one"));
        }
        evicted
    }

    /// Pop every entry not read since `cutoff`, returning the values for the
    /// caller to drop.
    fn expire_before(&mut self, cutoff: SystemTime) -> Vec<V> {
        let mut expired = Vec::new();
        // `last_read` is stamped on read and reads move the entry to the
        // head, so the LRU order is also age order: we can stop at the first
        // entry that is still live rather than walking the whole cache.
        while self
            .entries
            .peek_lru()
            .is_some_and(|(_, entry)| entry.last_read <= cutoff)
        {
            let value = self.pop_lru().expect("peek_lru just succeeded");
            expired.push(value);
        }

        expired
    }
}

/// What an insert pushed out of the cache: the value that was under the key,
/// if any, and the cold entries evicted to make room. Both are the caller's
/// to drop, with the lock released.
struct Displaced<V> {
    old: Option<V>,
    evicted: Vec<V>,
}

/// Entry points that take the time as an argument, for tests of the age-based
/// expiry. Production code reads the clock, which tests must not pin: it is
/// process-wide, and `cargo test` runs them in parallel.
#[cfg(test)]
impl<K, V> Cache<K, V>
where
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    /// A hit at `now`, or `None` if the key isn't cached.
    fn get_at(&self, key: &K, now: SystemTime) -> Option<V> {
        self.lock().get(key, now)
    }

    /// Insert `value` as if it had been loaded at `now`.
    fn insert_at(&self, key: K, value: V, now: SystemTime) {
        let weight = (self.weigher)(&value);
        let displaced = self.lock().insert(key, value, weight, now);
        drop(displaced);
    }
}

/// Which outcomes of a lookup count towards the hit and miss counters.
#[derive(Clone, Copy)]
enum Counted {
    HitsOnly,
    HitsAndMisses,
    Nothing,
}

/// What [`Cache::start_load`] decided.
enum Started<V> {
    /// Nobody is loading this key, so the caller runs the load, under this id.
    Lead(u64),
    Cached(V),
    /// Someone else is loading this key. The result arrives here.
    Waiting(oneshot::Receiver<LoadAnswer<V>>),
}

/// What [`Cache::start_loads`] decided for the keys it did not find cached.
struct StartedLoads<K, V> {
    /// The keys the caller loads, with their load ids.
    leads: Vec<(K, u64)>,
    /// The keys someone else is loading, with where their answers arrive.
    waiting: Vec<(K, oneshot::Receiver<LoadAnswer<V>>)>,
}

/// Releases a key's pending marker if the load never completes.
///
/// A load that is cancelled (its future dropped) or panics would otherwise
/// leave the key marked as in flight forever, and every later caller would
/// queue up behind a load that is never going to finish.
struct LoadGuard<'a, K: Eq + Hash + Clone + Send + Sync + 'static, V: Clone + Send + Sync + 'static>
{
    cache: &'a Cache<K, V>,
    key: &'a K,
    load_id: u64,
    armed: bool,
}

impl<K: Eq + Hash + Clone + Send + Sync + 'static, V: Clone + Send + Sync + 'static>
    LoadGuard<'_, K, V>
{
    /// The load completed, so the pending marker is `finish_load`'s to clear.
    /// Consuming the guard also releases its borrow of the key.
    fn finish(mut self) {
        self.armed = false;
    }
}

impl<K: Eq + Hash + Clone + Send + Sync + 'static, V: Clone + Send + Sync + 'static> Drop
    for LoadGuard<'_, K, V>
{
    fn drop(&mut self) {
        if !self.armed {
            return;
        }

        let mut inner = self.cache.lock();

        if inner.pending.get(self.key) == Some(&self.load_id) {
            inner.pending.remove(self.key);
        }

        // Dropping the senders wakes everyone waiting on us with a cancelled
        // channel, which sends them back around to retry.
        inner.waiters.remove(&self.load_id);
    }
}

/// [`LoadGuard`] for the keys of a batch load.
struct BatchLoadGuard<
    'a,
    K: Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
> {
    cache: &'a Cache<K, V>,
    leads: &'a [(K, u64)],
    armed: bool,
}

impl<K: Eq + Hash + Clone + Send + Sync + 'static, V: Clone + Send + Sync + 'static>
    BatchLoadGuard<'_, K, V>
{
    fn finish(mut self) {
        self.armed = false;
    }
}

impl<K: Eq + Hash + Clone + Send + Sync + 'static, V: Clone + Send + Sync + 'static> Drop
    for BatchLoadGuard<'_, K, V>
{
    fn drop(&mut self) {
        if !self.armed {
            return;
        }

        let mut inner = self.cache.lock();
        for (key, load_id) in self.leads {
            if inner.pending.get(key) == Some(load_id) {
                inner.pending.remove(key);
            }
            inner.waiters.remove(load_id);
        }
    }
}

/// Take a lock, recovering from poisoning.
///
/// Nothing in a locked region in this module runs caller-supplied code, so a
/// panic while holding one means a bug here rather than corrupt state. Failing
/// every subsequent cache read would turn that into an outage. The worst that
/// carrying on can do is serve a stale entry.
pub(crate) fn lock_ignoring_poison<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A cache of size zero would be a cache that never caches anything, which is
/// never what anyone means. Treat it as one entry, as `LruCache` does for a
/// cache factor that rounds to nothing.
fn non_zero(size: usize) -> usize {
    size.max(1)
}

#[cfg(test)]
mod tests {
    use std::{
        pin::pin,
        sync::atomic::{AtomicUsize, Ordering},
        task::Poll,
        time::{Duration, UNIX_EPOCH},
    };

    use futures::{
        executor::block_on,
        future::{poll_fn, FutureExt},
        Future,
    };

    use super::*;

    /// `secs` seconds after the epoch, for the expiry tests.
    pub(super) fn at(secs: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(secs)
    }

    fn cache(max_size: usize) -> Arc<Cache<&'static str, u32>> {
        Cache::new("test_cache", max_size)
    }

    /// A loader that returns `value`, counting how many times it ran.
    fn counting_loader(
        value: u32,
        count: &AtomicUsize,
    ) -> impl Fn() -> std::future::Ready<Result<u32, anyhow::Error>> + '_ {
        move || {
            count.fetch_add(1, Ordering::SeqCst);
            std::future::ready(Ok(value))
        }
    }

    #[test]
    fn loads_once_then_hits() {
        let cache = cache(10);
        let loads = AtomicUsize::new(0);

        let first = block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        let second = block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();

        assert_eq!((first, second), (1, 1));
        assert_eq!(loads.load(Ordering::SeqCst), 1);
        assert_eq!(cache.hit_counts(), (1, 1));
    }

    #[test]
    fn evicts_the_least_recently_used() {
        let cache = cache(2);
        let loads = AtomicUsize::new(0);

        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        block_on(cache.get_or_load("b", counting_loader(2, &loads))).unwrap();
        // Touch "a" so that "b" becomes the coldest entry.
        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        block_on(cache.get_or_load("c", counting_loader(3, &loads))).unwrap();

        assert_eq!(cache.len(), 2);
        assert_eq!(cache.eviction_counts().0, 1);

        // "b" went, so looking it up loads again. "a" and "c" are still here.
        let loads_before = loads.load(Ordering::SeqCst);
        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        block_on(cache.get_or_load("c", counting_loader(3, &loads))).unwrap();
        assert_eq!(loads.load(Ordering::SeqCst), loads_before);
        block_on(cache.get_or_load("b", counting_loader(2, &loads))).unwrap();
        assert_eq!(loads.load(Ordering::SeqCst), loads_before + 1);
    }

    #[test]
    fn invalidate_drops_the_entry() {
        let cache = cache(10);
        let loads = AtomicUsize::new(0);

        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        assert_eq!(cache.invalidate(&"a"), Some(1));
        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();

        assert_eq!(loads.load(Ordering::SeqCst), 2);
        assert_eq!(cache.eviction_counts().2, 1);
    }

    #[test]
    fn invalidate_all_empties_the_cache() {
        let cache = cache(10);
        let loads = AtomicUsize::new(0);

        block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        block_on(cache.get_or_load("b", counting_loader(2, &loads))).unwrap();

        let mut dropped = cache.invalidate_all();
        dropped.sort_unstable();

        assert_eq!(dropped, vec![1, 2]);
        assert_eq!(cache.len(), 0);
    }

    #[test]
    fn errors_are_not_cached() {
        let cache = cache(10);
        let loads = AtomicUsize::new(0);

        let failing = || {
            loads.fetch_add(1, Ordering::SeqCst);
            std::future::ready(Err(anyhow::anyhow!("nope")))
        };

        assert!(block_on(cache.get_or_load("a", failing)).is_err());
        assert_eq!(cache.len(), 0);

        // The next caller tries again rather than getting the failure back.
        let value = block_on(cache.get_or_load("a", counting_loader(1, &loads))).unwrap();
        assert_eq!(value, 1);
        assert_eq!(loads.load(Ordering::SeqCst), 2);
    }

    /// A loader that blocks until released, so a test can hold a load open.
    struct GatedLoader<'a> {
        starts: &'a AtomicUsize,
        gate: futures::future::Shared<oneshot::Receiver<()>>,
        value: u32,
    }

    impl<'a> GatedLoader<'a> {
        fn new(value: u32, starts: &'a AtomicUsize) -> (Self, oneshot::Sender<()>) {
            let (release, gate) = oneshot::channel();
            (
                GatedLoader {
                    starts,
                    gate: gate.shared(),
                    value,
                },
                release,
            )
        }

        fn load(&self) -> impl Future<Output = Result<u32, anyhow::Error>> {
            self.starts.fetch_add(1, Ordering::SeqCst);

            let gate = self.gate.clone();
            let value = self.value;
            async move {
                let _ = gate.await;
                Ok(value)
            }
        }
    }

    /// The highest-risk case: a key invalidated while its load is in flight
    /// must not end up cached, or the cache holds a stale value that nothing
    /// will ever evict.
    #[test]
    fn invalidating_an_in_flight_load_does_not_cache_it() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (loader, release) = GatedLoader::new(7, &starts);

        block_on(async {
            let mut load = pin!(cache.get_or_load("a", || loader.load()));

            // Get as far as the loader, then leave it hanging.
            poll_fn(|cx| {
                assert!(load.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            assert_eq!(starts.load(Ordering::SeqCst), 1);

            cache.invalidate(&"a");
            release.send(()).unwrap();

            // The caller still gets its value...
            assert_eq!(load.await.unwrap(), 7);
        });

        // ...but the value is not cached, because it may already be stale.
        assert_eq!(cache.len(), 0);
    }

    #[test]
    fn concurrent_misses_share_one_load() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (loader, release) = GatedLoader::new(7, &starts);

        let values = block_on(async {
            let mut loads = vec![
                cache.get_or_load("a", || loader.load()).boxed_local(),
                cache.get_or_load("a", || loader.load()).boxed_local(),
                cache.get_or_load("a", || loader.load()).boxed_local(),
            ];

            // Let all three reach the cache: the first becomes the leader, the
            // other two queue up behind it.
            poll_fn(|cx| {
                for load in &mut loads {
                    assert!(load.poll_unpin(cx).is_pending());
                }
                Poll::Ready(())
            })
            .await;

            release.send(()).unwrap();
            futures::future::join_all(loads).await
        });

        assert_eq!(
            values.into_iter().map(Result::unwrap).collect::<Vec<_>>(),
            vec![7, 7, 7]
        );
        // Exactly one load ran, and only the leader was charged a miss.
        assert_eq!(starts.load(Ordering::SeqCst), 1);
        assert_eq!(cache.hit_counts(), (2, 1));
    }

    /// If the caller doing the load goes away, whoever is waiting on it has to
    /// pick the work up rather than wait forever.
    #[test]
    fn a_dropped_load_does_not_wedge_the_key() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (loader, _release) = GatedLoader::new(7, &starts);
        let loads = AtomicUsize::new(0);

        block_on(async {
            {
                let mut load = pin!(cache.get_or_load("a", || loader.load()));
                poll_fn(|cx| {
                    assert!(load.poll_unpin(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                // ...and now the leader is dropped, still holding the key.
            }

            let value = cache
                .get_or_load("a", counting_loader(1, &loads))
                .await
                .unwrap();
            assert_eq!(value, 1);
        });

        assert_eq!(loads.load(Ordering::SeqCst), 1);
    }

    /// A waiter whose leader goes away becomes the leader itself, and the
    /// counters see one miss for it, not a hit and then a miss.
    #[test]
    fn a_waiter_on_a_dropped_load_is_charged_once() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (gated, _release) = GatedLoader::new(7, &starts);
        let loads = AtomicUsize::new(0);

        let value = block_on(async {
            let mut waiter = pin!(cache.get_or_load("a", counting_loader(1, &loads)));
            {
                let mut leader = pin!(cache.get_or_load("a", || gated.load()));
                poll_fn(|cx| {
                    assert!(leader.poll_unpin(cx).is_pending());
                    assert!(waiter.poll_unpin(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                // The leader's future is dropped here, with the waiter queued
                // behind it. (`pin!` puts it on the stack, so leaving the block
                // is what drops it.)
            }

            waiter.await.unwrap()
        });

        assert_eq!(value, 1);
        assert_eq!(loads.load(Ordering::SeqCst), 1);
        assert_eq!(cache.hit_counts(), (0, 2));
    }

    #[test]
    fn get_answers_only_from_the_cache() {
        let cache = cache(10);

        // A miss here is not counted: the caller goes on to `get_or_load`,
        // which counts it.
        assert_eq!(cache.get(&"a"), None);
        assert_eq!(cache.hit_counts(), (0, 0));

        cache.insert_at("a", 1, at(1));
        assert_eq!(cache.get(&"a"), Some(1));
        assert_eq!(cache.hit_counts(), (1, 0));
    }

    #[test]
    fn expiry_takes_only_entries_past_the_cutoff() {
        let cache = cache(10);

        cache.insert_at("old", 1, at(1));
        cache.insert_at("new", 2, at(5));

        assert_eq!(cache.expire_before(at(3)), vec![1]);
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.eviction_counts().1, 1);

        // Nothing else is old enough yet, and the walk stops at the first live
        // entry rather than clearing the cache.
        assert!(cache.expire_before(at(3)).is_empty());
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn reading_an_entry_keeps_it_from_expiring() {
        let cache = cache(10);

        cache.insert_at("a", 1, at(1));
        cache.insert_at("b", 2, at(2));

        // Reading `a` restamps it and moves it to the head, so `b` is now the
        // coldest entry even though it was inserted later.
        assert_eq!(cache.get_at(&"a", at(5)), Some(1));

        assert_eq!(cache.expire_before(at(3)), vec![2]);
        assert_eq!(cache.len(), 1);
        assert!(cache.expire_before(at(3)).is_empty());
        assert_eq!(cache.get_at(&"a", at(6)), Some(1));
    }

    #[test]
    fn insert_fills_the_cache_directly() {
        let cache = cache(2);
        let loads = AtomicUsize::new(0);

        assert!(cache.insert("a", 1).is_empty());
        assert_eq!(cache.get(&"a"), Some(1));

        // A loaded key is replaced, and the old value handed back.
        assert_eq!(cache.insert("a", 2), vec![1]);
        assert_eq!(
            block_on(cache.get_or_load("a", counting_loader(9, &loads))).unwrap(),
            2
        );
        assert_eq!(loads.load(Ordering::SeqCst), 0);

        // Inserting past the bound evicts the coldest entry.
        cache.insert("b", 3);
        assert_eq!(cache.insert("c", 4), vec![2]);
        assert_eq!(cache.len(), 2);
        assert_eq!(cache.eviction_counts(), (1, 0, 1));
    }

    #[test]
    fn weighted_values_share_the_bound() {
        let cache: Arc<Cache<&'static str, Vec<u8>>> =
            Cache::new_weighted("weighted", 10, |value| value.len());

        cache.insert("a", vec![0; 4]);
        cache.insert("b", vec![0; 4]);
        assert_eq!((cache.len(), cache.size()), (2, 8));

        // Touch "a", so "b" is the coldest and goes when "c" needs room.
        assert!(cache.get(&"a").is_some());
        assert_eq!(cache.insert("c", vec![0; 4]), vec![vec![0; 4]]);
        assert!(cache.get(&"b").is_none());
        assert_eq!((cache.len(), cache.size()), (2, 8));

        // Too heavy to cache at all, and the key it would have replaced goes.
        cache.insert("a", vec![0; 11]);
        assert!(cache.get(&"a").is_none());
        assert_eq!((cache.len(), cache.size()), (1, 4));

        // The factor scales the weight bound.
        cache.insert("d", vec![0; 4]);
        assert_eq!(cache.set_cache_factor(0.5).len(), 1);
        assert_eq!((cache.max_size(), cache.size()), (5, 4));
    }

    #[test]
    fn cache_factor_resizes_and_evicts() {
        let cache = cache(4);
        let loads = AtomicUsize::new(0);

        for key in ["a", "b", "c", "d"] {
            block_on(cache.get_or_load(key, counting_loader(1, &loads))).unwrap();
        }
        assert_eq!(cache.len(), 4);

        // Halving the size evicts the two coldest entries.
        assert_eq!(cache.set_cache_factor(0.5).len(), 2);
        assert_eq!(cache.max_size(), 2);
        assert_eq!(cache.len(), 2);

        // The factor applies to the declared size, not the current one, so
        // going back to 1.0 restores the original.
        assert!(cache.set_cache_factor(1.0).is_empty());
        assert_eq!(cache.max_size(), 4);
    }

    /// A batch loader over `values`, recording the keys of each call.
    fn batch_loader<'a>(
        values: &'a [(&'static str, u32)],
        calls: &'a Mutex<Vec<Vec<&'static str>>>,
    ) -> impl FnMut(
        Vec<&'static str>,
    ) -> std::future::Ready<Result<HashMap<&'static str, u32>, anyhow::Error>>
           + 'a {
        move |keys| {
            let mut sorted = keys.clone();
            sorted.sort_unstable();
            calls.lock().unwrap().push(sorted);
            let found = values
                .iter()
                .filter(|(key, _)| keys.contains(key))
                .cloned()
                .collect();
            std::future::ready(Ok(found))
        }
    }

    #[test]
    fn a_batch_loads_only_the_misses_in_one_call() {
        let cache = cache(10);
        cache.insert("a", 1);
        let calls = Mutex::new(Vec::new());
        let values = [("b", 2), ("c", 3)];

        let found = block_on(cache.get_or_load_many(
            &["a", "b", "c", "b", "missing"],
            batch_loader(&values, &calls),
        ))
        .unwrap();

        assert_eq!(found, HashMap::from([("a", 1), ("b", 2), ("c", 3)]));
        assert_eq!(*calls.lock().unwrap(), vec![vec!["b", "c", "missing"]]);
        // Found keys are cached, a key that was not found is not.
        assert_eq!(cache.len(), 3);
        assert_eq!(cache.hit_counts(), (1, 3));
    }

    #[test]
    fn a_batch_returns_but_does_not_cache_extra_keys() {
        let cache = cache(10);
        let found = block_on(cache.get_or_load_many(&["a"], |_| {
            std::future::ready(Ok(HashMap::from([("a", 1), ("extra", 2)])))
        }))
        .unwrap();

        assert_eq!(found, HashMap::from([("a", 1), ("extra", 2)]));
        assert_eq!(cache.get(&"extra"), None);
        assert_eq!(cache.get(&"a"), Some(1));
    }

    #[test]
    fn a_batch_joins_a_load_in_flight() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (loader, release) = GatedLoader::new(7, &starts);
        let calls = Mutex::new(Vec::new());
        let values = [("a", 99), ("b", 2)];

        let (single, batch) = block_on(async {
            let mut single = pin!(cache.get_or_load("a", || loader.load()));
            poll_fn(|cx| {
                assert!(single.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;

            let mut batch =
                pin!(cache.get_or_load_many(&["a", "b"], batch_loader(&values, &calls)));
            poll_fn(|cx| {
                // "b" is loaded at once, and the batch waits on "a".
                assert!(batch.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;

            release.send(()).unwrap();
            (single.await.unwrap(), batch.await.unwrap())
        });

        assert_eq!(single, 7);
        assert_eq!(batch, HashMap::from([("a", 7), ("b", 2)]));
        assert_eq!(*calls.lock().unwrap(), vec![vec!["b"]]);
        assert_eq!(starts.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn invalidating_a_key_of_a_batch_in_flight_does_not_cache_it() {
        let cache = cache(10);
        let (release, gate) = oneshot::channel::<()>();
        let gate = gate.shared();

        block_on(async {
            let mut batch = pin!(cache.get_or_load_many(&["a", "b"], |keys| {
                let gate = gate.clone();
                async move {
                    let _ = gate.await;
                    Ok(keys.into_iter().map(|key| (key, 1)).collect())
                }
            }));
            poll_fn(|cx| {
                assert!(batch.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;

            cache.invalidate(&"a");
            release.send(()).unwrap();
            assert_eq!(batch.await.unwrap().len(), 2);
        });

        assert_eq!(cache.get(&"a"), None);
        assert_eq!(cache.get(&"b"), Some(1));
    }

    #[test]
    fn abandoned_loads_answer_but_do_not_cache() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (loader, release) = GatedLoader::new(7, &starts);

        block_on(async {
            let mut load = pin!(cache.get_or_load("a", || loader.load()));
            poll_fn(|cx| {
                assert!(load.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;

            cache.abandon_loads();
            release.send(()).unwrap();
            assert_eq!(load.await.unwrap(), 7);
        });

        assert_eq!(cache.len(), 0);
    }

    /// A single load waiting on a batch that did not find its key runs its
    /// own load rather than failing.
    #[test]
    fn a_single_load_waiting_on_a_batch_miss_loads_itself() {
        let cache = cache(10);
        let (release, gate) = oneshot::channel::<()>();
        let gate = gate.shared();
        let loads = AtomicUsize::new(0);

        let value = block_on(async {
            let mut batch = pin!(cache.get_or_load_many(&["a"], |_| {
                let gate = gate.clone();
                async move {
                    let _ = gate.await;
                    Ok(HashMap::new())
                }
            }));
            let mut single = pin!(cache.get_or_load("a", counting_loader(5, &loads)));
            poll_fn(|cx| {
                assert!(batch.poll_unpin(cx).is_pending());
                assert!(single.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;

            release.send(()).unwrap();
            assert!(batch.await.unwrap().is_empty());
            single.await.unwrap()
        });

        assert_eq!(value, 5);
        assert_eq!(loads.load(Ordering::SeqCst), 1);
    }

    /// A batch waiting on a load whose caller goes away loads the key itself.
    #[test]
    fn a_batch_waiting_on_a_dropped_load_loads_itself() {
        let cache = cache(10);
        let starts = AtomicUsize::new(0);
        let (gated, _release) = GatedLoader::new(7, &starts);
        let calls = Mutex::new(Vec::new());
        let values = [("a", 1)];

        let found = block_on(async {
            let mut batch = pin!(cache.get_or_load_many(&["a"], batch_loader(&values, &calls)));
            {
                let mut leader = pin!(cache.get_or_load("a", || gated.load()));
                poll_fn(|cx| {
                    assert!(leader.poll_unpin(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
                poll_fn(|cx| {
                    assert!(batch.poll_unpin(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
            }
            batch.await.unwrap()
        });

        assert_eq!(found, HashMap::from([("a", 1)]));
        assert_eq!(*calls.lock().unwrap(), vec![vec!["a"]]);
    }

    /// A dropped batch leaves none of its keys marked as loading.
    #[test]
    fn a_dropped_batch_does_not_wedge_its_keys() {
        let cache = cache(10);
        let loads = AtomicUsize::new(0);

        block_on(async {
            {
                let mut batch = pin!(cache.get_or_load_many(&["a", "b"], |_| {
                    futures::future::pending::<Result<HashMap<&str, u32>, anyhow::Error>>()
                }));
                poll_fn(|cx| {
                    assert!(batch.poll_unpin(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
            }
            assert_eq!(
                cache
                    .get_or_load("b", counting_loader(2, &loads))
                    .await
                    .unwrap(),
                2
            );
        });

        assert_eq!(loads.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn invalidate_matching_drops_entries_and_pending_loads() {
        let cache: Arc<Cache<(&'static str, u32), u32>> = Cache::new("matching", 10);
        cache.insert(("a", 1), 1);
        cache.insert(("a", 2), 2);
        cache.insert(("b", 1), 3);

        let mut removed = cache.invalidate_matching(|key| key.0 == "a");
        removed.sort();
        assert_eq!(removed, vec![1, 2]);
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.get(&("b", 1)), Some(3));
        assert_eq!(cache.eviction_counts().2, 2);

        // A load in flight for a matching key does not get to cache what it
        // finds, as for `invalidate`.
        let starts = AtomicUsize::new(0);
        let (loader, release) = GatedLoader::new(7, &starts);
        block_on(async {
            let mut load = pin!(cache.get_or_load(("a", 3), || loader.load()));
            poll_fn(|cx| {
                assert!(load.poll_unpin(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            assert!(cache.invalidate_matching(|key| key.0 == "a").is_empty());
            release.send(()).unwrap();
            assert_eq!(load.await.unwrap(), 7);
        });
        assert!(cache.get(&("a", 3)).is_none());
    }

    /// A cache of `(room, n)` values, indexed by room.
    fn indexed(max_size: usize) -> Arc<Cache<&'static str, (Arc<str>, u32)>> {
        fn room(value: &(Arc<str>, u32)) -> &str {
            &value.0
        }
        Cache::new_indexed("indexed", max_size, room)
    }

    fn in_room(room: &str, n: u32) -> (Arc<str>, u32) {
        (Arc::from(room), n)
    }

    #[test]
    fn invalidate_index_drops_only_that_index_key() {
        let cache = indexed(10);
        cache.insert("a", in_room("!r1", 1));
        cache.insert("b", in_room("!r1", 2));
        cache.insert("c", in_room("!r2", 3));

        let mut removed: Vec<u32> = cache
            .invalidate_index("!r1")
            .into_iter()
            .map(|value| value.1)
            .collect();
        removed.sort_unstable();
        assert_eq!(removed, vec![1, 2]);
        assert_eq!(cache.len(), 1);
        assert!(cache.get(&"c").is_some());
        assert_eq!(cache.eviction_counts().2, 2);

        // Nothing is left under the key, and an unknown key is harmless.
        assert!(cache.invalidate_index("!r1").is_empty());
        assert!(cache.invalidate_index("!nope").is_empty());
    }

    /// Every way an entry leaves the cache takes it out of the index too, so
    /// a later invalidation by index key cannot drop a re-inserted entry that
    /// moved to another index key, nor leak keys.
    #[test]
    fn the_index_follows_every_removal() {
        let cache = indexed(2);

        // Replaced under a different index key.
        cache.insert("a", in_room("!r1", 1));
        cache.insert("a", in_room("!r2", 2));
        assert!(cache.invalidate_index("!r1").is_empty());
        assert_eq!(cache.get(&"a"), Some(in_room("!r2", 2)));

        // Evicted for size.
        cache.insert("b", in_room("!r3", 3));
        cache.insert("c", in_room("!r3", 4));
        assert!(cache.get(&"a").is_none());
        assert!(cache.invalidate_index("!r2").is_empty());

        // Invalidated by key, then by everything.
        cache.invalidate(&"b");
        assert_eq!(cache.invalidate_index("!r3").len(), 1);
        cache.insert("d", in_room("!r4", 5));
        cache.invalidate_all();
        assert!(cache.invalidate_index("!r4").is_empty());

        // Expired.
        cache.insert_at("e", in_room("!r5", 6), at(1));
        cache.expire_before(at(2));
        assert!(cache.invalidate_index("!r5").is_empty());

        assert!(cache.lock().index.as_ref().unwrap().keys.is_empty());
    }

    #[test]
    fn invalidate_index_on_an_unindexed_cache_does_nothing() {
        let cache: Arc<Cache<&'static str, (Arc<str>, u32)>> = Cache::new("plain", 10);
        cache.insert("a", in_room("!r1", 1));
        assert!(cache.invalidate_index("!r1").is_empty());
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn touch_marks_an_entry_as_read() {
        let cache = cache(2);
        assert!(!cache.touch(&"a"));

        cache.insert("a", 1);
        cache.insert("b", 2);
        // Touching "a" makes "b" the coldest, so "b" goes for "c".
        assert!(cache.touch(&"a"));
        cache.insert("c", 3);
        assert!(cache.get(&"b").is_none());
        assert_eq!(cache.get(&"a"), Some(1));
        // Only the `get` counted.
        assert_eq!(cache.hit_counts(), (1, 0));
    }

    #[test]
    fn touch_or_insert_keeps_what_is_there() {
        let cache = cache(2);
        assert!(cache.touch_or_insert_with("a", || 1).is_empty());
        assert_eq!(cache.get(&"a"), Some(1));

        // Already cached, so the value is not even built.
        cache.touch_or_insert_with("a", || unreachable!());
        assert_eq!(cache.get(&"a"), Some(1));

        cache.insert("b", 2);
        // "a" was touched last, so inserting "c" evicts "b".
        cache.touch_or_insert_with("a", || unreachable!());
        assert_eq!(cache.touch_or_insert_with("c", || 3), vec![2]);
        assert_eq!(cache.hit_counts(), (2, 0));
    }

    #[test]
    fn lookups_count_as_asked() {
        let cache = cache(2);
        cache.insert("a", 1);

        assert_eq!(cache.get_or_count_miss(&"a"), Some(1));
        assert_eq!(cache.get_or_count_miss(&"b"), None);
        assert_eq!(cache.hit_counts(), (1, 1));

        assert_eq!(cache.get_uncounted(&"a"), Some(1));
        assert_eq!(cache.get_uncounted(&"b"), None);
        assert_eq!(cache.hit_counts(), (1, 1));

        cache.count_hit();
        cache.count_miss();
        assert_eq!(cache.hit_counts(), (2, 2));
    }

    #[test]
    fn evict_coldest_stops_at_the_bound_and_the_count() {
        let cache = cache(10);
        cache.insert_at("a", 1, at(1));
        cache.insert_at("b", 2, at(2));
        cache.insert_at("c", 3, at(3));
        assert_eq!(cache.oldest_access(), Some(at(1)));

        // Only entries read strictly before the bound go.
        assert_eq!(cache.evict_coldest(at(2), 10), vec![1]);
        assert_eq!(cache.oldest_access(), Some(at(2)));

        // And no more than asked for.
        assert_eq!(cache.evict_coldest(at(10), 1), vec![2]);
        assert_eq!(cache.oldest_access(), Some(at(3)));
        assert_eq!(cache.eviction_counts().0, 2);

        cache.evict_coldest(at(10), 10);
        assert_eq!(cache.oldest_access(), None);
    }
}
