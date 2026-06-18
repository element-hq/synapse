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

//! The synchronous core of client event serialization.
//!
//! This module turns events from their internal/federation shape into the JSON
//! shape sent to clients: applying the requested [`EventFormat`], folding in
//! redactions, module-callback unsigned additions, bundled aggregations
//! (references, edits and thread summaries) and field filtering.
//!
//! It operates purely on already-fetched data — all DB/IO (fetching redactions,
//! running module callbacks, resolving the admin/MSC4354 config) is performed
//! up front by the Python caller and passed in.
//!
//! The entry point is [`serialize_events`], which reads the Python inputs (the
//! redaction map, bundled aggregations and module-callback additions) once per
//! batch and then serializes each event — recursing entirely in Rust into
//! redactions and bundled aggregations via [`serialize_event`].

use std::collections::HashMap;

use pyo3::{
    exceptions::PyValueError, pyclass, pyfunction, pymethods, Bound, IntoPyObject, PyAny, PyResult,
    Python,
};
use pythonize::pythonize;
use serde_json::{Map, Number, Value};

use crate::{
    events::{
        constants::{
            event_field::{CONTENT, EVENT_ID, ROOM_ID, SENDER, UNSIGNED},
            event_type::{M_ROOM_CREATE, M_ROOM_REDACTION},
            redaction_field::REDACTS,
            relation_type, unsigned_field,
        },
        json_object::JsonObject,
        relations::BundledAggregations,
        Event,
    },
    types::Requester,
};

/// The user_id field copied from `sender` by the v1 client format.
const USER_ID: &str = "user_id";

/// Keys dropped by the v2 client event format.
const V2_DROP_KEYS: [&str; 7] = [
    "auth_events",
    "prev_events",
    "hashes",
    "signatures",
    "depth",
    "origin",
    "prev_state",
];

/// Keys copied from `unsigned` to the top level by the v1 client event format.
const V1_COPY_KEYS: [&str; 6] = [
    "age",
    "redacted_because",
    "replaces_state",
    "prev_content",
    "invite_room_state",
    "knock_room_state",
];

/// The format used to convert an event from its federation shape to the shape
/// sent to clients.
#[pyclass(eq, eq_int, frozen, from_py_object)]
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum EventFormat {
    /// Return the event dict unchanged (federation format).
    Raw,
    /// The legacy `/events`-style client format.
    ClientV1,
    /// The `/sync`-style client format.
    ClientV2,
    /// Like `ClientV2`, but also strips `room_id`.
    ClientV2WithoutRoomId,
}

/// Configuration for serializing an event for clients.
///
/// The output shape is chosen by [`EventFormat`]. The field `requester`, when
/// set, controls whether sender-only fields (such as the transaction ID) are
/// included.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct SerializeEventConfig {
    /// Whether to apply the client event format transform (v1/v2/raw). When
    /// `false`, the federation-format event is returned as-is.
    as_client_event: bool,
    /// Which client event format variant to apply (only used when
    /// `as_client_event` is `true`).
    event_format: EventFormat,
    /// The entity requesting the event. Used to gate sender-only fields such as
    /// `transaction_id` and `delay_id`.
    requester: Option<Requester>,
    /// If set, only include these field paths in the output. An empty list
    /// returns an empty event; `None` returns all fields.
    ///
    /// The fields can be "dotted" fields, e.g. `content.body`.
    event_field_allowlist: Option<Vec<String>>,
    /// Whether to include `invite_room_state` / `knock_room_state` in
    /// `unsigned`. These are stripped by default and only included for specific
    /// endpoints (e.g. `/sync` invite/knock handling).
    include_stripped_room_state: bool,
    /// When `true`, add server-admin-only metadata to `unsigned`
    /// (`io.element.synapse.soft_failed`,
    /// `io.element.synapse.policy_server_spammy`).
    include_admin_metadata: bool,
    /// Whether MSC4354 (sticky events) is enabled. When `true`, the remaining
    /// stickiness TTL is computed and added to `unsigned`.
    msc4354_enabled: bool,
}

