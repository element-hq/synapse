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

//! The Python view of the Rust [`Store`].
//!
//! The main datastore builds a [`RustStore`] in its `__init__`, over its own
//! `db_pool`, and keeps it as `rust_store`. `hs.get_rust_store()` hands the
//! same store to the Rust handlers. So the datastore owns the store, as it
//! owns everything else that reads the database, and the handlers borrow it.
//!
//! A store function that Python calls as well as Rust is exposed as a
//! callable cache object, such as [`IsFeatureEnabledForUser`]. The datastore
//! binds it under the cache's name, so Python callers call it like the
//! `@cached` function it replaces, and Synapse's invalidation machinery finds
//! it by that name exactly as it would a `@cached` function. See
//! [`crate::caches::python`].
//!
//! The event cache is `EventsWorkerStore._get_event_cache`, bound as a
//! [`RustEventCache`]: Python reads and fills the same cache as the Rust
//! readers, and its invalidations drop the store's chain cover and state
//! group caches along with the events. Those other caches reach
//! `register_cache` through [`RustStore::caches`], for metrics and cache
//! factors.
//!
//! The caches that mirror a Python `@cached` function reach the datastore
//! through [`RustStore::mirrored_caches`], which attaches each to the
//! Python cache it names, so that every invalidation of the Python cache
//! reaches it.

use std::sync::Arc;

use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods, PyTuple},
    Bound, Py, PyAny, PyClassInitializer, PyResult, Python,
};

use crate::{
    caches::python::{PyCacheKey, RustCache},
    deferred::{create_deferred, succeed},
    events::Event,
    runtime::RustRuntime,
    storage::{
        caches::{new_event_cache, EventCache, StoreCaches},
        db::python_db_pool::PythonDatabasePoolWrapper,
        events::CachedEvent,
        store::{IsFeatureEnabledKey, PerUserExperimentalFeature, Store},
    },
};

/// The Rust [`Store`], as the main datastore holds it.
#[pyclass(frozen, name = "RustStore")]
pub struct RustStore {
    store: Arc<Store>,
    is_feature_enabled_for_user: Py<IsFeatureEnabledForUser>,
    /// The event cache, for `EventsWorkerStore` to bind.
    event_cache: Py<RustEventCache>,
    /// The chain cover and state group caches, as Python sees them.
    caches: Vec<Py<RustCache>>,
    /// The caches that mirror a Python cache, with the Python cache's name.
    mirrored_caches: Vec<(&'static str, Py<RustCache>)>,
}

impl RustStore {
    pub fn store(&self) -> &Arc<Store> {
        &self.store
    }
}

#[pymethods]
impl RustStore {
    /// Build the store over `db_pool`, the datastore's `DatabasePool`, with
    /// its caches in the runtime's registry and an event cache of
    /// `event_cache_size` events.
    #[new]
    fn py_new(
        py: Python<'_>,
        runtime: &RustRuntime,
        db_pool: &Bound<'_, PyAny>,
        event_cache_size: usize,
    ) -> PyResult<Self> {
        let db_pool = PythonDatabasePoolWrapper::new(db_pool, runtime.clone())?;
        let store = Arc::new(Store::new(
            Box::new(db_pool),
            runtime.caches(),
            event_cache_size,
        ));

        let is_feature_enabled_for_user = Py::new(
            py,
            PyClassInitializer::from(RustCache::new(
                store.is_feature_enabled_for_user_cache.clone(),
            ))
            .add_subclass(IsFeatureEnabledForUser {
                store: Arc::clone(&store),
                runtime: runtime.clone(),
            }),
        )?;

        let store_caches = &store.caches;
        let event_cache = Py::new(
            py,
            RustEventCache::initializer(
                store_caches.events.clone(),
                Some(Arc::clone(store_caches)),
            ),
        )?;
        let caches = vec![
            Py::new(py, RustCache::new(store_caches.chain_positions.clone()))?,
            Py::new(py, RustCache::new(store_caches.chain_links.clone()))?,
            Py::new(py, RustCache::new(store_caches.state_groups.clone()))?,
        ];

        let mirrored_caches = store_caches
            .mirrored()
            .into_iter()
            .map(|mirrored| {
                Ok((
                    mirrored.python_name,
                    Py::new(py, RustCache::new(mirrored.cache))?,
                ))
            })
            .collect::<PyResult<Vec<_>>>()?;

        Ok(RustStore {
            store,
            is_feature_enabled_for_user,
            event_cache,
            caches,
            mirrored_caches,
        })
    }

