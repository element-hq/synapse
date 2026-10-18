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

//! [`PersistEffects`]: the Python cache updates a persist transaction calls
//! for once it commits.
//!
//! The Python transaction queues them with `txn.call_after`. The Rust one
//! collects them in this value instead, and Python applies it in one call to
//! `apply_rust_persist_effects`, so Rust never calls into Python
//! per event. On failure nothing is applied, as `call_after` callbacks do not
//! run then.
//!
//! The chunk's stream IDs come back unfinished, and Python marks them
//! finished only after the cache updates. Python's `get_next_mult` context
//! manager has the same order: the `call_after` callbacks run at commit,
//! before the context exits and finishes the IDs. So `get_current_token`
//! never passes an event that `_events_stream_cache` and the other stream
//! change caches have not been told about, and `/sync` cannot skip it. On
//! failure Rust finishes the IDs itself, as nothing is applied then.
//!
//! What other workers need, the `cache_invalidation_stream_by_instance`
//! rows, is written inside the transaction, not here.

use pyo3::prelude::*;

use crate::database::postgres::write::TxnTimings;

/// A stream change cache that a persist moves an entity forward in.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamChangeCache {
    /// `_events_stream_cache`, keyed by room.
    Events,
    /// `_curr_state_delta_stream_cache`, keyed by room.
    CurrentStateDeltas,
    /// `_membership_stream_cache`, keyed by user.
    Membership,
}

impl StreamChangeCache {
    /// The name `apply_rust_persist_effects` knows it by.
    fn name(self) -> &'static str {
        match self {
            StreamChangeCache::Events => "events",
            StreamChangeCache::CurrentStateDeltas => "current_state_deltas",
            StreamChangeCache::Membership => "membership",
        }
    }

    fn from_name(name: &str) -> PyResult<Self> {
        match name {
            "events" => Ok(StreamChangeCache::Events),
            "current_state_deltas" => Ok(StreamChangeCache::CurrentStateDeltas),
            "membership" => Ok(StreamChangeCache::Membership),
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "unknown stream change cache {name:?}"
            ))),
        }
    }
}

/// The arguments of one `_invalidate_caches_for_event` call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EventInvalidation {
    pub stream_ordering: i64,
    pub event_id: String,
    pub room_id: String,
    pub event_type: String,
    pub state_key: Option<String>,
    pub redacts: Option<String>,
    pub relates_to: Option<String>,
    pub backfilled: bool,
}

type EventInvalidationTuple = (
    i64,
    String,
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    bool,
);

impl From<EventInvalidationTuple> for EventInvalidation {
    fn from(t: EventInvalidationTuple) -> Self {
        EventInvalidation {
            stream_ordering: t.0,
            event_id: t.1,
            room_id: t.2,
            event_type: t.3,
            state_key: t.4,
            redacts: t.5,
            relates_to: t.6,
            backfilled: t.7,
        }
    }
}

/// See the module docs. Built up in Rust with the `add_*` methods, read in
/// Python through the getters, each of which matches one kind of
/// `call_after` callback.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PersistEffects {
    /// `_invalidate_caches_for_event(*args)`, per event persisted.
    events: Vec<EventInvalidation>,
    /// `_invalidate_local_get_event_cache(event_id)`: the events persisted
    /// and the events they redact.
    event_cache_invalidations: Vec<String>,
    /// `_invalidate_state_caches(room_id, members_changed)`.
    state_caches: Vec<(String, Vec<String>)>,
    /// `cache.invalidate(keys)` for the named cache: the local half of
    /// `_invalidate_cache_and_stream`.
    cache_invalidations: Vec<(String, Vec<String>)>,
    /// The IDs of the cache stream rows the transaction wrote, to mark
    /// finished once the caches are invalidated, then
    /// `ReplicationNotifier.notify_replication()`.
    cache_stream_ids: Vec<i64>,
    /// The IDs of the sticky events stream rows the transaction wrote or
    /// moved, to mark finished once the caches are invalidated, as
    /// `get_next_mult_txn` does, with `stream_positions` updated after.
    sticky_stream_ids: Vec<i64>,
    /// The IDs of the profile updates stream rows the transaction wrote,
    /// finished as `sticky_stream_ids` are.
    profile_stream_ids: Vec<i64>,
    /// The events to put in `_get_event_cache`, by `_add_to_cache`'s
    /// `local_prefill`. Python holds the events.
    event_cache_prefills: Vec<String>,
    /// `_invalidate_local_get_event_cache_room_id(room_id)`, after the
    /// prefills: the room's events, once MSC4293 starts redacting a user's.
    event_cache_room_invalidations: Vec<String>,
    /// `_get_state_group_for_event.prefill((event_id,), state_group)`.
    state_group_prefills: Vec<(String, i64)>,
    /// `get_latest_event_ids_in_room.prefill((room_id,), extremities)`.
    latest_event_ids: Option<(String, Vec<String>)>,
    /// `entity_has_changed(entity, stream_id)` on a stream change cache.
    stream_changes: Vec<(StreamChangeCache, String, i64)>,
    /// The chunk's stream IDs, to mark finished after everything else here,
    /// then `ReplicationNotifier.notify_replication()`. As the ID generator
    /// tracks them, so positive on the backfill stream too.
    stream_ids: Vec<i64>,
    /// Whether `stream_ids` are on the backfill stream.
    backfilled: bool,
    /// Rows were written to the cache stream:
    /// `Notifier.on_new_replication_data()`.
    new_replication_data: bool,
    /// What the write interactions behind these effects took, as `(name,
    /// nanoseconds)`, for Python to record as it records its own. Whole
    /// nanoseconds, as a float would cost the struct its `Eq`.
    txn_timings: Vec<(String, u64)>,
}

