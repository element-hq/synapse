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

//! The types that cross the Python boundary for one room's entry: the
//! request-wide [`SyncRequest`], the per-room [`RoomSyncInput`] and the
//! [`RoomSyncResult`] Python turns into a `JoinedSyncResult` or
//! `ArchivedSyncResult`.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::{
    prelude::*,
    types::{PyDict, PyList, PyTuple},
};
use pythonize::{depythonize, pythonize};
use serde_json::Value;

use crate::events::Event;
use crate::sync::filter::FilterCollection;
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
    /// `get_room_max_token()` when Python dispatched the room.
    pub room_max_token: RoomStreamToken,
}

/// See [`RoomSyncInputData`].
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomSyncInput {
    pub(crate) data: Arc<RoomSyncInputData>,
}

#[pymethods]
impl RoomSyncInput {
    #[new]
    #[pyo3(signature = (*, room_id, rtype, events, newly_joined, full_state, since, upto, end, out_of_band, ephemeral, tags, account_data, sticky_event_ids, always_include, room_max_token))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        room_id: &str,
        rtype: &str,
        events: Option<Vec<Bound<'_, Event>>>,
        newly_joined: bool,
        full_state: bool,
        since: Option<RoomStreamToken>,
        upto: RoomStreamToken,
        end: RoomStreamToken,
        out_of_band: bool,
        ephemeral: Vec<Bound<'_, PyAny>>,
        tags: Option<Bound<'_, PyAny>>,
        account_data: &Bound<'_, PyAny>,
        sticky_event_ids: Vec<String>,
        always_include: bool,
        room_max_token: RoomStreamToken,
    ) -> PyResult<Self> {
        let rtype = match rtype {
            "joined" => RoomType::Joined,
            "archived" => RoomType::Archived,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "Unrecognized rtype: {other:?}"
                )))
            }
        };
        let py = account_data.py();
        let mut account_data_items = Vec::new();
        for item in account_data
            .call_method0(pyo3::intern!(py, "items"))?
            .try_iter()?
        {
            let (account_data_type, content): (String, Bound<'_, PyAny>) = item?.extract()?;
            account_data_items.push((account_data_type, depythonize(&content)?));
        }
        Ok(RoomSyncInput {
            data: Arc::new(RoomSyncInputData {
                room_id: Arc::from(room_id),
                rtype,
                events: events.map(|events| {
                    events
                        .iter()
                        .map(|event| Arc::new(event.get().shallow_clone()))
                        .collect()
                }),
                newly_joined,
                full_state,
                since,
                upto,
                end,
                out_of_band,
                ephemeral: ephemeral
                    .iter()
                    .map(|event| depythonize(event))
                    .collect::<Result<_, _>>()?,
                tags: tags.map(|tags| depythonize(&tags)).transpose()?,
                account_data: account_data_items,
                sticky_event_ids: sticky_event_ids.into_iter().map(Arc::from).collect(),
                always_include,
                room_max_token,
            }),
        })
    }
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

#[pymethods]
impl RoomSyncResult {
    #[getter]
    fn room_id(&self) -> &str {
        &self.data.room_id
    }

    #[getter]
    fn rtype(&self) -> &'static str {
        match self.data.rtype {
            RoomType::Joined => "joined",
            RoomType::Archived => "archived",
        }
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
