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

//! State resolution in Rust (v2 and v2.1), reading the database through the
//! native Postgres driver.
//!
//! - [`store`]: what state resolution reads from the database, as a trait
//!   with a Postgres implementation and an in-memory one for tests.
//! - [`auth_chain`]: the chain cover auth chain difference.
//! - [`v2`]: the algorithm.
//! - [`conflict_cache`]: the cache of resolved conflicted sets.
//!
//! [`StateResolutionHandler`] is the Python entry point, used by
//! `synapse.state.StateResolutionHandler` when `rust_state_res` is enabled.

use std::{
    borrow::Borrow,
    collections::{HashMap, HashSet},
    future::Future,
    hash::{Hash, Hasher},
    pin::Pin,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::Context;
use pyo3::{
    create_exception,
    exceptions::{PyException, PyRuntimeError, PyValueError},
    intern,
    prelude::*,
    types::{PyDict, PyModule, PyTuple},
};

use crate::database::postgres::pool::PyConnectionPool;
use crate::deferred::create_deferred;
use crate::event_auth::AuthFailure;
use crate::events::Event;
use crate::room_versions::{RoomVersion, StateResolutionVersions};
use crate::runtime::RustRuntime;

pub mod auth_chain;
pub mod conflict_cache;
pub mod store;
pub mod v2;

use conflict_cache::ConflictCache;
use store::{NoChainCoverIndex, PostgresStateResStore};

create_exception!(
    synapse.synapse_rust.state,
    NoChainCoverIndexError,
    PyException,
    "The room has no chain cover index, so the Rust state resolution cannot \
     resolve it. Fall back to the Python implementation."
);

/// A `(type, state_key)` pair.
///
/// Maps keyed on this can be queried with a borrowed `(&str, &str)` through
/// [`KeyRef`], so lookups don't allocate.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct StateKey(pub Arc<str>, pub Arc<str>);

impl StateKey {
    pub fn new(event_type: &str, state_key: &str) -> Self {
        StateKey(Arc::from(event_type), Arc::from(state_key))
    }

    pub fn as_tuple(&self) -> (&str, &str) {
        (&self.0, &self.1)
    }
}

impl Hash for StateKey {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.as_tuple().hash(state)
    }
}

/// A borrowed view of a [`StateKey`], for allocation-free map lookups.
pub trait KeyRef {
    fn key(&self) -> (&str, &str);
}

impl KeyRef for StateKey {
    fn key(&self) -> (&str, &str) {
        self.as_tuple()
    }
}

impl KeyRef for (&str, &str) {
    fn key(&self) -> (&str, &str) {
        *self
    }
}

impl<'a> Borrow<dyn KeyRef + 'a> for StateKey {
    fn borrow(&self) -> &(dyn KeyRef + 'a) {
        self
    }
}

impl Hash for dyn KeyRef + '_ {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.key().hash(state)
    }
}

impl PartialEq for dyn KeyRef + '_ {
    fn eq(&self, other: &Self) -> bool {
        self.key() == other.key()
    }
}

impl Eq for dyn KeyRef + '_ {}

/// Look up a borrowed `(type, state_key)` in a map keyed on [`StateKey`].
pub fn lookup<'m, V>(map: &'m HashMap<StateKey, V>, key: (&str, &str)) -> Option<&'m V> {
    map.get(&key as &dyn KeyRef)
}

/// A map from `(type, state_key)` to event ID.
pub type StateMap = HashMap<StateKey, Arc<str>>;

/// An event taking part in state resolution.
///
/// Either an event handed in from Python (shared with the Python object, read
/// without the GIL as `Event` is frozen) or one loaded from the database by
/// Rust. The auth event IDs are computed once, as the algorithm reads them
/// many times.
#[derive(Clone)]
pub struct StateResEvent(Arc<StateResEventInner>);

struct StateResEventInner {
    source: EventSource,
    auth_event_ids: Box<[Arc<str>]>,
}

enum EventSource {
    Python(Py<Event>),
    Rust(Event),
}

impl StateResEvent {
    /// Wrap an event from Python.
    pub fn from_py(event: Py<Event>) -> anyhow::Result<Self> {
        let auth_event_ids = auth_event_ids_of(event.get())?;
        Ok(StateResEvent(Arc::new(StateResEventInner {
            source: EventSource::Python(event),
            auth_event_ids,
        })))
    }

    /// Wrap an event loaded by Rust.
    pub fn from_rust(event: Event) -> anyhow::Result<Self> {
        let auth_event_ids = auth_event_ids_of(&event)?;
        Ok(StateResEvent(Arc::new(StateResEventInner {
            source: EventSource::Rust(event),
            auth_event_ids,
        })))
    }

