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

//! `/messages` in Rust, reading the database through the native Postgres
//! driver.
//!
//! [`MessagesHandler`] is the Python entry point, used by
//! `synapse.handlers.pagination.PaginationHandler.get_messages` when
//! `rust_messages` is on. It computes the page of a room's events the user
//! may see, with their bundled aggregations and the lazy-loaded members.
//! Rust works out the topological position of a live `from` token and
//! clamps it to a departed user's leave. Python keeps the auth checks and
//! the backfill, which [`MessagesResult::needs_backfill`] asks for.
//!
//! The client's event filter goes into the page query
//! ([`crate::storage::event_filter_sql`]) and is then checked on each event
//! ([`Filter::check`]), as in the Python. Filters on relations stay in
//! Python.
//!
//! It shares the v2 sync handler's [`SyncStore`], so the two read through
//! the same caches, and reuses the sync handler's visibility check
//! ([`filter_events_for_client`]) and bundled aggregations
//! ([`bundled_aggregations`]).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use anyhow::Context;
use futures::future::BoxFuture;
use pyo3::{
    create_exception,
    exceptions::{PyException, PyRuntimeError, PyValueError},
    intern,
    prelude::*,
    types::{PyDict, PyList, PyModule},
};
use serde_json::Value;
use tracing::Instrument;

use crate::database::postgres::pool::{ConnectionPool, PyConnectionPool};
use crate::deferred::{create_deferred_with, OnCancel};
use crate::events::constants::event_type::M_ROOM_MEMBER;
use crate::events::Event;
use crate::runtime::RustRuntime;
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::{anyhow_to_py, PaginationOrder, SyncReader, SyncStore};
use crate::sync::client_events::{filter_events_for_client, ClientFilterContext};
use crate::sync::filter::{Filter, FilterFlags};
use crate::sync::relations::{bundled_aggregations, Bundle};
use crate::sync::request::{bundles_to_py, filtered_to_py};
use crate::sync::room::Fallback;
use crate::types::state_filter::StateFilter;
use crate::types::stream_token::{Direction, RoomStreamToken};
use crate::visibility::{FilteredEvent, SoftFailedPolicy};

create_exception!(
    synapse.synapse_rust.messages,
    RustMessagesFallback,
    PyException,
    "The request is one Rust leaves to Python."
);

/// `BACKFILL_BECAUSE_TOO_MANY_GAPS_THRESHOLD` of
/// `synapse.handlers.pagination`: how many single-event gaps in a page are
/// tolerated before backfilling.
const BACKFILL_BECAUSE_TOO_MANY_GAPS_THRESHOLD: usize = 3;

/// One `/messages` request, after Python's auth checks and token handling.
#[derive(Debug, Clone)]
pub struct MessagesRequestData {
    pub room_id: Arc<str>,
    /// The `from` token, as the client sent it.
    pub from_key: RoomStreamToken,
    pub to_key: Option<RoomStreamToken>,
    /// The token of the user's leave, for a user who left, going
    /// backwards: the page starts there if it is before `from_key`.
    pub leave_key: Option<RoomStreamToken>,
    /// The topological position of `from_key`, when an earlier read of
    /// the same request worked it out.
    pub curr_topo: Option<i64>,
    pub direction: Direction,
    pub limit: i64,
    /// Whether the user is not in the room and was not when the events
    /// were sent: a world-readable room.
    pub is_peeking: bool,
    pub lazy_load_members: bool,
    /// The request's event filter, if it has one. It has no
    /// `related_by_senders` or `related_by_rel_types`: Python serves those.
    pub event_filter: Option<Filter>,
    pub client_filter: ClientFilterContext,
}

/// See [`MessagesRequestData`]. Built by Python in `get_messages`.
#[pyclass(frozen, skip_from_py_object)]
pub struct MessagesRequest {
    data: Arc<MessagesRequestData>,
}

