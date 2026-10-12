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

//! The Python view of a Rust cache.
//!
//! Synapse's invalidation machinery is duck typed: `_attempt_to_invalidate_cache`
//! resolves `getattr(store, cache_name)` and calls `.invalidate(key)` or
//! `.invalidate_all()` on whatever comes back, and `_invalidate_cache_and_stream`
//! reads `.__name__` to label the replication row. [`RustCache`] implements that
//! protocol, plus the handful of members `register_cache` wants for metrics and
//! resizing. The datastore binds one under the cache's name, and the cache has
//! joined every existing invalidation path.
//!
//! The Rust `Store` builds its caches, and a cache that Python also reads
//! through is bound as a subclass that adds `__call__`. See
//! [`crate::storage::python`].
//!
//! Note that everything here runs with the GIL held, and takes the cache's lock
//! underneath it. That is the safe order: Tokio readers take the cache lock and
//! never reach for the GIL. See the locking rule in the parent module.

use std::{hash::Hash, sync::Arc, time::SystemTime};

use pyo3::{
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods, PyTuple},
    Bound, PyResult, Python,
};

use crate::caches::Cache;

/// A cache key that Synapse's invalidation machinery can produce.
///
/// Keys reach us two ways: directly, as the tuple passed to
/// `_invalidate_cache_and_stream`, and over the cache invalidation stream,
/// whose column is `keys text[]`, so anything arriving from another worker is
/// a string whatever it started as. An implementation has to cope with both.
pub trait PyCacheKey: Sized {
    /// Decode a key, or `None` if the tuple cannot be one of this cache's keys.
    ///
    /// `None` means "matches nothing", never an error. A worker on a newer
    /// version may stream keys of a shape this one doesn't know, and a Python
    /// cache shrugs those off. Raising here instead would fail the whole batch
    /// of invalidations it arrived in.
    fn from_py_key(key: &Bound<'_, PyTuple>) -> Option<Self>;
}

/// A [`Cache`] with its key and value types erased, so that one `#[pyclass]`
/// can serve every cache and the [`registry`](super::registry) can hold them
/// all.
pub(crate) trait CacheHandle: Send + Sync {
    fn name(&self) -> &'static str;
    fn stats(&self) -> CacheStats;
    fn invalidate_key(&self, key: &Bound<'_, PyTuple>);
    fn invalidate_everything(&self);
    fn set_cache_factor(&self, factor: f64);

    /// Evict every entry not read since `cutoff`, returning how many went.
    fn expire_entries_before(&self, cutoff: SystemTime) -> usize;
}

/// What `register_cache` reads off a cache at each scrape.
pub(crate) struct CacheStats {
    pub len: usize,
    pub max_size: usize,
    pub hits: u64,
    pub misses: u64,
    pub evicted_size: u64,
    pub evicted_time: u64,
    pub evicted_invalidation: u64,
}

impl<K, V> CacheHandle for Cache<K, V>
where
    K: PyCacheKey + Eq + Hash + Clone + Send + Sync + 'static,
    V: Clone + Send + Sync + 'static,
{
    fn name(&self) -> &'static str {
        Cache::name(self)
    }

    fn stats(&self) -> CacheStats {
        let (hits, misses) = self.hit_counts();
        let (evicted_size, evicted_time, evicted_invalidation) = self.eviction_counts();
        CacheStats {
            len: self.len(),
            max_size: self.max_size(),
            hits,
            misses,
            evicted_size,
            evicted_time,
            evicted_invalidation,
        }
    }

    fn invalidate_key(&self, key: &Bound<'_, PyTuple>) {
        let Some(key) = K::from_py_key(key) else {
            log::debug!(
                "Ignoring invalidation of cache {} with a key of the wrong shape: {key}",
                self.name
            );
            return;
        };

        // The removed value is dropped here, once `invalidate` has returned and
        // the cache lock is free again.
        drop(self.invalidate(&key));
    }

    fn invalidate_everything(&self) {
        drop(self.invalidate_all());
    }

    fn set_cache_factor(&self, factor: f64) {
        drop(Cache::set_cache_factor(self, factor));
    }

    fn expire_entries_before(&self, cutoff: SystemTime) -> usize {
        let expired = self.expire_before(cutoff);
        let count = expired.len();

        // Dropped here, with the cache lock released.
        drop(expired);

        count
    }
}

/// A Rust cache, as Synapse sees it. Bound on the datastore under the
/// cache's name.
#[pyclass(frozen, subclass, name = "RustCache")]
pub struct RustCache {
    handle: Arc<dyn CacheHandle>,
}

impl RustCache {
    pub(crate) fn new(handle: Arc<dyn CacheHandle>) -> Self {
        RustCache { handle }
    }
}

#[pymethods]
impl RustCache {
    /// The name Synapse knows this cache by: the datastore attribute it is
    /// bound to, the metrics label, and the `cache_func` column of its rows
    /// on the cache invalidation stream. `_invalidate_cache_and_stream` reads
    /// it to label the row it writes.
    #[getter]
    #[pyo3(name = "__name__")]
    fn py_name(&self) -> &'static str {
        self.handle.name()
    }

    /// Drop one entry, given the tuple of arguments that identifies it.
    ///
    /// A tuple that cannot be one of this cache's keys matches nothing, as it
    /// would for a Python cache.
    fn invalidate(&self, keys: &Bound<'_, PyTuple>) {
        self.handle.invalidate_key(keys);
    }

    /// Drop every entry.
    fn invalidate_all(&self) {
        self.handle.invalidate_everything();
    }

    fn __len__(&self) -> usize {
        self.handle.stats().len
    }

    #[getter]
    fn max_size(&self) -> usize {
        self.handle.stats().max_size
    }

    /// Resize the cache. Wired up by `register_cache(resizable=True)` so that
    /// `SYNAPSE_CACHE_FACTOR_*` and `caches.per_cache_factors` reach it.
    fn set_cache_factor(&self, factor: f64) {
        self.handle.set_cache_factor(factor);
    }

    #[getter]
    fn hits(&self) -> u64 {
        self.handle.stats().hits
    }

    #[getter]
    fn misses(&self) -> u64 {
        self.handle.stats().misses
    }

    #[getter]
    fn evicted_size(&self) -> u64 {
        self.handle.stats().evicted_size
    }

    #[getter]
    fn evicted_time(&self) -> u64 {
        self.handle.stats().evicted_time
    }

    #[getter]
    fn evicted_invalidation(&self) -> u64 {
        self.handle.stats().evicted_invalidation
    }
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "caches")?;

    child_module.add_class::<RustCache>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import caches` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.caches", child_module)?;

    Ok(())
}
