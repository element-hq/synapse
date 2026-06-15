/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2024 New Vector, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by New Vector Limited]
 *
 */

//! Classes for representing Matrix events.
//!
//! # Overview
//!
//! A Matrix event has a JSON shape that varies by *room version*. The
//! per-room-version shape is captured in the [`formats`] module, where
//! [`FormattedEvent`] is a generic container parametrised by the
//! room-version-specific portion (`EventFormatV1`, `EventFormatV2V3`,
//! `EventFormatV4`, `EventFormatVMSC4242`). See [`formats`] for the layout
//! of the over-the-wire JSON and how the room-version-agnostic fields are
//! split from the version-specific ones.
//!
//! [`Event`] is the `pyclass` exposed to Python. It bundles a fully parsed
//! [`FormattedEvent`] (with the version-specific part type-erased as
//! [`formats::EventFormatEnum`]) together with the pieces of state that
//! live alongside the event JSON in Synapse:
//!
//! - `event_id` — either taken from the event JSON (format v1) or derived
//!   from the canonical-JSON hash (v2+); computed once at construction
//!   time and cached.
//! - `room_version` — a `'static` reference into the global room-version
//!   table, used to drive format-dependent behaviour (e.g. where the
//!   `redacts` field lives, which redaction rules apply).
//! - `internal_metadata` — Synapse-internal flags that are *not* part of
//!   the federated event (outlier status, soft-failure, stream positions,
//!   …). These come from a separate dict at construction time.
//! - `rejected_reason` — `None` for accepted events; otherwise a short
//!   string describing why auth rejected the event.
//!

use std::sync::Arc;

use anyhow::Error;
use pyo3::{
    exceptions::{PyAttributeError, PyKeyError, PyValueError},
    pyclass, pyfunction, pymethods,
    types::{PyAnyMethods, PyDict, PyDictMethods, PyList, PyMapping, PyModule, PyModuleMethods},
    wrap_pyfunction, Bound, IntoPyObject, PyAny, PyResult, Python,
};
use pythonize::{depythonize, pythonize};

use crate::events::{
    constants::event_type::M_ROOM_MEMBER,
    formats::{
        EventFormatEnum, EventFormatV1, EventFormatV2V3, EventFormatV4, EventFormatVMSC4242,
        FormattedEvent,
    },
    signatures::Signatures,
    unsigned::Unsigned,
    utils::redact,
};
use crate::{
    duration::SynapseDuration,
    events::{
        constants::event_field::{HASHES, MSC4354_STICKY, SIGNATURES, UNSIGNED},
        constants::membership_field::MEMBERSHIP,
        constants::redaction_field::REDACTS,
        constants::unsigned_field::{AGE, AGE_TS, REDACTED_BECAUSE},
        internal_metadata::EventInternalMetadata,
        utils::calculate_event_id,
    },
    room_versions::{EventFormatVersions, RoomVersion},
};

pub mod constants;
pub mod filter;
pub mod formats;
pub mod internal_metadata;
pub mod json_object;
pub mod relations;
pub mod serialize;
pub mod signatures;
pub mod unsigned;
pub mod utils;