#[pymethods]
impl SerializeEventConfig {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        *,
        as_client_event = true,
        event_format = EventFormat::ClientV1,
        requester = None,
        event_field_allowlist = None,
        include_stripped_room_state = false,
        include_admin_metadata = false,
        msc4354_enabled = false,
    ))]
    fn new(
        as_client_event: bool,
        event_format: EventFormat,
        requester: Option<Bound<'_, Requester>>,
        event_field_allowlist: Option<Vec<String>>,
        include_stripped_room_state: bool,
        include_admin_metadata: bool,
        msc4354_enabled: bool,
    ) -> PyResult<Self> {
        let requester = requester.map(|r| r.get().clone());

        Ok(Self {
            as_client_event,
            event_format,
            requester,
            event_field_allowlist,
            include_stripped_room_state,
            include_admin_metadata,
            msc4354_enabled,
        })
    }

    #[getter]
    fn as_client_event(&self) -> bool {
        self.as_client_event
    }

    #[getter]
    fn event_format(&self) -> EventFormat {
        self.event_format
    }

    #[getter]
    fn requester<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, Requester>>> {
        self.requester
            .as_ref()
            .map(|r| r.clone().into_pyobject(py))
            .transpose()
    }

    #[getter]
    fn event_field_allowlist(&self) -> Option<Vec<String>> {
        self.event_field_allowlist.clone()
    }

    #[getter]
    fn include_stripped_room_state(&self) -> bool {
        self.include_stripped_room_state
    }

    #[getter]
    fn include_admin_metadata(&self) -> bool {
        self.include_admin_metadata
    }

    #[getter]
    fn msc4354_enabled(&self) -> bool {
        self.msc4354_enabled
    }

    /// Returns a copy of this config with `include_admin_metadata` enabled.
    fn for_admin(&self) -> Self {
        Self {
            include_admin_metadata: true,
            ..self.clone()
        }
    }

    /// Returns a copy of this config with `msc4354_enabled` set as given.
    #[pyo3(signature = (enabled = true))]
    fn with_msc4354(&self, enabled: bool) -> Self {
        Self {
            msc4354_enabled: enabled,
            ..self.clone()
        }
    }
}

/// Synchronously serialize a batch of events for clients.
///
/// `events` is a list of `(event, membership)` pairs, where `event` is a
/// `FilteredEvent.event` and `membership` the corresponding
/// `FilteredEvent.membership`. All DB/IO must already have been performed by the
/// Python caller: `redaction_map` maps redaction event IDs to events,
/// `unsigned_additions` maps event IDs to module-callback unsigned fields, and
/// `bundle_aggregations` maps event IDs to their bundled aggregations.
///
/// These three maps are shared across the whole batch, so they are read out of
/// Python once and then reused for every event.
#[pyfunction]
#[pyo3(signature = (
    events,
    time_now_ms,
    config,
    *,
    bundle_aggregations = None,
    redaction_map = None,
    unsigned_additions = None,
))]
pub fn serialize_events<'py>(
    py: Python<'py>,
    events: Vec<(Bound<'py, Event>, Option<String>)>,
    time_now_ms: i64,
    config: &SerializeEventConfig,
    bundle_aggregations: Option<HashMap<String, Bound<'py, BundledAggregations>>>,
    redaction_map: Option<HashMap<String, Bound<'py, Event>>>,
    unsigned_additions: Option<HashMap<String, JsonObject>>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let redaction_map = redaction_map.unwrap_or_default();
    let unsigned_additions = unsigned_additions.unwrap_or_default();

    events
        .iter()
        .map(|(event, membership)| {
            let serialized = serialize_event(
                event.get(),
                time_now_ms,
                config,
                membership.as_deref(),
                bundle_aggregations.as_ref(),
                &redaction_map,
                &unsigned_additions,
            )?;
            Ok(pythonize(py, &Value::Object(serialized))?)
        })
        .collect()
}