#[pymethods]
impl MessagesRequest {
    #[new]
    #[pyo3(signature = (*, room_id, user_id, from_key, to_key, leave_key, direction, limit, is_peeking, lazy_load_members, event_filter, ignored_users, is_server_admin, return_soft_failed_events, return_policy_server_spammy_events, msc3874_enabled, msc4354_enabled))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        room_id: &str,
        user_id: &str,
        from_key: RoomStreamToken,
        to_key: Option<RoomStreamToken>,
        leave_key: Option<RoomStreamToken>,
        direction: &str,
        limit: i64,
        is_peeking: bool,
        lazy_load_members: bool,
        event_filter: Option<&Bound<'_, PyAny>>,
        ignored_users: HashSet<String>,
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        msc3874_enabled: bool,
        msc4354_enabled: bool,
    ) -> PyResult<Self> {
        let event_filter = event_filter
            .map(|json| -> PyResult<Filter> {
                // A filter Rust cannot read, such as one with an integer
                // out of range anywhere in it, is left to Python.
                let json: Value = pythonize::depythonize(json).map_err(|e| {
                    RustMessagesFallback::new_err(format!("unreadable filter: {e}"))
                })?;
                let flags = FilterFlags {
                    msc3874_enabled,
                    ..FilterFlags::default()
                };
                Ok(Filter::new(Some(&json), flags))
            })
            .transpose()?;
        if event_filter
            .as_ref()
            .is_some_and(Filter::needs_relation_check)
        {
            return Err(PyValueError::new_err(
                "filters on relations are not supported",
            ));
        }
        let direction = Direction::parse(direction)
            .ok_or_else(|| PyValueError::new_err(format!("unknown direction {direction:?}")))?;
        let admin_policy = is_server_admin.then_some(SoftFailedPolicy {
            return_soft_failed_events,
            return_policy_server_spammy_events,
        });
        Ok(MessagesRequest {
            data: Arc::new(MessagesRequestData {
                room_id: Arc::from(room_id),
                from_key,
                to_key,
                leave_key,
                curr_topo: None,
                direction,
                limit,
                is_peeking,
                lazy_load_members,
                event_filter,
                client_filter: ClientFilterContext {
                    user_id: Arc::from(user_id),
                    ignored_users: Arc::new(ignored_users.into_iter().map(Arc::from).collect()),
                    admin_policy,
                    msc4354_enabled,
                },
            }),
        })
    }

    /// This request with the topological position of its `from` token
    /// known, as a read of it returned it, so that reading the page again
    /// does not work it out again.
    fn with_curr_topo(&self, curr_topo: i64) -> Self {
        let mut data = (*self.data).clone();
        data.curr_topo = Some(curr_topo);
        MessagesRequest {
            data: Arc::new(data),
        }
    }
}

/// A computed page: what `get_messages` builds its `GetMessagesResult`
/// from.
#[derive(Default)]
pub struct MessagesResultData {
    /// The events the user may see, in the order of the page.
    pub events: Vec<FilteredEvent>,
    pub bundles: HashMap<Arc<str>, Bundle>,
    /// The lazy-loaded members, when asked for and there are any.
    pub state: Option<Vec<Event>>,
    /// The room key of the response's `start` token: the request's, or the
    /// user's leave.
    pub from_key: RoomStreamToken,
    /// Going backwards, the topological position of the `from` token as
    /// the client sent it, which backfill starts from.
    pub curr_topo: Option<i64>,
    /// The room key of the response's `end` token.
    pub next_key: RoomStreamToken,
    /// Whether the page is empty and there is nothing after it, in which
    /// case the response has no `end` token.
    pub end_of_history: bool,
    /// Whether the page, going backwards, has gaps or is short enough that
    /// Python should backfill before answering.
    pub needs_backfill: bool,
}

/// See [`MessagesResultData`]. Each getter builds new Python objects.
#[pyclass(frozen, skip_from_py_object)]
pub struct MessagesResult {
    data: MessagesResultData,
}

#[pymethods]
impl MessagesResult {
    /// The events as `(event, membership)` pairs, in the order of the page.
    #[getter]
    fn events<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        filtered_to_py(py, &self.data.events)
    }

    /// The bundled aggregations of the events, by event ID.
    #[getter]
    fn bundled_aggregations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        bundles_to_py(py, &self.data.bundles)
    }

    #[getter]
    fn state<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyList>>> {
        self.data
            .state
            .as_ref()
            .map(|events| {
                let events = events
                    .iter()
                    .map(|event| Bound::new(py, event.shallow_clone()))
                    .collect::<PyResult<Vec<_>>>()?;
                PyList::new(py, events)
            })
            .transpose()
    }

    /// The room key of the response's `start` token.
    #[getter]
    fn start_key(&self) -> RoomStreamToken {
        self.data.from_key.clone()
    }

    #[getter]
    fn curr_topo(&self) -> Option<i64> {
        self.data.curr_topo
    }

    #[getter]
    fn next_key(&self) -> RoomStreamToken {
        self.data.next_key.clone()
    }

    #[getter]
    fn end_of_history(&self) -> bool {
        self.data.end_of_history
    }

    #[getter]
    fn needs_backfill(&self) -> bool {
        self.data.needs_backfill
    }
}

/// The backfill check of `get_messages` over the depths of a backwards
/// page: whether it has a gap of more than one event, too many single-event
/// gaps, or fewer events than asked for.
fn page_needs_backfill(events: &[Event], limit: i64) -> bool {
    depths_need_backfill(events.iter().map(Event::depth).collect(), limit)
}

