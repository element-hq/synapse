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

//! A request's connection state, owned by Rust from load to persist.
//!
//! [`ConnectionState`] holds the state loaded at the request's position
//! and the request's updates to it. Python's extensions read and update it
//! through [`ConnectionStateView`] and [`RoomStatusMapView`], which have
//! the method surface of Python's `PerConnectionState` and
//! `MutablePerConnectionState` as far as the extensions use it.
//!
//! The receipts stream's tokens stay in their database form, a string:
//! Rust never compares them, and the view parses one into a Python
//! `MultiWriterStreamToken` only when Python asks for it.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use anyhow::Context;
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    intern,
    prelude::*,
    sync::PyOnceLock,
    types::{PyDict, PyTuple},
};

use crate::caches::lock_ignoring_poison;
use crate::sliding_sync::config::RoomSyncConfig;
use crate::sliding_sync::connection::{
    HaveSentRoom, MutablePerConnectionState, MutableRoomStatusMap, PerConnectionState,
    RoomLazyMembershipChanges, RoomStatusMap,
};
use crate::sliding_sync::result::required_state_map_from_py;
use crate::sliding_sync::SlidingSyncStats;
use crate::storage::sliding_sync_store::{ConnectionStateUpdates, StoredConnectionState};
use crate::types::stream_token::RoomStreamToken;

/// The `instance_map` table: each writer's instance ID and name.
#[derive(Debug, Clone, Default)]
pub struct InstanceMap {
    by_id: HashMap<i64, Arc<str>>,
    by_name: HashMap<Arc<str>, i64>,
}

impl InstanceMap {
    pub fn new(pairs: impl IntoIterator<Item = (i64, Arc<str>)>) -> Self {
        let mut map = InstanceMap::default();
        for (id, name) in pairs {
            map.by_name.insert(Arc::clone(&name), id);
            map.by_id.insert(id, name);
        }
        map
    }

    /// `get_name_from_instance_id`.
    pub fn name(&self, id: i64) -> Option<Arc<str>> {
        self.by_id.get(&id).cloned()
    }

    /// `get_id_for_instance`, for a writer the table already has.
    pub fn id(&self, name: &str) -> Option<i64> {
        self.by_name.get(name).copied()
    }

    /// Name to ID, as the room computation formats tokens with.
    pub fn ids_by_name(&self) -> HashMap<String, i64> {
        self.by_name
            .iter()
            .map(|(name, id)| (name.to_string(), *id))
            .collect()
    }
}

impl StoredConnectionState {
    /// Whether a stored room token names a writer `instances` lacks.
    pub fn names_unknown_writer(&self, instances: &InstanceMap) -> bool {
        self.rooms.values().any(|status| match status {
            HaveSentRoom::Previously(token) => {
                RoomStreamToken::parse_with_instances(token, |id| instances.name(id)).is_err()
                    && RoomStreamToken::parse_with_instances(token, |_| Some(Arc::from(""))).is_ok()
            }
            HaveSentRoom::Never | HaveSentRoom::Live => false,
        })
    }
}

/// The streams of a connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stream {
    Rooms,
    Receipts,
    AccountData,
}

/// Apply `f` to a status's token.
fn map_status<T, U, E>(
    status: &HaveSentRoom<T>,
    f: impl FnOnce(&T) -> Result<U, E>,
) -> Result<HaveSentRoom<U>, E> {
    Ok(match status {
        HaveSentRoom::Never => HaveSentRoom::Never,
        HaveSentRoom::Live => HaveSentRoom::Live,
        HaveSentRoom::Previously(token) => HaveSentRoom::Previously(f(token)?),
    })
}

fn map_statuses<T, U>(
    statuses: &HashMap<String, HaveSentRoom<T>>,
    f: impl Fn(&T) -> anyhow::Result<U>,
) -> anyhow::Result<HashMap<String, HaveSentRoom<U>>> {
    statuses
        .iter()
        .map(|(room_id, status)| Ok((room_id.clone(), map_status(status, &f)?)))
        .collect()
}

