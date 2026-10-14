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
    collections::HashMap,
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

/// How much a cached value counts towards the cache's bound.
pub type Weigher<V> = fn(&V) -> usize;

/// The default [`Weigher`]: every value weighs one, so the bound is a number
/// of entries.
pub fn weigh_one<V>(_: &V) -> usize {
    1
}

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

    /// Callers waiting on an in-flight load, keyed by that load's id.
    waiters: HashMap<u64, Vec<oneshot::Sender<Result<V, SharedError>>>>,

    next_load_id: u64,

    /// The bound this cache was declared with, before any cache factor is
    /// applied. `set_cache_factor` scales this rather than the current bound,
    /// so that repeated resizes don't compound.
    original_max_weight: usize,
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
                    Ok(result) => {
                        self.hits.fetch_add(1, Ordering::Relaxed);
                        return result;
                    }
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
        let value = self.lock().get(key, self.clock.now());
        if value.is_some() {
            self.hits.fetch_add(1, Ordering::Relaxed);
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
            let _ = waiter.send(result.clone());
        }

        result
    }
}

/// The parts of the cache that depend on the time take it as an argument,
/// rather than reading [`Cache::clock`], so that the tests can drive them with
/// explicit timestamps and stay clear of the process-wide virtual clock.
impl<K, V> Inner<K, V>
where
    K: Eq + Hash,
    V: Clone,
{
    /// Look `key` up, stamping its entry as read at `now` and moving it to the
    /// head of the LRU.
    fn get(&mut self, key: &K, now: SystemTime) -> Option<V> {
        let entry = self.entries.get_mut(key)?;
        entry.last_read = now;
        Some(entry.value.clone())
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
        Some(entry.value)
    }

    /// Pop the coldest entry, returning its value for the caller to drop.
    fn pop_lru(&mut self) -> Option<V> {
        let (_, entry) = self.entries.pop_lru()?;
        self.total_weight -= entry.weight;
        Some(entry.value)
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

/// What [`Cache::start_load`] decided.
enum Started<V> {
    /// Nobody is loading this key, so the caller runs the load, under this id.
    Lead(u64),
    Cached(V),
    /// Someone else is loading this key. The result arrives here.
    Waiting(oneshot::Receiver<Result<V, SharedError>>),
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
}
