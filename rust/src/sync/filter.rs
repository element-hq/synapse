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

//! `synapse.api.filtering`: the `Filter` and `FilterCollection` a sync
//! request is filtered by.
//!
//! A [`FilterCollection`] is built from the filter JSON the Python one was
//! built from, once per request, and answers the same questions. The checks
//! are pure functions of the filter and the event. The one exception is the
//! relation checks (`related_by_senders`, `related_by_rel_types`), which
//! need a database lookup: [`Filter::needs_relation_check`] says when, and
//! [`RelationLookup`] is how the caller supplies it.
//!
//! Filters apply to three shapes of thing: events, presence updates, and the
//! JSON of ephemeral events and account data. [`Filterable`] gives each
//! shape's fields.

use std::{collections::HashSet, future::Future, sync::Arc};

use pyo3::{prelude::*, types::PyDict};
use serde_json::Value;

use crate::events::{constants::event_field::M_RELATES_TO, Event};

/// The EDU type presence updates carry.
const PRESENCE_EDU_TYPE: &str = "m.presence";

/// `EventContentFields.LABELS`.
const LABELS_FIELD: &str = "org.matrix.labels";

/// Something a [`Filter`] can be applied to.
pub trait Filterable {
    fn sender(&self) -> Option<&str>;
    fn room_id(&self) -> Option<&str>;
    fn event_type(&self) -> Option<&str>;
    /// The event's labels (`org.matrix.labels` in its content).
    fn labels(&self) -> Vec<&str>;
    /// The `rel_type` of the event's relation, if it is an event with a
    /// valid one. Only events have relations.
    fn rel_type(&self) -> Option<&str>;
    /// Whether the event's content has a string `url` field. `None` for
    /// shapes the `contains_url` check does not apply to.
    fn contains_url(&self) -> Option<bool>;
    /// Presence updates are matched on their user and the presence EDU type
    /// alone, with no wildcard on the type.
    fn is_presence(&self) -> bool {
        false
    }
}

impl Filterable for Event {
    fn sender(&self) -> Option<&str> {
        Some(Event::sender(self))
    }

    fn room_id(&self) -> Option<&str> {
        Some(Event::room_id(self))
    }

    fn event_type(&self) -> Option<&str> {
        Some(self.type_())
    }

    fn labels(&self) -> Vec<&str> {
        labels_of(self.content().get_field(LABELS_FIELD))
    }

    fn rel_type(&self) -> Option<&str> {
        relation_type_of(self.content().get_field(M_RELATES_TO))
    }

    fn contains_url(&self) -> Option<bool> {
        Some(
            self.content()
                .get_field("url")
                .is_some_and(Value::is_string),
        )
    }
}

/// A presence update, which the Python filters as a `UserPresenceState`.
pub struct Presence<'a> {
    pub user_id: &'a str,
}

impl Filterable for Presence<'_> {
    fn sender(&self) -> Option<&str> {
        Some(self.user_id)
    }

    fn room_id(&self) -> Option<&str> {
        None
    }

    fn event_type(&self) -> Option<&str> {
        Some(PRESENCE_EDU_TYPE)
    }

    fn labels(&self) -> Vec<&str> {
        Vec::new()
    }

    fn rel_type(&self) -> Option<&str> {
        None
    }

    fn contains_url(&self) -> Option<bool> {
        None
    }

    fn is_presence(&self) -> bool {
        true
    }
}

/// The JSON of an ephemeral event or account data entry, as a dict with
/// `type`, `content` and perhaps `sender` and `room_id`.
impl Filterable for Value {
    fn sender(&self) -> Option<&str> {
        // Presence events had their sender in `content.user_id`. The Python
        // still looks there when there is no `sender`.
        self.get("sender")
            .and_then(Value::as_str)
            .filter(|sender| !sender.is_empty())
            .or_else(|| self.content().and_then(|c| c.get("user_id")?.as_str()))
    }

    fn room_id(&self) -> Option<&str> {
        self.get("room_id").and_then(Value::as_str)
    }

