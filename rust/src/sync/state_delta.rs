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

//! The state delta arithmetic of `synapse.handlers.sync`: what state to
//! send for a room, given the state at a few points and what the timeline
//! holds. The database reads that produce those inputs are the store's.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use pyo3::{
    prelude::*,
    types::{PyDict, PyModule, PyModuleMethods},
};

use crate::events::constants::event_type::M_ROOM_MEMBER;
use crate::state::{StateKey, StateMap};

/// `_calculate_state`: the state to include in a sync response.
///
/// `timeline_contains` is the state in the timeline, `timeline_start` and
/// `timeline_end` the state at its ends, and `previous_timeline_end` the
/// state at the end of the previous sync (empty for an initial sync). With
/// `lazy_load_members`, `timeline_start` has been filtered to the members
/// the client needs, and those are sent even if the previous sync had them.
///
/// The Python docstring explains why the state at the end of the timeline
/// takes part: a fork in the DAG can put a state event the client needs
/// outside the state at the timeline's start.
///
/// One difference from the Python: when the start and end of the timeline
/// disagree on a key and neither event is in the timeline or the previous
/// state, the Python sends whichever its set iteration happens to visit
/// last. This sends the one from the end of the timeline, which is the
/// state the client should end up with.
pub fn calculate_state(
    timeline_contains: &StateMap,
    timeline_start: &StateMap,
    timeline_end: &StateMap,
    previous_timeline_end: &StateMap,
    lazy_load_members: bool,
) -> StateMap {
    let mut event_id_to_state_key: HashMap<&Arc<str>, &StateKey> = HashMap::new();
    for map in [
        timeline_contains,
        timeline_start,
        timeline_end,
        previous_timeline_end,
    ] {
        for (key, event_id) in map {
            event_id_to_state_key.insert(event_id, key);
        }
    }

    let timeline_end_ids: HashSet<&Arc<str>> = timeline_end.values().collect();
    let timeline_start_ids: HashSet<&Arc<str>> = timeline_start.values().collect();
    let mut previous_timeline_end_ids: HashSet<&Arc<str>> =
        previous_timeline_end.values().collect();
    let timeline_contains_ids: HashSet<&Arc<str>> = timeline_contains.values().collect();

    // With lazy loading, the memberships in `timeline_start` are the ones
    // the timeline's senders need, and they may never have been sent: take
    // them out of what the previous sync is assumed to have covered.
    if lazy_load_members {
        for (key, event_id) in timeline_start {
            if &*key.0 == M_ROOM_MEMBER {
                previous_timeline_end_ids.remove(event_id);
            }
        }
    }

    // The start's entries first, so that the end's overwrite them.
    timeline_start_ids
        .iter()
        .chain(&timeline_end_ids)
        .filter(|id| !previous_timeline_end_ids.contains(*id))
        .filter(|id| !timeline_contains_ids.contains(*id))
        .map(|id| ((*event_id_to_state_key[*id]).clone(), Arc::clone(id)))
        .collect()
}

/// A row of `current_state_delta_stream`, in stream order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateDelta {
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
    /// `None` when the key was removed from the current state.
    pub event_id: Option<Arc<str>>,
    pub prev_event_id: Option<Arc<str>>,
}

impl StateDelta {
    fn key(&self) -> StateKey {
        StateKey(Arc::clone(&self.event_type), Arc::clone(&self.state_key))
    }
}

/// Roll `state` back over `deltas`, which run forwards from the point
/// `state` is wanted at to the point it was read at. The deltas are walked
/// backwards so that the earliest change to a key is what sticks.
///
/// This is the `use_state_after` loop in `_compute_state_delta_for_full_
/// sync`, which reads the current state and rolls it back to the end of the
/// timeline.
pub fn roll_back_state(state: &mut StateMap, deltas: &[StateDelta]) {
    for delta in deltas.iter().rev() {
        match &delta.prev_event_id {
            Some(prev_event_id) => {
                state.insert(delta.key(), Arc::clone(prev_event_id));
            }
            None => {
                state.remove(&delta.key());
            }
        }
    }
}

/// What applying the deltas since the last sync gives, for the
/// `use_state_after` incremental path.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct AppliedDeltas {
    /// The state to send, on top of `initial`.
    pub state: StateMap,
    /// The keys in `state` that the deltas changed.
    pub changed: HashSet<StateKey>,
    /// Keys removed from the current state that the caller has to look up
    /// in state groups at the end of the timeline: the keys the timeline
    /// changed, and the user's own membership. See the Python comment on
    /// why a server leaving a room produces these.
    pub cleared: HashSet<StateKey>,
}