/// A `MultiWriterStreamToken`'s database form as `(stream, [(instance ID,
/// position)])`: the bare stream, or `m{stream}~{id}.{pos}~...`.
fn parse_multi_writer(string: &str) -> anyhow::Result<(i64, Vec<(i64, i64)>)> {
    let invalid = || anyhow::anyhow!("invalid stream token {string:?}");
    if string.starts_with(|c: char| c.is_ascii_digit()) {
        return Ok((string.parse().map_err(|_| invalid())?, Vec::new()));
    }
    let rest = string.strip_prefix('m').ok_or_else(invalid)?;
    let mut parts = rest.split('~');
    let stream = parts
        .next()
        .ok_or_else(invalid)?
        .parse()
        .map_err(|_| invalid())?;
    let mut positions = Vec::new();
    for part in parts {
        // Tokens of the form `m5~` were created by a bug.
        if part.is_empty() {
            continue;
        }
        let (id, position) = part.split_once('.').ok_or_else(invalid)?;
        positions.push((
            id.parse().map_err(|_| invalid())?,
            position.parse().map_err(|_| invalid())?,
        ));
    }
    Ok((stream, positions))
}

/// A Python `MultiWriterStreamToken` from its database form.
fn multi_writer_to_py<'py>(
    py: Python<'py>,
    string: &str,
    instances: &InstanceMap,
) -> PyResult<Bound<'py, PyAny>> {
    let (stream, positions) =
        parse_multi_writer(string).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let instance_map = PyDict::new(py);
    for (id, position) in positions {
        let name = instances
            .name(id)
            .ok_or_else(|| PyValueError::new_err(format!("unknown instance ID {id}")))?;
        instance_map.set_item(&*name, position)?;
    }
    let immutabledict = py
        .import(intern!(py, "immutabledict"))?
        .getattr(intern!(py, "immutabledict"))?
        .call1((instance_map,))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "stream"), stream)?;
    kwargs.set_item(intern!(py, "instance_map"), immutabledict)?;
    py.import(intern!(py, "synapse.types"))?
        .getattr(intern!(py, "MultiWriterStreamToken"))?
        .call((), Some(&kwargs))
}

/// The database form of a Python `MultiWriterStreamToken`, as its
/// `to_string` writes it: writers at or behind `stream` are left out.
fn multi_writer_from_py(token: &Bound<'_, PyAny>, instances: &InstanceMap) -> PyResult<String> {
    let py = token.py();
    let stream: i64 = token.getattr(intern!(py, "stream"))?.extract()?;
    let mut entries = Vec::new();
    for item in token
        .getattr(intern!(py, "instance_map"))?
        .call_method0(intern!(py, "items"))?
        .try_iter()?
    {
        let (name, position): (String, i64) = item?.extract()?;
        if position <= stream {
            continue;
        }
        let id = instances
            .id(&name)
            .ok_or_else(|| PyValueError::new_err(format!("unknown instance name {name}")))?;
        entries.push(format!("{id}.{position}"));
    }
    if entries.is_empty() {
        Ok(stream.to_string())
    } else {
        Ok(format!("m{stream}~{}", entries.join("~")))
    }
}

/// The loaded state and the request's updates.
pub struct ConnectionStateData {
    pub previous: PerConnectionState<String>,
    pub new: MutablePerConnectionState<String>,
}

impl ConnectionStateData {
    /// The state as stored, its tokens parsed. An empty `stored` is the
    /// state of a connection's first request.
    pub fn from_stored(
        stored: &StoredConnectionState,
        instances: &InstanceMap,
    ) -> anyhow::Result<Self> {
        let rooms = map_statuses(&stored.rooms, |token| {
            RoomStreamToken::parse_with_instances(token, |id| instances.name(id))
                .map_err(anyhow::Error::new)
        })?;
        let account_data = map_statuses(&stored.account_data, |token| {
            token
                .parse::<i64>()
                .with_context(|| format!("account data token {token:?}"))
        })?;
        let previous = PerConnectionState {
            last_used_ts: stored.last_used_ts,
            rooms: RoomStatusMap::new(rooms),
            receipts: RoomStatusMap::new(stored.receipts.clone()),
            account_data: RoomStatusMap::new(account_data),
            room_configs: Arc::new(stored.room_configs.clone()),
        };
        let new = previous.get_mutable();
        Ok(ConnectionStateData { previous, new })
    }