use json_object::JsonObject;

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Register the `JsonObject` class as a `Mapping` so that `isinstance` works.
    PyMapping::register::<JsonObject>(py)?;

    let child_module = PyModule::new(py, "events")?;
    child_module.add_class::<EventInternalMetadata>()?;
    child_module.add_class::<Signatures>()?;
    child_module.add_class::<Unsigned>()?;
    child_module.add_class::<JsonObject>()?;
    child_module.add_class::<json_object::JsonObjectKeysView>()?;
    child_module.add_class::<json_object::JsonObjectValuesView>()?;
    child_module.add_class::<json_object::JsonObjectItemsView>()?;
    child_module.add_class::<Event>()?;
    child_module.add_class::<relations::BundledAggregations>()?;
    child_module.add_class::<relations::ThreadAggregation>()?;
    child_module.add_class::<serialize::EventFormat>()?;
    child_module.add_class::<serialize::SerializeEventConfig>()?;
    child_module.add_function(wrap_pyfunction!(filter::event_visible_to_server_py, m)?)?;
    child_module.add_function(wrap_pyfunction!(redact_event_py, m)?)?;
    child_module.add_function(wrap_pyfunction!(redact_event_dict, m)?)?;
    child_module.add_function(wrap_pyfunction!(serialize::serialize_events, m)?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import events` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.events", child_module)?;

    Ok(())
}

/// The Rust-side representation of a Matrix event, exposed to Python.
///
/// Wraps a parsed [`FormattedEvent`] together with the per-event state
/// that Synapse tracks outside the event JSON (event ID, internal
/// metadata, rejection reason, and a reference to the room version that
/// produced this event). See the module-level docs for the high-level
/// design.
///
/// `Clone` is shallow (see [`FormattedEvent`]) and lets an `Event` be held by
/// value, e.g. inside [`BundledAggregations`](crate::events::relations::BundledAggregations).
#[pyclass(frozen, weakref, skip_from_py_object)]
#[derive(Clone)]
pub struct Event {
    /// The parsed event JSON.
    parsed_event: FormattedEvent,

    /// The event ID. For format v1 this is read directly from the JSON;
    /// for v2+ it is computed from the canonical-JSON hash at
    /// construction time and cached here.
    event_id: Arc<str>,

    /// The calculated room ID.
    ///
    /// For some room versions, this may be derived, e.g. for create events in
    /// v4.
    room_id: Arc<str>,

    /// Synapse-internal per-event state that lives outside the federated
    /// JSON (e.g. outlier flag, soft-failure, stream positions).
    #[pyo3(get)]
    internal_metadata: EventInternalMetadata,

    /// The room version this event was parsed for.
    #[pyo3(get)]
    room_version: &'static RoomVersion,

    /// `None` for accepted events; otherwise a short reason set by auth
    /// when the event was rejected.
    rejected_reason: Option<Box<str>>,
}

#[pymethods]
impl Event {
    /// Construct an Event from a JSON string, room version, and internal
    /// metadata dict.
    ///
    /// We do no accept a Python dict directly because of the issues with
    /// depythonize and large integers (see [`FormattedEvent`] for details).
    #[new]
    fn new_from_py<'a, 'py>(
        py: Python<'py>,
        event_json: &str,
        room_version: &'a Bound<'py, PyAny>,
        internal_metadata_dict: &'a Bound<'py, PyDict>,
        rejected_reason: Option<String>,
    ) -> PyResult<Self> {
        let room_version: &RoomVersion = {
            let r = room_version.getattr("identifier")?;
            let room_version_str = r.extract::<&str>()?;
            room_version_str
                .parse()
                .map_err(|e| PyValueError::new_err(format!("Unsupported room version: {}", e)))?
        };

        let rejected_reason = rejected_reason.map(String::into_boxed_str);

        // Parse the event JSON into a FormattedEvent, converting any failures to
        // a `ValueError`.
        let parsed_event = event_dict_from_json_str(room_version, event_json).map_err(|err| {
            let new_err = PyValueError::new_err(format!(
                "Failed to parse event for room version {}: {}",
                room_version, err
            ));
            new_err.set_cause(py, Some(PyValueError::new_err(err.to_string())));
            new_err
        })?;

        let internal_metadata = EventInternalMetadata::new(internal_metadata_dict)?;

        let event_id = match &*parsed_event.specific_fields {
            EventFormatEnum::V1(format) => {
                // V1/V2 events have the event_id in the event dict.
                Arc::clone(&format.event_id)
            }
            _ => {
                // Calculate the event ID by hashing the event JSON. This can
                // fail if the event can't be serialized to canonical JSON (e.g.
                // having out-of-range integers), which we report as
                // `ValueError` as it indicates the event is invalid.
                let event_value = serde_json::to_value(&parsed_event).map_err(|err| {
                    PyValueError::new_err(format!("Failed to serialize event: {}", err))
                })?;
                calculate_event_id(&event_value, room_version)
                    .map_err(|err| {
                        PyValueError::new_err(format!("Failed to calculate event_id: {}", err))
                    })?
                    .into()
            }
        };

        let room_id = match &*parsed_event.specific_fields {
            EventFormatEnum::V1(format) => Arc::clone(&format.room_id),
            EventFormatEnum::V2V3(format) => Arc::clone(&format.room_id),
            EventFormatEnum::V4(format) => format
                .room_id(&event_id, &parsed_event.common_fields)
                .map_err(|err| {
                PyValueError::new_err(format!(
                    "Failed to calculate room_id for event {}: {}",
                    event_id, err
                ))
            })?,
            EventFormatEnum::VMSC4242(format) => format
                .room_id(&event_id, &parsed_event.common_fields)
                .map_err(|err| {
                    PyValueError::new_err(format!(
                        "Failed to calculate room_id for event {}: {}",
                        event_id, err
                    ))
                })?,
        };

        Ok(Self {
            parsed_event,

            event_id,
            room_id,
            room_version,
            rejected_reason,
            internal_metadata,
        })
    }

    /// Convert the event to a dictionary suitable for serialisation.
    fn get_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &self.parsed_event)?)
    }

    /// Like `get_dict`, but serializes `unsigned` in a form suitable for
    /// persistence.
    fn get_dict_for_persistence<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let binding = self.get_dict(py)?;
        let dict = binding.cast::<PyDict>()?;

        dict.set_item("unsigned", self.parsed_event.unsigned.for_persistence(py)?)?;

        Ok(binding)
    }

    /// Like [`Event::get_dict`], but serializes `unsigned` in a form suitable
    /// for sending over federation.
    #[pyo3(signature = (time_now = None))]
    fn get_pdu_json<'py>(
        &self,
        py: Python<'py>,
        time_now: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let obj = self.get_dict(py)?;
        let dict = obj.cast::<PyDict>()?;

        // Get or create the unsigned dict
        if let Ok(Some(unsigned)) = dict.get_item(UNSIGNED) {
            let unsigned = unsigned.cast::<PyDict>()?;

            if let Some(time_now) = time_now {
                if let Ok(Some(age_ts)) = unsigned.get_item(AGE_TS) {
                    let age = time_now - age_ts.extract::<i64>()?;
                    unsigned.set_item(AGE, age)?;
                    unsigned.del_item(AGE_TS)?;
                }
            }

            // This may be a frozen event
            unsigned.del_item(REDACTED_BECAUSE).ok();
        }

        Ok(obj)
    }

    /// Like [`Event::get_dict`], except strips fields like `signatures`,
    /// `hashes` and `unsigned` so that the result is suitable as a template for
    /// creating new events. Used in make_{join,leave,knock} flows.
    fn get_templated_pdu_json<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // Use get_dict but strip signatures, unsigned, and hashes — the
        // joining/leaving/knocking server will re-sign and recalculate hashes.
        let obj = self.get_dict(py)?;
        let dict = obj.cast::<PyDict>()?;
        dict.del_item(SIGNATURES).ok();
        dict.del_item(UNSIGNED).ok();
        dict.del_item(HASHES).ok();

        Ok(obj)
    }

    #[getter]
    fn rejected_reason(&self) -> Option<&str> {
        self.rejected_reason.as_deref()
    }

    /// Returns the list of prev event IDs. The order matches the order
    /// specified in the event, though there is no meaning to it.
    fn prev_event_ids(&self) -> Vec<String> {
        match &*self.parsed_event.specific_fields {
            EventFormatEnum::V1(format) => format.prev_event_ids(),
            EventFormatEnum::V2V3(format) => format.prev_events.clone(),
            EventFormatEnum::V4(format) => format.prev_events.clone(),
            EventFormatEnum::VMSC4242(format) => format.prev_events.clone(),
        }
    }

    /// Returns the list of auth event IDs. The order matches the order
    /// specified in the event, though there is no meaning to it.
    fn auth_event_ids(&self) -> PyResult<Vec<String>> {
        match &*self.parsed_event.specific_fields {
            EventFormatEnum::V1(format) => Ok(format.auth_event_ids()),
            EventFormatEnum::V2V3(format) => Ok(format.auth_event_ids()),
            EventFormatEnum::V4(format) => {
                Ok(format.auth_event_ids(&self.parsed_event.common_fields)?)
            }
            EventFormatEnum::VMSC4242(format) => Ok(format.auth_event_ids(self)?),
        }
    }

    #[getter]
    fn membership<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let content = self.content();
        let value = content.get_field(MEMBERSHIP);
        match value {
            Some(value) => Ok(pythonize(py, value)?),
            None => Err(PyKeyError::new_err(MEMBERSHIP)),
        }
    }

    fn is_state(&self) -> bool {
        self.parsed_event.common_fields.state_key.is_some()
    }

    /// Get the state key of this event, or None if it's not a state event.
    fn get_state_key(&self) -> Option<&str> {
        self.parsed_event.common_fields.state_key.as_deref_opt()
    }

    /// The EventFormatVersion implemented by this event.
    #[getter]
    fn format_version(&self) -> i32 {
        self.room_version.event_format
    }

    /// Returns a deep copy of this object, such that modifying the copy will
    /// not affect the original.
    fn deep_copy(&self) -> PyResult<Event> {
        let internal_metadata = self.internal_metadata.deep_copy()?;

        let new_event = Event {
            parsed_event: self.parsed_event.deep_copy(),
            internal_metadata,
            room_version: self.room_version,
            rejected_reason: self.rejected_reason.clone(),
            event_id: self.event_id.clone(),
            room_id: self.room_id.clone(),
        };
        Ok(new_event)
    }

    /// If this event has the `msc4354_sticky` top-level field, returns a
    /// `SynapseDuration` representing the sticky duration. Otherwise returns
    /// `None`.
    fn sticky_duration(&self) -> Option<SynapseDuration> {
        const MAX_DURATION: SynapseDuration = SynapseDuration::from_hours(1);

        let sticky_obj = self
            .parsed_event
            .common_fields
            .other_fields
            .get(MSC4354_STICKY);

        let sticky_obj = match sticky_obj {
            Some(serde_json::Value::Object(obj)) => obj,
            _ => return None,
        };

        // Check for a valid duration field. The MSC requires `duration_ms` to
        // be a non-negative integer. If it's missing or invalid, we treat the
        // event as non-sticky by returning `None`.
        let duration_ms = sticky_obj.get("duration_ms")?.as_u64()?;

        let duration = SynapseDuration::from_milliseconds(duration_ms);

        let duration = std::cmp::min(duration, MAX_DURATION);

        Some(duration)
    }

    fn __str__(&self) -> PyResult<String> {
        self.__repr__()
    }

    /// We want to include some representative fields in the repr for
    /// debugging purposes.
    ///
    /// The end result will look something like:
    /// `<Event event_id=$def:example.com, type=m.room.message>`
    fn __repr__(&self) -> PyResult<String> {
        let mut fields: Vec<(&str, &str)> = Vec::with_capacity(6);

        if let Some(reason) = self.rejected_reason.as_deref() {
            fields.push(("REJECTED", reason));
        };

        fields.push(("event_id", &self.event_id));
        fields.push(("type", self.r#type()));

        if let Some(state_key) = self.get_state_key() {
            fields.push(("state_key", state_key));
        };

        // Include the membership field for membership events.
        if self.r#type() == M_ROOM_MEMBER && self.is_state() {
            let content = &self.parsed_event.common_fields.content;
            let membership_field = content.get_field(MEMBERSHIP).and_then(|m| m.as_str());

            if let Some(membership_str) = membership_field {
                fields.push(("membership", membership_str));
            }
        }

        if self.internal_metadata.is_outlier()? {
            fields.push(("outlier", "true"));
        }

        let fields_str = fields
            .into_iter()
            .map(|(name, value)| format!("{}={}", name, value))
            .collect::<Vec<_>>()
            .join(", ");

        Ok(format!("<Event {}>", fields_str))
    }

    // Below are the methods for interacting with the event as a mapping.
    //
    // These are rarely used, so we take the easy approach of re-serializing the
    // event to a Python dict and then delegating to the standard dict methods.
    // We can't remove these functions as third-party modules may rely on them.

    fn __contains__<'py>(&self, py: Python<'py>, key: &str) -> PyResult<bool> {
        let dict = self.get_dict(py)?;
        dict.contains(key)
    }

    /// This is deprecated in favor of `get`, but we still need to support it
    /// for backwards compatibility with modules. This is therefore not exposed
    /// in the type stubs.
    fn __getitem__<'py>(&self, py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyAny>> {
        let dict = self.get_dict(py)?;
        if dict.contains(key)? {
            dict.get_item(key)
        } else {
            Err(PyKeyError::new_err(key.to_owned()))
        }
    }

    #[pyo3(signature = (key, default=None))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        key: &str,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let dict = self.get_dict(py)?;
        if dict.contains(key)? {
            dict.get_item(key)
        } else {
            Ok(default.into_pyobject(py)?)
        }
    }

    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let dict = self.get_dict(py)?;
        let dict = dict.cast::<PyDict>()?;
        Ok(dict.items())
    }

    fn keys<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let dict = self.get_dict(py)?;
        let dict = dict.cast::<PyDict>()?;
        Ok(dict.keys())
    }

    // Below are the getters for the top-level fields on Matrix events.

    #[getter]
    fn event_id(&self) -> &str {
        &self.event_id
    }

    #[getter]
    fn room_id(&self) -> &str {
        &self.room_id
    }

    #[getter]
    fn signatures(&self) -> Signatures {
        self.parsed_event.signatures.clone()
    }

    #[getter]
    fn content(&self) -> JsonObject {
        self.parsed_event.common_fields.content.clone()
    }

    #[getter]
    fn depth(&self) -> i64 {
        self.parsed_event.common_fields.depth
    }

    #[getter]
    fn hashes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (key, value) in &self.parsed_event.common_fields.hashes {
            dict.set_item(&**key, &**value)?;
        }
        Ok(dict)
    }

    #[getter]
    fn origin_server_ts(&self) -> i64 {
        self.parsed_event.common_fields.origin_server_ts
    }

    #[getter]
    fn sender(&self) -> &str {
        &self.parsed_event.common_fields.sender
    }

    /// Deprecated alias for `sender`. Kept for backwards compatibility with
    /// modules and tests that still read `event.user_id`. This is therefore not
    /// exposed in the type stubs.
    #[getter]
    fn user_id(&self) -> &str {
        &self.parsed_event.common_fields.sender
    }

    #[getter(state_key)]
    // We can't call this `state_key` because that would generate a
    // `get_state_key` method which already exists.
    fn state_key_attr(&self) -> PyResult<&str> {
        let Some(state_key) = self.parsed_event.common_fields.state_key.as_deref_opt() else {
            return Err(PyAttributeError::new_err("state_key"));
        };
        Ok(state_key)
    }

    #[getter]
    fn r#type(&self) -> &str {
        &self.parsed_event.common_fields.type_
    }

    #[getter]
    fn unsigned(&self) -> Unsigned {
        self.parsed_event.unsigned.clone()
    }

    #[getter]
    fn prev_state_events(&self) -> PyResult<Vec<String>> {
        // `prev_state_events` should only be called after validating the event
        // is of a format that supports MSC4242, so we return an AttributeError
        // for formats that don't support it.
        match &*self.parsed_event.specific_fields {
            EventFormatEnum::V1(_) | EventFormatEnum::V2V3(_) | EventFormatEnum::V4(_) => {
                Err(PyAttributeError::new_err("prev_state_events"))
            }
            EventFormatEnum::VMSC4242(format) => Ok(format.prev_state_events.clone()),
        }
    }

    #[getter]
    fn redacts<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let common = &self.parsed_event.common_fields;
        let value = if self.room_version.updated_redaction_rules {
            common.content.get_field(REDACTS)
        } else {
            common.other_fields.get(REDACTS)
        };
        value
            .map(|v| pythonize(py, v).map_err(Into::into))
            .transpose()
    }
}

/// Parses a JSON string into a [`FormattedEvent`] for the given room version.
fn event_dict_from_json_str(
    room_version: &RoomVersion,
    event_json: &str,
) -> Result<FormattedEvent, Error> {
    let formatted_event: FormattedEvent = match room_version.event_format {
        EventFormatVersions::ROOM_V1_V2 => {
            let event_format: FormattedEvent<EventFormatV1> = serde_json::from_str(event_json)?;
            event_format.into()
        }
        EventFormatVersions::ROOM_V3 | EventFormatVersions::ROOM_V4_PLUS => {
            let event_format: FormattedEvent<EventFormatV2V3> = serde_json::from_str(event_json)?;
            event_format.into()
        }
        EventFormatVersions::ROOM_V11_HYDRA_PLUS => {
            let event_format: FormattedEvent<EventFormatV4> = serde_json::from_str(event_json)?;
            event_format.into()
        }
        EventFormatVersions::ROOM_VMSC4242 => {
            let event_format: FormattedEvent<EventFormatVMSC4242> =
                serde_json::from_str(event_json)?;
            event_format.into()
        }
        _ => {
            return Err(anyhow::anyhow!(
                "Unsupported room version: {}",
                room_version
            ));
        }
    };

    formatted_event.validate()?;

    Ok(formatted_event)
}

/// Returns a pruned version of the given event, which removes all keys we don't
/// know about or think could potentially be dodgy.
///
/// Returns the redacted event as a dict.
#[pyfunction(name = "redact_event")]
fn redact_event_py(event: &Event) -> PyResult<Event> {
    let event_value = serde_json::to_value(&event.parsed_event).map_err(|err| {
        PyValueError::new_err(format!("Failed to serialize event for redaction: {}", err))
    })?;

    let redacted_value = redact(&event_value, event.room_version)?;

    // We can't convert from a value into [`Event`] directly, so we round-trip
    // through JSON. See [`FormattedEvent`] for details on why we can't go
    // directly through Python dicts.
    let redacted_event_json = serde_json::to_string(&redacted_value).map_err(|err| {
        PyValueError::new_err(format!("Failed to serialize redacted event: {}", err))
    })?;
    let redacted_formatted_event =
        event_dict_from_json_str(event.room_version, &redacted_event_json).map_err(|err| {
            PyValueError::new_err(format!("Failed to deserialize redacted event: {}", err))
        })?;

    let redacted_event = Event {
        parsed_event: redacted_formatted_event,
        event_id: Arc::clone(&event.event_id),
        room_id: Arc::clone(&event.room_id),
        room_version: event.room_version,
        rejected_reason: event.rejected_reason.clone(),
        internal_metadata: event.internal_metadata.deep_copy()?,
    };

    // Mark event as redacted
    redacted_event.internal_metadata.set_redacted(true)?;

    Ok(redacted_event)
}

/// Returns a pruned version of the given event dict, which removes all keys we
/// don't know about or think could potentially be dodgy.
///
/// Returns the redacted event as a dict.
#[pyfunction(name = "redact_event_dict")]
fn redact_event_dict<'py>(
    py: Python<'py>,
    room_version: &RoomVersion,
    event_dict: &'py Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let event_value = depythonize(event_dict)?;

    let redacted = redact(&event_value, room_version)?;

    let redacted_py = pythonize(py, &redacted)?;

    Ok(redacted_py)
}

#[cfg(test)]
mod tests {

    use super::*;
    use crate::events::{
        constants::event_type::M_ROOM_CREATE,
        formats::{EventFormatV1, EventFormatV2V3, EventFormatV4, EventFormatVMSC4242},
    };

    #[test]
    fn test_basic_v3_roundtrip() {
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@anon-20260225_142731-20:localhost:8800","content":{"room_version":"10","creator":"@anon-20260225_142731-20:localhost:8800"},"depth":1,"room_id":"!qVoJSympOqdUQRUfiC:localhost:8800","state_key":"","origin_server_ts":1772029657149,"hashes":{"sha256":"RIDkn4CrExGMOfRZlHl//1weAro5QC/q2D76YcyAUqk"},"signatures":{"localhost:8800":{"ed25519:a_GMSl":"GU7WmvI2Kd5kLrXKrWpRbUfEiVKGgH0sxQNEpBMMvgF3QhHN25AubVMmIClht5r/c+Iihb1xsq1j5Sw+RGfiDg"}},"unsigned":{"age_ts":1772029657149}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV2V3> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        // Check a couple of fields are as expected as a sanity check.
        assert_eq!(&*event.common_fields.type_, M_ROOM_CREATE);
        assert_eq!(
            &*event.specific_fields.room_id,
            "!qVoJSympOqdUQRUfiC:localhost:8800"
        );

        assert_eq!(event_value, parsed_value);
    }

    #[test]
    fn test_room_id_for_create_event_format_v4() {
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@erikj:jki.re","content":{"room_version":"12","predecessor":{"room_id":"!VuNGkDTdbMOOxSmuDa:jki.re"}},"depth":1,"state_key":"","origin_server_ts":1775568141481,"hashes":{"sha256":"qBX+glsKvogXFrvsEN0eh13pO2kpuE6o/b4yREPtOqw"},"signatures":{"jki.re":{"ed25519:auto":"n/4gHQRagk3r1r24L/7a+oaMMf9cysVfQRYdjpDZcf4ppkVym33rhTW18Vy4zMa1L5nsWLkxsBvbrRRDYUOhBQ"}},"unsigned":{"age_ts":1775568141481}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();

        let event_id = calculate_event_id(&event_value, &RoomVersion::V12).unwrap();

        assert_eq!(
            &*event
                .specific_fields
                .room_id(&event_id, &event.common_fields)
                .unwrap(),
            "!BeXKh925K_M46DwsuJFR0EyBpE1P7CFUDGuWW4xw55Y"
        );
    }

    #[test]
    fn test_basic_v1_roundtrip() {
        let json = r#"{"auth_events":[["$auth1:localhost",{"sha256":"abc"}],["$auth2:localhost",{"sha256":"def"}]],"prev_events":[["$prev1:localhost",{"sha256":"ghi"}]],"type":"m.room.message","sender":"@user:localhost","content":{"body":"hello","msgtype":"m.text"},"depth":5,"room_id":"!room:localhost","event_id":"$event1:localhost","origin_server_ts":1234567890,"hashes":{"sha256":"base64hash"},"signatures":{"localhost":{"ed25519:key":"sig"}},"unsigned":{}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV1> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        // Check a few fields are as expected as a sanity check.
        assert_eq!(&*event.common_fields.type_, "m.room.message");
        assert!(event.common_fields.state_key.is_absent());
        assert_eq!(&*event.specific_fields.room_id, "!room:localhost");
        assert_eq!(&*event.specific_fields.event_id, "$event1:localhost");

        // Check auth/prev event extraction
        let auth_ids = event.specific_fields.auth_event_ids();
        assert_eq!(auth_ids, vec!["$auth1:localhost", "$auth2:localhost"]);

        let prev_ids = event.specific_fields.prev_event_ids();
        assert_eq!(prev_ids, vec!["$prev1:localhost"]);

        assert_eq!(event_value, parsed_value);
    }

    #[test]
    fn test_basic_v4_roundtrip_with_room_id() {
        // A regular (non-create) V4 event has an explicit room_id.
        let json = r#"{"auth_events":["$auth1","$auth2"],"prev_events":["$prev1"],"type":"m.room.message","sender":"@user:localhost","content":{"body":"hello","msgtype":"m.text"},"depth":5,"room_id":"!room:localhost","origin_server_ts":1234567890,"hashes":{"sha256":"base64hash"},"signatures":{"localhost":{"ed25519:key":"sig"}},"unsigned":{}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        // Check a few fields are as expected as a sanity check.
        assert_eq!(&*event.common_fields.type_, "m.room.message");
        assert_eq!(
            event.specific_fields.room_id.as_deref_opt(),
            Some("!room:localhost")
        );
        assert_eq!(
            event.specific_fields.auth_events,
            vec!["$auth1".to_string(), "$auth2".to_string()]
        );
        assert_eq!(
            event.specific_fields.prev_events,
            vec!["$prev1".to_string()]
        );

        assert_eq!(event_value, parsed_value);
    }

    #[test]
    fn test_basic_v4_roundtrip_create_event() {
        // A V4 create event for a v12 room has no room_id field.
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@erikj:jki.re","content":{"room_version":"12"},"depth":1,"state_key":"","origin_server_ts":1775568141481,"hashes":{"sha256":"qBX+glsKvogXFrvsEN0eh13pO2kpuE6o/b4yREPtOqw"},"signatures":{"jki.re":{"ed25519:auto":"sig"}},"unsigned":{}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        // Check a few fields are as expected as a sanity check.
        assert!(event.specific_fields.room_id.is_absent());
        assert_eq!(&*event.common_fields.type_, M_ROOM_CREATE);

        // Create events have no implicit auth events.
        assert!(event
            .specific_fields
            .auth_event_ids(&event.common_fields)
            .unwrap()
            .is_empty());

        assert_eq!(event_value, parsed_value);
    }

    #[test]
    fn test_v4_auth_event_ids_implicit_create() {
        // Non-create events implicitly include the create event (derived from
        // the room ID) in their auth chain.
        let json = r#"{"auth_events":["$auth1"],"prev_events":["$prev1"],"type":"m.room.message","sender":"@user:localhost","content":{"body":"hi","msgtype":"m.text"},"depth":5,"room_id":"!BeXKh925K_M46DwsuJFR0EyBpE1P7CFUDGuWW4xw55Y","origin_server_ts":1234567890,"hashes":{"sha256":"h"},"signatures":{"localhost":{"ed25519:k":"s"}},"unsigned":{}}"#;

        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();

        let auth_ids = event
            .specific_fields
            .auth_event_ids(&event.common_fields)
            .unwrap();
        assert_eq!(
            auth_ids,
            vec![
                "$auth1".to_string(),
                "$BeXKh925K_M46DwsuJFR0EyBpE1P7CFUDGuWW4xw55Y".to_string(),
            ]
        );
    }

    #[test]
    fn test_v4_validate_rejects_missing_room_id_for_non_create() {
        // A v12 non-create event without a room_id must fail validation.
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.message","sender":"@u:l","content":{},"depth":2,"state_key":"","origin_server_ts":1,"hashes":{"sha256":"h"},"signatures":{"l":{"ed25519:k":"s"}},"unsigned":{}}"#;
        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();
        assert!(event
            .specific_fields
            .validate(&event.common_fields)
            .is_err());
    }

    #[test]
    fn test_v4_validate_accepts_create_without_room_id() {
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@u:l","content":{"room_version":"12"},"depth":1,"state_key":"","origin_server_ts":1,"hashes":{"sha256":"h"},"signatures":{"l":{"ed25519:k":"s"}},"unsigned":{}}"#;
        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();
        event
            .specific_fields
            .validate(&event.common_fields)
            .unwrap();
    }

    #[test]
    fn test_basic_vmsc4242_roundtrip() {
        // VMSC4242 introduces a `prev_state_events` field on top of V4.
        let json = r#"{"auth_events":["$auth1"],"prev_events":["$prev1"],"prev_state_events":["$pstate1","$pstate2"],"type":"m.room.member","sender":"@user:localhost","content":{"membership":"join"},"depth":5,"room_id":"!room:localhost","state_key":"@user:localhost","origin_server_ts":1234567890,"hashes":{"sha256":"h"},"signatures":{"localhost":{"ed25519:k":"s"}},"unsigned":{}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatVMSC4242> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        assert_eq!(
            event.specific_fields.prev_state_events,
            vec!["$pstate1".to_string(), "$pstate2".to_string()]
        );
        assert_eq!(
            event.specific_fields.room_id.as_deref_opt(),
            Some("!room:localhost")
        );
        assert_eq!(
            event.common_fields.state_key.as_deref_opt(),
            Some("@user:localhost")
        );

        assert_eq!(event_value, parsed_value);
    }

    #[test]
    fn test_vmsc4242_room_id_for_create_event() {
        let json = r#"{"auth_events":[],"prev_events":[],"prev_state_events":[],"type":"m.room.create","sender":"@erikj:jki.re","content":{"room_version":"12","predecessor":{"room_id":"!VuNGkDTdbMOOxSmuDa:jki.re"}},"depth":1,"state_key":"","origin_server_ts":1775568141481,"hashes":{"sha256":"qBX+glsKvogXFrvsEN0eh13pO2kpuE6o/b4yREPtOqw"},"signatures":{"jki.re":{"ed25519:auto":"n/4gHQRagk3r1r24L/7a+oaMMf9cysVfQRYdjpDZcf4ppkVym33rhTW18Vy4zMa1L5nsWLkxsBvbrRRDYUOhBQ"}},"unsigned":{"age_ts":1775568141481}}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatVMSC4242> = serde_json::from_str(json).unwrap();

        // The event_id calculation is independent of the `prev_state_events`
        // field not being present in V4, so the same event_id derivation works.
        let event_id = calculate_event_id(&event_value, &RoomVersion::V12).unwrap();

        assert_eq!(
            &*event
                .specific_fields
                .room_id(&event_id, &event.common_fields)
                .unwrap(),
            "!BeXKh925K_M46DwsuJFR0EyBpE1P7CFUDGuWW4xw55Y"
        );
    }

    #[test]
    fn test_event_format_enum_untagged_roundtrip() {
        // The untagged EventFormatEnum serialization/deserialization is
        // driven by fields, so serializing any variant must match the
        // original JSON exactly.
        let v2v3_json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@a:b","content":{},"depth":1,"room_id":"!r:b","state_key":"","origin_server_ts":1,"hashes":{"sha256":"h"},"signatures":{"b":{"ed25519:k":"s"}},"unsigned":{}}"#;
        let v2v3_value: serde_json::Value = serde_json::from_str(v2v3_json).unwrap();
        let v2v3_container: FormattedEvent<EventFormatV2V3> =
            serde_json::from_str(v2v3_json).unwrap();
        assert_eq!(serde_json::to_value(&v2v3_container).unwrap(), v2v3_value);
        assert_eq!(
            serde_json::to_value(v2v3_container.into_general()).unwrap(),
            v2v3_value
        );

        let v4_json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.create","sender":"@a:b","content":{"room_version":"12"},"depth":1,"state_key":"","origin_server_ts":1,"hashes":{"sha256":"h"},"signatures":{"b":{"ed25519:k":"s"}},"unsigned":{}}"#;
        let v4_value: serde_json::Value = serde_json::from_str(v4_json).unwrap();
        let v4_container: FormattedEvent<EventFormatV4> = serde_json::from_str(v4_json).unwrap();
        assert_eq!(serde_json::to_value(&v4_container).unwrap(), v4_value);
        assert_eq!(
            serde_json::to_value(v4_container.into_general()).unwrap(),
            v4_value
        );
    }

    #[test]
    fn test_unknown_top_level_fields_preserved_roundtrip() {
        // Extra top-level fields (e.g. unknown or experimental) are captured
        // via `other_fields` and must round-trip losslessly.
        let json = r#"{"auth_events":[],"prev_events":[],"type":"m.room.message","sender":"@a:b","content":{"body":"hi","msgtype":"m.text"},"depth":1,"room_id":"!r:b","origin_server_ts":1,"hashes":{"sha256":"h"},"signatures":{"b":{"ed25519:k":"s"}},"unsigned":{},"msc4354_sticky":{"duration_ms":5000},"some_unknown_field":"some_value"}"#;
        let event_value: serde_json::Value = serde_json::from_str(json).unwrap();

        let event: FormattedEvent<EventFormatV4> = serde_json::from_str(json).unwrap();
        let parsed_value = serde_json::to_value(&event).unwrap();

        assert!(event
            .common_fields
            .other_fields
            .contains_key(MSC4354_STICKY));
        assert!(event
            .common_fields
            .other_fields
            .contains_key("some_unknown_field"));

        assert_eq!(event_value, parsed_value);
    }
}