/// The delta loop of `_compute_state_delta_for_incremental_sync` with
/// `use_state_after`: apply `deltas` (in stream order, so the latest wins)
/// to `initial`, the lazy-loaded memberships fetched first.
pub fn apply_state_deltas(
    initial: StateMap,
    deltas: &[StateDelta],
    timeline_state: &StateMap,
    user_id: &str,
) -> AppliedDeltas {
    let mut applied = AppliedDeltas {
        state: initial,
        ..AppliedDeltas::default()
    };
    let own_membership = StateKey::new(M_ROOM_MEMBER, user_id);
    for delta in deltas {
        let key = delta.key();
        match &delta.event_id {
            None => {
                if timeline_state.contains_key(&key) || key == own_membership {
                    applied.cleared.insert(key);
                }
            }
            Some(event_id) => {
                applied.state.insert(key.clone(), Arc::clone(event_id));
                applied.changed.insert(key.clone());
                applied.cleared.remove(&key);
            }
        }
    }
    applied
}

// Entry points for the differential tests.

fn state_map_from_py(dict: &Bound<'_, PyDict>) -> PyResult<StateMap> {
    let mut map = StateMap::new();
    for (key, event_id) in dict.iter() {
        let (event_type, state_key): (String, String) = key.extract()?;
        let event_id: String = event_id.extract()?;
        map.insert(StateKey::new(&event_type, &state_key), Arc::from(event_id));
    }
    Ok(map)
}

fn state_map_to_py<'py>(py: Python<'py>, map: &StateMap) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (key, event_id) in map {
        dict.set_item((&*key.0, &*key.1), &**event_id)?;
    }
    Ok(dict)
}

fn keys_to_py(keys: &HashSet<StateKey>) -> HashSet<(String, String)> {
    keys.iter()
        .map(|key| (key.0.to_string(), key.1.to_string()))
        .collect()
}

type PyDelta = (String, String, Option<String>, Option<String>);

fn deltas_from_py(deltas: Vec<PyDelta>) -> Vec<StateDelta> {
    deltas
        .into_iter()
        .map(
            |(event_type, state_key, event_id, prev_event_id)| StateDelta {
                event_type: Arc::from(event_type),
                state_key: Arc::from(state_key),
                event_id: event_id.map(Arc::from),
                prev_event_id: prev_event_id.map(Arc::from),
            },
        )
        .collect()
}

/// `_calculate_state` through the Rust implementation.
#[pyfunction]
fn calculate_state_for_test<'py>(
    py: Python<'py>,
    timeline_contains: &Bound<'py, PyDict>,
    timeline_start: &Bound<'py, PyDict>,
    timeline_end: &Bound<'py, PyDict>,
    previous_timeline_end: &Bound<'py, PyDict>,
    lazy_load_members: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let state = calculate_state(
        &state_map_from_py(timeline_contains)?,
        &state_map_from_py(timeline_start)?,
        &state_map_from_py(timeline_end)?,
        &state_map_from_py(previous_timeline_end)?,
        lazy_load_members,
    );
    state_map_to_py(py, &state)
}

/// The full-sync `use_state_after` roll-back through the Rust
/// implementation. Deltas are `(type, state_key, event_id, prev_event_id)`
/// in stream order.
#[pyfunction]
fn roll_back_state_for_test<'py>(
    py: Python<'py>,
    state: &Bound<'py, PyDict>,
    deltas: Vec<PyDelta>,
) -> PyResult<Bound<'py, PyDict>> {
    let mut state = state_map_from_py(state)?;
    roll_back_state(&mut state, &deltas_from_py(deltas));
    state_map_to_py(py, &state)
}

/// The incremental `use_state_after` delta loop through the Rust
/// implementation: `(state, changed_keys, cleared_keys)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn apply_state_deltas_for_test<'py>(
    py: Python<'py>,
    initial: &Bound<'py, PyDict>,
    deltas: Vec<PyDelta>,
    timeline_state: &Bound<'py, PyDict>,
    user_id: &str,
) -> PyResult<(
    Bound<'py, PyDict>,
    HashSet<(String, String)>,
    HashSet<(String, String)>,
)> {
    let applied = apply_state_deltas(
        state_map_from_py(initial)?,
        &deltas_from_py(deltas),
        &state_map_from_py(timeline_state)?,
        user_id,
    );
    Ok((
        state_map_to_py(py, &applied.state)?,
        keys_to_py(&applied.changed),
        keys_to_py(&applied.cleared),
    ))
}