    /// The updates to persist, each token in its database form.
    pub fn updates(&self, instances: &InstanceMap) -> anyhow::Result<ConnectionStateUpdates> {
        Ok(ConnectionStateUpdates {
            rooms: map_statuses(self.new.rooms.get_updates(), |token| {
                token
                    .to_string_with_instances(|name| instances.id(name))
                    .map_err(anyhow::Error::new)
            })?,
            receipts: self.new.receipts.get_updates().clone(),
            account_data: map_statuses(self.new.account_data.get_updates(), |token| {
                Ok(token.to_string())
            })?,
            room_configs: self.new.get_room_config_updates().clone(),
            room_lazy_membership: self.new.room_lazy_membership.clone(),
        })
    }
}

/// A request's connection state. Made by
/// `SlidingSyncHandler.load_connection_state`.
#[pyclass(frozen, skip_from_py_object)]
pub struct ConnectionState {
    pub(crate) data: Arc<Mutex<ConnectionStateData>>,
    pub(crate) instances: Arc<InstanceMap>,
    /// What loading the state cost.
    load_stats: SlidingSyncStats,
}

impl ConnectionState {
    pub fn new(
        data: ConnectionStateData,
        instances: InstanceMap,
        load_stats: SlidingSyncStats,
    ) -> Self {
        ConnectionState {
            data: Arc::new(Mutex::new(data)),
            instances: Arc::new(instances),
            load_stats,
        }
    }
}

/// Python's `HaveSentRoom` class, with one `LIVE` and one `NEVER` value. The
/// class is frozen, so those two serve every room. The extensions ask for
/// each room in their lists, so building them per call showed in requests
/// with hundreds of rooms.
struct PyHaveSentRoom {
    class: Py<PyAny>,
    live: Py<PyAny>,
    never: Py<PyAny>,
}

static PY_HAVE_SENT_ROOM: PyOnceLock<PyHaveSentRoom> = PyOnceLock::new();

/// A Python `HaveSentRoom`.
fn have_sent_room_to_py<'py, T>(
    py: Python<'py>,
    status: &HaveSentRoom<T>,
    token_to_py: impl FnOnce(&T) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let cached = PY_HAVE_SENT_ROOM.get_or_try_init(py, || -> PyResult<_> {
        let class = py
            .import(intern!(py, "synapse.types.handlers.sliding_sync"))?
            .getattr(intern!(py, "HaveSentRoom"))?;
        Ok(PyHaveSentRoom {
            live: class.call_method0(intern!(py, "live"))?.unbind(),
            never: class.call_method0(intern!(py, "never"))?.unbind(),
            class: class.unbind(),
        })
    })?;
    match status {
        HaveSentRoom::Never => Ok(cached.never.bind(py).clone()),
        HaveSentRoom::Live => Ok(cached.live.bind(py).clone()),
        HaveSentRoom::Previously(token) => cached
            .class
            .bind(py)
            .call_method1(intern!(py, "previously"), (token_to_py(token)?,)),
    }
}

/// A room config as Python's glue builds a `RoomSyncConfig` from it:
/// `(timeline_limit, {type: {state keys}})`.
type PyRoomConfig = (i64, BTreeMap<String, BTreeSet<String>>);

/// A room's `RoomLazyMembershipChanges` for Python: `(room_id,
/// returned_user_id_to_last_seen_ts_map, invalidated_user_ids)`.
type PyLazyMembership = (String, HashMap<String, Option<i64>>, HashSet<String>);

fn config_to_py(config: &RoomSyncConfig) -> PyRoomConfig {
    (config.timeline_limit, config.required_state_map.clone())
}