    fn event_type(&self) -> Option<&str> {
        self.get("type").and_then(Value::as_str)
    }

    fn labels(&self) -> Vec<&str> {
        labels_of(self.content().and_then(|c| c.get(LABELS_FIELD)))
    }

    fn rel_type(&self) -> Option<&str> {
        None
    }

    fn contains_url(&self) -> Option<bool> {
        Some(
            self.content()
                .and_then(|c| c.get("url"))
                .is_some_and(Value::is_string),
        )
    }
}

trait JsonContent {
    /// The `content`, if it is an object. Account data may have other
    /// content, which the Python treats as empty.
    fn content(&self) -> Option<&serde_json::Map<String, Value>>;
}

impl JsonContent for Value {
    fn content(&self) -> Option<&serde_json::Map<String, Value>> {
        self.get("content").and_then(Value::as_object)
    }
}

/// The string entries of a `labels` list. The Python checks `v in labels`
/// on whatever the list holds, so only string labels can match a string.
fn labels_of(labels: Option<&Value>) -> Vec<&str> {
    labels
        .and_then(Value::as_array)
        .map(|labels| labels.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

/// `relation_from_event`'s `rel_type`: a relation needs a string `rel_type`
/// and a string `event_id`.
fn relation_type_of(relates_to: Option<&Value>) -> Option<&str> {
    let relation = relates_to?.as_object()?;
    let rel_type = relation.get("rel_type")?.as_str()?;
    relation.get("event_id")?.as_str()?;
    Some(rel_type)
}

/// `_matches_wildcard`: a filter value ending in `*` matches by prefix.
fn matches_wildcard(actual: Option<&str>, filter_value: &str) -> bool {
    match (filter_value.strip_suffix('*'), actual) {
        (Some(prefix), Some(actual)) => actual.starts_with(prefix),
        (Some(_), None) => false,
        (None, actual) => actual == Some(filter_value),
    }
}

/// One `Filter` definition: a `room_event_filter`, `room_filter` or plain
/// `filter` from the filter JSON.
#[derive(Debug, Clone, Default)]
pub struct Filter {
    pub limit: i64,
    pub lazy_load_members: bool,
    pub include_redundant_members: bool,
    pub unread_thread_notifications: bool,

    /// `None` means no restriction. An empty list matches nothing.
    pub types: Option<Vec<String>>,
    pub not_types: Vec<String>,
    pub rooms: Option<Vec<String>>,
    pub not_rooms: Vec<String>,
    pub senders: Option<Vec<String>>,
    pub not_senders: Vec<String>,
    pub contains_url: Option<bool>,
    pub labels: Option<Vec<String>>,
    pub not_labels: Vec<String>,
    pub related_by_senders: Option<Vec<String>>,
    pub related_by_rel_types: Option<Vec<String>>,
    /// MSC3874, only read when it is enabled.
    pub rel_types: Option<Vec<String>>,
    pub not_rel_types: Vec<String>,
}

/// The experimental feature flags the filters read from the config.
#[derive(Debug, Clone, Copy, Default)]
pub struct FilterFlags {
    pub msc3773_enabled: bool,
    pub msc3874_enabled: bool,
    pub include_profile_updates_in_sync: bool,
}

/// Read a list of strings, as the Python reads a filter field it then
/// matches values against. A list with other things in it keeps only its
/// strings: nothing but a string can equal a string.
fn string_list(value: Option<&Value>) -> Option<Vec<String>> {
    let list = value?.as_array()?;
    Some(
        list.iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect(),
    )
}

/// As [`string_list`], for a field whose absence means an empty list.
fn string_list_or_empty(value: Option<&Value>) -> Vec<String> {
    string_list(value).unwrap_or_default()
}

/// A boolean filter field, read as Python's truth test would read it.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

impl Filter {
    /// Build from one filter definition. Anything that is not an object is
    /// an empty definition, as `filter_json.get(..., {})` on a mapping that
    /// holds something else would be in Python only by accident, and the
    /// schema does not allow it.
    pub fn new(filter_json: Option<&Value>, flags: FilterFlags) -> Self {
        let empty = serde_json::Map::new();
        let json = filter_json.and_then(Value::as_object).unwrap_or(&empty);
        let get = |key: &str| json.get(key);

        let limit = match get("limit") {
            Some(Value::Number(n)) => n
                .as_i64()
                .or_else(|| n.as_f64().map(|f| f as i64))
                .unwrap_or(10),
            _ => 10,
        };

        let mut unread_thread_notifications = truthy(get("unread_thread_notifications"));
        if !unread_thread_notifications && flags.msc3773_enabled {
            unread_thread_notifications =
                truthy(get("org.matrix.msc3773.unread_thread_notifications"));
        }

        let (rel_types, not_rel_types) = if flags.msc3874_enabled {
            (
                string_list(get("org.matrix.msc3874.rel_types")),
                string_list_or_empty(get("org.matrix.msc3874.not_rel_types")),
            )
        } else {
            (None, Vec::new())
        };

        Filter {
            limit,
            lazy_load_members: truthy(get("lazy_load_members")),
            include_redundant_members: truthy(get("include_redundant_members")),
            unread_thread_notifications,
            types: string_list(get("types")),
            not_types: string_list_or_empty(get("not_types")),
            rooms: string_list(get("rooms")),
            not_rooms: string_list_or_empty(get("not_rooms")),
            senders: string_list(get("senders")),
            not_senders: string_list_or_empty(get("not_senders")),
            contains_url: get("contains_url").and_then(Value::as_bool),
            labels: string_list(get("org.matrix.labels")),
            not_labels: string_list_or_empty(get("org.matrix.not_labels")),
            related_by_senders: string_list(get("related_by_senders")),
            related_by_rel_types: string_list(get("related_by_rel_types")),
            rel_types,
            not_rel_types,
        }
    }

    pub fn filters_all_types(&self) -> bool {
        self.types.as_deref() == Some(&[]) || self.not_types.iter().any(|t| t == "*")
    }

    pub fn filters_all_senders(&self) -> bool {
        self.senders.as_deref() == Some(&[]) || self.not_senders.iter().any(|s| s == "*")
    }

    pub fn filters_all_rooms(&self) -> bool {
        self.rooms.as_deref() == Some(&[]) || self.not_rooms.iter().any(|r| r == "*")
    }

    /// `_check`: whether the filter matches `event`, relation checks aside.
    pub fn check<E: Filterable>(&self, event: &E) -> bool {
        if event.is_presence() {
            let user_id = event.sender();
            return self.check_field(&self.not_senders, self.senders.as_deref(), |v| {
                user_id == Some(v)
            }) && self.check_field(&self.not_types, self.types.as_deref(), |v| {
                v == PRESENCE_EDU_TYPE
            });
        }

        let room_id = event.room_id();
        let sender = event.sender();
        let event_type = event.event_type();
        let labels = event.labels();
        let rel_type = event.rel_type();

        let matched = self.check_field(&self.not_rooms, self.rooms.as_deref(), |v| {
            room_id == Some(v)
        }) && self.check_field(&self.not_senders, self.senders.as_deref(), |v| {
            sender == Some(v)
        }) && self.check_field(&self.not_types, self.types.as_deref(), |v| {
            matches_wildcard(event_type, v)
        }) && self.check_field(&self.not_labels, self.labels.as_deref(), |v| {
            labels.contains(&v)
        }) && self.check_field(&self.not_rel_types, self.rel_types.as_deref(), |v| {
            rel_type == Some(v)
        });
        if !matched {
            return false;
        }

        if let (Some(wanted), Some(has)) = (self.contains_url, event.contains_url()) {
            if wanted != has {
                return false;
            }
        }

        true
    }

    /// `_check_fields` for one field: reject on any disallowed match, then
    /// reject unless some allowed value matches, when there is an allowed
    /// list.
    fn check_field(
        &self,
        disallowed: &[String],
        allowed: Option<&[String]>,
        matches: impl Fn(&str) -> bool,
    ) -> bool {
        if disallowed.iter().any(|v| matches(v)) {
            return false;
        }
        match allowed {
            Some(allowed) => allowed.iter().any(|v| matches(v)),
            None => true,
        }
    }

    /// Whether [`Filter::filter`] has to look relations up.
    pub fn needs_relation_check(&self) -> bool {
        self.related_by_senders
            .as_ref()
            .is_some_and(|v| !v.is_empty())
            || self
                .related_by_rel_types
                .as_ref()
                .is_some_and(|v| !v.is_empty())
    }

    /// `filter_rooms`: the given rooms this filter lets through.
    pub fn filter_rooms<'a>(
        &self,
        room_ids: impl IntoIterator<Item = &'a str>,
    ) -> HashSet<&'a str> {
        room_ids
            .into_iter()
            .filter(|room_id| !self.not_rooms.iter().any(|r| r == room_id))
            .filter(|room_id| match &self.rooms {
                Some(rooms) => rooms.iter().any(|r| r == room_id),
                None => true,
            })
            .collect()
    }

    /// `filter`: the events the filter lets through, in order, including the
    /// relation check through `lookup` when the filter has one.
    pub async fn filter<L: RelationLookup>(
        &self,
        events: Vec<Arc<Event>>,
        lookup: &L,
    ) -> Result<Vec<Arc<Event>>, L::Error> {
        let mut result: Vec<Arc<Event>> = events.into_iter().filter(|e| self.check(&**e)).collect();
        if self.needs_relation_check() {
            let ids: Vec<&str> = result.iter().map(|e| e.event_id()).collect();
            let keep = lookup
                .events_have_relations(
                    &ids,
                    self.related_by_senders.as_deref(),
                    self.related_by_rel_types.as_deref(),
                )
                .await?;
            result.retain(|e| keep.contains(e.event_id()));
        }
        Ok(result)
    }
}

