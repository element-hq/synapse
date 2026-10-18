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

//! One sliding sync request as Rust sees it: the request body, plus what
//! Python adds to it.
//!
//! Python validates the body with pydantic (`SlidingSyncBody`), which also
//! produces the error messages. Rust then parses the same bytes with serde
//! into [`SlidingSyncBody`]. The two parses are held equal by
//! `tests/rest/client/sliding_sync/test_rust_request.py`. The types follow
//! the pydantic model field for field, including where it tells an explicit
//! `null` from a missing field (`slow_get_all_rooms`, and an extension's
//! `enabled`, `lists` and `rooms`).
//!
//! Rust only parses bodies pydantic has accepted, so it does not repeat the
//! validation: the `timeline_limit` maximum, the range bounds, the list key
//! length, the number of lists and the format of the `since` tokens. One
//! accepted body does not parse here: an integer outside `i64` (`u64` for a
//! range), which Python takes as an arbitrary-precision integer. Python then
//! computes the request itself.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::marker::PhantomData;
use std::sync::Arc;

use pyo3::{exceptions::PyValueError, prelude::*};
use pythonize::pythonize;
use serde::{
    de::{MapAccess, Visitor},
    ser::SerializeSeq,
    Deserialize, Deserializer, Serialize, Serializer,
};

use crate::types::stream_token::RoomStreamToken;
use crate::visibility::SoftFailedPolicy;

/// A JSON object read into its entries in document order. A repeated key
/// keeps its first position and its last value, as the Python dict that
/// `json.loads` builds does.
///
/// Serialises as a list of `[key, value]` pairs, so the order survives the
/// trip to Python in tests.
#[derive(Debug, Clone, PartialEq)]
pub struct OrderedMap<V>(pub Vec<(String, V)>);

impl<V> Default for OrderedMap<V> {
    fn default() -> Self {
        OrderedMap(Vec::new())
    }
}

impl<V> OrderedMap<V> {
    pub fn iter(&self) -> impl Iterator<Item = (&str, &V)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v))
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn get(&self, key: &str) -> Option<&V> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }
}

impl<'de, V: Deserialize<'de>> Deserialize<'de> for OrderedMap<V> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct OrderedMapVisitor<V>(PhantomData<V>);

        impl<'de, V: Deserialize<'de>> Visitor<'de> for OrderedMapVisitor<V> {
            type Value = OrderedMap<V>;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a JSON object")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Self::Value, A::Error> {
                let mut entries: Vec<(String, V)> = Vec::new();
                let mut positions: HashMap<String, usize> = HashMap::new();
                while let Some((key, value)) = access.next_entry::<String, V>()? {
                    match positions.get(&key) {
                        Some(&position) => entries[position].1 = value,
                        None => {
                            positions.insert(key.clone(), entries.len());
                            entries.push((key, value));
                        }
                    }
                }
                Ok(OrderedMap(entries))
            }
        }

        deserializer.deserialize_map(OrderedMapVisitor(PhantomData))
    }
}

impl<V: Serialize> Serialize for OrderedMap<V> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.0.len()))?;
        for entry in &self.0 {
            seq.serialize_element(entry)?;
        }
        seq.end()
    }
}

fn some_false() -> Option<bool> {
    Some(false)
}

fn all_lists_or_rooms() -> Option<Vec<String>> {
    Some(vec!["*".to_owned()])
}

fn hundred() -> i64 {
    100
}

/// `SlidingSyncBody`: the request body.
#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct SlidingSyncBody {
    #[serde(default)]
    pub conn_id: Option<String>,
    /// The sliding window lists, by the client's name for them.
    #[serde(default)]
    pub lists: Option<OrderedMap<SlidingSyncList>>,
    /// The room subscriptions, by room ID.
    #[serde(default)]
    pub room_subscriptions: Option<OrderedMap<RoomSubscription>>,
    #[serde(default)]
    pub extensions: Option<Extensions>,
}

/// `SlidingSyncBody.SlidingSyncList`.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct SlidingSyncList {
    /// `(event type, state key)` pairs, with the `*`, `$LAZY` and `$ME`
    /// wildcards still in them.
    pub required_state: Vec<(String, String)>,
    /// At most 1000. Pydantic sets no minimum.
    pub timeline_limit: i64,
    /// Inclusive `[start, end]` ranges. `None` means the whole list.
    #[serde(default)]
    pub ranges: Option<Vec<(u64, u64)>>,
    /// `false` when missing, `None` for an explicit `null`.
    #[serde(default = "some_false")]
    pub slow_get_all_rooms: Option<bool>,
    #[serde(default)]
    pub filters: Option<Filters>,
}