#[pymethods]
impl ConnectionState {
    /// What loading the state cost.
    #[getter]
    fn stats(&self) -> SlidingSyncStats {
        self.load_stats
    }

    /// The state as loaded: `PerConnectionState`.
    #[getter(previous)]
    fn previous_view(slf: &Bound<'_, Self>) -> ConnectionStateView {
        ConnectionStateView {
            state: slf.clone().unbind(),
            mutable: false,
        }
    }

    /// The state with the request's updates: `MutablePerConnectionState`.
    #[getter(new)]
    fn new_view(slf: &Bound<'_, Self>) -> ConnectionStateView {
        ConnectionStateView {
            state: slf.clone().unbind(),
            mutable: true,
        }
    }

    /// What Python's `get_room_sync_data` reads of the state for `room_ids`,
    /// for the rooms Rust leaves to Python: `(last_used_ts, {room_id:
    /// (timeline_limit, required_state_map)}, {room_id: HaveSentRoom})`.
    #[allow(clippy::type_complexity)]
    fn python_state_for_rooms<'py>(
        &self,
        py: Python<'py>,
        room_ids: Vec<String>,
    ) -> PyResult<(
        Option<i64>,
        HashMap<String, PyRoomConfig>,
        HashMap<String, Bound<'py, PyAny>>,
    )> {
        // Copy out under the lock, then build the Python objects without it.
        let (last_used_ts, configs, sent) = {
            let data = lock_ignoring_poison(&self.data);
            let previous = &data.previous;
            let mut configs = HashMap::new();
            let mut sent = Vec::new();
            for room_id in room_ids {
                if let Some(config) = previous.room_configs.get(&room_id) {
                    configs.insert(room_id.clone(), config_to_py(config));
                }
                let status = previous.rooms.have_sent_room(&room_id);
                if !status.is_never() {
                    sent.push((room_id, status));
                }
            }
            (previous.last_used_ts, configs, sent)
        };
        let statuses = statuses_to_py(py, sent, |token| token.into_pyobject(py))?
            .into_iter()
            .collect();
        Ok((last_used_ts, configs, statuses))
    }

    /// Record what Python's `get_room_sync_data` changed for the rooms it
    /// computed: `room_configs` maps a room to `(timeline_limit,
    /// required_state_map)`, and `lazy_memberships` a room to
    /// `(returned_user_id_to_last_seen_ts_map, invalidated_user_ids)`.
    fn apply_python_rooms(
        &self,
        room_configs: Vec<(String, i64, Bound<'_, PyAny>)>,
        lazy_memberships: Vec<PyLazyMembership>,
    ) -> PyResult<()> {
        let mut configs = Vec::with_capacity(room_configs.len());
        for (room_id, timeline_limit, map) in room_configs {
            let config = RoomSyncConfig {
                timeline_limit,
                required_state_map: required_state_map_from_py(&map)?,
            };
            configs.push((room_id, config));
        }
        let mut data = lock_ignoring_poison(&self.data);
        for (room_id, config) in configs {
            data.new.set_room_config(&room_id, config);
        }
        for (room_id, returned, invalidated) in lazy_memberships {
            data.new.room_lazy_membership.insert(
                room_id,
                RoomLazyMembershipChanges {
                    returned_user_id_to_last_seen_ts_map: returned,
                    invalidated_user_ids: invalidated,
                },
            );
        }
        Ok(())
    }

    /// The request's room config and lazy membership updates, for tests:
    /// `({room_id: (timeline_limit, required_state_map)}, {room_id:
    /// (returned_user_id_to_last_seen_ts_map, invalidated_user_ids)})`.
    #[allow(clippy::type_complexity)]
    fn updates_for_test(
        &self,
    ) -> (
        HashMap<String, PyRoomConfig>,
        HashMap<String, (HashMap<String, Option<i64>>, HashSet<String>)>,
    ) {
        let data = lock_ignoring_poison(&self.data);
        let configs = data
            .new
            .get_room_config_updates()
            .iter()
            .map(|(room_id, config)| (room_id.clone(), config_to_py(config)))
            .collect();
        let lazy = data
            .new
            .room_lazy_membership
            .iter()
            .map(|(room_id, changes)| {
                (
                    room_id.clone(),
                    (
                        changes.returned_user_id_to_last_seen_ts_map.clone(),
                        changes.invalidated_user_ids.clone(),
                    ),
                )
            })
            .collect();
        (configs, lazy)
    }
}