/// `events_have_relations`: which of `event_ids` have a relation from one
/// of `related_by_senders`, of one of `related_by_rel_types`. The store
/// supplies this.
pub trait RelationLookup {
    type Error;

    fn events_have_relations(
        &self,
        event_ids: &[&str],
        related_by_senders: Option<&[String]>,
        related_by_rel_types: Option<&[String]>,
    ) -> impl Future<Output = Result<HashSet<String>, Self::Error>> + Send;
}

/// `FilterCollection`: the filters of one request, built from its filter
/// JSON. Shared read-only across the request's rooms.
#[pyclass(frozen, skip_from_py_object, name = "FilterCollection")]
#[derive(Debug, Clone)]
pub struct FilterCollection {
    room_filter: Filter,
    room_timeline_filter: Filter,
    room_state_filter: Filter,
    room_ephemeral_filter: Filter,
    room_account_data_filter: Filter,
    presence_filter: Filter,
    global_account_data_filter: Filter,

    pub include_leave: bool,
    pub event_fields: Vec<String>,
    pub event_format: String,
    pub profile_fields: HashSet<String>,
}

impl FilterCollection {
    pub fn new(filter_json: &Value, flags: FilterFlags) -> Self {
        let empty = Value::Object(Default::default());
        let room = filter_json.get("room").unwrap_or(&empty);

        // The room filter keeps only its `rooms` and `not_rooms`.
        let mut room_only = serde_json::Map::new();
        if let Some(room) = room.as_object() {
            for key in ["rooms", "not_rooms"] {
                if let Some(value) = room.get(key) {
                    room_only.insert(key.to_owned(), value.clone());
                }
            }
        }

        let profile_fields = if flags.include_profile_updates_in_sync {
            filter_json
                .get("org.matrix.msc4429.profile_fields")
                .and_then(Value::as_object)
                .map(|f| string_list_or_empty(f.get("ids")).into_iter().collect())
                .unwrap_or_default()
        } else {
            HashSet::new()
        };

        FilterCollection {
            room_filter: Filter::new(Some(&Value::Object(room_only)), flags),
            room_timeline_filter: Filter::new(room.get("timeline"), flags),
            room_state_filter: Filter::new(room.get("state"), flags),
            room_ephemeral_filter: Filter::new(room.get("ephemeral"), flags),
            room_account_data_filter: Filter::new(room.get("account_data"), flags),
            presence_filter: Filter::new(filter_json.get("presence"), flags),
            global_account_data_filter: Filter::new(filter_json.get("account_data"), flags),
            include_leave: truthy(room.get("include_leave")),
            event_fields: string_list_or_empty(filter_json.get("event_fields")),
            event_format: filter_json
                .get("event_format")
                .and_then(Value::as_str)
                .unwrap_or("client")
                .to_owned(),
            profile_fields,
        }
    }

