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
 */

//! The bundled aggregations attached to an event for client serialization.
//!
//! These mirror the Matrix "server-side aggregation" data (references, edits
//! and thread summaries) that is folded into an event's `unsigned.m.relations`
//! section when serializing for clients. They are built by the Python
//! `RelationsHandler` and consumed by [`serialize_events`](crate::events::serialize::serialize_events).
//!
//! The events they reference ([`Event`]) are stored by value rather than as
//! Python handles; cloning an `Event` is cheap (it shares the underlying data
//! behind `Arc`s) and the events are only ever read here.

use pyo3::{pyclass, pymethods};

use crate::events::{json_object::JsonObject, Event};

/// A thread's bundled summary: its latest event, the number of events in the
/// thread, and whether the requesting user has participated.
#[pyclass(frozen, skip_from_py_object, get_all)]
#[derive(Clone)]
pub struct ThreadAggregation {
    /// The latest event in the thread.
    pub latest_event: Event,
    /// The total number of events in the thread.
    pub count: i64,
    /// Whether the requesting user has sent an event to the thread.
    pub current_user_participated: bool,
}

#[pymethods]
impl ThreadAggregation {
    #[new]
    fn new(latest_event: &Event, count: i64, current_user_participated: bool) -> Self {
        Self {
            latest_event: latest_event.clone(),
            count,
            current_user_participated,
        }
    }
}

/// The bundled aggregations for a single event.
///
/// Some values require additional processing during serialization (the edit
/// and the thread's latest event are themselves serialized).
#[pyclass(frozen, skip_from_py_object, get_all)]
#[derive(Clone)]
pub struct BundledAggregations {
    /// The `m.reference` aggregation (e.g. `{"chunk": [{"event_id": ...}]}`).
    pub references: Option<JsonObject>,
    /// The edit (`m.replace`) event that applies to this event.
    pub replace: Option<Event>,
    /// The thread (`m.thread`) summary for this event.
    pub thread: Option<ThreadAggregation>,
}

#[pymethods]
impl BundledAggregations {
    #[new]
    #[pyo3(signature = (references = None, replace = None, thread = None))]
    fn new(
        references: Option<JsonObject>,
        replace: Option<&Event>,
        thread: Option<&ThreadAggregation>,
    ) -> Self {
        Self {
            references,
            replace: replace.cloned(),
            thread: thread.cloned(),
        }
    }

    /// Whether there are any aggregations to bundle.
    ///
    /// Matches the Python `bool(self.references or self.replace or self.thread)`:
    /// an empty `references` mapping counts as falsey.
    fn __bool__(&self) -> bool {
        self.references.as_ref().is_some_and(|r| !r.is_empty())
            || self.replace.is_some()
            || self.thread.is_some()
    }
}