    pub fn event(&self) -> &Event {
        match &self.0.source {
            EventSource::Python(event) => event.get(),
            EventSource::Rust(event) => event,
        }
    }

    pub fn auth_event_ids(&self) -> &[Arc<str>] {
        &self.0.auth_event_ids
    }

    /// The event as a Python object, creating one for an event loaded by Rust.
    pub fn to_py(&self, py: Python<'_>) -> PyResult<Py<Event>> {
        match &self.0.source {
            EventSource::Python(event) => Ok(event.clone_ref(py)),
            EventSource::Rust(event) => Py::new(py, event.shallow_clone()),
        }
    }
}

impl std::ops::Deref for StateResEvent {
    type Target = Event;

    fn deref(&self) -> &Event {
        self.event()
    }
}

fn auth_event_ids_of(event: &Event) -> anyhow::Result<Box<[Arc<str>]>> {
    let ids = event
        .auth_event_ids()
        .map_err(|e| anyhow::anyhow!("{e}"))
        .with_context(|| format!("auth event IDs of {}", event.event_id()))?;
    Ok(ids.into_iter().map(Arc::from).collect())
}

/// Events by ID.
pub type EventMap = HashMap<Arc<str>, StateResEvent>;

/// Interns strings, so the same event ID or event type read from several
/// Python state sets shares one allocation.
#[derive(Default)]
struct Interner(HashSet<Arc<str>>);

impl Interner {
    fn intern(&mut self, s: &str) -> Arc<str> {
        if let Some(existing) = self.0.get(s) {
            return Arc::clone(existing);
        }
        let new: Arc<str> = Arc::from(s);
        self.0.insert(Arc::clone(&new));
        new
    }
}

/// Convert a Python state map (any mapping of `(type, state_key)` to event
/// ID) to a [`StateMap`].
fn state_map_from_py(state: &Bound<'_, PyAny>, interner: &mut Interner) -> PyResult<StateMap> {
    let mut map = StateMap::new();
    let mut add = |key: Bound<'_, PyAny>, value: Bound<'_, PyAny>| -> PyResult<()> {
        let key = key.cast_into::<PyTuple>()?;
        let event_type = key.get_item(0)?;
        let state_key = key.get_item(1)?;
        map.insert(
            StateKey(
                interner.intern(event_type.extract()?),
                interner.intern(state_key.extract()?),
            ),
            interner.intern(value.extract()?),
        );
        Ok(())
    };

    if let Ok(dict) = state.cast::<PyDict>() {
        for (key, value) in dict.iter() {
            add(key, value)?;
        }
    } else {
        for item in state
            .call_method0(intern!(state.py(), "items"))?
            .try_iter()?
        {
            let (key, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
            add(key, value)?;
        }
    }
    Ok(map)
}

/// Convert a [`StateMap`] to a Python dict.
fn state_map_to_py<'py>(py: Python<'py>, state: &StateMap) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (key, event_id) in state {
        dict.set_item((&*key.0, &*key.1), &**event_id)?;
    }
    Ok(dict)
}

/// Convert a Python dict of event ID to `Event` to an [`EventMap`].
fn event_map_from_py(
    events: Option<&Bound<'_, PyDict>>,
    interner: &mut Interner,
) -> PyResult<EventMap> {
    let mut map = EventMap::new();
    for (event_id, event) in events.into_iter().flat_map(|m| m.iter()) {
        let event = event.cast_into::<Event>()?.unbind();
        map.insert(
            interner.intern(event_id.extract()?),
            StateResEvent::from_py(event).map_err(|e| PyValueError::new_err(format!("{e:#}")))?,
        );
    }
    Ok(map)
}

/// Resolve state with [`v2::resolve`] over an in-memory store holding
/// `persisted_events`, treating `unpersisted_events` as the caller's event
/// map. Returns the resolved state. For tests: trial compares it with the
/// Python implementation.
#[pyfunction]
#[pyo3(signature = (room_id, room_version, state_sets, persisted_events, unpersisted_events=None))]
fn resolve_in_memory_for_test<'py>(
    py: Python<'py>,
    room_id: &str,
    room_version: &str,
    state_sets: Vec<Bound<'py, PyAny>>,
    persisted_events: Bound<'py, PyDict>,
    unpersisted_events: Option<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    let room_version: &'static RoomVersion = room_version
        .parse()
        .map_err(|e| PyValueError::new_err(format!("{e}")))?;
    let mut interner = Interner::default();
    let state_sets = state_sets
        .iter()
        .map(|set| state_map_from_py(set, &mut interner))
        .collect::<PyResult<Vec<_>>>()?;
    let store =
        store::MemoryStateResStore::new(event_map_from_py(Some(&persisted_events), &mut interner)?);
    let unpersisted = event_map_from_py(unpersisted_events.as_ref(), &mut interner)?;

    // Release the GIL: the auth checks take it for third-party invites.
    let resolution = py
        .detach(|| {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .context("building a runtime")?;
            runtime.block_on(v2::resolve(
                &store,
                room_id,
                room_version,
                &state_sets,
                unpersisted,
                None,
            ))
        })
        .map_err(|e| PyRuntimeError::new_err(format!("{e:#}")))?;

    state_map_to_py(py, &resolution.merged())
}

