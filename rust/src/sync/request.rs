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

//! The types that cross the Python boundary for a room's entry: the
//! request-wide [`SyncRequest`], and the [`RoomSyncResult`] Python turns
//! into a `JoinedSyncResult` or `ArchivedSyncResult`. A room's input,
//! [`RoomSyncInputData`], is built in Rust by [`crate::sync::rooms`].

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyDict, PyList, PyTuple},
};
use pythonize::pythonize;
use serde_json::Value;

use crate::events::Event;
use crate::sync::filter::FilterCollection;
use crate::sync::rooms::{RoomToken, TokenBase};
use crate::types::stream_token::RoomStreamToken;
use crate::visibility::{FilteredEvent, SoftFailedPolicy};

/// What the per-room computation reads about the request, fixed for the
/// whole request.
#[derive(Debug)]
pub struct SyncRequestData {
    pub user_id: Arc<str>,
    pub device_id: Option<Arc<str>>,
    pub filter: FilterCollection,
    pub use_state_after: bool,
    /// The room part of the response's `next_batch`.
    pub now_room_key: RoomStreamToken,
    pub ignored_users: Arc<HashSet<Arc<str>>>,
    /// `Some` for a server admin: what they asked to see of soft-failed
    /// events.
    pub admin_policy: Option<SoftFailedPolicy>,
    /// `push.enable_push`: without it there are no unread counts to read.
    pub enable_push: bool,
    pub msc4354_enabled: bool,
}

/// One `/sync` request, as the per-room computation sees it. Built once by
/// Python in `_generate_sync_entry_for_rooms`.
#[pyclass(frozen, skip_from_py_object)]
pub struct SyncRequest {
    pub(crate) data: Arc<SyncRequestData>,
}

#[pymethods]
impl SyncRequest {
    #[new]
    #[pyo3(signature = (*, user_id, device_id, filter, use_state_after, now_room_key, ignored_users, is_server_admin, return_soft_failed_events, return_policy_server_spammy_events, enable_push, msc4354_enabled))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        user_id: &str,
        device_id: Option<&str>,
        filter: &Bound<'_, FilterCollection>,
        use_state_after: bool,
        now_room_key: RoomStreamToken,
        ignored_users: HashSet<String>,
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        enable_push: bool,
        msc4354_enabled: bool,
    ) -> Self {
        let admin_policy = is_server_admin.then_some(SoftFailedPolicy {
            return_soft_failed_events,
            return_policy_server_spammy_events,
        });
        SyncRequest {
            data: Arc::new(SyncRequestData {
                user_id: Arc::from(user_id),
                device_id: device_id.map(Arc::from),
                filter: filter.get().clone(),
                use_state_after,
                now_room_key,
                ignored_users: Arc::new(ignored_users.into_iter().map(Arc::from).collect()),
                admin_policy,
                enable_push,
                msc4354_enabled,
            }),
        }
    }
}

/// Whether the user is in the room or has left it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoomType {
    Joined,
    Archived,
}

/// The `rtype` of a `RoomSyncResultBuilder`.
pub fn rtype_str(rtype: RoomType) -> &'static str {
    match rtype {
        RoomType::Joined => "joined",
        RoomType::Archived => "archived",
    }
}

/// One room's `RoomSyncResultBuilder`, plus what `handle_room_entries`
/// passes alongside it.
pub struct RoomSyncInputData {
    pub room_id: Arc<str>,
    pub rtype: RoomType,
    /// The events between the since and upto tokens, if Python has them.
    pub events: Option<Vec<Arc<Event>>>,
    pub newly_joined: bool,
    /// The room's `full_state`, the request's, or `newly_joined`.
    pub full_state: bool,
    pub since: Option<RoomStreamToken>,
    pub upto: RoomStreamToken,
    pub end: RoomStreamToken,
    pub out_of_band: bool,
    pub ephemeral: Vec<Value>,
    /// All the room's tags, or `None` if they have not changed.
    pub tags: Option<Value>,
    /// The room's changed account data, in order.
    pub account_data: Vec<(String, Value)>,
    pub sticky_event_ids: Vec<Arc<str>>,
    pub always_include: bool,
}

/// Where the timeline's `prev_batch` token comes from: the room's upto
/// token, or the response's `now_token` when a gap reset the timeline.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PrevBatchBase {
    Upto,
    Now,
}