pub fn register_test_functions(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(calculate_state_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(roll_back_state_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(apply_state_deltas_for_test, m)?)?;
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

    fn delta(t: &str, k: &str, event_id: Option<&str>, prev: Option<&str>) -> StateDelta {
        StateDelta {
            event_type: Arc::from(t),
            state_key: Arc::from(k),
            event_id: event_id.map(Arc::from),
            prev_event_id: prev.map(Arc::from),
        }
    }

    #[test]
    fn state_from_a_fork_comes_from_the_timeline_end() {
        // The docstring's example: S2 is in the state at the end of the
        // timeline but not at its start, and not in the timeline.
        let result = calculate_state(
            &state(&[]),
            &state(&[("m.room.create", "", "$e1")]),
            &state(&[("m.room.create", "", "$e1"), ("m.room.topic", "", "$s2")]),
            &state(&[("m.room.create", "", "$e1")]),
            false,
        );
        assert_eq!(result, state(&[("m.room.topic", "", "$s2")]));
    }

    #[test]
    fn lazy_loaded_members_are_sent_again() {
        let member = ("m.room.member", "@a:x", "$m");
        let without = calculate_state(
            &state(&[]),
            &state(&[member]),
            &state(&[member]),
            &state(&[member]),
            false,
        );
        assert!(without.is_empty());
        let with = calculate_state(
            &state(&[]),
            &state(&[member]),
            &state(&[member]),
            &state(&[member]),
            true,
        );
        assert_eq!(with, state(&[member]));
        // A non-member key the previous sync had is still left out.
        let topic = ("m.room.topic", "", "$t");
        let with = calculate_state(
            &state(&[]),
            &state(&[topic]),
            &state(&[topic]),
            &state(&[topic]),
            true,
        );
        assert!(with.is_empty());
    }

    #[test]
    fn timeline_state_is_not_repeated() {
        let topic = ("m.room.topic", "", "$t");
        let result = calculate_state(
            &state(&[topic]),
            &state(&[]),
            &state(&[topic]),
            &state(&[]),
            false,
        );
        assert!(result.is_empty());
    }

    #[test]
    fn rolling_back_keeps_the_earliest_change() {
        let mut current = state(&[("m.room.topic", "", "$t3"), ("m.room.name", "", "$n")]);
        roll_back_state(
            &mut current,
            &[
                delta("m.room.topic", "", Some("$t2"), Some("$t1")),
                delta("m.room.name", "", Some("$n"), None),
                delta("m.room.topic", "", Some("$t3"), Some("$t2")),
            ],
        );
        assert_eq!(current, state(&[("m.room.topic", "", "$t1")]));
    }

    #[test]
    fn applying_deltas_tracks_changed_and_cleared_keys() {
        let applied = apply_state_deltas(
            state(&[("m.room.member", "@b:x", "$mb")]),
            &[
                delta("m.room.topic", "", Some("$t1"), None),
                delta("m.room.topic", "", Some("$t2"), Some("$t1")),
                // The server left: the timeline's keys and the user's
                // membership are cleared, other keys are dropped.
                delta("m.room.name", "", None, Some("$n")),
                delta("m.room.member", "@me:x", None, Some("$me")),
                delta("m.room.member", "@c:x", None, Some("$mc")),
            ],
            &state(&[("m.room.name", "", "$n2")]),
            "@me:x",
        );
        assert_eq!(
            applied.state,
            state(&[
                ("m.room.member", "@b:x", "$mb"),
                ("m.room.topic", "", "$t2")
            ])
        );
        assert_eq!(
            applied.changed,
            HashSet::from([StateKey::new("m.room.topic", "")])
        );
        assert_eq!(
            applied.cleared,
            HashSet::from([
                StateKey::new("m.room.name", ""),
                StateKey::new("m.room.member", "@me:x"),
            ])
        );

        // A later delta with an event un-clears the key.
        let applied = apply_state_deltas(
            StateMap::new(),
            &[
                delta("m.room.name", "", None, Some("$n")),
                delta("m.room.name", "", Some("$n2"), None),
            ],
            &state(&[("m.room.name", "", "$n2")]),
            "@me:x",
        );
        assert!(applied.cleared.is_empty());
        assert_eq!(applied.state, state(&[("m.room.name", "", "$n2")]));
    }
}