    pub fn timeline_limit(&self) -> i64 {
        self.room_timeline_filter.limit
    }

    pub fn presence_limit(&self) -> i64 {
        self.presence_filter.limit
    }

    pub fn ephemeral_limit(&self) -> i64 {
        self.room_ephemeral_filter.limit
    }

    pub fn lazy_load_members(&self) -> bool {
        self.room_state_filter.lazy_load_members
    }

    pub fn include_redundant_members(&self) -> bool {
        self.room_state_filter.include_redundant_members
    }

    pub fn unread_thread_notifications(&self) -> bool {
        self.room_timeline_filter.unread_thread_notifications
    }

    pub fn room_timeline_filter(&self) -> &Filter {
        &self.room_timeline_filter
    }

    pub fn room_state_filter(&self) -> &Filter {
        &self.room_state_filter
    }

    /// `filter_room_timeline` without the relation check: whether a timeline
    /// event passes the room and timeline filters.
    pub fn check_room_timeline(&self, event: &Event) -> bool {
        self.room_filter.check(event) && self.room_timeline_filter.check(event)
    }

    /// `filter_room_state` without the relation check.
    pub fn check_room_state(&self, event: &Event) -> bool {
        self.room_filter.check(event) && self.room_state_filter.check(event)
    }

    pub fn check_room_ephemeral(&self, event: &Value) -> bool {
        self.room_filter.check(event) && self.room_ephemeral_filter.check(event)
    }