/// The bound on the conflict cache, in state entries across all cached maps.
/// Matches `_conflict_resolution_cache` in Python.
const CONFLICT_CACHE_MAX_SIZE: usize = 100_000;

/// How long an unused conflict cache entry lives. Matches
/// `EVICTION_TIMEOUT_SECONDS`.
const CONFLICT_CACHE_EXPIRY: Duration = Duration::from_secs(60 * 60);

/// Timings and counts for one resolution, for metrics. Times are in seconds.
#[pyclass(frozen, get_all, skip_from_py_object)]
#[derive(Debug, Default, Clone)]
pub struct StateResStats {
    /// Wall time for the whole resolution.
    pub total_time: f64,
    /// Time waiting on the database.
    pub db_time: f64,
    /// Time building events from database rows.
    pub parse_time: f64,
    /// Time in the algorithm (sorts and auth checks).
    pub cpu_time: f64,
    pub sort_time: f64,
    pub auth_time: f64,
    pub mainline_time: f64,
    pub db_queries: u64,
    pub db_rows: u64,
    /// Events loaded from the database.
    pub db_events: u64,
    /// Events in the full conflicted set.
    pub conflicted_events: usize,
    /// The resolution of the conflicted set came from the cache.
    pub cache_hit: bool,
}

#[pymethods]
impl StateResStats {
    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}

/// What a resolution's deferred resolves to: the state and the stats.
struct ResolveOutput {
    state: StateMap,
    stats: StateResStats,
}

impl<'py> IntoPyObject<'py> for ResolveOutput {
    type Target = PyTuple;
    type Output = Bound<'py, PyTuple>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        let state = state_map_to_py(py, &self.state)?;
        (state, Bound::new(py, self.stats)?).into_pyobject(py)
    }
}

/// Map a resolution failure to a Python exception.
fn resolve_error_to_py(err: anyhow::Error) -> PyErr {
    if let Some(no_index) = err.downcast_ref::<NoChainCoverIndex>() {
        return NoChainCoverIndexError::new_err(no_index.to_string());
    }
    if let Some(failure) = err.downcast_ref::<AuthFailure>() {
        return failure.clone().into_py_err();
    }
    PyRuntimeError::new_err(format!("State resolution failed: {err:#}"))
}

/// Resolves state with the Rust implementation of state resolution v2 and
/// v2.1, reading the database through the native Postgres driver.
///
/// Each resolution reads the database inside one read-only transaction on a
/// pooled connection, and returns the connection before running the
/// algorithm itself, which does no I/O.
#[pyclass(frozen)]
pub struct StateResolutionHandler {
    /// The Python `RustConnectionPool`. Its `_pool` is read per call, as
    /// `close()`/`start()` replace it.
    db_pool: Py<PyAny>,
    runtime: RustRuntime,
    cache: Arc<ConflictCache>,
}

impl StateResolutionHandler {
    pub fn new(db_pool: Py<PyAny>, runtime: RustRuntime) -> Self {
        StateResolutionHandler {
            db_pool,
            runtime,
            cache: Arc::new(ConflictCache::new(
                CONFLICT_CACHE_MAX_SIZE,
                CONFLICT_CACHE_EXPIRY,
            )),
        }
    }

