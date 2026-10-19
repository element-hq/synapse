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
    types::{PyMapping, PyTuple},
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
    /// Shared, since every event of a chunk carries a copy.
    pub state_group_deltas: Arc<HashMap<(i64, i64), StateMap>>,
    /// Whether the state at the event is partial.
    pub partial_state: bool,
    /// Whether an application service sent the event. Only used to label
    /// the persisted events metric.
    pub app_service: bool,
    /// The event's push actions, as rows of `event_push_actions` without
    /// the event's own columns, when the push rules were evaluated on this
    /// worker. `None` when they are in the staging table, or there are none.
    pub push_actions: Option<Vec<PushActionRow>>,
    /// Set on the create event when the chunk creates the room: the
    /// transaction inserts the `rooms` row, with this visibility, and the
    /// chunk fails with [`super::txn::RoomIdCollision`] if one is there.
    pub new_room_is_public: Option<bool>,
}

/// `(user_id, actions, notif, highlight, unread, thread_id)`: Python's
/// `PushActionRow`.
pub type PushActionRow = (String, String, i16, i16, i16, String);

impl<'a, 'py> FromPyObject<'a, 'py> for PersistContext {
    type Error = PyErr;

    fn extract(context: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let py = context.py();

        let state_delta_due_to_event = context.getattr(intern!(py, "_state_delta_due_to_event"))?;
        let state_delta_due_to_event = if state_delta_due_to_event.is_none() {
            None
        } else {
            Some(state_map_from_py(
                state_delta_due_to_event.cast::<PyMapping>()?,
            )?)
        };

        let mut state_group_deltas = HashMap::new();
        let deltas = context.getattr(intern!(py, "state_group_deltas"))?;
        for item in deltas.cast::<PyMapping>()?.items()?.iter() {
            let (key, delta): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item.extract()?;
            let (prev, current): (i64, i64) = key.extract()?;
            state_group_deltas.insert(
                (prev, current),
                state_map_from_py(delta.cast::<PyMapping>()?)?,
            );
        }

        let push_actions = context.getattr(intern!(py, "push_actions"))?;
        let push_actions = if push_actions.is_none() {
            None
        } else {
            Some(push_actions.extract()?)
        };

        Ok(PersistContext {
            push_actions,
            new_room_is_public: context
                .getattr(intern!(py, "new_room_is_public"))?
                .extract()?,
            rejected: context.getattr(intern!(py, "rejected"))?.extract()?,
            state_group: context.getattr(intern!(py, "_state_group"))?.extract()?,
            state_group_before_event: context
                .getattr(intern!(py, "state_group_before_event"))?
                .extract()?,
            state_delta_due_to_event,
            state_group_deltas: Arc::new(state_group_deltas),
            partial_state: context.getattr(intern!(py, "partial_state"))?.extract()?,
            app_service: !context.getattr(intern!(py, "app_service"))?.is_none(),
        })
    }
}

impl PersistContext {
    /// Whether the event was rejected, as Python's `if context.rejected`
    /// reads it: an empty reason does not count.
    pub fn is_rejected(&self) -> bool {
        self.rejected
            .as_deref()
            .is_some_and(|reason| !reason.is_empty())
    }
}

/// Convert a mapping of `(type, state_key)` to event ID: a dict, or the
/// `immutabledict` of a context that came over replication.
fn state_map_from_py(mapping: &Bound<'_, PyMapping>) -> PyResult<StateMap> {
    let items = mapping.items()?;
    let mut map = StateMap::with_capacity(items.len());
    for item in items.iter() {
        let (key, event_id): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item.extract()?;
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