    pub fn check_room_account_data(&self, event: &Value) -> bool {
        self.room_filter.check(event) && self.room_account_data_filter.check(event)
    }

    pub fn check_presence(&self, user_id: &str) -> bool {
        self.presence_filter.check(&Presence { user_id })
    }

    pub fn check_global_account_data(&self, event: &Value) -> bool {
        self.global_account_data_filter.check(event)
    }

    /// `filter_room_timeline`, relation check included.
    pub async fn filter_room_timeline<L: RelationLookup>(
        &self,
        events: Vec<Arc<Event>>,
        lookup: &L,
    ) -> Result<Vec<Arc<Event>>, L::Error> {
        let events = self.room_filter.filter(events, lookup).await?;
        self.room_timeline_filter.filter(events, lookup).await
    }

    /// `filter_room_state`, relation check included.
    pub async fn filter_room_state<L: RelationLookup>(
        &self,
        events: Vec<Arc<Event>>,
        lookup: &L,
    ) -> Result<Vec<Arc<Event>>, L::Error> {
        let events = self.room_filter.filter(events, lookup).await?;
        self.room_state_filter.filter(events, lookup).await
    }

    pub fn blocks_all_rooms(&self) -> bool {
        self.room_filter.filters_all_rooms()
    }

    pub fn blocks_all_presence(&self) -> bool {
        self.presence_filter.filters_all_types() || self.presence_filter.filters_all_senders()
    }

    pub fn blocks_all_global_account_data(&self) -> bool {
        self.global_account_data_filter.filters_all_types()
            || self.global_account_data_filter.filters_all_senders()
    }

    pub fn blocks_all_room_ephemeral(&self) -> bool {
        Self::blocks_all(&self.room_ephemeral_filter)
    }

    pub fn blocks_all_room_account_data(&self) -> bool {
        Self::blocks_all(&self.room_account_data_filter)
    }

    pub fn blocks_all_room_timeline(&self) -> bool {
        Self::blocks_all(&self.room_timeline_filter)
    }

