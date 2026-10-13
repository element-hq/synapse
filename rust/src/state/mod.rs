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
//! - [`lru`]: the conflict, event and chain cover caches.
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
pub mod lru;
pub mod store;
pub mod v2;

use lru::{ConflictCache, StateMapCache, StateResCaches};
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
/// An event handed in from Python is a shallow copy sharing the Python
/// object's data, including its internal metadata. The auth event IDs are
/// computed once, as the algorithm reads them many times.
#[derive(Clone)]
pub struct StateResEvent(Arc<StateResEventInner>);

struct StateResEventInner {
    event: Event,
    auth_event_ids: Box<[Arc<str>]>,
}

impl StateResEvent {
    pub fn new(event: Event) -> anyhow::Result<Self> {
        let auth_event_ids = auth_event_ids_of(&event)?;
        Ok(StateResEvent(Arc::new(StateResEventInner {
            event,
            auth_event_ids,
        })))
    }

    pub fn event(&self) -> &Event {
        &self.0.event
    }

    pub fn auth_event_ids(&self) -> &[Arc<str>] {
        &self.0.auth_event_ids
    }

    /// The event as a new Python object sharing this event's data.
    pub fn to_py(&self, py: Python<'_>) -> PyResult<Py<Event>> {
        Py::new(py, self.0.event.shallow_clone())
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
struct Interner(rustc_hash::FxHashSet<Arc<str>>);

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
        let event = event.cast::<Event>()?.get().shallow_clone();
        map.insert(
            interner.intern(event_id.extract()?),
            StateResEvent::new(event).map_err(|e| PyValueError::new_err(format!("{e:#}")))?,
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
        .map(|set| Ok(Arc::new(state_map_from_py(set, &mut interner)?)))
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

/// The bound on the event cache, in events.
/// TODO: size this from `event_cache_size`, as the Python event cache is.
const EVENT_CACHE_MAX_SIZE: usize = 10_000;

/// The bound on the chain position cache, in events. A resolution needs the
/// position of every event in its state sets, so this holds more than the
/// event cache, whose events are only the conflicted ones and their auth
/// difference.
/// TODO: size from the cache factor.
const CHAIN_POSITION_CACHE_MAX_SIZE: usize = 100_000;

/// The bound on the chain link cache, in links.
/// TODO: size from the cache factor.
const CHAIN_LINK_CACHE_MAX_SIZE: usize = 1_000_000;

/// How long an unused event, chain position or chain link cache entry lives.
const STORE_CACHE_EXPIRY: Duration = Duration::from_secs(60 * 60);

/// The bound on the converted state map cache, in state entries. The Python
/// state group caches hold 50,000 non-member and 500,000 member entries.
/// TODO: size from the cache factor.
const STATE_MAP_CACHE_MAX_SIZE: usize = 500_000;

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
    /// Time computing the auth chain difference, caches included.
    pub auth_chain_time: f64,
    /// Time loading events, caches included.
    pub load_events_time: f64,
    /// Time checking out the connection, beginning and committing the
    /// transaction.
    pub connection_time: f64,
    pub db_queries: u64,
    pub db_rows: u64,
    /// Events loaded from the database.
    pub db_events: u64,
    /// Events found in the handler's event cache.
    pub event_cache_hits: u64,
    /// Chain positions found in the handler's chain position cache.
    pub chain_position_cache_hits: u64,
    /// Chains whose links came from the handler's chain link cache.
    pub chain_link_cache_hits: u64,
    /// State groups whose converted state came from the handler's state map
    /// cache, so they were not converted from Python. `resolve_state_groups`
    /// only.
    pub state_map_cache_hits: u64,
    /// Time converting the state sets from Python, under the GIL.
    pub convert_in_time: f64,
    /// Time converting the resolved state to Python, under the GIL.
    pub convert_out_time: f64,
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
        let start = Instant::now();
        let state = state_map_to_py(py, &self.state)?;
        let mut stats = self.stats;
        stats.convert_out_time = start.elapsed().as_secs_f64();
        (state, Bound::new(py, stats)?).into_pyobject(py)
    }
}

/// How a resolution of state groups relates to its input groups: the fields
/// of a `_StateCacheEntry`. See [`make_state_cache_entry`].
enum StateGroupsResult {
    /// The resolved state is the state of this input group.
    Group(i64),
    /// The resolved state is this input group's with these changes.
    Delta { prev_group: i64, delta: StateMap },
    /// No input group can be the base: each removes a key.
    State(StateMap),
}

/// Port of `_make_state_cache_entry`: how `new_state` relates to the input
/// groups, in the order they were given, as the Python breaks ties by its
/// dict's order.
fn make_state_cache_entry(
    new_state: StateMap,
    groups: &[(i64, Arc<StateMap>)],
) -> StateGroupsResult {
    // First look for exact matches, by the set of event IDs as the Python
    // does.
    let new_event_ids: HashSet<&Arc<str>> = new_state.values().collect();
    for (group, state) in groups {
        if new_event_ids.len() != state.len() {
            continue;
        }
        let old_event_ids: HashSet<&Arc<str>> = state.values().collect();
        if new_event_ids == old_event_ids {
            return StateGroupsResult::Group(*group);
        }
    }

    // Failing that, the closest match. A delta cannot remove keys, so a
    // group with a key the new state lacks is no candidate.
    let mut best: Option<(i64, StateMap)> = None;
    for (group, old_state) in groups {
        if old_state.keys().any(|key| !new_state.contains_key(key)) {
            continue;
        }
        let delta: StateMap = new_state
            .iter()
            .filter(|(key, event_id)| old_state.get(*key) != Some(event_id))
            .map(|(key, event_id)| (key.clone(), Arc::clone(event_id)))
            .collect();
        // `if not delta_ids or len(n_delta_ids) < len(delta_ids)`.
        let better = match &best {
            None => true,
            Some((_, best_delta)) => best_delta.is_empty() || delta.len() < best_delta.len(),
        };
        if better {
            best = Some((*group, delta));
        }
    }

    match best {
        Some((prev_group, delta)) => StateGroupsResult::Delta { prev_group, delta },
        None => StateGroupsResult::State(new_state),
    }
}

/// What a state group resolution's deferred resolves to.
struct StateGroupsOutput {
    result: StateGroupsResult,
    stats: StateResStats,
}

impl StateGroupsOutput {
    fn new(output: ResolveOutput, groups: &[(i64, Arc<StateMap>)]) -> Self {
        StateGroupsOutput {
            result: make_state_cache_entry(output.state, groups),
            stats: output.stats,
        }
    }
}

impl<'py> IntoPyObject<'py> for StateGroupsOutput {
    type Target = PyTuple;
    type Output = Bound<'py, PyTuple>;
    type Error = PyErr;