/// `SlidingSyncBody.RoomSubscription`.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct RoomSubscription {
    pub required_state: Vec<(String, String)>,
    pub timeline_limit: i64,
}

/// `SlidingSyncList.Filters`. Every field is optional, and a missing field
/// means no filter on it.
#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct Filters {
    #[serde(default)]
    pub is_dm: Option<bool>,
    #[serde(default)]
    pub spaces: Option<Vec<String>>,
    #[serde(default)]
    pub is_encrypted: Option<bool>,
    #[serde(default)]
    pub is_invite: Option<bool>,
    /// `None` inside the list is a room without a type.
    #[serde(default)]
    pub room_types: Option<Vec<Option<String>>>,
    #[serde(default)]
    pub not_room_types: Option<Vec<Option<String>>>,
    #[serde(default)]
    pub room_name_like: Option<String>,
    #[serde(default)]
    pub tags: Option<Vec<String>>,
    #[serde(default)]
    pub not_tags: Option<Vec<String>>,
}

/// `SlidingSyncBody.Extensions`. An unknown extension is ignored.
#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct Extensions {
    #[serde(default)]
    pub to_device: Option<ToDeviceExtension>,
    #[serde(default)]
    pub e2ee: Option<E2eeExtension>,
    #[serde(default)]
    pub account_data: Option<ListsAndRoomsExtension>,
    #[serde(default)]
    pub receipts: Option<ListsAndRoomsExtension>,
    #[serde(default)]
    pub typing: Option<ListsAndRoomsExtension>,
    /// MSC4308. Only the unstable name is accepted.
    #[serde(default, rename = "io.element.msc4308.thread_subscriptions")]
    pub thread_subscriptions: Option<ThreadSubscriptionsExtension>,
    /// MSC4354. `None` when absent: pydantic rejects an explicit `null`.
    #[serde(default, rename = "org.matrix.msc4354.sticky_events")]
    pub sticky_events: Option<StickyEventsExtension>,
    /// MSC4262. As `sticky_events`.
    #[serde(default, rename = "org.matrix.msc4262.profiles")]
    pub profiles: Option<ProfilesExtension>,
}

/// `Extensions.ToDeviceExtension` (MSC3885).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ToDeviceExtension {
    #[serde(default = "some_false")]
    pub enabled: Option<bool>,
    #[serde(default = "hundred")]
    pub limit: i64,
    /// The device inbox stream position, as a string of digits.
    #[serde(default)]
    pub since: Option<String>,
}

/// `Extensions.E2eeExtension` (MSC3884).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct E2eeExtension {
    #[serde(default = "some_false")]
    pub enabled: Option<bool>,
}

/// The account data (MSC3959), receipts (MSC3960) and typing (MSC3961)
/// extensions, which share their fields.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ListsAndRoomsExtension {
    #[serde(default = "some_false")]
    pub enabled: Option<bool>,
    /// The list names to cover. `["*"]`, every list, when missing.
    #[serde(default = "all_lists_or_rooms")]
    pub lists: Option<Vec<String>>,
    /// The room subscriptions to cover. `["*"]`, every one, when missing.
    #[serde(default = "all_lists_or_rooms")]
    pub rooms: Option<Vec<String>>,
}

/// `Extensions.ThreadSubscriptionsExtension` (MSC4308).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ThreadSubscriptionsExtension {
    #[serde(default = "some_false")]
    pub enabled: Option<bool>,
    #[serde(default = "hundred")]
    pub limit: i64,
}

/// `Extensions.StickyEventsExtension` (MSC4354).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct StickyEventsExtension {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "hundred")]
    pub limit: i64,
    /// A `sticky_` token, unparsed.
    #[serde(default)]
    pub since: Option<String>,
}

/// `Extensions.ProfilesExtension` (MSC4262).
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct ProfilesExtension {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub fields: Option<Vec<String>>,
}

impl SlidingSyncBody {
    /// Parse a body pydantic has accepted.
    pub fn parse(body: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(body)
    }
}

/// Where an incremental request starts: the `pos` the client sent, less
/// the stream keys Rust does not read yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FromToken {
    pub room_key: RoomStreamToken,
    pub connection_position: i64,
}