    fn blocks_all(filter: &Filter) -> bool {
        filter.filters_all_types() || filter.filters_all_senders() || filter.filters_all_rooms()
    }
}

/// Read a Python dict as JSON. `JsonDict`s hold only JSON values, so a
/// failure means the dict is not one.
fn json_from_py(dict: &Bound<'_, PyDict>) -> PyResult<Value> {
    pythonize::depythonize(dict).map_err(Into::into)
}

#[pymethods]
impl FilterCollection {
    /// Build from the filter JSON, with the config flags the Python
    /// `FilterCollection` reads from the homeserver.
    #[new]
    #[pyo3(signature = (filter_json, *, msc3773_enabled = false, msc3874_enabled = false, include_profile_updates_in_sync = false))]
    fn py_new(
        filter_json: &Bound<'_, PyDict>,
        msc3773_enabled: bool,
        msc3874_enabled: bool,
        include_profile_updates_in_sync: bool,
    ) -> PyResult<Self> {
        let flags = FilterFlags {
            msc3773_enabled,
            msc3874_enabled,
            include_profile_updates_in_sync,
        };
        Ok(FilterCollection::new(&json_from_py(filter_json)?, flags))
    }

    #[pyo3(name = "timeline_limit")]
    fn py_timeline_limit(&self) -> i64 {
        self.timeline_limit()
    }

    #[pyo3(name = "presence_limit")]
    fn py_presence_limit(&self) -> i64 {
        self.presence_limit()
    }

    #[pyo3(name = "ephemeral_limit")]
    fn py_ephemeral_limit(&self) -> i64 {
        self.ephemeral_limit()
    }

    #[pyo3(name = "lazy_load_members")]
    fn py_lazy_load_members(&self) -> bool {
        self.lazy_load_members()
    }

    #[pyo3(name = "include_redundant_members")]
    fn py_include_redundant_members(&self) -> bool {
        self.include_redundant_members()
    }

    #[pyo3(name = "unread_thread_notifications")]
    fn py_unread_thread_notifications(&self) -> bool {
        self.unread_thread_notifications()
    }

    #[getter(include_leave)]
    fn py_include_leave(&self) -> bool {
        self.include_leave
    }

    #[getter(event_fields)]
    fn py_event_fields(&self) -> Vec<String> {
        self.event_fields.clone()
    }

    #[getter(event_format)]
    fn py_event_format(&self) -> &str {
        &self.event_format
    }

    #[getter(profile_fields)]
    fn py_profile_fields(&self) -> HashSet<String> {
        self.profile_fields.clone()
    }

    #[pyo3(name = "blocks_all_rooms")]
    fn py_blocks_all_rooms(&self) -> bool {
        self.blocks_all_rooms()
    }

    #[pyo3(name = "blocks_all_presence")]
    fn py_blocks_all_presence(&self) -> bool {
        self.blocks_all_presence()
    }

    #[pyo3(name = "blocks_all_global_account_data")]
    fn py_blocks_all_global_account_data(&self) -> bool {
        self.blocks_all_global_account_data()
    }

    #[pyo3(name = "blocks_all_room_ephemeral")]
    fn py_blocks_all_room_ephemeral(&self) -> bool {
        self.blocks_all_room_ephemeral()
    }

    #[pyo3(name = "blocks_all_room_account_data")]
    fn py_blocks_all_room_account_data(&self) -> bool {
        self.blocks_all_room_account_data()
    }

    #[pyo3(name = "blocks_all_room_timeline")]
    fn py_blocks_all_room_timeline(&self) -> bool {
        self.blocks_all_room_timeline()
    }

    /// Whether the timeline filter needs the relation lookup that
    /// `filter_room_timeline` does on top of `check_room_timeline`.
    fn timeline_needs_relation_check(&self) -> bool {
        self.room_timeline_filter.needs_relation_check()
    }

    /// Whether `event` passes the room and timeline filters, relation
    /// check aside.
    #[pyo3(name = "check_room_timeline")]
    fn py_check_room_timeline(&self, event: &Event) -> bool {
        self.check_room_timeline(event)
    }