/// A room's entry: a `JoinedSyncResult` or an `ArchivedSyncResult`, less
/// the bundled aggregations, which Python adds.
pub struct RoomSyncResultData {
    pub room_id: Arc<str>,
    pub rtype: RoomType,
    pub timeline: Vec<FilteredEvent>,
    pub prev_batch_base: PrevBatchBase,
    /// The room key of `prev_batch`, if it differs from the base token's.
    pub prev_batch_room_key: Option<RoomStreamToken>,
    pub limited: bool,
    /// Whether Python should bundle aggregations for the timeline.
    pub bundle_aggregations: bool,
    pub state: Vec<Event>,
    pub ephemeral: Vec<Value>,
    pub account_data: Vec<Value>,
    /// `None` when there is nothing to summarise, an empty object when no
    /// summary was asked for.
    pub summary: Option<Value>,
    pub unread_notifications: HashMap<&'static str, i64>,
    pub unread_thread_notifications: Vec<(Arc<str>, i64, i64)>,
    pub unread_count: i64,
    pub sticky: Vec<FilteredEvent>,
    /// The redactions of the redacted events above, by ID.
    pub redactions: HashMap<Arc<str>, Event>,
    /// The room's upto token. Set once the room is computed.
    pub upto: Option<RoomToken>,
}

/// See [`RoomSyncResultData`]. Each getter builds new Python objects.
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomSyncResult {
    pub(crate) data: RoomSyncResultData,
}

fn filtered_to_py<'py>(py: Python<'py>, events: &[FilteredEvent]) -> PyResult<Bound<'py, PyList>> {
    let items = events
        .iter()
        .map(|filtered| {
            let event = Bound::new(py, filtered.event.shallow_clone())?;
            PyTuple::new(
                py,
                [
                    event.into_any(),
                    filtered.membership.into_pyobject(py)?.into_any(),
                ],
            )
        })
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, items)
}

impl RoomSyncResult {
    fn upto(&self) -> PyResult<&RoomToken> {
        self.data
            .upto
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("the room's upto token is not set"))
    }
}

#[pymethods]
impl RoomSyncResult {
    #[getter]
    fn room_id(&self) -> &str {
        &self.data.room_id
    }

    /// Whether the entry has anything to send, as `JoinedSyncResult` and
    /// `ArchivedSyncResult` define it.
    fn __bool__(&self) -> bool {
        let data = &self.data;
        let base =
            !data.timeline.is_empty() || !data.state.is_empty() || !data.account_data.is_empty();
        match data.rtype {
            RoomType::Joined => base || !data.ephemeral.is_empty() || !data.sticky.is_empty(),
            RoomType::Archived => base,
        }
    }

    #[getter]
    fn rtype(&self) -> &'static str {
        rtype_str(self.data.rtype)
    }

    /// The timeline as `(event, membership)` pairs, oldest first.
    #[getter]
    fn timeline<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        filtered_to_py(py, &self.data.timeline)
    }

    /// Whether `prev_batch` is built on the response's now token rather
    /// than the room's upto token.
    #[getter]
    fn prev_batch_from_now(&self) -> bool {
        self.data.prev_batch_base == PrevBatchBase::Now
    }

    #[getter]
    fn prev_batch_room_key(&self) -> Option<RoomStreamToken> {
        self.data.prev_batch_room_key.clone()
    }

    #[getter]
    fn limited(&self) -> bool {
        self.data.limited
    }

    /// Whether the room's upto token is built on the response's now token
    /// rather than its since token.
    #[getter]
    fn upto_from_now(&self) -> PyResult<bool> {
        Ok(self.upto()?.base == TokenBase::Now)
    }

    /// The room key of the room's upto token.
    #[getter]
    fn upto_room_key(&self) -> PyResult<RoomStreamToken> {
        Ok(self.upto()?.room_key.clone())
    }

    #[getter]
    fn bundle_aggregations(&self) -> bool {
        self.data.bundle_aggregations
    }

    #[getter]
    fn state<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let events = self
            .data
            .state
            .iter()
            .map(|event| Bound::new(py, event.shallow_clone()))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, events)
    }

    #[getter]
    fn ephemeral<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &self.data.ephemeral)?)
    }

    #[getter]
    fn account_data<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &self.data.account_data)?)
    }

    #[getter]
    fn summary<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.data
            .summary
            .as_ref()
            .map(|summary| pythonize(py, summary).map_err(Into::into))
            .transpose()
    }

    #[getter]
    fn unread_notifications<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (key, value) in &self.data.unread_notifications {
            dict.set_item(key, value)?;
        }
        Ok(dict)
    }

    #[getter]
    fn unread_thread_notifications<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (thread_id, notification_count, highlight_count) in
            &self.data.unread_thread_notifications
        {
            let counts = PyDict::new(py);
            counts.set_item("notification_count", notification_count)?;
            counts.set_item("highlight_count", highlight_count)?;
            dict.set_item(&**thread_id, counts)?;
        }
        Ok(dict)
    }

    #[getter]
    fn unread_count(&self) -> i64 {
        self.data.unread_count
    }

    #[getter]
    fn sticky<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        filtered_to_py(py, &self.data.sticky)
    }
}