    /// Convert the inputs under the GIL and start a resolution: either the
    /// answer, if nothing conflicts, or the future that computes it.
    fn start(
        &self,
        py: Python<'_>,
        room_id: String,
        room_version: &str,
        state_sets: Vec<Bound<'_, PyAny>>,
        event_map: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Started> {
        let room_version: &'static RoomVersion = room_version
            .parse()
            .map_err(|e| PyValueError::new_err(format!("{e}")))?;
        if room_version.state_res == StateResolutionVersions::V1 {
            return Err(PyValueError::new_err(
                "Rust state resolution only supports v2 and v2.1",
            ));
        }

        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        let pool = pool.cast::<PyConnectionPool>()?.get().pool.clone();

        let mut interner = Interner::default();
        let state_sets = state_sets
            .iter()
            .map(|set| state_map_from_py(set, &mut interner))
            .collect::<PyResult<Vec<_>>>()?;

        // Without conflicts there is nothing to read or resolve, so answer
        // straight away rather than on the runtime.
        let (unconflicted, conflicted) = v2::separate(&state_sets);
        if conflicted.is_empty() {
            return Ok(Started::Done(ResolveOutput {
                state: unconflicted,
                stats: StateResStats::default(),
            }));
        }

        let events = event_map_from_py(event_map.as_ref(), &mut interner)?;

        let handle = self.runtime.tokio_handle()?;
        let cache = Arc::clone(&self.cache);

        Ok(Started::Pending(Box::pin(async move {
            let start = Instant::now();
            let store = PostgresStateResStore::begin(&pool, handle)
                .await
                .map_err(resolve_error_to_py)?;
            let prepared = v2::prepare_separated(
                &store,
                &room_id,
                room_version,
                &state_sets,
                (unconflicted, conflicted),
                events,
                Some(&cache),
            )
            .await
            .map_err(resolve_error_to_py)?;

            // The rest does no I/O, so give the connection back first.
            let db = store.finish().await.map_err(resolve_error_to_py)?;
            let resolution = match prepared {
                v2::Prepared::Done(resolution) => resolution,
                v2::Prepared::Pending(pending) => pending
                    .run(Some(&cache))
                    .await
                    .map_err(resolve_error_to_py)?,
            };

            let core = &resolution.stats;
            let stats = StateResStats {
                total_time: start.elapsed().as_secs_f64(),
                db_time: db.db_time.as_secs_f64(),
                parse_time: db.parse_time.as_secs_f64(),
                cpu_time: (core.sort_time + core.auth_time + core.mainline_time).as_secs_f64(),
                sort_time: core.sort_time.as_secs_f64(),
                auth_time: core.auth_time.as_secs_f64(),
                mainline_time: core.mainline_time.as_secs_f64(),
                db_queries: db.queries,
                db_rows: db.rows,
                db_events: db.events_fetched,
                conflicted_events: core.conflicted_events,
                cache_hit: core.cache_hit,
            };
            Ok(ResolveOutput {
                state: resolution.merged(),
                stats,
            })
        })))
    }
}

/// A started resolution. See [`StateResolutionHandler::start`].
enum Started {
    Done(ResolveOutput),
    Pending(Pin<Box<dyn Future<Output = PyResult<ResolveOutput>> + Send>>),
}

#[pymethods]
impl StateResolutionHandler {
    /// Resolve the state sets.
    ///
    /// `event_map` holds events that are not persisted yet; everything else
    /// is read from the database. It is not modified.
    ///
    /// Returns a deferred resolving to `(state, stats)`. If nothing
    /// conflicts, the deferred has already fired. Fails with
    /// `NoChainCoverIndexError` if the room has no chain cover index, in
    /// which case the caller should use the Python implementation.
    #[pyo3(signature = (room_id, room_version, state_sets, event_map=None))]
    fn resolve<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        room_version: &str,
        state_sets: Vec<Bound<'py, PyAny>>,
        event_map: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match self.start(py, room_id, room_version, state_sets, event_map)? {
            Started::Done(output) => py
                .import(intern!(py, "twisted.internet.defer"))?
                .call_method1(intern!(py, "succeed"), (output,)),
            Started::Pending(future) => create_deferred(py, &self.runtime, future),
        }
    }

    /// As [`Self::resolve`], but blocks (releasing the GIL) and returns
    /// `(state, stats)` directly.
    ///
    /// For tests: under trial's fake reactor nothing waits for a deferred
    /// fired from another thread, so code that resolves state in the
    /// background would not see the result in time. The Rust DB pool's test
    /// shim blocks the main thread in the same way.
    #[pyo3(signature = (room_id, room_version, state_sets, event_map=None))]
    fn resolve_blocking<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        room_version: &str,
        state_sets: Vec<Bound<'py, PyAny>>,
        event_map: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let output = match self.start(py, room_id, room_version, state_sets, event_map)? {
            Started::Done(output) => output,
            Started::Pending(future) => {
                let handle = self.runtime.tokio_handle()?;
                py.detach(|| handle.block_on(future))?
            }
        };
        output.into_pyobject(py)
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "state")?;
    child_module.add_class::<StateResolutionHandler>()?;
    child_module.add_class::<StateResStats>()?;
    child_module.add(
        "NoChainCoverIndexError",
        py.get_type::<NoChainCoverIndexError>(),
    )?;
    store::register_test_functions(py, &child_module)?;
    child_module.add_function(wrap_pyfunction!(resolve_in_memory_for_test, &child_module)?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import state` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.state", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn borrowed_lookup() {
        let mut map: StateMap = HashMap::new();
        map.insert(StateKey::new("m.room.member", "@a:b"), Arc::from("$e"));
        assert_eq!(
            lookup(&map, ("m.room.member", "@a:b")).map(|v| &**v),
            Some("$e")
        );
        assert!(lookup(&map, ("m.room.member", "@c:d")).is_none());
    }
}
