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

//! The per-connection state: what a sliding sync connection has sent down
//! before (`synapse.types.handlers.sliding_sync`).
//!
//! Python's mutable twins wrap the previous state in a `ChainMap`, whose
//! top map is the request's updates. Here the mutable types hold the
//! previous maps behind an `Arc` and the updates beside them, and persist
//! writes only the updates, as Python's does.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::{exceptions::PyValueError, prelude::*, types::PyModule};

use crate::sliding_sync::config::{config_from_py, RoomSyncConfig};
use crate::types::stream_token::RoomStreamToken;

/// `LAZY_MEMBERS_UPDATE_INTERVAL`, in milliseconds: how stale a lazy
/// member's last-seen timestamp may get before it is rewritten.
pub const LAZY_MEMBERS_UPDATE_INTERVAL_MS: i64 = 60 * 60 * 1000;

/// `HaveSentRoom`: whether the room was sent down the connection. The
/// valid transitions are never to live, live to previously, and previously
/// to live.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HaveSentRoom<T> {
    /// Never sent, or forgotten.
    Never,
    /// Sent, with every update since.
    Live,
    /// Sent, with the updates after the token still to send.
    Previously(T),
}

impl<T> HaveSentRoom<T> {
    pub fn is_never(&self) -> bool {
        matches!(self, HaveSentRoom::Never)
    }

    pub fn is_live(&self) -> bool {
        matches!(self, HaveSentRoom::Live)
    }
}

/// `RoomStatusMap`: the status of each room for one stream.
#[derive(Debug, Clone)]
pub struct RoomStatusMap<T> {
    statuses: Arc<HashMap<String, HaveSentRoom<T>>>,
}

impl<T> Default for RoomStatusMap<T> {
    fn default() -> Self {
        RoomStatusMap {
            statuses: Arc::new(HashMap::new()),
        }
    }
}

impl<T: Clone> RoomStatusMap<T> {
    pub fn new(statuses: HashMap<String, HaveSentRoom<T>>) -> Self {
        RoomStatusMap {
            statuses: Arc::new(statuses),
        }
    }

    pub fn have_sent_room(&self, room_id: &str) -> HaveSentRoom<T> {
        self.statuses
            .get(room_id)
            .cloned()
            .unwrap_or(HaveSentRoom::Never)
    }

    pub fn get_mutable(&self) -> MutableRoomStatusMap<T> {
        MutableRoomStatusMap {
            base: Arc::clone(&self.statuses),
            updates: HashMap::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.statuses.len()
    }

    pub fn is_empty(&self) -> bool {
        self.statuses.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &HaveSentRoom<T>)> {
        self.statuses.iter().map(|(k, v)| (k.as_str(), v))
    }
}

/// `MutableRoomStatusMap`: a [`RoomStatusMap`] and the request's updates
/// to it.
#[derive(Debug, Clone)]
pub struct MutableRoomStatusMap<T> {
    base: Arc<HashMap<String, HaveSentRoom<T>>>,
    updates: HashMap<String, HaveSentRoom<T>>,
}

impl<T: Clone> MutableRoomStatusMap<T> {
    pub fn have_sent_room(&self, room_id: &str) -> HaveSentRoom<T> {
        self.updates
            .get(room_id)
            .or_else(|| self.base.get(room_id))
            .cloned()
            .unwrap_or(HaveSentRoom::Never)
    }

    /// `get_updates`: only the changes made.
    pub fn get_updates(&self) -> &HashMap<String, HaveSentRoom<T>> {
        &self.updates
    }

    /// `record_sent_rooms`: these rooms were sent in the response. A live
    /// room records nothing.
    pub fn record_sent_rooms<'a>(&mut self, room_ids: impl IntoIterator<Item = &'a str>) {
        for room_id in room_ids {
            if self.have_sent_room(room_id).is_live() {
                continue;
            }
            self.updates.insert(room_id.to_owned(), HaveSentRoom::Live);
        }
    }