/// What every part of a request's computation reads, fixed for the whole
/// request.
#[derive(Debug)]
pub struct SlidingSyncRequestData {
    pub body: SlidingSyncBody,
    pub user_id: Arc<str>,
    pub device_id: Option<Arc<str>>,
    /// `None` for an initial request.
    pub from: Option<FromToken>,
    /// The room key of the token the response is computed up to.
    pub to_room_key: RoomStreamToken,
    /// The rest of that token's string form, after the room key and its
    /// `_`. A room's `prev_batch` is a new room key followed by it.
    pub to_suffix: Arc<str>,
    /// `Some` for a server admin: what they asked to see of soft-failed
    /// events.
    pub admin_policy: Option<SoftFailedPolicy>,
    /// `have_finished_sliding_sync_background_jobs`. Rust covers only the
    /// path that reads the sliding sync tables, so a request made before
    /// they are filled is Python's.
    pub have_finished_bg_jobs: bool,
    /// `rooms_to_exclude_from_sync`.
    pub rooms_to_exclude: Arc<HashSet<Arc<str>>>,
    pub msc4306_enabled: bool,
    pub msc4354_enabled: bool,
    /// `include_profile_updates_in_sync`, which gates MSC4262.
    pub profiles_enabled: bool,
}

/// One sliding sync request. Built by Python in
/// `SlidingSyncHandler._build_rust_request` for each computation of the
/// response, as the token it runs up to changes between the first try and
/// the one after waiting.
#[pyclass(frozen, skip_from_py_object)]
pub struct SlidingSyncRequest {
    pub(crate) data: Arc<SlidingSyncRequestData>,
}