/// The recursive core: serialize a single event, fold in its redaction,
/// module-callback additions and field filtering, then recurse into any
/// bundled aggregations.
#[allow(clippy::too_many_arguments)]
fn serialize_event(
    event: &Event,
    time_now_ms: i64,
    config: &SerializeEventConfig,
    membership: Option<&str>,
    bundle_aggregations: Option<&HashMap<String, Bound<'_, BundledAggregations>>>,
    redaction_map: &HashMap<String, Bound<'_, Event>>,
    unsigned_additions: &HashMap<String, JsonObject>,
) -> PyResult<Map<String, Value>> {
    let mut serialized = serialize_event_value(event, time_now_ms, config, membership)?;

    // If the event was redacted, include the (pre-fetched) redaction event in
    // the serialized event's unsigned section.
    if let Some(redacted_by) = event.internal_metadata.redacted_by()? {
        unsigned_mut(&mut serialized).insert(
            unsigned_field::REDACTED_BY.to_owned(),
            Value::String(redacted_by.clone()),
        );

        if let Some(redaction_event) = redaction_map.get(&redacted_by) {
            let serialized_redaction = Value::Object(serialize_event_value(
                redaction_event.get(),
                time_now_ms,
                config,
                None,
            )?);
            unsigned_mut(&mut serialized).insert(
                unsigned_field::REDACTED_BECAUSE.to_owned(),
                serialized_redaction.clone(),
            );
            // The v1 client format (apply_event_format) copies redacted_because
            // up to the top level, but since we add it after that runs, do it
            // here too.
            if config.as_client_event && config.event_format == EventFormat::ClientV1 {
                serialized.insert(
                    unsigned_field::REDACTED_BECAUSE.to_owned(),
                    serialized_redaction,
                );
            }
        }
    }

    // Merge in the module-callback additions. Start from a copy of the additions
    // and overlay the event's own unsigned on top, so modules can't clobber
    // existing fields.
    if let Some(adds) = unsigned_additions.get(event.event_id()) {
        let unsigned = unsigned_mut(&mut serialized);
        for (key, value) in adds {
            // Don't let modules clobber existing unsigned fields.
            if let serde_json::map::Entry::Vacant(entry) = unsigned.entry(&**key) {
                entry.insert(value.clone());
            }
        }
    }

    // Only include fields that the client has requested.
    if let Some(fields) = &config.event_field_allowlist {
        if !fields.is_empty() {
            serialized = only_fields(&serialized, fields);
        }
    }

    // Inject any bundled aggregations. Note this happens after field filtering;
    // aggregations are always returned.
    if let Some(bundles) = bundle_aggregations {
        if let Some(aggregation) = bundles.get(event.event_id()) {
            inject_bundled_aggregations(
                time_now_ms,
                config,
                aggregation.get(),
                &mut serialized,
                bundle_aggregations,
                redaction_map,
                unsigned_additions,
            )?;
        }
    }

    Ok(serialized)
}

/// Inject an event's bundled aggregations (references, edit, thread summary)
/// into the `m.relations` section of its serialized `unsigned`.
#[allow(clippy::too_many_arguments)]
fn inject_bundled_aggregations(
    time_now_ms: i64,
    config: &SerializeEventConfig,
    aggregation: &BundledAggregations,
    serialized_event: &mut Map<String, Value>,
    bundle_aggregations: Option<&HashMap<String, Bound<'_, BundledAggregations>>>,
    redaction_map: &HashMap<String, Bound<'_, Event>>,
    unsigned_additions: &HashMap<String, JsonObject>,
) -> PyResult<()> {
    let mut serialized_aggregations = Map::new();

    if let Some(references) = &aggregation.references {
        if !references.is_empty() {
            serialized_aggregations.insert(
                relation_type::REFERENCE.to_owned(),
                Value::Object(
                    references
                        .iter()
                        .map(|(k, v)| (k.clone().into_string(), v.clone()))
                        .collect(),
                ),
            );
        }
    }

    if let Some(replace) = &aggregation.replace {
        // Bundle the *whole* edit event (serialized without its own bundled
        // aggregations). The spec (v1.5) only requires event_id/origin_server_ts/
        // sender, but per MSC3925 we include the full edit.
        // https://spec.matrix.org/v1.5/client-server-api/#server-side-aggregation-of-mreplace-relationships
        let serialized = serialize_event(
            replace,
            time_now_ms,
            config,
            None,
            None,
            redaction_map,
            unsigned_additions,
        )?;
        serialized_aggregations
            .insert(relation_type::REPLACE.to_owned(), Value::Object(serialized));
    }

    if let Some(thread) = &aggregation.thread {
        // The thread's latest event is serialized with the same bundle map, so
        // it may recurse further.
        let serialized_latest = serialize_event(
            &thread.latest_event,
            time_now_ms,
            config,
            None,
            bundle_aggregations,
            redaction_map,
            unsigned_additions,
        )?;

        let mut thread_summary = Map::new();
        thread_summary.insert("latest_event".to_owned(), Value::Object(serialized_latest));
        thread_summary.insert(
            "count".to_owned(),
            Value::Number(Number::from(thread.count)),
        );
        thread_summary.insert(
            "current_user_participated".to_owned(),
            Value::Bool(thread.current_user_participated),
        );
        serialized_aggregations.insert(
            relation_type::THREAD.to_owned(),
            Value::Object(thread_summary),
        );
    }

    if !serialized_aggregations.is_empty() {
        let unsigned = unsigned_mut(serialized_event);
        let relations = object_entry_mut(unsigned, unsigned_field::M_RELATIONS);
        for (key, value) in serialized_aggregations {
            relations.insert(key, value);
        }
    }

    Ok(())
}

