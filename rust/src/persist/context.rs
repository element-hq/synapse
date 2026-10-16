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

//! [`PersistContext`]: what persisting an event needs from its Python
//! `EventContext`.

use std::{collections::HashMap, sync::Arc};

use pyo3::{
    intern,
    prelude::*,
    types::{PyDict, PyTuple},
};

use crate::state::{StateKey, StateMap};

/// The fields of an `EventContext` that persisting reads, converted once
/// when the batch crosses into Rust.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PersistContext {
    /// Why the event was rejected, if it was.
    pub rejected: Option<String>,
    /// The state group after the event. `None` for an outlier. (Python's
    /// `state_group` property raises for a rejected event, so this reads the
    /// field behind it.)
    pub state_group: Option<i64>,
    /// The state group before the event.
    pub state_group_before_event: Option<i64>,
    /// The state the event changes, if it is a state event.
    pub state_delta_due_to_event: Option<StateMap>,
    /// Deltas between pairs of state groups `(prev, current)` known when the
    /// context was built, which can save a state resolution.
    pub state_group_deltas: HashMap<(i64, i64), StateMap>,
    /// Whether the state at the event is partial.
    pub partial_state: bool,
    /// Whether an application service sent the event. Only used to label
    /// the persisted events metric.
    pub app_service: bool,
}

impl<'a, 'py> FromPyObject<'a, 'py> for PersistContext {
    type Error = PyErr;

    fn extract(context: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let py = context.py();

        let state_delta_due_to_event = context.getattr(intern!(py, "_state_delta_due_to_event"))?;
        let state_delta_due_to_event = if state_delta_due_to_event.is_none() {
            None
        } else {
            Some(state_map_from_py(
                state_delta_due_to_event.cast::<PyDict>()?,
            )?)
        };

        let mut state_group_deltas = HashMap::new();
        let deltas = context.getattr(intern!(py, "state_group_deltas"))?;
        for (key, delta) in deltas.cast::<PyDict>()?.iter() {
            let (prev, current): (i64, i64) = key.extract()?;
            state_group_deltas.insert((prev, current), state_map_from_py(delta.cast::<PyDict>()?)?);
        }

        Ok(PersistContext {
            rejected: context.getattr(intern!(py, "rejected"))?.extract()?,
            state_group: context.getattr(intern!(py, "_state_group"))?.extract()?,
            state_group_before_event: context
                .getattr(intern!(py, "state_group_before_event"))?
                .extract()?,
            state_delta_due_to_event,
            state_group_deltas,
            partial_state: context.getattr(intern!(py, "partial_state"))?.extract()?,
            app_service: !context.getattr(intern!(py, "app_service"))?.is_none(),
        })
    }
}

/// Convert a dict of `(type, state_key)` to event ID.
fn state_map_from_py(dict: &Bound<'_, PyDict>) -> PyResult<StateMap> {
    let mut map = StateMap::with_capacity(dict.len());
    for (key, event_id) in dict.iter() {
        let key = key.cast_into::<PyTuple>()?;
        let event_type: String = key.get_item(0)?.extract()?;
        let state_key: String = key.get_item(1)?.extract()?;
        let event_id: String = event_id.extract()?;
        map.insert(
            StateKey(Arc::from(event_type), Arc::from(state_key)),
            Arc::from(event_id),
        );
    }
    Ok(map)
}