#[pymethods]
impl SlidingSyncRequest {
    /// Raises `ValueError` if the body does not parse.
    #[new]
    #[pyo3(signature = (body, *, user_id, device_id, from_room_key, connection_position, to_room_key, to_suffix, is_server_admin, return_soft_failed_events, return_policy_server_spammy_events, have_finished_bg_jobs, rooms_to_exclude, msc4306_enabled, msc4354_enabled, profiles_enabled))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        body: &[u8],
        user_id: &str,
        device_id: Option<&str>,
        from_room_key: Option<RoomStreamToken>,
        connection_position: Option<i64>,
        to_room_key: RoomStreamToken,
        to_suffix: &str,
        is_server_admin: bool,
        return_soft_failed_events: bool,
        return_policy_server_spammy_events: bool,
        have_finished_bg_jobs: bool,
        rooms_to_exclude: HashSet<String>,
        msc4306_enabled: bool,
        msc4354_enabled: bool,
        profiles_enabled: bool,
    ) -> PyResult<Self> {
        let body = SlidingSyncBody::parse(body)
            .map_err(|e| PyValueError::new_err(format!("parsing the sliding sync body: {e}")))?;
        let from = match (from_room_key, connection_position) {
            (Some(room_key), Some(connection_position)) => Some(FromToken {
                room_key,
                connection_position,
            }),
            (None, None) => None,
            _ => {
                return Err(PyValueError::new_err(
                    "from_room_key and connection_position go together",
                ))
            }
        };
        let admin_policy = is_server_admin.then_some(SoftFailedPolicy {
            return_soft_failed_events,
            return_policy_server_spammy_events,
        });
        Ok(SlidingSyncRequest {
            data: Arc::new(SlidingSyncRequestData {
                body,
                user_id: Arc::from(user_id),
                device_id: device_id.map(Arc::from),
                from,
                to_room_key,
                to_suffix: Arc::from(to_suffix),
                admin_policy,
                have_finished_bg_jobs,
                rooms_to_exclude: Arc::new(rooms_to_exclude.into_iter().map(Arc::from).collect()),
                msc4306_enabled,
                msc4354_enabled,
                profiles_enabled,
            }),
        })
    }

    /// The parsed body, with every default filled in and each JSON object
    /// keyed by name (lists, room subscriptions) as a list of `[name,
    /// value]` pairs. For the differential test.
    fn body_for_test<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &self.data.body)?)
    }

    #[getter]
    fn user_id(&self) -> &str {
        &self.data.user_id
    }

    #[getter]
    fn device_id(&self) -> Option<&str> {
        self.data.device_id.as_deref()
    }

    #[getter]
    #[allow(clippy::wrong_self_convention)]
    fn from_room_key(&self) -> Option<RoomStreamToken> {
        self.data.from.as_ref().map(|from| from.room_key.clone())
    }

    #[getter]
    fn connection_position(&self) -> Option<i64> {
        self.data.from.as_ref().map(|from| from.connection_position)
    }

    #[getter]
    fn to_room_key(&self) -> RoomStreamToken {
        self.data.to_room_key.clone()
    }

    #[getter]
    fn to_suffix(&self) -> &str {
        &self.data.to_suffix
    }

    #[getter]
    fn is_server_admin(&self) -> bool {
        self.data.admin_policy.is_some()
    }

    #[getter]
    fn have_finished_bg_jobs(&self) -> bool {
        self.data.have_finished_bg_jobs
    }

    #[getter]
    fn rooms_to_exclude(&self) -> HashSet<String> {
        self.data
            .rooms_to_exclude
            .iter()
            .map(|room_id| room_id.to_string())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_body() {
        let body = SlidingSyncBody::parse(b"{}").unwrap();
        assert_eq!(body, SlidingSyncBody::default());
    }

    #[test]
    fn list_defaults_and_nulls() {
        let body = SlidingSyncBody::parse(
            br#"{
                "lists": {
                    "a": {"required_state": [["m.room.name", ""]], "timeline_limit": 5},
                    "b": {"required_state": [], "timeline_limit": 0, "slow_get_all_rooms": null,
                          "ranges": [[0, 9], [20, 29]], "filters": {"room_types": [null, "m.space"]}}
                }
            }"#,
        )
        .unwrap();
        let lists = body.lists.unwrap();
        let a = lists.get("a").unwrap();
        assert_eq!(a.slow_get_all_rooms, Some(false));
        assert_eq!(a.ranges, None);
        assert_eq!(
            a.required_state,
            vec![("m.room.name".to_owned(), String::new())]
        );
        let b = lists.get("b").unwrap();
        assert_eq!(b.slow_get_all_rooms, None);
        assert_eq!(b.ranges, Some(vec![(0, 9), (20, 29)]));
        assert_eq!(
            b.filters.as_ref().unwrap().room_types,
            Some(vec![None, Some("m.space".to_owned())])
        );
    }

    #[test]
    fn ordered_map_keeps_document_order_and_the_last_value() {
        let map: OrderedMap<i64> = serde_json::from_str(r#"{"z": 1, "a": 2, "z": 3}"#).unwrap();
        assert_eq!(map.0, vec![("z".to_owned(), 3), ("a".to_owned(), 2)]);
    }

    #[test]
    fn extension_defaults() {
        let body = SlidingSyncBody::parse(
            br#"{"extensions": {
                "to_device": {},
                "account_data": {"enabled": true, "lists": null},
                "io.element.msc4308.thread_subscriptions": {"enabled": true},
                "thread_subscriptions": {"enabled": false},
                "org.matrix.msc4354.sticky_events": {"since": "sticky_4"},
                "unknown": {"enabled": true}
            }}"#,
        )
        .unwrap();
        let extensions = body.extensions.unwrap();
        let to_device = extensions.to_device.unwrap();
        assert_eq!(to_device.enabled, Some(false));
        assert_eq!(to_device.limit, 100);
        let account_data = extensions.account_data.unwrap();
        assert_eq!(account_data.lists, None);
        assert_eq!(account_data.rooms, Some(vec!["*".to_owned()]));
        assert_eq!(extensions.thread_subscriptions.unwrap().enabled, Some(true));
        let sticky = extensions.sticky_events.unwrap();
        assert!(!sticky.enabled);
        assert_eq!(sticky.since.as_deref(), Some("sticky_4"));
        assert_eq!(extensions.profiles, None);
        assert_eq!(extensions.e2ee, None);
    }

    #[test]
    fn unknown_fields_are_ignored() {
        let body = SlidingSyncBody::parse(
            br#"{"conn_id": "c", "foo": [1, 2],
                "room_subscriptions": {"!r:x": {"required_state": [], "timeline_limit": 1, "bar": null}}}"#,
        )
        .unwrap();
        assert_eq!(body.conn_id.as_deref(), Some("c"));
        assert_eq!(body.room_subscriptions.unwrap().len(), 1);
    }

    #[test]
    fn out_of_range_integers_do_not_parse() {
        assert!(SlidingSyncBody::parse(
            br#"{"lists": {"a": {"required_state": [], "timeline_limit": 1, "ranges": [[0, 18446744073709551616]]}}}"#
        )
        .is_err());
    }
}