/// Serialize a single event to its client JSON shape, without recursing into
/// redactions or bundled aggregations (those are handled by the caller).
fn serialize_event_value(
    event: &Event,
    time_now_ms: i64,
    config: &SerializeEventConfig,
    membership: Option<&str>,
) -> PyResult<Map<String, Value>> {
    let mut d: Map<String, Value> = match serde_json::to_value(&event.parsed_event) {
        Ok(Value::Object(map)) => map,
        Ok(_) => {
            return Err(PyValueError::new_err(
                "event did not serialize to a JSON object",
            ))
        }
        Err(err) => {
            return Err(PyValueError::new_err(format!(
                "Failed to serialize event: {err}"
            )))
        }
    };

    // Always include the event_id field, for room version v3+ these aren't in
    // the event JSON.
    d.insert(
        EVENT_ID.to_owned(),
        Value::String(event.event_id().to_owned()),
    );

    // age_ts -> age
    if let Some(Value::Object(unsigned)) = d.get_mut(UNSIGNED) {
        if let Some(age_ts) = unsigned.get(unsigned_field::AGE_TS).and_then(Value::as_i64) {
            unsigned.insert(
                unsigned_field::AGE.to_owned(),
                Value::Number(Number::from(time_now_ms - age_ts)),
            );
            unsigned.remove(unsigned_field::AGE_TS);
        }
    }

    // Include the transaction_id / delay_id in the unsigned section if the event
    // was sent by the same session (or, where appropriate, the same sender) as
    // the one requesting the event.
    if let Some(requester) = &config.requester {
        if requester.user_id == event.sender() {
            if let Some(txn_id) = event.internal_metadata.txn_id()? {
                if let Some(event_device_id) = event.internal_metadata.device_id()? {
                    if Some(event_device_id.as_str()) == requester.device_id.as_deref() {
                        unsigned_mut(&mut d).insert(
                            unsigned_field::TRANSACTION_ID.to_owned(),
                            Value::String(txn_id),
                        );
                    }
                } else {
                    // No device ID is stored for some events: old events, and
                    // those created by appservices, guests, or with admin-API
                    // tokens. For those, fall back to the access token: only
                    // include the transaction ID if the event was sent from the
                    // same token (or for guests/appservices, which we can't
                    // check, so assume the same session).
                    let event_token_id = event.internal_metadata.token_id()?;
                    let token_matches = event_token_id.is_some()
                        && requester.access_token_id.is_some()
                        && event_token_id == requester.access_token_id;
                    if token_matches || requester.is_guest || requester.app_service_id.is_some() {
                        unsigned_mut(&mut d).insert(
                            unsigned_field::TRANSACTION_ID.to_owned(),
                            Value::String(txn_id),
                        );
                    }
                }
            }

            if let Some(delay_id) = event.internal_metadata.delay_id()? {
                unsigned_mut(&mut d)
                    .insert(unsigned_field::DELAY_ID.to_owned(), Value::String(delay_id));
            }
        }
    }

    // Strip invite/knock room state unless requested.
    if !config.include_stripped_room_state {
        if let Some(Value::Object(unsigned)) = d.get_mut(UNSIGNED) {
            unsigned.remove(unsigned_field::INVITE_ROOM_STATE);
            unsigned.remove(unsigned_field::KNOCK_ROOM_STATE);
        }
    }

    if config.as_client_event {
        apply_event_format(config.event_format, &mut d);
    }

    // Ensure the room_id field is set for create events in MSC4291 rooms.
    if event.r#type() == M_ROOM_CREATE && event.room_version.msc4291_room_ids_as_hashes {
        d.insert(
            ROOM_ID.to_owned(),
            Value::String(event.room_id().to_owned()),
        );
    }

    // A redaction stores the redacted event ID in different places depending
    // on the room version (top-level `redacts` vs `content.redacts`). It's
    // already in the version-correct place; copy it to the *other* one too,
    // for forwards/backwards-compatibility with clients.
    if event.r#type() == M_ROOM_REDACTION {
        let common = &event.parsed_event.common_fields;
        let redacts = if event.room_version.updated_redaction_rules {
            common.content.get_field(REDACTS)
        } else {
            common.other_fields.get(REDACTS)
        };
        // Skip a present-but-null value: the Python `e.redacts` property
        // surfaced JSON null as `None`, and the old code guarded with
        // `e.redacts is not None`.
        if let Some(redacts) = redacts.filter(|v| !v.is_null()) {
            let redacts = redacts.clone();
            if event.room_version.updated_redaction_rules {
                d.insert(REDACTS.to_owned(), redacts);
            } else {
                object_entry_mut(&mut d, CONTENT).insert(REDACTS.to_owned(), redacts);
            }
        }
    }

    if config.include_admin_metadata {
        if event.internal_metadata.soft_failed()? {
            unsigned_mut(&mut d).insert(unsigned_field::SOFT_FAILED.to_owned(), Value::Bool(true));
        }
        if event.internal_metadata.policy_server_spammy()? {
            unsigned_mut(&mut d).insert(
                unsigned_field::POLICY_SERVER_SPAMMY.to_owned(),
                Value::Bool(true),
            );
        }
    }

    if config.msc4354_enabled {
        if let Some(sticky_duration) = event.sticky_duration() {
            // min() ensures the origin server can't claim a time in the future
            // to exceed the stickiness duration limit.
            let expires_at = std::cmp::min(event.origin_server_ts(), time_now_ms)
                + sticky_duration.as_millis() as i64;
            if expires_at > time_now_ms {
                unsigned_mut(&mut d).insert(
                    unsigned_field::STICKY_TTL.to_owned(),
                    Value::Number(Number::from(expires_at - time_now_ms)),
                );
            }
        }
    }

    if let Some(membership) = membership {
        unsigned_mut(&mut d).insert(
            unsigned_field::MEMBERSHIP.to_owned(),
            Value::String(membership.to_owned()),
        );
    }

    Ok(d)
}