impl PersistEffects {
    pub fn add_event(&mut self, invalidation: EventInvalidation) {
        self.events.push(invalidation);
    }

    pub fn add_event_cache_invalidation(&mut self, event_id: String) {
        self.event_cache_invalidations.push(event_id);
    }

    pub fn add_state_caches(&mut self, room_id: String, members_changed: Vec<String>) {
        self.state_caches.push((room_id, members_changed));
    }

    pub fn add_cache_invalidation(&mut self, cache_name: &str, keys: Vec<String>) {
        self.cache_invalidations.push((cache_name.to_owned(), keys));
    }

    pub fn add_cache_stream_id(&mut self, stream_id: i64) {
        self.cache_stream_ids.push(stream_id);
    }

    pub fn cache_stream_ids(&self) -> &[i64] {
        &self.cache_stream_ids
    }

    pub fn add_sticky_stream_ids(&mut self, ids: &[i64]) {
        self.sticky_stream_ids.extend_from_slice(ids);
    }

    pub fn sticky_stream_ids(&self) -> &[i64] {
        &self.sticky_stream_ids
    }

    pub fn add_profile_stream_ids(&mut self, ids: &[i64]) {
        self.profile_stream_ids.extend_from_slice(ids);
    }

    pub fn profile_stream_ids(&self) -> &[i64] {
        &self.profile_stream_ids
    }

    pub fn add_event_cache_prefill(&mut self, event_id: String) {
        self.event_cache_prefills.push(event_id);
    }

    pub fn add_event_cache_room_invalidation(&mut self, room_id: String) {
        if !self.event_cache_room_invalidations.contains(&room_id) {
            self.event_cache_room_invalidations.push(room_id);
        }
    }

    pub fn add_state_group_prefill(&mut self, event_id: String, state_group: i64) {
        self.state_group_prefills.push((event_id, state_group));
    }

    pub fn set_latest_event_ids(&mut self, room_id: String, extremities: Vec<String>) {
        self.latest_event_ids = Some((room_id, extremities));
    }

    pub fn add_stream_change(&mut self, cache: StreamChangeCache, entity: String, stream_id: i64) {
        self.stream_changes.push((cache, entity, stream_id));
    }

    pub fn add_stream_ids(&mut self, ids: impl IntoIterator<Item = i64>, backfilled: bool) {
        self.stream_ids.extend(ids);
        self.backfilled = backfilled;
    }

    pub fn set_new_replication_data(&mut self) {
        self.new_replication_data = true;
    }

    /// Add what write interactions took, from
    /// [`crate::database::postgres::write::with_txn_timings`].
    pub fn add_txn_timings(&mut self, timings: TxnTimings) {
        self.txn_timings.extend(
            timings
                .into_iter()
                .map(|(name, secs)| (name, (secs * 1e9) as u64)),
        );
    }

    /// Start again, for a transaction that is retried.
    pub fn clear(&mut self) {
        *self = PersistEffects::default();
    }
}

