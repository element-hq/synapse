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
//! When the main database uses the native driver, events are loaded from
//! the database here too, for Python as for Rust: `EventsWorkerStore` calls
//! [`RustStore::load_events`] for the events its caches miss. So every event
//! load goes through the one [`EventLoader`], and loads of the same event
//! from either side coalesce in the event cache.
//!
//! The caches that mirror a Python `@cached` function reach the datastore
//! through [`RustStore::mirrored_caches`], which attaches each to the
//! Python cache it names, so that every invalidation of the Python cache
//! reaches it.

use std::{collections::HashMap, future::Future, sync::Arc};

use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    import_exception, intern, pyclass, pymethods,
    types::{
        PyAnyMethods, PyDict, PyDictMethods, PyModule, PyModuleMethods, PyTuple, PyWeakrefMethods,
        PyWeakrefReference,
    },
    Bound, IntoPyObject, Py, PyAny, PyClassInitializer, PyErr, PyResult, Python,
};

use crate::{
    caches::{
        python::{PyCacheKey, RustCache},
        SharedError,
    },
    database::postgres::pool::{ConnectionPool, PyConnectionPool},
    deferred::{create_deferred, succeed},
    events::Event,
    runtime::RustRuntime,
    storage::{
        caches::{new_event_cache, EventCache, StoreCaches},
        db::python_db_pool::PythonDatabasePoolWrapper,
        events::{CachedEvent, EventLoadStats, EventLoader, UnusableEvent},
        pool_querier::PoolQuerier,
        state_groups::{store_state_group_chain, StateGroupChain, StateRow},
        store::{IsFeatureEnabledKey, PerUserExperimentalFeature, Store},
    },
};

import_exception!(
    synapse.storage.databases.main.events_worker,
    InvalidEventError
);
import_exception!(
    synapse.storage.databases.main.events_worker,
    DatabaseCorruptionError
);

/// The Python error for a failed event load: `InvalidEventError` or
/// `DatabaseCorruptionError` for an event that cannot be used, as the
/// Python loader raises, and `RuntimeError` for anything else.
fn event_load_error_to_py(error: anyhow::Error) -> PyErr {
    match unusable_event(&error) {
        Some(UnusableEvent::Invalid(message)) => InvalidEventError::new_err(message.clone()),
        Some(UnusableEvent::Corrupt {
            room_id,
            persisted_event_id,
            computed_event_id,
        }) => DatabaseCorruptionError::new_err((
            room_id.clone(),
            persisted_event_id.clone(),
            computed_event_id.clone(),
        )),
        None => PyRuntimeError::new_err(format!("loading events failed: {error:#}")),
    }
}

/// The [`UnusableEvent`] behind `error`, which may have reached us through
/// another caller's load in the event cache.
fn unusable_event(error: &anyhow::Error) -> Option<&UnusableEvent> {
    if let Some(unusable) = error.downcast_ref::<UnusableEvent>() {
        return Some(unusable);
    }
    error
        .downcast_ref::<SharedError>()
        .and_then(|shared| unusable_event(shared.inner()))
}

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
    runtime: RustRuntime,
    /// The datastore's `DatabasePool`, when it uses the native driver. Its
    /// `_db_pool._pool` is read per call: `close()` and `start()` replace
    /// the `_pool`, and the replication tests swap a worker's `_db_pool` for
    /// the main process's. A weak reference, as the `DatabasePool` references
    /// the homeserver, which owns this: see `PythonDatabasePoolWrapper`.
    native_db_pool: Option<Py<PyWeakrefReference>>,
}

impl RustStore {
    pub fn store(&self) -> &Arc<Store> {
        &self.store
    }

    /// The native driver's pool, or an error if the database does not use
    /// that driver or the pool is closed.
    fn pool(&self, py: Python<'_>) -> PyResult<ConnectionPool> {
        let Some(db_pool) = &self.native_db_pool else {
            return Err(PyRuntimeError::new_err(
                "this needs the native Postgres driver",
            ));
        };
        let db_pool = db_pool
            .bind(py)
            .upgrade()
            .ok_or_else(|| PyRuntimeError::new_err("the database pool has been dropped"))?;
        let pool = db_pool
            .getattr(intern!(py, "_db_pool"))?
            .getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
    }

    /// The future that stores `chain`, to run on the tokio runtime.
    fn state_group_chain_future(
        &self,
        py: Python<'_>,
        chain: StateGroupChain,
        now_ms: i64,
    ) -> PyResult<impl Future<Output = PyResult<(i64, Vec<i64>)>> + Send + 'static> {
        let pool = self.pool(py)?;
        Ok(async move {
            let stored = store_state_group_chain(&pool, &chain, now_ms)
                .await
                .map_err(|err| PyRuntimeError::new_err(err.to_string()))?;
            Ok((stored.group_before, stored.groups_after))
        })
    }

    fn start_load<'py>(
        &self,
        py: Python<'py>,
        event_ids: Vec<String>,
        reload: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let load = self.load(py, event_ids, reload)?;
        // Shared with other callers through the cache, so a cancelled caller
        // must leave the load running for them.
        create_deferred(py, &self.runtime, load)
    }

    /// [`RustStore::start_load`], blocking until the load is done, with the
    /// GIL released.
    fn load_blocking(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
        reload: bool,
    ) -> PyResult<LoadedEvents> {
        let load = self.load(py, event_ids, reload)?;
        self.runtime.block_on_scoped(py, load)?
    }

    /// The future that loads `event_ids`, to run on the tokio runtime.
    fn load(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
        reload: bool,
    ) -> PyResult<impl Future<Output = PyResult<LoadedEvents>> + Send + 'static> {
        let pool = self.pool(py)?;
        let store = Arc::clone(&self.store);
        let ids: Vec<Arc<str>> = event_ids.into_iter().map(Arc::from).collect();
        Ok(async move {
            let mut stats = EventLoadStats::default();
            let events = async {
                let querier = PoolQuerier::checkout(&pool).await?;
                let loader = EventLoader::new(&querier, Some(&store.caches.events));
                if reload {
                    loader.reload(&ids, &mut stats).await
                } else {
                    loader.load(&ids, &mut stats).await
                }
            }
            .await;
            events
                .map(|events| LoadedEvents {
                    events,
                    fetched: stats.fetched,
                })
                .map_err(event_load_error_to_py)
        })
    }
}