    /// `(state_group, prev_group, delta_ids, state, stats)`.
    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        let start = Instant::now();
        let (state_group, prev_group, delta_ids, state) = match self.result {
            StateGroupsResult::Group(group) => (Some(group), None, None, None),
            StateGroupsResult::Delta { prev_group, delta } => (
                None,
                Some(prev_group),
                Some(state_map_to_py(py, &delta)?),
                None,
            ),
            StateGroupsResult::State(state) => {
                (None, None, None, Some(state_map_to_py(py, &state)?))
            }
        };
        let mut stats = self.stats;
        stats.convert_out_time = start.elapsed().as_secs_f64();
        (
            state_group,
            prev_group,
            delta_ids,
            state,
            Bound::new(py, stats)?,
        )
            .into_pyobject(py)
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
    /// The event, chain position and chain link caches the store reads.
    caches: Arc<StateResCaches>,
    /// State groups' state as already converted from Python.
    state_maps: Arc<StateMapCache>,
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
            caches: Arc::new(StateResCaches::new(
                EVENT_CACHE_MAX_SIZE,
                CHAIN_POSITION_CACHE_MAX_SIZE,
                CHAIN_LINK_CACHE_MAX_SIZE,
                STORE_CACHE_EXPIRY,
            )),
            state_maps: Arc::new(StateMapCache::new(
                STATE_MAP_CACHE_MAX_SIZE,
                STORE_CACHE_EXPIRY,
            )),
        }
    }

    pub(crate) fn caches(&self) -> Arc<StateResCaches> {
        Arc::clone(&self.caches)
    }

    /// Convert the state sets under the GIL and start a resolution: either
    /// the answer, if nothing conflicts, or the future that computes it.
    fn start(
        &self,
        py: Python<'_>,
        room_id: String,
        room_version: &str,
        state_sets: Vec<Bound<'_, PyAny>>,
        event_map: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Started> {
        let convert_start = Instant::now();
        let mut interner = Interner::default();
        let state_sets = state_sets
            .iter()
            .map(|set| Ok(Arc::new(state_map_from_py(set, &mut interner)?)))
            .collect::<PyResult<Vec<_>>>()?;
        let converted = Converted {
            state_sets,
            interner,
            stats: StateResStats {
                convert_in_time: convert_start.elapsed().as_secs_f64(),
                ..StateResStats::default()
            },
        };
        self.start_converted(py, room_id, room_version, converted, event_map)
    }

    /// As [`Self::start`], for state groups: the state of a group that took
    /// part in an earlier resolution comes from the state map cache instead
    /// of being converted again. Returns the groups' state in the order
    /// given, for [`make_state_cache_entry`].
    #[allow(clippy::type_complexity)]
    fn start_groups(
        &self,
        py: Python<'_>,
        room_id: String,
        room_version: &str,
        state_groups: &Bound<'_, PyAny>,
        event_map: Option<Bound<'_, PyDict>>,
    ) -> PyResult<(Started, Vec<(i64, Arc<StateMap>)>)> {
        let convert_start = Instant::now();
        let mut interner = Interner::default();
        let mut groups: Vec<(i64, Arc<StateMap>)> = Vec::new();
        let mut hits = 0;
        for item in state_groups
            .call_method0(intern!(py, "items"))?
            .try_iter()?
        {
            let (group, state): (i64, Bound<'_, PyAny>) = item?.extract()?;
            // The cache holds a group's full state, which is what both
            // callers pass. A smaller map is a filtered one: it is converted
            // for this call only, and does not replace what is cached.
            let given = state.len()?;
            let cached = self.state_maps.get(&group);
            let map = match cached {
                Some(map) if map.len() == given => {
                    hits += 1;
                    map
                }
                cached => {
                    let map = Arc::new(state_map_from_py(&state, &mut interner)?);
                    if cached.is_none_or(|cached| cached.len() < given) {
                        self.state_maps.insert(group, Arc::clone(&map), map.len());
                    }
                    map
                }
            };
            groups.push((group, map));
        }
        let converted = Converted {
            state_sets: groups.iter().map(|(_, map)| Arc::clone(map)).collect(),
            interner,
            stats: StateResStats {
                convert_in_time: convert_start.elapsed().as_secs_f64(),
                state_map_cache_hits: hits,
                ..StateResStats::default()
            },
        };
        let started = self.start_converted(py, room_id, room_version, converted, event_map)?;
        Ok((started, groups))
    }

    /// Start a resolution of converted state sets.
    fn start_converted(
        &self,
        py: Python<'_>,
        room_id: String,
        room_version: &str,
        converted: Converted,
        event_map: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Started> {
        let Converted {
            state_sets,
            mut interner,
            stats,
        } = converted;
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

        // Without conflicts there is nothing to read or resolve, so answer
        // straight away rather than on the runtime.
        let (unconflicted, conflicted) = v2::separate(&state_sets);
        if conflicted.is_empty() {
            return Ok(Started::Done(ResolveOutput {
                state: unconflicted,
                stats,
            }));
        }

        let events = event_map_from_py(event_map.as_ref(), &mut interner)?;

        let handle = self.runtime.tokio_handle()?;
        let cache = Arc::clone(&self.cache);
        let caches = Arc::clone(&self.caches);

        Ok(Started::Pending(Box::pin(async move {
            let start = Instant::now();
            let store = PostgresStateResStore::begin(&pool, handle, Some(caches))
                .await
                .map_err(resolve_error_to_py)?;
            let mut connection_time = start.elapsed();
            let prepared = v2::prepare(
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
            let finish_start = Instant::now();
            let db = store.finish().await.map_err(resolve_error_to_py)?;
            connection_time += finish_start.elapsed();
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
                convert_in_time: stats.convert_in_time,
                state_map_cache_hits: stats.state_map_cache_hits,
                db_time: db.db_time.as_secs_f64(),
                parse_time: db.parse_time.as_secs_f64(),
                cpu_time: (core.sort_time + core.auth_time + core.mainline_time).as_secs_f64(),
                sort_time: core.sort_time.as_secs_f64(),
                auth_time: core.auth_time.as_secs_f64(),
                mainline_time: core.mainline_time.as_secs_f64(),
                auth_chain_time: core.auth_chain_time.as_secs_f64(),
                load_events_time: core.load_events_time.as_secs_f64(),
                connection_time: connection_time.as_secs_f64(),
                db_queries: db.queries,
                db_rows: db.rows,
                db_events: db.events_fetched,
                event_cache_hits: db.events_cached,
                chain_position_cache_hits: db.chain_positions_cached,
                chain_link_cache_hits: db.chain_links_cached,
                convert_out_time: 0.0,
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

/// State sets converted from Python, with the interner that built them (so
/// the event map shares its strings) and what the conversion recorded.
struct Converted {
    state_sets: Vec<Arc<StateMap>>,
    interner: Interner,
    stats: StateResStats,
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

    /// Resolve the state of state groups, as
    /// `StateResolutionHandler.resolve_state_groups` does.
    ///
    /// `state_groups` maps each group's ID to its full state. A group that
    /// took part in an earlier resolution is not converted again.
    ///
    /// Returns a deferred resolving to `(state_group, prev_group, delta_ids,
    /// state, stats)`, the fields of a `_StateCacheEntry` chosen as
    /// `_make_state_cache_entry` does: the input group the resolved state
    /// equals, else the input group it differs least from and the
    /// difference, else the state itself. Fails as [`Self::resolve`] does.
    #[pyo3(signature = (room_id, room_version, state_groups, event_map=None))]
    fn resolve_state_groups<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        room_version: &str,
        state_groups: &Bound<'py, PyAny>,
        event_map: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (started, groups) =
            self.start_groups(py, room_id, room_version, state_groups, event_map)?;
        match started {
            Started::Done(output) => py
                .import(intern!(py, "twisted.internet.defer"))?
                .call_method1(
                    intern!(py, "succeed"),
                    (StateGroupsOutput::new(output, &groups),),
                ),
            Started::Pending(future) => create_deferred(py, &self.runtime, async move {
                Ok(StateGroupsOutput::new(future.await?, &groups))
            }),
        }
    }

    /// As [`Self::resolve_state_groups`], but blocks (releasing the GIL) and
    /// returns the tuple directly. For tests, as [`Self::resolve_blocking`].
    #[pyo3(signature = (room_id, room_version, state_groups, event_map=None))]
    fn resolve_state_groups_blocking<'py>(
        &self,
        py: Python<'py>,
        room_id: String,
        room_version: &str,
        state_groups: &Bound<'py, PyAny>,
        event_map: Option<Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let (started, groups) =
            self.start_groups(py, room_id, room_version, state_groups, event_map)?;
        let output = match started {
            Started::Done(output) => output,
            Started::Pending(future) => {
                let handle = self.runtime.tokio_handle()?;
                py.detach(|| handle.block_on(future))?
            }
        };
        StateGroupsOutput::new(output, &groups).into_pyobject(py)
    }

    /// The number of state groups in the state map cache.
    fn state_map_cache_len(&self) -> usize {
        self.state_maps.len()
    }

    /// Empty the conflict cache, keeping the event and chain caches. For
    /// benchmarks and tests that want to time the algorithm again on loaded
    /// events.
    fn clear_conflict_cache_for_test(&self) {
        self.cache.clear();
    }

    /// The number of events in the event cache.
    fn event_cache_len(&self) -> usize {
        self.caches.events.len()
    }

    /// The number of chains in the chain link cache.
    fn chain_link_cache_len(&self) -> usize {
        self.caches.chain_links.len()
    }

    /// Drop an event from the event and chain position caches. Called
    /// whenever the Python event cache drops it, so a change to its rejection
    /// status or internal metadata reaches the next resolution.
    fn invalidate_event(&self, event_id: &str) {
        self.caches.invalidate_event(event_id);
    }

    /// Empty the event, chain and state map caches. Called when a room is
    /// purged.
    fn clear_event_cache(&self) {
        self.caches.clear();
        self.state_maps.clear();
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

    fn state(entries: &[(&str, &str, &str)]) -> StateMap {
        entries
            .iter()
            .map(|(t, k, e)| (StateKey::new(t, k), Arc::from(*e)))
            .collect()
    }

    #[test]
    fn state_cache_entry_matches_the_python() {
        let a = Arc::new(state(&[
            ("m.room.create", "", "$c"),
            ("m.room.topic", "", "$t1"),
        ]));
        let b = Arc::new(state(&[
            ("m.room.create", "", "$c"),
            ("m.room.topic", "", "$t2"),
        ]));
        let groups = vec![(1, Arc::clone(&a)), (2, Arc::clone(&b))];

        // An exact match reuses the group.
        assert!(matches!(
            make_state_cache_entry((*b).clone(), &groups),
            StateGroupsResult::Group(2)
        ));

        // Otherwise the group with the smallest delta, the first on a tie.
        let new = state(&[("m.room.create", "", "$c"), ("m.room.topic", "", "$t3")]);
        match make_state_cache_entry(new, &groups) {
            StateGroupsResult::Delta { prev_group, delta } => {
                assert_eq!(prev_group, 1);
                assert_eq!(delta, state(&[("m.room.topic", "", "$t3")]));
            }
            _ => panic!("expected a delta"),
        }

        // A group with a key the new state lacks cannot be the base.
        let new = state(&[("m.room.create", "", "$c")]);
        assert!(matches!(
            make_state_cache_entry(new, &groups),
            StateGroupsResult::State(_)
        ));
    }

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