    /// The caches that mirror a Python `@cached` function, each with the
    /// name of the function, for the datastore to attach as a mirror of
    /// that function's cache and to register for metrics.
    #[getter]
    fn mirrored_caches(&self, py: Python<'_>) -> Vec<(&'static str, Py<RustCache>)> {
        self.mirrored_caches
            .iter()
            .map(|(name, cache)| (*name, cache.clone_ref(py)))
            .collect()
    }

    /// The chain cover and state group caches, for the datastore to register
    /// for metrics and cache factors. Nothing invalidates them by name: the
    /// event cache drops them with the events.
    #[getter]
    fn caches(&self, py: Python<'_>) -> Vec<Py<RustCache>> {
        self.caches
            .iter()
            .map(|cache| cache.clone_ref(py))
            .collect()
    }

    /// The event cache, for `EventsWorkerStore` to bind as its
    /// `_get_event_cache`.
    #[getter]
    fn event_cache(&self, py: Python<'_>) -> Py<RustEventCache> {
        self.event_cache.clone_ref(py)
    }

    /// `Store::is_feature_enabled_for_user` as a callable cache, for the
    /// datastore to bind under that name.
    #[getter]
    fn is_feature_enabled_for_user(&self, py: Python<'_>) -> Py<IsFeatureEnabledForUser> {
        self.is_feature_enabled_for_user.clone_ref(py)
    }
}

/// `Store::is_feature_enabled_for_user`, callable from Python, and the cache
/// behind it.
///
/// Calling it returns a Twisted deferred of the answer. A hit is answered
/// synchronously, with an already-fired deferred, so a cached lookup costs
/// Python no more than its own caches do. Only a miss goes through tokio.
///
/// As a subclass of [`RustCache`], it is also the cache: `invalidate`,
/// `__name__` and the counters are all here, so `_invalidate_cache_and_stream`
/// and `register_cache` take it as they would a `@cached` function.
#[pyclass(frozen, extends = RustCache, name = "IsFeatureEnabledForUser")]
pub struct IsFeatureEnabledForUser {
    store: Arc<Store>,
    runtime: RustRuntime,
}

#[pymethods]
impl IsFeatureEnabledForUser {
    /// Whether `feature` is enabled for `user_id`. `False` when there is no
    /// row, as for the Python function this replaces.
    ///
    /// Raises `ValueError` for a feature Rust doesn't know. Python's
    /// `ExperimentalFeature` and [`PerUserExperimentalFeature`] list the same
    /// features, so that means one side has gained a feature without the
    /// other, which should fail loudly.
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        user_id: String,
        feature: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let feature = PerUserExperimentalFeature::from_name(&feature).ok_or_else(|| {
            PyValueError::new_err(format!("unknown per-user experimental feature: {feature}"))
        })?;
        let key = IsFeatureEnabledKey::new(&user_id, feature);
        if let Some(enabled) = self.store.is_feature_enabled_for_user_cache.get(&key) {
            return succeed(py, enabled.unwrap_or(false));
        }

        let store = Arc::clone(&self.store);
        create_deferred(py, &self.runtime, async move {
            store
                .is_feature_enabled_for_user(key)
                .await
                .map(|enabled| enabled.unwrap_or(false))
                .map_err(|err| {
                    PyRuntimeError::new_err(format!("is_feature_enabled_for_user failed: {err:#}"))
                })
        })
    }
}

/// A hit in [`RustEventCache`]: the event, and its redacted form if it has one.
type PyCacheHit = (Py<Event>, Option<Py<Event>>);

/// The event cache, as `EventsWorkerStore` holds it in `_get_event_cache`.
///
/// The same cache the Rust readers fill and read, so an event is held once
/// whichever side loaded it. An entry holds Rust events, and each hit hands
/// Python a new `Event` object sharing the cached event's data, internal
/// metadata and unsigned data included. So a change Python makes to one,
/// such as adding `prev_content`, shows on every copy. `EventsWorkerStore`
/// keeps its own weak map of the objects it handed out, and checks that
/// first, so that while Python holds an event it gets that same object back.
///
/// Keys are one-tuples of the event ID, as for the Python cache this
/// replaced, so that name-based invalidation reaches it. Invalidating an
/// event drops its chain position too, and invalidating a room drops the
/// store's chain cover and state group caches, which a purge deletes from.
#[pyclass(frozen, extends = RustCache, name = "RustEventCache")]
pub struct RustEventCache {
    events: Arc<EventCache>,
    /// The store's caches, when this is the store's event cache.
    store_caches: Option<Arc<StoreCaches>>,
}

impl RustEventCache {
    fn initializer(
        events: Arc<EventCache>,
        store_caches: Option<Arc<StoreCaches>>,
    ) -> PyClassInitializer<Self> {
        PyClassInitializer::from(RustCache::new(events.clone())).add_subclass(RustEventCache {
            events,
            store_caches,
        })
    }

