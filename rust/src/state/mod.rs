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

use std::{
    borrow::Borrow,
    collections::{HashMap, HashSet},
    hash::{Hash, Hasher},
    sync::Arc,
};

use anyhow::Context;
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    intern,
    prelude::*,
    types::{PyDict, PyModule, PyTuple},
};

use crate::events::Event;
use crate::room_versions::RoomVersion;

pub mod auth_chain;
pub mod conflict_cache;
pub mod store;
pub mod v2;

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

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "state")?;
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