/// [`page_needs_backfill`] over the depths of the page's events.
fn depths_need_backfill(mut depths: Vec<i64>, limit: i64) -> bool {
    let count = depths.len() as i64;
    depths.sort_unstable();
    depths.dedup();

    let mut number_of_gaps = 0;
    let mut found_big_gap = false;
    let mut previous = depths.first().copied().unwrap_or(0);
    for depth in depths {
        let gap = (depth - previous).abs();
        if gap > 1 {
            number_of_gaps += 1;
        }
        if gap > 2 {
            found_big_gap = true;
            break;
        }
        previous = depth;
    }
    found_big_gap || number_of_gaps > BACKFILL_BECAUSE_TOO_MANY_GAPS_THRESHOLD || count < limit
}

/// The body of `get_messages` after the token handling: the page, the
/// visibility filter, the lazy-loaded members and the bundled
/// aggregations.
///
/// Fails with [`Fallback`] for what Python computes instead: a room with
/// partial state, or a thread whose latest reply is from an ignored user.
pub async fn get_messages(
    reader: &SyncReader<'_>,
    request: &MessagesRequestData,
    now_ms: i64,
) -> anyhow::Result<MessagesResultData> {
    let room_id = &*request.room_id;
    // The Python reads the state with `await_full_state`.
    if reader.is_partial_state_room(room_id).await? {
        return Err(Fallback(format!("{room_id} has partial state")).into());
    }

    // Going backwards, backfill needs the topological position of the
    // `from` token, and a user who left pages from their leave if it is
    // before that. As the Python, a topological part of 0 counts as none.
    let mut from_key = request.from_key.clone();
    let mut curr_topo = None;
    if request.direction == Direction::Backwards {
        let topological = match (request.curr_topo, from_key.topological) {
            (Some(known), _) => known,
            (None, Some(topological)) if topological != 0 => topological,
            _ => {
                reader
                    .current_topological_token(room_id, from_key.stream)
                    .instrument(tracing::info_span!("messages.topological_token"))
                    .await?
            }
        };
        curr_topo = Some(topological);
        if let Some(leave_key) = &request.leave_key {
            let leave_topological = leave_key
                .topological
                .context("the leave token has no topological part")?;
            if leave_topological < topological {
                from_key = leave_key.clone();
            }
        }
    }

    let event_filter = request.event_filter.as_ref();
    let (events, next_key, limited) = reader
        .paginate_room_events_filtered(
            room_id,
            &from_key,
            request.to_key.as_ref(),
            request.direction,
            request.limit,
            PaginationOrder::Topological,
            event_filter,
        )
        .instrument(tracing::info_span!("messages.page"))
        .await?;

    let needs_backfill =
        request.direction == Direction::Backwards && page_needs_backfill(&events, request.limit);

    let mut result = MessagesResultData {
        from_key,
        curr_topo,
        next_key,
        needs_backfill,
        ..MessagesResultData::default()
    };
    if !limited && events.is_empty() {
        result.end_of_history = true;
        return Ok(result);
    }

    // The SQL does not apply every field of the filter, so each event is
    // checked against it, as the Python does. Where the Python check raises,
    // Python answers.
    if let Some(filter) = event_filter {
        if let Some(event) = events.iter().find(|event| filter.check_raises(*event)) {
            return Err(Fallback(format!(
                "the filter check raises on the labels of {}",
                event.event_id()
            ))
            .into());
        }
    }
    let events: Vec<Arc<Event>> = events
        .into_iter()
        .filter(|event| event_filter.is_none_or(|filter| filter.check(event)))
        .map(Arc::new)
        .collect();
    let filtered = filter_events_for_client(
        reader,
        &request.client_filter,
        &events,
        &HashSet::new(),
        request.is_peeking,
        now_ms,
    )
    .instrument(tracing::info_span!("messages.filter"))
    .await?;
    if filtered.is_empty() {
        return Ok(result);
    }

    if request.lazy_load_members {
        result.state = lazy_loaded_members(reader, &filtered)
            .instrument(tracing::info_span!("messages.state"))
            .await?;
    }

    let context = &request.client_filter;
    result.bundles =
        bundled_aggregations(reader, &filtered, &context.user_id, &context.ignored_users)
            .instrument(tracing::info_span!("messages.aggregations"))
            .await?;
    result.events = filtered;
    Ok(result)
}