/// `PerConnectionState` (`mutable` false) or `MutablePerConnectionState`
/// over a [`ConnectionState`], as the extensions read them.
#[pyclass(frozen, skip_from_py_object)]
pub struct ConnectionStateView {
    state: Py<ConnectionState>,
    mutable: bool,
}

impl ConnectionStateView {
    fn stream(&self, py: Python<'_>, stream: Stream) -> RoomStatusMapView {
        RoomStatusMapView {
            state: self.state.clone_ref(py),
            stream,
            mutable: self.mutable,
        }
    }
}

#[pymethods]
impl ConnectionStateView {
    #[getter]
    fn rooms(&self, py: Python<'_>) -> RoomStatusMapView {
        self.stream(py, Stream::Rooms)
    }

    #[getter]
    fn receipts(&self, py: Python<'_>) -> RoomStatusMapView {
        self.stream(py, Stream::Receipts)
    }

    #[getter]
    fn account_data(&self, py: Python<'_>) -> RoomStatusMapView {
        self.stream(py, Stream::AccountData)
    }

    #[getter]
    fn last_used_ts(&self, py: Python<'_>) -> Option<i64> {
        let state = self.state.bind(py).get();
        let data = lock_ignoring_poison(&state.data);
        if self.mutable {
            data.new.last_used_ts
        } else {
            data.previous.last_used_ts
        }
    }
}

/// One stream's `RoomStatusMap` (or `MutableRoomStatusMap`) of a
/// [`ConnectionStateView`].
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomStatusMapView {
    state: Py<ConnectionState>,
    stream: Stream,
    mutable: bool,
}

impl RoomStatusMapView {
    /// The statuses of `room_ids`, or every room's when `None`, as Python
    /// `HaveSentRoom`s. The statuses are copied out first, so the lock is
    /// not held while calling into Python.
    fn statuses<'py>(
        &self,
        py: Python<'py>,
        room_ids: Option<&[String]>,
    ) -> PyResult<Vec<(String, Bound<'py, PyAny>)>> {
        let state = self.state.bind(py).get();
        let instances = &state.instances;
        macro_rules! copy {
            ($stream:ident) => {{
                let data = lock_ignoring_poison(&state.data);
                copy_statuses(
                    &data.previous.$stream,
                    &data.new.$stream,
                    self.mutable,
                    room_ids,
                )
            }};
        }
        match self.stream {
            Stream::Rooms => statuses_to_py(py, copy!(rooms), |token: &RoomStreamToken| {
                token.into_pyobject(py)
            }),
            Stream::Receipts => statuses_to_py(py, copy!(receipts), |token: &String| {
                multi_writer_to_py(py, token, instances)
            }),
            Stream::AccountData => statuses_to_py(py, copy!(account_data), |token: &i64| {
                Ok(token.into_pyobject(py)?.into_any())
            }),
        }
    }

    fn check_mutable(&self) -> PyResult<()> {
        if self.mutable {
            Ok(())
        } else {
            Err(PyRuntimeError::new_err(
                "the previous connection state is read-only",
            ))
        }
    }
}

/// The statuses of `room_ids`, or every room's when `None`, from
/// `previous`, or from `new` when `mutable`.
fn copy_statuses<T: Clone>(
    previous: &RoomStatusMap<T>,
    new: &MutableRoomStatusMap<T>,
    mutable: bool,
    room_ids: Option<&[String]>,
) -> Vec<(String, HaveSentRoom<T>)> {
    match room_ids {
        Some(room_ids) => room_ids
            .iter()
            .map(|room_id| {
                let status = if mutable {
                    new.have_sent_room(room_id)
                } else {
                    previous.have_sent_room(room_id)
                };
                (room_id.clone(), status)
            })
            .collect(),
        None if mutable => new
            .copy()
            .iter()
            .map(|(room_id, status)| (room_id.to_owned(), status.clone()))
            .collect(),
        None => previous
            .iter()
            .map(|(room_id, status)| (room_id.to_owned(), status.clone()))
            .collect(),
    }
}