#[pymethods]
impl PersistEffects {
    /// Build effects from Python, for tests.
    #[new]
    #[pyo3(signature = (
        *,
        events = Vec::new(),
        event_cache_invalidations = Vec::new(),
        state_caches = Vec::new(),
        cache_invalidations = Vec::new(),
        cache_stream_ids = Vec::new(),
        sticky_stream_ids = Vec::new(),
        profile_stream_ids = Vec::new(),
        event_cache_prefills = Vec::new(),
        event_cache_room_invalidations = Vec::new(),
        state_group_prefills = Vec::new(),
        latest_event_ids = None,
        stream_changes = Vec::new(),
        stream_ids = Vec::new(),
        backfilled = false,
        new_replication_data = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        events: Vec<EventInvalidationTuple>,
        event_cache_invalidations: Vec<String>,
        state_caches: Vec<(String, Vec<String>)>,
        cache_invalidations: Vec<(String, Vec<String>)>,
        cache_stream_ids: Vec<i64>,
        sticky_stream_ids: Vec<i64>,
        profile_stream_ids: Vec<i64>,
        event_cache_prefills: Vec<String>,
        event_cache_room_invalidations: Vec<String>,
        state_group_prefills: Vec<(String, i64)>,
        latest_event_ids: Option<(String, Vec<String>)>,
        stream_changes: Vec<(String, String, i64)>,
        stream_ids: Vec<i64>,
        backfilled: bool,
        new_replication_data: bool,
    ) -> PyResult<Self> {
        Ok(PersistEffects {
            events: events.into_iter().map(EventInvalidation::from).collect(),
            event_cache_invalidations,
            state_caches,
            cache_invalidations,
            cache_stream_ids,
            sticky_stream_ids,
            profile_stream_ids,
            event_cache_prefills,
            event_cache_room_invalidations,
            state_group_prefills,
            latest_event_ids,
            stream_changes: stream_changes
                .into_iter()
                .map(|(cache, entity, stream_id)| {
                    Ok((StreamChangeCache::from_name(&cache)?, entity, stream_id))
                })
                .collect::<PyResult<_>>()?,
            stream_ids,
            backfilled,
            new_replication_data,
            txn_timings: Vec::new(),
        })
    }

    #[getter]
    fn events(&self) -> Vec<EventInvalidationTuple> {
        self.events
            .iter()
            .map(|e| {
                (
                    e.stream_ordering,
                    e.event_id.clone(),
                    e.room_id.clone(),
                    e.event_type.clone(),
                    e.state_key.clone(),
                    e.redacts.clone(),
                    e.relates_to.clone(),
                    e.backfilled,
                )
            })
            .collect()
    }

    #[getter]
    fn event_cache_invalidations(&self) -> Vec<String> {
        self.event_cache_invalidations.clone()
    }

    #[getter]
    fn state_caches(&self) -> Vec<(String, Vec<String>)> {
        self.state_caches.clone()
    }

    #[getter]
    fn cache_invalidations(&self) -> Vec<(String, Vec<String>)> {
        self.cache_invalidations.clone()
    }

    #[getter]
    #[pyo3(name = "cache_stream_ids")]
    fn py_cache_stream_ids(&self) -> Vec<i64> {
        self.cache_stream_ids.clone()
    }

    #[getter]
    #[pyo3(name = "sticky_stream_ids")]
    fn py_sticky_stream_ids(&self) -> Vec<i64> {
        self.sticky_stream_ids.clone()
    }

    #[getter]
    #[pyo3(name = "profile_stream_ids")]
    fn py_profile_stream_ids(&self) -> Vec<i64> {
        self.profile_stream_ids.clone()
    }

    #[getter]
    fn event_cache_prefills(&self) -> Vec<String> {
        self.event_cache_prefills.clone()
    }

    #[getter]
    fn event_cache_room_invalidations(&self) -> Vec<String> {
        self.event_cache_room_invalidations.clone()
    }

    #[getter]
    fn state_group_prefills(&self) -> Vec<(String, i64)> {
        self.state_group_prefills.clone()
    }

    #[getter]
    fn latest_event_ids(&self) -> Option<(String, Vec<String>)> {
        self.latest_event_ids.clone()
    }

    #[getter]
    fn stream_changes(&self) -> Vec<(&'static str, String, i64)> {
        self.stream_changes
            .iter()
            .map(|(cache, entity, stream_id)| (cache.name(), entity.clone(), *stream_id))
            .collect()
    }

    #[getter]
    fn stream_ids(&self) -> Vec<i64> {
        self.stream_ids.clone()
    }

    #[getter]
    fn backfilled(&self) -> bool {
        self.backfilled
    }

    #[getter]
    fn new_replication_data(&self) -> bool {
        self.new_replication_data
    }

    /// `(name, seconds)` of each write interaction behind these effects.
    #[getter]
    fn txn_timings(&self) -> TxnTimings {
        self.txn_timings
            .iter()
            .map(|(name, nanos)| (name.clone(), *nanos as f64 / 1e9))
            .collect()
    }

    fn __repr__(&self) -> String {
        format!("{self:?}")
    }
}
