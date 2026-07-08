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

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PySet, PyTuple};
use pythonize::depythonize;
use rezzy::{
    auth::roaring::AuthGraph, resolve_lattice_fold, LeanEvent, SharedState, StateResVersion,
};
use serde_json::Value;

use crate::events::{Event, EventResolverData};

#[pyfunction]
#[pyo3(text_signature = "(state_sets, event_map, /)")]
pub fn get_auth_chain_difference_from_event_graph<'py>(
    py: Python<'py>,
    state_sets: Bound<'py, PyAny>,
    event_map: Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PySet>> {
    let mut auth_graph_events: HashMap<String, LeanEvent<String, ()>> =
        HashMap::with_capacity(event_map.len());
    for (k, v) in event_map.iter() {
        let event_id: String = k.extract()?;
        let auth_ids: Vec<String> = if let Ok(event) = v.extract::<PyRef<Event>>() {
            event.auth_event_ids()?
        } else {
            v.call_method0("auth_event_ids")?.extract()?
        };
        auth_graph_events.insert(
            event_id.clone(),
            LeanEvent {
                event_id,
                event_type: String::new(),
                state_key: None,
                power_level: 0,
                origin_server_ts: 0,
                sender: String::new(),
                content: (),
                prev_events: Vec::new(),
                auth_events: auth_ids,
                depth: 0,
                rejected: false,
                soft_fail: false,
            },
        );
    }
    let auth_graph = AuthGraph::build(&auth_graph_events);

    let mut union: Option<HashSet<String>> = None;
    let mut intersection: HashSet<String> = HashSet::new();

    for state_set in state_sets.try_iter()? {
        let state_set = state_set?;
        let values = state_set.call_method0("values")?;
        let mut state_set_ids = Vec::with_capacity(values.len()?);
        for value in values.try_iter()? {
            state_set_ids.push(value?.extract()?);
        }
        let closure: HashSet<String> = auth_graph
            .auth_difference(&[], &state_set_ids)
            .into_iter()
            .collect();

        match &mut union {
            None => {
                intersection = closure.clone();
                union = Some(closure);
            }
            Some(union) => {
                union.extend(closure.iter().cloned());
                intersection = intersection.intersection(&closure).cloned().collect();
            }
        }
    }

    let Some(union) = union else {
        return PySet::empty(py);
    };

    let result: HashSet<String> = union.difference(&intersection).cloned().collect();
    PySet::new(py, result)
}

fn resolver_data_to_lean_event(data: EventResolverData) -> LeanEvent<String, Value> {
    LeanEvent {
        event_id: data.event_id,
        event_type: data.event_type,
        state_key: data.state_key,
        power_level: 0,
        origin_server_ts: data.origin_server_ts,
        sender: data.sender,
        content: data.content,
        prev_events: data.prev_events,
        auth_events: data.auth_events,
        depth: data.depth,
        rejected: data.rejected,
        soft_fail: data.soft_failed,
    }
}

fn py_to_lean_event(py_ev: &Bound<'_, PyAny>) -> PyResult<LeanEvent<String, Value>> {
    let event_id: String = py_ev.getattr("event_id")?.extract()?;
    let event_type: String = py_ev.getattr("type")?.extract()?;
    let state_key: Option<String> = py_ev.call_method0("get_state_key")?.extract()?;
    let sender: String = py_ev.getattr("sender")?.extract()?;
    let origin_server_ts: u64 = py_ev.getattr("origin_server_ts")?.extract()?;
    let depth: u64 = py_ev.getattr("depth")?.extract()?;

    let prev_events: Vec<String> = py_ev.call_method0("prev_event_ids")?.extract()?;
    let auth_events: Vec<String> = py_ev.call_method0("auth_event_ids")?.extract()?;
    let rejected_reason: Option<String> = py_ev.getattr("rejected_reason")?.extract()?;
    let soft_failed: bool = py_ev
        .getattr("internal_metadata")?
        .call_method0("is_soft_failed")?
        .extract()?;

    let py_content = py_ev.getattr("content")?;
    let content: Value = depythonize(&py_content)?;

    let power_level: i64 = 0;

    Ok(LeanEvent {
        event_id,
        event_type,
        state_key,
        power_level,
        origin_server_ts,
        sender,
        content,
        prev_events,
        auth_events,
        depth,
        rejected: rejected_reason.is_some(),
        soft_fail: soft_failed,
    })
}

#[pyfunction]
#[pyo3(text_signature = "(unconflicted_state, conflicted_event_ids, event_map, /)")]
pub fn resolve_v2_via_lattice_fold<'py>(
    py: Python<'py>,
    unconflicted_state: Bound<'py, PyDict>,
    conflicted_event_ids: Bound<'py, PyAny>,
    event_map: Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let version = StateResVersion::V2;

    let mut unconf_state = SharedState::new();
    for (k, v) in unconflicted_state.iter() {
        let key: (String, String) = k.extract()?;
        let val: String = v.extract()?;
        unconf_state.insert(key, val);
    }

    let conflicted_ids: Vec<String> = conflicted_event_ids.extract()?;

    let mut parsed_events: HashMap<String, LeanEvent<String, Value>> =
        HashMap::with_capacity(event_map.len());
    for (k, v) in event_map.iter() {
        let event_id: String = k.extract()?;
        let lean_ev = if let Ok(event) = v.extract::<PyRef<Event>>() {
            resolver_data_to_lean_event(event.resolver_data()?)
        } else {
            py_to_lean_event(&v)?
        };
        parsed_events.insert(event_id, lean_ev);
    }

    let mut conflicted_events = HashMap::with_capacity(conflicted_ids.len());
    for id in conflicted_ids {
        if let Some(ev) = parsed_events.get(&id) {
            conflicted_events.insert(id.clone(), ev.clone());
        }
    }

    let resolved = resolve_lattice_fold(unconf_state, conflicted_events, &parsed_events, version);

    let py_resolved = PyDict::new(py);
    for ((type_, state_key), event_id) in resolved {
        let py_key = PyTuple::new(py, &[type_, state_key])?;
        py_resolved.set_item(py_key, event_id)?;
    }

    Ok(py_resolved)
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "state_res")?;
    child_module.add_function(wrap_pyfunction!(
        get_auth_chain_difference_from_event_graph,
        &child_module
    )?)?;
    child_module.add_function(wrap_pyfunction!(
        resolve_v2_via_lattice_fold,
        &child_module
    )?)?;
    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.state_res", child_module)?;

    Ok(())
}