fn statuses_to_py<'py, T>(
    py: Python<'py>,
    statuses: Vec<(String, HaveSentRoom<T>)>,
    token_to_py: impl Fn(&T) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Vec<(String, Bound<'py, PyAny>)>> {
    statuses
        .into_iter()
        .map(|(room_id, status)| Ok((room_id, have_sent_room_to_py(py, &status, &token_to_py)?)))
        .collect()
}

/// Room IDs from any iterable of strings.
fn room_ids_from_py(room_ids: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    room_ids
        .try_iter()?
        .map(|room_id| room_id?.extract())
        .collect()
}

#[pymethods]
impl RoomStatusMapView {
    fn have_sent_room<'py>(&self, py: Python<'py>, room_id: String) -> PyResult<Bound<'py, PyAny>> {
        let mut statuses = self.statuses(py, Some(&[room_id]))?;
        Ok(statuses.pop().expect("one room asked for").1)
    }

    /// Every room's status, as Python's `_statuses` mapping.
    #[getter]
    fn _statuses<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (room_id, status) in self.statuses(py, None)? {
            dict.set_item(room_id, status)?;
        }
        Ok(dict)
    }

    fn record_sent_rooms(&self, py: Python<'_>, room_ids: &Bound<'_, PyAny>) -> PyResult<()> {
        self.check_mutable()?;
        let room_ids = room_ids_from_py(room_ids)?;
        let state = self.state.bind(py).get();
        let mut data = lock_ignoring_poison(&state.data);
        let room_ids = room_ids.iter().map(String::as_str);
        match self.stream {
            Stream::Rooms => data.new.rooms.record_sent_rooms(room_ids),
            Stream::Receipts => data.new.receipts.record_sent_rooms(room_ids),
            Stream::AccountData => data.new.account_data.record_sent_rooms(room_ids),
        }
        Ok(())
    }

    fn record_unsent_rooms(
        &self,
        py: Python<'_>,
        room_ids: &Bound<'_, PyAny>,
        from_token: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.check_mutable()?;
        let room_ids = room_ids_from_py(room_ids)?;
        let state = self.state.bind(py).get();
        let room_ids = room_ids.iter().map(String::as_str);
        match self.stream {
            Stream::Rooms => {
                let token: RoomStreamToken = from_token.extract()?;
                let mut data = lock_ignoring_poison(&state.data);
                data.new.rooms.record_unsent_rooms(room_ids, &token);
            }
            Stream::Receipts => {
                let token = multi_writer_from_py(from_token, &state.instances)?;
                let mut data = lock_ignoring_poison(&state.data);
                data.new.receipts.record_unsent_rooms(room_ids, &token);
            }
            Stream::AccountData => {
                let token: i64 = from_token.extract()?;
                let mut data = lock_ignoring_poison(&state.data);
                data.new.account_data.record_unsent_rooms(room_ids, &token);
            }
        }
        Ok(())
    }

    /// `get_updates`: the request's changes, as `{room_id: HaveSentRoom}`.
    fn get_updates<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.check_mutable()?;
        let state = self.state.bind(py).get();
        let room_ids: Vec<String> = {
            let data = lock_ignoring_poison(&state.data);
            match self.stream {
                Stream::Rooms => data.new.rooms.get_updates().keys().cloned().collect(),
                Stream::Receipts => data.new.receipts.get_updates().keys().cloned().collect(),
                Stream::AccountData => data
                    .new
                    .account_data
                    .get_updates()
                    .keys()
                    .cloned()
                    .collect(),
            }
        };
        let dict = PyDict::new(py);
        for (room_id, status) in self.statuses(py, Some(&room_ids))? {
            dict.set_item(room_id, status)?;
        }
        Ok(dict)
    }
}