/// `db_pool`, a `DatabasePool`, if it runs on the native driver.
fn native_db_pool(db_pool: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyWeakrefReference>>> {
    let py = db_pool.py();
    let connection_pool = db_pool.getattr(intern!(py, "_db_pool"))?;
    let rust_connection_pool_class = py
        .import("synapse.storage.rust_pool")?
        .getattr("RustConnectionPool")?;
    if !connection_pool.is_instance(&rust_connection_pool_class)? {
        return Ok(None);
    }
    Ok(Some(PyWeakrefReference::new(db_pool)?.unbind()))
}

/// What [`RustStore::load_events`] answers: for each event ID, the event
/// and its redacted form, as new Python objects sharing the cached data,
/// and how many events this load read from the database. Events read by
/// another caller's load that this one joined are not counted.
struct LoadedEvents {
    events: HashMap<Arc<str>, CachedEvent>,
    fetched: u64,
}

impl<'py> IntoPyObject<'py> for LoadedEvents {
    type Target = PyTuple;
    type Output = Bound<'py, PyTuple>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let dict = PyDict::new(py);
        for (event_id, event) in self.events {
            dict.set_item(&*event_id, (event.to_py(py)?, event.redacted_to_py(py)?))?;
        }
        (dict, self.fetched).into_pyobject(py)
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
        let native_db_pool = native_db_pool(db_pool)?;
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
            runtime: runtime.clone(),
            native_db_pool,
        })
    }

    /// Whether [`RustStore::load_events`] can be used: whether the database
    /// uses the native driver.
    #[getter]
    fn loads_events(&self) -> bool {
        self.native_db_pool.is_some()
    }

    /// `_get_events_from_db` behind the event cache: a deferred of the events
    /// by ID, each with its redacted form if it has a valid redaction,
    /// rejected events included and unknown ones left out, and the number of
    /// events this call read from the database. Each event comes
    /// from the cache, from a load already in flight, or from the database,
    /// and those read from the database are cached.
    ///
    /// The result can hold events that were not asked for: the redaction
    /// events read to check an unconfirmed redaction.
    fn load_events<'py>(
        &self,
        py: Python<'py>,
        event_ids: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start_load(py, event_ids, false)
    }

    /// [`RustStore::load_events`] reading every event from the database,
    /// for a caller that must not trust the cache. What it reads replaces
    /// what was cached.
    fn reload_events<'py>(
        &self,
        py: Python<'py>,
        event_ids: Vec<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.start_load(py, event_ids, true)
    }

    /// As [`RustStore::load_events`], but blocks and returns the result.
    /// For tests, which run Python's database transactions synchronously
    /// too: see `_BlockingRustCalls`.
    fn load_events_blocking(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<LoadedEvents> {
        self.load_blocking(py, event_ids, false)
    }

    /// As [`RustStore::reload_events`], but blocks and returns the result.
    /// For tests.
    fn reload_events_blocking(
        &self,
        py: Python<'_>,
        event_ids: Vec<String>,
    ) -> PyResult<LoadedEvents> {
        self.load_blocking(py, event_ids, true)
    }

    /// Store a chain of new state groups for a batch of new events, as
    /// `store_state_deltas_for_batched` does. A deferred of the group before
    /// the first event and the group after each of `state_events`. See
    /// [`store_state_group_chain`].
    #[pyo3(signature = (room_id, prev_group, full_state_before, state_events, now_ms))]
    fn store_state_group_chain<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        prev_group: Option<i64>,
        full_state_before: Option<(String, Vec<StateRow>)>,
        state_events: Vec<StateRow>,
        now_ms: i64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let store = self.state_group_chain_future(
            py,
            StateGroupChain {
                room_id,
                prev_group,
                full_state_before,
                state_events,
            },
            now_ms,
        )?;
        create_deferred(py, &self.runtime, store)
    }

    /// As [`RustStore::store_state_group_chain`], but blocks and returns the
    /// result. For tests.
    #[pyo3(signature = (room_id, prev_group, full_state_before, state_events, now_ms))]
    fn store_state_group_chain_blocking(
        &self,
        py: Python<'_>,
        room_id: String,
        prev_group: Option<i64>,
        full_state_before: Option<(String, Vec<StateRow>)>,
        state_events: Vec<StateRow>,
        now_ms: i64,
    ) -> PyResult<(i64, Vec<i64>)> {
        let store = self.state_group_chain_future(
            py,
            StateGroupChain {
                room_id,
                prev_group,
                full_state_before,
                state_events,
            },
            now_ms,
        )?;
        self.runtime.block_on_scoped(py, store)?
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
            None => {
                drop(self.events.invalidate_index(&room_id));
                self.events.abandon_loads();
            }
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