    /// `record_unsent_rooms`: these rooms have updates that were not sent.
    /// Only a live room changes, to previously at `from_token`.
    pub fn record_unsent_rooms<'a>(
        &mut self,
        room_ids: impl IntoIterator<Item = &'a str>,
        from_token: &T,
    ) {
        for room_id in room_ids {
            if !self.have_sent_room(room_id).is_live() {
                continue;
            }
            self.updates.insert(
                room_id.to_owned(),
                HaveSentRoom::Previously(from_token.clone()),
            );
        }
    }

    /// `copy`: the flattened map.
    pub fn copy(&self) -> RoomStatusMap<T> {
        let mut statuses = (*self.base).clone();
        statuses.extend(self.updates.iter().map(|(k, v)| (k.clone(), v.clone())));
        RoomStatusMap::new(statuses)
    }

    pub fn len(&self) -> usize {
        self.base.len()
            + self
                .updates
                .keys()
                .filter(|room_id| !self.base.contains_key(*room_id))
                .count()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// `PerConnectionState`: a snapshot of what the connection has sent down.
/// `R` is the receipts stream's token, a `MultiWriterStreamToken` in
/// Python.
#[derive(Debug, Clone)]
pub struct PerConnectionState<R> {
    /// When the connection was last used, in milliseconds. Only accurate
    /// to `UPDATE_INTERVAL_LAST_USED_TS`.
    pub last_used_ts: Option<i64>,
    pub rooms: RoomStatusMap<RoomStreamToken>,
    pub receipts: RoomStatusMap<R>,
    pub account_data: RoomStatusMap<i64>,
    /// The config of each room sent down before.
    pub room_configs: Arc<HashMap<String, RoomSyncConfig>>,
}

impl<R> Default for PerConnectionState<R> {
    fn default() -> Self {
        PerConnectionState {
            last_used_ts: None,
            rooms: RoomStatusMap::default(),
            receipts: RoomStatusMap::default(),
            account_data: RoomStatusMap::default(),
            room_configs: Arc::new(HashMap::new()),
        }
    }
}

impl<R: Clone> PerConnectionState<R> {
    pub fn get_mutable(&self) -> MutablePerConnectionState<R> {
        MutablePerConnectionState {
            last_used_ts: self.last_used_ts,
            rooms: self.rooms.get_mutable(),
            receipts: self.receipts.get_mutable(),
            account_data: self.account_data.get_mutable(),
            room_configs_base: Arc::clone(&self.room_configs),
            room_config_updates: HashMap::new(),
            room_lazy_membership: HashMap::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.account_data.len() + self.rooms.len() + self.receipts.len() + self.room_configs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// `RoomLazyMembershipChanges`: the lazy-loaded memberships one room
/// needed in the request.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoomLazyMembershipChanges {
    /// The users whose membership the room lazily loaded, to when it was
    /// last needed before, or `None` if this request sent it first. It
    /// includes users not sent because they were sent before.
    pub returned_user_id_to_last_seen_ts_map: HashMap<String, Option<i64>>,
    /// The users whose latest membership was not sent down.
    pub invalidated_user_ids: HashSet<String>,
}

impl RoomLazyMembershipChanges {
    /// `get_returned_user_ids_to_update`: the users whose last-seen
    /// timestamp the database needs: sent first now, or not rewritten for
    /// [`LAZY_MEMBERS_UPDATE_INTERVAL_MS`].
    pub fn get_returned_user_ids_to_update(&self, now_ms: i64) -> Vec<&str> {
        self.returned_user_id_to_last_seen_ts_map
            .iter()
            .filter(|(_, last_seen_ts)| {
                last_seen_ts.is_none_or(|ts| now_ms - ts >= LAZY_MEMBERS_UPDATE_INTERVAL_MS)
            })
            .map(|(user_id, _)| user_id.as_str())
            .collect()
    }

    /// `has_updates`: whether the room's lazy members need persisting.
    pub fn has_updates(&self, now_ms: i64) -> bool {
        !self.invalidated_user_ids.is_empty()
            || !self.get_returned_user_ids_to_update(now_ms).is_empty()
    }
}

/// `MutablePerConnectionState`: the state and the request's updates.
#[derive(Debug, Clone)]
pub struct MutablePerConnectionState<R> {
    pub last_used_ts: Option<i64>,
    pub rooms: MutableRoomStatusMap<RoomStreamToken>,
    pub receipts: MutableRoomStatusMap<R>,
    pub account_data: MutableRoomStatusMap<i64>,
    room_configs_base: Arc<HashMap<String, RoomSyncConfig>>,
    room_config_updates: HashMap<String, RoomSyncConfig>,
    /// The lazy-loaded memberships each room needed in the request.
    pub room_lazy_membership: HashMap<String, RoomLazyMembershipChanges>,
}

impl<R: Clone> MutablePerConnectionState<R> {
    /// The room's config, the request's update first.
    pub fn room_config(&self, room_id: &str) -> Option<&RoomSyncConfig> {
        self.room_config_updates
            .get(room_id)
            .or_else(|| self.room_configs_base.get(room_id))
    }

    pub fn set_room_config(&mut self, room_id: &str, config: RoomSyncConfig) {
        self.room_config_updates.insert(room_id.to_owned(), config);
    }

    /// `get_room_config_updates`.
    pub fn get_room_config_updates(&self) -> &HashMap<String, RoomSyncConfig> {
        &self.room_config_updates
    }

    /// `has_updates`: whether persisting would change anything. A spurious
    /// persist makes a new connection position, which invalidates caches.
    pub fn has_updates(&self, now_ms: i64) -> bool {
        !self.rooms.get_updates().is_empty()
            || !self.receipts.get_updates().is_empty()
            || !self.account_data.get_updates().is_empty()
            || !self.room_config_updates.is_empty()
            || self
                .room_lazy_membership
                .values()
                .any(|change| change.has_updates(now_ms))
    }

    /// `copy`: the flattened state.
    pub fn copy(&self) -> PerConnectionState<R> {
        let mut room_configs = (*self.room_configs_base).clone();
        room_configs.extend(
            self.room_config_updates
                .iter()
                .map(|(k, v)| (k.clone(), v.clone())),
        );
        PerConnectionState {
            last_used_ts: self.last_used_ts,
            rooms: self.rooms.copy(),
            receipts: self.receipts.copy(),
            account_data: self.account_data.copy(),
            room_configs: Arc::new(room_configs),
        }
    }

    /// `len()` of Python's mutable state: the `ChainMap`s count each room
    /// once.
    pub fn len(&self) -> usize {
        let room_configs = self.room_configs_base.len()
            + self
                .room_config_updates
                .keys()
                .filter(|room_id| !self.room_configs_base.contains_key(*room_id))
                .count();
        self.account_data.len() + self.rooms.len() + self.receipts.len() + room_configs
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// A status from Python's `(flag, token)` test form.
fn status_from_py(flag: &str, token: Option<i64>) -> PyResult<HaveSentRoom<i64>> {
    match (flag, token) {
        ("never", None) => Ok(HaveSentRoom::Never),
        ("live", None) => Ok(HaveSentRoom::Live),
        ("previously", Some(token)) => Ok(HaveSentRoom::Previously(token)),
        _ => Err(PyValueError::new_err(format!(
            "bad status {flag} {token:?}"
        ))),
    }
}

fn status_to_py(status: &HaveSentRoom<i64>) -> (&'static str, Option<i64>) {
    match status {
        HaveSentRoom::Never => ("never", None),
        HaveSentRoom::Live => ("live", None),
        HaveSentRoom::Previously(token) => ("previously", Some(*token)),
    }
}

type PyStatuses = HashMap<String, (String, Option<i64>)>;

fn statuses_from_py(statuses: PyStatuses) -> PyResult<HashMap<String, HaveSentRoom<i64>>> {
    statuses
        .into_iter()
        .map(|(room_id, (flag, token))| Ok((room_id, status_from_py(&flag, token)?)))
        .collect()
}

fn statuses_to_py<'a>(
    statuses: impl Iterator<Item = (&'a String, &'a HaveSentRoom<i64>)>,
) -> HashMap<String, (&'static str, Option<i64>)> {
    statuses
        .map(|(room_id, status)| (room_id.clone(), status_to_py(status)))
        .collect()
}

/// A `MutableRoomStatusMap[int]` through the Rust implementation: from
/// `statuses`, apply `ops` of `("sent", room_ids, None)` or `("unsent",
/// room_ids, token)`. Returns `(updates, flattened, len)`, each status as
/// `(flag, token)`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn room_status_map_ops_for_test(
    statuses: PyStatuses,
    ops: Vec<(String, Vec<String>, Option<i64>)>,
) -> PyResult<(
    HashMap<String, (&'static str, Option<i64>)>,
    HashMap<String, (&'static str, Option<i64>)>,
    usize,
)> {
    let map = RoomStatusMap::new(statuses_from_py(statuses)?);
    let mut mutable = map.get_mutable();
    for (op, room_ids, token) in &ops {
        let room_ids = room_ids.iter().map(String::as_str);
        match (op.as_str(), token) {
            ("sent", None) => mutable.record_sent_rooms(room_ids),
            ("unsent", Some(token)) => mutable.record_unsent_rooms(room_ids, token),
            _ => return Err(PyValueError::new_err(format!("bad op {op}"))),
        }
    }
    let flattened = mutable.copy();
    Ok((
        statuses_to_py(mutable.get_updates().iter()),
        statuses_to_py(flattened.statuses.iter()),
        mutable.len(),
    ))
}

/// `RoomLazyMembershipChanges` through the Rust implementation: returns
/// `(get_returned_user_ids_to_update, has_updates)` at `now_ms`.
#[pyfunction]
fn lazy_membership_changes_for_test(
    returned_user_id_to_last_seen_ts_map: HashMap<String, Option<i64>>,
    invalidated_user_ids: HashSet<String>,
    now_ms: i64,
) -> (Vec<String>, bool) {
    let changes = RoomLazyMembershipChanges {
        returned_user_id_to_last_seen_ts_map,
        invalidated_user_ids,
    };
    let mut to_update: Vec<String> = changes
        .get_returned_user_ids_to_update(now_ms)
        .into_iter()
        .map(str::to_owned)
        .collect();
    to_update.sort();
    (to_update, changes.has_updates(now_ms))
}

/// `MutablePerConnectionState.has_updates` and `len` through the Rust
/// implementation, every stream's token an int (rooms' as a live
/// `RoomStreamToken` at that position). `room_configs` and
/// `room_config_updates` map a room to `(timeline_limit, {type: state
/// keys})`, and `lazy` a room to `(returned map, invalidated)`.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn per_connection_state_for_test(
    rooms: PyStatuses,
    rooms_sent: Vec<String>,
    receipts: PyStatuses,
    receipts_sent: Vec<String>,
    account_data: PyStatuses,
    account_data_sent: Vec<String>,
    room_configs: HashMap<String, (i64, std::collections::BTreeMap<String, Vec<String>>)>,
    room_config_updates: HashMap<String, (i64, std::collections::BTreeMap<String, Vec<String>>)>,
    lazy: HashMap<String, (HashMap<String, Option<i64>>, HashSet<String>)>,
    now_ms: i64,
) -> PyResult<(bool, usize, usize)> {
    let to_room_tokens = |statuses: HashMap<String, HaveSentRoom<i64>>| {
        statuses
            .into_iter()
            .map(|(room_id, status)| {
                let status = match status {
                    HaveSentRoom::Never => HaveSentRoom::Never,
                    HaveSentRoom::Live => HaveSentRoom::Live,
                    HaveSentRoom::Previously(token) => {
                        HaveSentRoom::Previously(RoomStreamToken::live(token))
                    }
                };
                (room_id, status)
            })
            .collect()
    };
    let state = PerConnectionState {
        last_used_ts: None,
        rooms: RoomStatusMap::new(to_room_tokens(statuses_from_py(rooms)?)),
        receipts: RoomStatusMap::new(statuses_from_py(receipts)?),
        account_data: RoomStatusMap::new(statuses_from_py(account_data)?),
        room_configs: Arc::new(
            room_configs
                .into_iter()
                .map(|(room_id, (limit, map))| (room_id, config_from_py(limit, map)))
                .collect(),
        ),
    };
    let base_len = state.len();
    let mut mutable = state.get_mutable();
    mutable
        .rooms
        .record_sent_rooms(rooms_sent.iter().map(String::as_str));
    mutable
        .receipts
        .record_sent_rooms(receipts_sent.iter().map(String::as_str));
    mutable
        .account_data
        .record_sent_rooms(account_data_sent.iter().map(String::as_str));
    for (room_id, (limit, map)) in room_config_updates {
        mutable.set_room_config(&room_id, config_from_py(limit, map));
    }
    for (room_id, (returned, invalidated)) in lazy {
        mutable.room_lazy_membership.insert(
            room_id,
            RoomLazyMembershipChanges {
                returned_user_id_to_last_seen_ts_map: returned,
                invalidated_user_ids: invalidated,
            },
        );
    }
    Ok((mutable.has_updates(now_ms), base_len, mutable.len()))
}

pub fn register_test_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(room_status_map_ops_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(lazy_membership_changes_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(per_connection_state_for_test, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transitions() {
        let map = RoomStatusMap::new(HashMap::from([
            ("!live".to_owned(), HaveSentRoom::Live),
            ("!prev".to_owned(), HaveSentRoom::Previously(3)),
        ]));
        let mut mutable = map.get_mutable();
        mutable.record_sent_rooms(["!live", "!prev", "!new"]);
        // A live room records nothing.
        assert_eq!(
            mutable.get_updates(),
            &HashMap::from([
                ("!prev".to_owned(), HaveSentRoom::Live),
                ("!new".to_owned(), HaveSentRoom::Live),
            ])
        );

        let mut mutable = map.get_mutable();
        mutable.record_unsent_rooms(["!live", "!prev", "!new"], &7);
        assert_eq!(
            mutable.get_updates(),
            &HashMap::from([("!live".to_owned(), HaveSentRoom::Previously(7))])
        );
        assert_eq!(mutable.have_sent_room("!prev"), HaveSentRoom::Previously(3));
        assert_eq!(mutable.have_sent_room("!new"), HaveSentRoom::Never);
        assert_eq!(mutable.len(), 2);
    }

    #[test]
    fn lazy_members_rewritten_after_the_interval() {
        let changes = RoomLazyMembershipChanges {
            returned_user_id_to_last_seen_ts_map: HashMap::from([
                ("@new:x".to_owned(), None),
                ("@stale:x".to_owned(), Some(0)),
                ("@fresh:x".to_owned(), Some(LAZY_MEMBERS_UPDATE_INTERVAL_MS)),
            ]),
            invalidated_user_ids: HashSet::new(),
        };
        let mut to_update =
            changes.get_returned_user_ids_to_update(LAZY_MEMBERS_UPDATE_INTERVAL_MS + 1);
        to_update.sort();
        assert_eq!(to_update, vec!["@new:x", "@stale:x"]);

        let changes = RoomLazyMembershipChanges {
            returned_user_id_to_last_seen_ts_map: HashMap::from([(
                "@fresh:x".to_owned(),
                Some(10),
            )]),
            invalidated_user_ids: HashSet::new(),
        };
        assert!(!changes.has_updates(20));
    }
}