/// Apply the client event format transform in place.
fn apply_event_format(format: EventFormat, d: &mut Map<String, Value>) {
    match format {
        EventFormat::Raw => {}
        EventFormat::ClientV2 => format_for_client_v2(d),
        EventFormat::ClientV2WithoutRoomId => {
            format_for_client_v2(d);
            d.remove(ROOM_ID);
        }
        EventFormat::ClientV1 => {
            format_for_client_v2(d);

            let sender = d.get(SENDER).filter(|v| !v.is_null()).cloned();
            if let Some(sender) = sender {
                d.insert(USER_ID.to_owned(), sender);
            }

            let mut to_copy = Vec::new();
            if let Some(Value::Object(unsigned)) = d.get(UNSIGNED) {
                for key in V1_COPY_KEYS {
                    if let Some(value) = unsigned.get(key) {
                        to_copy.push((key.to_owned(), value.clone()));
                    }
                }
            }
            for (key, value) in to_copy {
                d.insert(key, value);
            }
        }
    }
}

fn format_for_client_v2(d: &mut Map<String, Value>) {
    for key in V2_DROP_KEYS {
        d.remove(key);
    }
}

/// Return a mutable reference to `map["unsigned"]`, creating it as an empty
/// object if it is missing or not an object.
fn unsigned_mut(map: &mut Map<String, Value>) -> &mut Map<String, Value> {
    object_entry_mut(map, UNSIGNED)
}

/// Return a mutable reference to `map[key]`, creating it as an empty object if
/// missing or not an object.
fn object_entry_mut<'a>(map: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    let entry = map
        .entry(key.to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    // The key may have already held a non-object value; replace it so the
    // caller always gets an object back.
    if !entry.is_object() {
        *entry = Value::Object(Map::new());
    }
    entry
        .as_object_mut()
        .expect("just ensured the entry is an object")
}