    /// The event ID in a key, or `None` if the tuple is not a key of this
    /// cache. Such a key matches nothing.
    fn event_id(key: &Bound<'_, PyTuple>) -> Option<Arc<str>> {
        <Arc<str> as PyCacheKey>::from_py_key(key)
    }

    fn invalidate_event(&self, key: &Bound<'_, PyTuple>) {
        let Some(event_id) = Self::event_id(key) else {
            return;
        };
        match &self.store_caches {
            Some(store_caches) => store_caches.invalidate_event(&event_id),
            None => drop(self.events.invalidate(&event_id)),
        }
    }

    fn clear_all(&self) {
        match &self.store_caches {
            Some(store_caches) => store_caches.clear(),
            None => drop(self.events.invalidate_all()),
        }
    }
}

#[pymethods]
impl RustEventCache {
    /// An event cache of its own, for a datastore without a Rust store, such
    /// as the database port script's. It is in the runtime's registry, so it
    /// takes part in age and memory-pressure eviction like any other.
    #[new]
    fn py_new(runtime: &RustRuntime, max_size: usize) -> PyClassInitializer<Self> {
        Self::initializer(new_event_cache(runtime.caches(), max_size), None)
    }

    /// The cached event and its redacted form, if it had a valid redaction
    /// when loaded, each as a new Python object. `None` if it isn't cached.
    #[pyo3(signature = (key, update_metrics = true))]
    fn get_local(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyTuple>,
        update_metrics: bool,
    ) -> PyResult<Option<PyCacheHit>> {
        let Some(event_id) = Self::event_id(key) else {
            return Ok(None);
        };
        let cached = if update_metrics {
            self.events.get_or_count_miss(&event_id)
        } else {
            self.events.get_uncounted(&event_id)
        };
        match cached {
            Some(cached) => Ok(Some((cached.to_py(py)?, cached.redacted_to_py(py)?))),
            None => Ok(None),
        }
    }

    /// Mark the event as read, for a hit Python answered from its own map of
    /// live events. `event` is that Python object: if the cache had evicted
    /// the event, it goes back in, sharing the object's data.
    #[pyo3(signature = (key, event, update_metrics = true))]
    fn touch(&self, key: &Bound<'_, PyTuple>, event: &Bound<'_, Event>, update_metrics: bool) {
        let Some(event_id) = Self::event_id(key) else {
            return;
        };
        // One lock for the check and the insert, so that an entry a Rust
        // reader loaded meanwhile, perhaps with a redaction this object
        // doesn't know of, is kept.
        let evicted = self
            .events
            .touch_or_insert_with(event_id, || CachedEvent::new(event.get().shallow_clone()));
        drop(evicted);
        if update_metrics {
            self.events.count_hit();
        }
    }

    /// Cache an event and its redacted form, if it has a valid redaction.
    /// The cache shares the objects' data, so later changes to their internal
    /// metadata or unsigned data show in the cache too.
    fn set_local(
        &self,
        key: &Bound<'_, PyTuple>,
        event: &Bound<'_, Event>,
        redacted_event: Option<&Bound<'_, Event>>,
    ) {
        let Some(event_id) = Self::event_id(key) else {
            return;
        };
        let cached = CachedEvent::with_redaction(
            event.get().shallow_clone(),
            redacted_event.map(|redacted| redacted.get().shallow_clone()),
        );
        drop(self.events.insert(event_id, cached));
    }

    /// Drop an event, and its chain position.
    fn invalidate(&self, key: &Bound<'_, PyTuple>) {
        self.invalidate_event(key);
    }

    /// As [`RustEventCache::invalidate`]. There is no cache beyond this one
    /// to invalidate first.
    fn invalidate_local(&self, key: &Bound<'_, PyTuple>) {
        self.invalidate_event(key);
    }

    /// Drop a room's events, for a purge, along with every chain position,
    /// chain link and state group. `index_key` is a one-tuple of the room
    /// ID.
    fn invalidate_on_extra_index_local(&self, index_key: &Bound<'_, PyTuple>) {
        let Some(room_id) = Self::event_id(index_key) else {
            return;
        };
        match &self.store_caches {
            Some(store_caches) => store_caches.invalidate_room(&room_id),
            None => drop(self.events.invalidate_index(&room_id)),
        }
    }

    /// Empty the cache, and the store's chain cover and state group caches.
    fn invalidate_all(&self) {
        self.clear_all();
    }

    /// As [`RustEventCache::invalidate_all`].
    fn clear(&self) {
        self.clear_all();
    }
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "storage")?;

    child_module.add_class::<RustStore>()?;
    child_module.add_class::<IsFeatureEnabledForUser>()?;
    child_module.add_class::<RustEventCache>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import storage` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.storage", child_module)?;

    Ok(())
}