    /// Whether `event` passes the room and state filters, relation check
    /// aside.
    #[pyo3(name = "check_room_state")]
    fn py_check_room_state(&self, event: &Event) -> bool {
        self.check_room_state(event)
    }

    #[pyo3(name = "check_room_ephemeral")]
    fn py_check_room_ephemeral(&self, event: &Bound<'_, PyDict>) -> PyResult<bool> {
        Ok(self.check_room_ephemeral(&json_from_py(event)?))
    }

    #[pyo3(name = "check_room_account_data")]
    fn py_check_room_account_data(&self, event: &Bound<'_, PyDict>) -> PyResult<bool> {
        Ok(self.check_room_account_data(&json_from_py(event)?))
    }

    #[pyo3(name = "check_presence")]
    fn py_check_presence(&self, user_id: &str) -> bool {
        self.check_presence(user_id)
    }

    #[pyo3(name = "check_global_account_data")]
    fn py_check_global_account_data(&self, event: &Bound<'_, PyDict>) -> PyResult<bool> {
        Ok(self.check_global_account_data(&json_from_py(event)?))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn filter(json: Value) -> Filter {
        Filter::new(Some(&json), FilterFlags::default())
    }

    #[test]
    fn wildcards() {
        assert!(matches_wildcard(Some("m.room.message"), "m.room.*"));
        assert!(matches_wildcard(Some("m.room.message"), "m.room.message"));
        assert!(!matches_wildcard(Some("m.room.message"), "m.room.member"));
        assert!(matches_wildcard(Some("anything"), "*"));
        assert!(!matches_wildcard(None, "*"));
        assert!(!matches_wildcard(None, "m.room.message"));
    }

    #[test]
    fn json_events_match_on_their_fields() {
        let f = filter(json!({
            "types": ["m.typing", "m.receipt"],
            "not_senders": ["@spam:example.com"],
            "rooms": ["!a:x"],
        }));
        assert!(f.check(&json!({"type": "m.typing", "room_id": "!a:x", "sender": "@a:x"})));
        assert!(!f.check(&json!({"type": "m.typing", "room_id": "!b:x", "sender": "@a:x"})));
        assert!(!f.check(&json!({"type": "m.presence", "room_id": "!a:x", "sender": "@a:x"})));
        assert!(!f
            .check(&json!({"type": "m.typing", "room_id": "!a:x", "sender": "@spam:example.com"})));
        // The sender may live in the content.
        assert!(!f.check(&json!({
            "type": "m.typing", "room_id": "!a:x",
            "content": {"user_id": "@spam:example.com"},
        })));
        // Non-dict content counts as empty.
        assert!(f.check(&json!({"type": "m.typing", "room_id": "!a:x", "content": "hi"})));
    }

    #[test]
    fn not_takes_priority_and_empty_lists_block() {
        let f = filter(json!({"types": ["m.*"], "not_types": ["m.room.member"]}));
        assert!(f.check(&json!({"type": "m.room.message"})));
        assert!(!f.check(&json!({"type": "m.room.member"})));
        assert!(!f.check(&json!({"type": "org.other"})));

        let f = filter(json!({"types": []}));
        assert!(f.filters_all_types());
        assert!(!f.check(&json!({"type": "m.room.message"})));
        assert!(filter(json!({"not_rooms": ["*"]})).filters_all_rooms());
        assert!(!filter(json!({})).filters_all_senders());
    }

    #[test]
    fn labels_and_urls() {
        let f = filter(json!({"org.matrix.labels": ["#fun"], "org.matrix.not_labels": ["#work"]}));
        let with = |labels: Value| json!({"type": "m.room.message", "content": {"org.matrix.labels": labels}});
        assert!(f.check(&with(json!(["#fun"]))));
        assert!(!f.check(&with(json!(["#fun", "#work"]))));
        assert!(!f.check(&with(json!([]))));
        assert!(!f.check(&json!({"type": "m.room.message"})));

        let f = filter(json!({"contains_url": true}));
        assert!(f.check(&json!({"type": "t", "content": {"url": "mxc://x"}})));
        assert!(!f.check(&json!({"type": "t", "content": {"url": 5}})));
        assert!(!f.check(&json!({"type": "t"})));
        let f = filter(json!({"contains_url": false}));
        assert!(f.check(&json!({"type": "t"})));
    }

    #[test]
    fn presence_matches_user_and_edu_type_only() {
        let f = filter(json!({"types": ["m.presence"], "senders": ["@a:x"]}));
        assert!(f.check(&Presence { user_id: "@a:x" }));
        assert!(!f.check(&Presence { user_id: "@b:x" }));
        // No wildcard matching for presence types.
        assert!(!filter(json!({"types": ["m.*"]})).check(&Presence { user_id: "@a:x" }));
        assert!(filter(json!({"not_senders": ["@b:x"]})).check(&Presence { user_id: "@a:x" }));
    }

    #[test]
    fn msc_fields_follow_their_flags() {
        let json = json!({
            "org.matrix.msc3773.unread_thread_notifications": true,
            "org.matrix.msc3874.rel_types": ["m.thread"],
        });
        let off = Filter::new(Some(&json), FilterFlags::default());
        assert!(!off.unread_thread_notifications);
        assert!(off.rel_types.is_none());
        let on = Filter::new(
            Some(&json),
            FilterFlags {
                msc3773_enabled: true,
                msc3874_enabled: true,
                include_profile_updates_in_sync: false,
            },
        );
        assert!(on.unread_thread_notifications);
        assert_eq!(on.rel_types.as_deref(), Some(&["m.thread".to_owned()][..]));
    }

    #[test]
    fn collection_reads_each_section() {
        let json = json!({
            "room": {
                "rooms": ["!a:x"],
                "timeline": {"limit": 3, "not_types": ["m.room.member"]},
                "state": {"lazy_load_members": true},
                "ephemeral": {"types": []},
                "include_leave": true,
            },
            "presence": {"senders": []},
            "event_fields": ["type"],
            "event_format": "federation",
            "org.matrix.msc4429.profile_fields": {"ids": ["displayname"]},
        });
        let flags = FilterFlags {
            include_profile_updates_in_sync: true,
            ..FilterFlags::default()
        };
        let c = FilterCollection::new(&json, flags);
        assert_eq!(c.timeline_limit(), 3);
        assert_eq!(c.presence_limit(), 10);
        assert!(c.lazy_load_members());
        assert!(!c.include_redundant_members());
        assert!(c.include_leave);
        assert_eq!(c.event_fields, vec!["type".to_owned()]);
        assert_eq!(c.event_format, "federation");
        assert_eq!(c.profile_fields, HashSet::from(["displayname".to_owned()]));
        assert!(c.blocks_all_room_ephemeral());
        assert!(c.blocks_all_presence());
        assert!(!c.blocks_all_rooms());
        assert!(!c.blocks_all_room_timeline());
        // The room filter applies to every room section.
        assert!(c.check_room_account_data(&json!({"type": "m.tag", "room_id": "!a:x"})));
        assert!(!c.check_room_account_data(&json!({"type": "m.tag", "room_id": "!b:x"})));
        // Without the flag the profile fields are not read.
        let c = FilterCollection::new(&json, FilterFlags::default());
        assert!(c.profile_fields.is_empty());
    }

    #[test]
    fn filter_rooms_applies_both_lists() {
        let f = filter(json!({"rooms": ["!a:x", "!b:x"], "not_rooms": ["!b:x"]}));
        let rooms = f.filter_rooms(["!a:x", "!b:x", "!c:x"]);
        assert_eq!(rooms, HashSet::from(["!a:x"]));
    }

    #[test]
    fn relation_check_only_when_asked() {
        assert!(!filter(json!({})).needs_relation_check());
        assert!(!filter(json!({"related_by_senders": []})).needs_relation_check());
        assert!(filter(json!({"related_by_rel_types": ["m.thread"]})).needs_relation_check());
    }
}
