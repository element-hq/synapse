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

//! What crosses to Python for the rooms of a request: [`RoomsResult`], and a
//! [`RoomResult`] per room Rust computed.
//!
//! A `RoomResult`'s events never become Python objects, except for module
//! callbacks: Python reads only what `current_sync_for_user` and the
//! extensions need, and the servlet encodes the room in Rust.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use pyo3::{intern, prelude::*, types::PyList};

use crate::caches::lock_ignoring_poison;
use crate::events::Event;
use crate::sliding_sync::config::{RequiredStateMap, RoomSyncConfig, MEMBER};
use crate::sliding_sync::connection::{MutablePerConnectionState, RoomLazyMembershipChanges};
use crate::sliding_sync::room_entry::{RoomEntry, RoomOutcome};
use crate::sliding_sync::SlidingSyncStats;
use crate::sync::relations::Bundle;

/// A `required_state_map` from Python: a mapping of types to sets of
/// state keys.
pub fn required_state_map_from_py(map: &Bound<'_, PyAny>) -> PyResult<RequiredStateMap> {
    let py = map.py();
    let mut result = RequiredStateMap::new();
    for item in map.call_method0(intern!(py, "items"))?.try_iter()? {
        let (state_type, state_keys): (String, Bound<'_, PyAny>) = item?.extract()?;
        let mut keys = BTreeSet::new();
        for key in state_keys.try_iter()? {
            keys.insert(key?.extract::<String>()?);
        }
        result.insert(state_type, keys);
    }
    Ok(result)
}

/// One room's entry, computed in Rust: `SlidingSyncResult.RoomResult` as
/// far as `current_sync_for_user` and the extensions read it.
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomResult {
    pub(crate) entry: Arc<RoomEntry>,
}

#[pymethods]
impl RoomResult {
    fn __bool__(&self) -> bool {
        self.entry.has_updates()
    }

    #[getter]
    fn room_id(&self) -> &str {
        &self.entry.room_id
    }

    #[getter]
    fn initial(&self) -> bool {
        self.entry.initial
    }

    #[getter]
    fn unstable_expanded_timeline(&self) -> bool {
        self.entry.unstable_expanded_timeline
    }

    #[getter]
    fn timeline_event_ids(&self) -> Vec<&str> {
        self.entry
            .timeline
            .iter()
            .map(|filtered| filtered.event.event_id())
            .collect()
    }

    #[getter]
    fn timeline_senders(&self) -> Vec<&str> {
        self.entry
            .timeline
            .iter()
            .map(|filtered| filtered.event.sender())
            .collect()
    }

    /// The state keys of the membership events in `required_state`.
    #[getter]
    fn required_state_member_keys(&self) -> Vec<&str> {
        self.entry
            .required_state
            .iter()
            .filter(|event| event.type_() == MEMBER)
            .filter_map(Event::state_key)
            .collect()
    }

    #[getter]
    fn hero_user_ids(&self) -> Vec<&str> {
        self.entry
            .heroes
            .iter()
            .map(|hero| hero.user_id.as_str())
            .collect()
    }

    /// Every event the room's encoding serializes, for the module
    /// callbacks that add to their unsigned data.
    fn events_to_serialize<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let bundled = self
            .entry
            .bundles
            .iter()
            .flat_map(|bundles| bundles.values().flat_map(Bundle::events));
        let events = self
            .entry
            .timeline
            .iter()
            .map(|filtered| &filtered.event)
            .chain(&self.entry.required_state)
            .chain(bundled)
            .map(|event| Bound::new(py, event.shallow_clone()))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, events)
    }
}

/// What [`crate::sliding_sync::room_entry::generate_room_entries`] made of
/// the rooms.
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomsResult {
    /// The entries to send. Taken by [`RoomsResult::take_entries`].
    entries: Mutex<Vec<Arc<RoomEntry>>>,
    /// The rooms computed with nothing to send.
    empty: Vec<Arc<str>>,
    /// `room_configs[room_id]` for the computed rooms whose config changes.
    room_config_updates: Vec<(Arc<str>, RoomSyncConfig)>,
    /// `room_lazy_membership[room_id]` for every computed room.
    lazy_memberships: Vec<(Arc<str>, RoomLazyMembershipChanges)>,
    fallbacks: Vec<(Arc<str>, String)>,
    errors: Vec<(Arc<str>, String)>,
    stats: SlidingSyncStats,
}

impl RoomsResult {
    /// The result of `outcomes`. An entry with nothing to send is dropped
    /// from an incremental response.
    pub fn new(
        outcomes: Vec<(Arc<str>, RoomOutcome)>,
        incremental: bool,
        mut stats: SlidingSyncStats,
    ) -> Self {
        let mut entries = Vec::new();
        let mut empty = Vec::new();
        let mut room_config_updates = Vec::new();
        let mut lazy_memberships = Vec::new();
        let mut fallbacks = Vec::new();
        let mut errors = Vec::new();
        for (room_id, outcome) in outcomes {
            match outcome {
                RoomOutcome::Computed(computed) => {
                    stats.rooms_computed += 1;
                    if computed.lazy_member_lookup {
                        stats.lazy_member_lookups += 1;
                    }
                    let mutations = computed.mutations;
                    if let Some(config) = mutations.room_config {
                        room_config_updates.push((Arc::clone(&room_id), config));
                    }
                    lazy_memberships.push((Arc::clone(&room_id), mutations.lazy_membership));
                    if computed.entry.has_updates() || !incremental {
                        entries.push(Arc::new(computed.entry));
                    } else {
                        empty.push(room_id);
                    }
                }
                RoomOutcome::Fallback(reason) => fallbacks.push((room_id, reason)),
                RoomOutcome::Error(error) => errors.push((room_id, error)),
            }
        }
        RoomsResult {
            entries: Mutex::new(entries),
            empty,
            room_config_updates,
            lazy_memberships,
            fallbacks,
            errors,
            stats,
        }
    }

    /// Record the computed rooms' changes in the connection state.
    pub fn apply_to<R: Clone>(&self, state: &mut MutablePerConnectionState<R>) {
        for (room_id, config) in &self.room_config_updates {
            state.set_room_config(room_id, config.clone());
        }
        for (room_id, changes) in &self.lazy_memberships {
            state
                .room_lazy_membership
                .insert(room_id.to_string(), changes.clone());
        }
    }
}

#[pymethods]
impl RoomsResult {
    /// The entries to send as `RoomResult`s. A second call returns an
    /// empty list.
    fn take_entries<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let entries = std::mem::take(&mut *lock_ignoring_poison(&self.entries));
        let items = entries
            .into_iter()
            .map(|entry| Bound::new(py, RoomResult { entry }))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, items)
    }

    #[getter]
    fn empty(&self) -> Vec<&str> {
        self.empty.iter().map(|room_id| &**room_id).collect()
    }

    /// `(room_id, reason)` for each room Python must compute.
    #[getter]
    fn fallbacks(&self) -> Vec<(&str, &str)> {
        self.fallbacks
            .iter()
            .map(|(room_id, reason)| (&**room_id, reason.as_str()))
            .collect()
    }

    /// `(room_id, error)` for each room Rust failed on.
    #[getter]
    fn errors(&self) -> Vec<(&str, &str)> {
        self.errors
            .iter()
            .map(|(room_id, error)| (&**room_id, error.as_str()))
            .collect()
    }

    #[getter]
    fn stats(&self) -> SlidingSyncStats {
        self.stats
    }
}