/// The memberships of the senders of `filtered` at its first event, or
/// `None` if there are none.
async fn lazy_loaded_members(
    reader: &SyncReader<'_>,
    filtered: &[FilteredEvent],
) -> anyhow::Result<Option<Vec<Event>>> {
    let Some(first) = filtered.first() else {
        return Ok(None);
    };
    let state_filter = StateFilter::from_types(
        filtered
            .iter()
            .map(|filtered| (M_ROOM_MEMBER, Some(filtered.event.sender()))),
    );
    let state_ids = reader
        .state_ids_for_event(first.event.event_id(), &state_filter)
        .await?;
    if state_ids.is_empty() {
        return Ok(None);
    }
    let mut event_ids: Vec<Arc<str>> = state_ids.values().cloned().collect();
    event_ids.sort_unstable();
    let events = reader
        .events(&event_ids, RedactBehaviour::Redact, false, false)
        .await?;
    Ok(Some(events))
}

/// Computes `/messages` pages with the Rust implementation, reading the
/// database through the native Postgres driver.
#[pyclass(frozen)]
pub struct MessagesHandler {
    /// The Python `RustConnectionPool`. Its `_pool` is read per call, as
    /// `close()`/`start()` replace it.
    db_pool: Py<PyAny>,
    runtime: RustRuntime,
    /// The queries, over the Rust store's caches. The v2 sync handler's.
    store: Arc<SyncStore>,
}

impl MessagesHandler {
    pub fn new(db_pool: Py<PyAny>, runtime: RustRuntime, store: Arc<SyncStore>) -> Self {
        MessagesHandler {
            db_pool,
            runtime,
            store,
        }
    }

    /// The connection pool to read through, or an error if it is closed.
    fn pool(&self, py: Python<'_>) -> PyResult<ConnectionPool> {
        let pool = self.db_pool.bind(py).getattr(intern!(py, "_pool"))?;
        if pool.is_none() {
            return Err(PyRuntimeError::new_err("the database pool is closed"));
        }
        Ok(pool.cast::<PyConnectionPool>()?.get().pool.clone())
    }

    fn start_get_messages(
        &self,
        py: Python<'_>,
        request: &Bound<'_, MessagesRequest>,
    ) -> PyResult<BoxFuture<'static, PyResult<MessagesResult>>> {
        let pool = self.pool(py)?;
        let request = Arc::clone(&request.get().data);
        let store = Arc::clone(&self.store);
        let now_ms = self.runtime.clock().now_millis() as i64;
        Ok(Box::pin(async move {
            let reader = store.reader(pool);
            let span = tracing::info_span!(
                "messages.get",
                room_id = &*request.room_id,
                limit = request.limit
            );
            let data = get_messages(&reader, &request, now_ms)
                .instrument(span)
                .await
                .map_err(error_to_py)?;
            Ok(MessagesResult { data })
        }))
    }
}

/// An error for Python: `RustMessagesFallback` for what Rust leaves to
/// Python.
fn error_to_py(e: anyhow::Error) -> PyErr {
    if let Some(fallback) = e.downcast_ref::<Fallback>() {
        return RustMessagesFallback::new_err(fallback.to_string());
    }
    anyhow_to_py(e)
}

#[pymethods]
impl MessagesHandler {
    /// The page of `request`: a deferred resolving to a `MessagesResult`.
    /// Fails with `RustMessagesFallback` if Python has to compute it.
    /// Cancelling the deferred stops the computation.
    fn get_messages<'py>(
        &self,
        py: Python<'py>,
        request: &Bound<'py, MessagesRequest>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let future = self.start_get_messages(py, request)?;
        create_deferred_with(py, &self.runtime, future, OnCancel::Abort)
    }

    /// As [`Self::get_messages`], but blocks. For tests.
    fn get_messages_blocking(
        &self,
        py: Python<'_>,
        request: &Bound<'_, MessagesRequest>,
    ) -> PyResult<MessagesResult> {
        let future = self.start_get_messages(py, request)?;
        self.runtime.block_on_scoped(py, future)?
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "messages")?;
    child_module.add_class::<MessagesHandler>()?;
    child_module.add_class::<MessagesRequest>()?;
    child_module.add_class::<MessagesResult>()?;
    child_module.add(
        "RustMessagesFallback",
        py.get_type::<RustMessagesFallback>(),
    )?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import messages` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.messages", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backfill_check() {
        // A full, continuous page.
        assert!(!depths_need_backfill(vec![4, 3, 2, 1], 4));
        // Short.
        assert!(depths_need_backfill(vec![3, 2, 1], 4));
        // Several events at one depth count as one depth, but as two
        // events.
        assert!(!depths_need_backfill(vec![3, 2, 2, 1], 4));
        // A two-event gap.
        assert!(depths_need_backfill(vec![6, 5, 4, 1], 4));
        // Up to three single-event gaps are tolerated.
        assert!(!depths_need_backfill(vec![8, 7, 5, 3, 1], 5));
        assert!(depths_need_backfill(vec![9, 7, 5, 3, 1], 5));
        // An empty page is short.
        assert!(depths_need_backfill(vec![], 1));
    }
}