/// Return a new map containing only the given (possibly dotted) field paths,
/// implementing the `event_field_allowlist` client filter.
fn only_fields(dictionary: &Map<String, Value>, fields: &[String]) -> Map<String, Value> {
    let mut output = Map::new();
    for field in fields {
        copy_field(dictionary, &mut output, &split_field(field));
    }
    output
}

/// Copy a single (possibly nested) field path from `src` into `dst`, creating
/// intermediate objects in `dst` as needed. A missing path is a no-op.
fn copy_field(src: &Map<String, Value>, dst: &mut Map<String, Value>, field: &[String]) {
    if field.is_empty() {
        return;
    }
    if field.len() == 1 {
        if let Some(value) = src.get(&field[0]) {
            dst.insert(field[0].clone(), value.clone());
        }
        return;
    }

    let (key_to_move, parents) = field.split_last().expect("field is non-empty");

    // Drill down into `src`.
    let mut sub = src;
    for parent in parents {
        match sub.get(parent) {
            Some(Value::Object(obj)) => sub = obj,
            _ => return,
        }
    }

    let Some(value) = sub.get(key_to_move) else {
        return;
    };
    let value = value.clone();

    // Build the nested objects in `dst` as required.
    let mut out = dst;
    for parent in parents {
        out = object_entry_mut(out, parent);
    }
    out.insert(key_to_move.clone(), value);
}

/// Split a dotted field path into its components, splitting on unescaped dots
/// and removing the escaping. A literal `.` or `\` in a key is escaped with `\`.
fn split_field(field: &str) -> Vec<String> {
    let bytes = field.as_bytes();
    let mut result = Vec::new();
    let mut prev_start = 0;

    for (i, &b) in bytes.iter().enumerate() {
        if b != b'.' {
            continue;
        }
        // Count the run of backslashes immediately preceding the dot. The dot is
        // escaped iff that count is odd.
        let mut backslashes = 0;
        let mut j = i;
        while j > 0 && bytes[j - 1] == b'\\' {
            backslashes += 1;
            j -= 1;
        }
        if backslashes % 2 == 0 {
            result.push(unescape(&field[prev_start..i]));
            prev_start = i + 1;
        }
    }

    result.push(unescape(&field[prev_start..]));
    result
}

/// Remove field-path escaping: `\\` and `\.` collapse to the second character;
/// any other `\x` is left as-is.
fn unescape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.peek() {
                Some('\\') | Some('.') => {
                    out.push(*chars.peek().expect("just peeked"));
                    chars.next();
                }
                _ => out.push('\\'),
            }
        } else {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::split_field;

    #[test]
    fn test_split_field() {
        // Ported from the Python `SplitFieldTestCase` that previously lived in
        // tests/events/test_utils.py (removed alongside `_split_field`).
        let cases: &[(&str, &[&str])] = &[
            // A field with no dots.
            ("m", &["m"]),
            // Simple dotted fields.
            ("m.foo", &["m", "foo"]),
            ("m.foo.bar", &["m", "foo", "bar"]),
            // Backslash is used as an escape character.
            (r"m\.foo", &["m.foo"]),
            (r"m\\.foo", &["m\\", "foo"]),
            (r"m\\\.foo", &[r"m\.foo"]),
            (r"m\\\\.foo", &["m\\\\", "foo"]),
            (r"m\foo", &[r"m\foo"]),
            (r"m\\foo", &[r"m\foo"]),
            (r"m\\\foo", &[r"m\\foo"]),
            (r"m\\\\foo", &[r"m\\foo"]),
            // Ensure that escapes at the end don't cause issues.
            ("m.foo\\", &["m", "foo\\"]),
            (r"m.foo\.", &["m", "foo."]),
            (r"m.foo\\.", &["m", "foo\\", ""]),
            (r"m.foo\\\.", &["m", r"foo\."]),
            // Empty parts (corresponding to empty-string properties) are allowed.
            (".m", &["", "m"]),
            ("..m", &["", "", "m"]),
            ("m.", &["m", ""]),
            ("m..", &["m", "", ""]),
            ("m..foo", &["m", "", "foo"]),
            // Invalid escape sequences are left alone.
            (r"\m", &[r"\m"]),
        ];

        for (input, expected) in cases {
            let expected: Vec<String> = expected.iter().map(|s| s.to_string()).collect();
            assert_eq!(split_field(input), expected, "split_field({input:?})");
        }
    }
}
