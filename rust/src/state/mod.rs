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

use std::{
    borrow::Borrow,
    collections::HashMap,
    hash::{Hash, Hasher},
    sync::Arc,
};

use anyhow::Context;
use pyo3::{prelude::*, types::PyModule};

use crate::events::Event;

pub mod auth_chain;
pub mod store;

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

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "state")?;
    store::register_test_functions(py, &child_module)?;

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