/// The database form of a receipts token from a Python
/// `MultiWriterStreamToken`, and back, through the Rust implementation,
/// with `instances` as `[(instance_id, name)]`. For tests.
#[pyfunction]
fn multi_writer_token_round_trip_for_test<'py>(
    py: Python<'py>,
    token: &Bound<'py, PyAny>,
    instances: Vec<(i64, String)>,
) -> PyResult<Bound<'py, PyTuple>> {
    let instances = InstanceMap::new(
        instances
            .into_iter()
            .map(|(id, name)| (id, Arc::from(name))),
    );
    let string = multi_writer_from_py(token, &instances)?;
    let parsed = multi_writer_to_py(py, &string, &instances)?;
    (string, parsed).into_pyobject(py)
}

pub fn register_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<ConnectionState>()?;
    m.add_class::<ConnectionStateView>()?;
    m.add_class::<RoomStatusMapView>()?;
    m.add_function(wrap_pyfunction!(multi_writer_token_round_trip_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_writers_in_stored_tokens() {
        let stored = |token: &str| StoredConnectionState {
            rooms: HashMap::from([(
                "!a:x".to_owned(),
                HaveSentRoom::Previously(token.to_owned()),
            )]),
            ..Default::default()
        };
        let known = InstanceMap::new([(3, Arc::from("w3"))]);
        let empty = InstanceMap::default();

        assert!(stored("m5~3.6").names_unknown_writer(&empty));
        assert!(!stored("m5~3.6").names_unknown_writer(&known));
        assert!(!stored("s5").names_unknown_writer(&empty));
        // A token that is broken otherwise is not a missing writer.
        assert!(!stored("x5").names_unknown_writer(&empty));
        assert!(!StoredConnectionState::default().names_unknown_writer(&empty));
    }

    #[test]
    fn multi_writer_tokens_parse() {
        assert_eq!(parse_multi_writer("5").unwrap(), (5, vec![]));
        assert_eq!(
            parse_multi_writer("m5~1.7~2.9").unwrap(),
            (5, vec![(1, 7), (2, 9)])
        );
        assert_eq!(parse_multi_writer("m5~").unwrap(), (5, vec![]));
        assert!(parse_multi_writer("s5").is_err());
        assert!(parse_multi_writer("").is_err());
    }

    #[test]
    fn stored_state_round_trips() {
        let instances = InstanceMap::new([(1, Arc::from("w1"))]);
        let stored = StoredConnectionState {
            last_used_ts: Some(3),
            rooms: HashMap::from([
                (
                    "!a".to_owned(),
                    HaveSentRoom::Previously("m5~1.7".to_owned()),
                ),
                ("!b".to_owned(), HaveSentRoom::Live),
            ]),
            receipts: HashMap::from([(
                "!a".to_owned(),
                HaveSentRoom::Previously("m5~1.7".to_owned()),
            )]),
            account_data: HashMap::from([(
                "!a".to_owned(),
                HaveSentRoom::Previously("4".to_owned()),
            )]),
            room_configs: HashMap::new(),
        };
        let mut data = ConnectionStateData::from_stored(&stored, &instances).unwrap();
        let token = match data.previous.rooms.have_sent_room("!a") {
            HaveSentRoom::Previously(token) => token,
            other => panic!("{other:?}"),
        };
        assert_eq!(token.get_stream_pos_for_instance("w1"), 7);
        data.new.rooms.record_unsent_rooms(["!b"], &token);
        data.new.account_data.record_sent_rooms(["!a"]);
        let updates = data.updates(&instances).unwrap();
        assert_eq!(
            updates.rooms,
            HashMap::from([(
                "!b".to_owned(),
                HaveSentRoom::Previously("m5~1.7".to_owned())
            )])
        );
        assert_eq!(
            updates.account_data,
            HashMap::from([("!a".to_owned(), HaveSentRoom::Live)])
        );
    }
}
