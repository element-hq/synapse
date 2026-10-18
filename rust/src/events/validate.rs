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

//! Fast checks for `synapse.events.validator` and `synapse.event_auth`.
//!
//! Each check answers "certainly valid" or "not sure". Python runs its own
//! check only when the answer is "not sure", so it still decides every
//! rejection and its error message. A check here must therefore never call
//! valid something the Python check rejects.

use pyo3::{exceptions::PyValueError, prelude::*};
use serde_json::{Map, Value};

use super::{json_object::JsonObject, Event};
use crate::canonical_json::{
    to_vec_canonical, CanonicalizationOptions, MAX_VALID_INTEGER, MIN_VALID_INTEGER,
};

/// The power levels fields `POWER_LEVELS_SCHEMA` requires to be integers.
const INT_FIELDS: [&str; 7] = [
    "ban",
    "events_default",
    "invite",
    "kick",
    "redact",
    "state_default",
    "users_default",
];

/// The power levels fields `POWER_LEVELS_SCHEMA` requires to be objects
/// whose values are integers.
const OBJECT_OF_INTS_FIELDS: [&str; 3] = ["events", "notifications", "users"];

/// Whether `value` is an integer in the canonical JSON range.
///
/// Numbers keep the text they were parsed from, so `1.0` or `1e3` are not
/// `i64`s here and count as "not sure". Python decides those.
fn is_canonical_int(value: &Value) -> bool {
    match value {
        Value::Number(number) => number
            .as_i64()
            .is_some_and(|int| (MIN_VALID_INTEGER..=MAX_VALID_INTEGER).contains(&int)),
        _ => false,
    }
}

/// Whether every number in `value` is an integer in the canonical JSON
/// range.
fn is_canonical_value(value: &Value) -> bool {
    match value {
        Value::Number(_) => is_canonical_int(value),
        Value::Array(values) => values.iter().all(is_canonical_value),
        Value::Object(map) => map.values().all(is_canonical_value),
        Value::Null | Value::Bool(_) | Value::String(_) => true,
    }
}

/// Whether every number in `value` is an integer written as Python
/// writes one: an optional minus sign, then digits without leading zeros.
/// `-0` is excluded, as Python prints it as `0`.
fn has_plain_integers_only(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            let text = number.to_string();
            let digits = text.strip_prefix('-').unwrap_or(&text);
            !digits.is_empty()
                && digits.bytes().all(|b| b.is_ascii_digit())
                && (digits == "0" || !digits.starts_with('0'))
                && text != "-0"
        }
        Value::Array(values) => values.iter().all(has_plain_integers_only),
        Value::Object(map) => map.values().all(has_plain_integers_only),
        Value::Null | Value::Bool(_) | Value::String(_) => true,
    }
}

fn is_object_of_ints(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.values().all(is_canonical_int),
        _ => false,
    }
}

/// Whether power levels `content` certainly matches `POWER_LEVELS_SCHEMA`.
fn power_levels_are_valid(content: &JsonObject) -> bool {
    INT_FIELDS
        .iter()
        .all(|field| content.get_field(field).is_none_or(is_canonical_int))
        && OBJECT_OF_INTS_FIELDS
            .iter()
            .all(|field| content.get_field(field).is_none_or(is_object_of_ints))
}

/// Whether `m.mentions` in `content`, if there is one, certainly matches
/// `MENTIONS_SCHEMA`.
fn mentions_are_valid(content: &JsonObject) -> bool {
    let Some(mentions) = content.get_field("m.mentions") else {
        return true;
    };
    let Value::Object(mentions) = mentions else {
        return false;
    };
    let user_ids_ok = match mentions.get("user_ids") {
        None => true,
        Some(Value::Array(user_ids)) => user_ids.iter().all(Value::is_string),
        Some(_) => false,
    };
    let room_ok = mentions.get("room").is_none_or(Value::is_boolean);
    user_ids_ok && room_ok
}

/// The event as `get_pdu_json()` returns it.
///
/// `get_pdu_json` also drops `unsigned.redacted_because`, which the Rust
/// event never holds.
fn pdu_json(event: &Event) -> PyResult<Map<String, Value>> {
    match event.to_pdu_value() {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(PyValueError::new_err("event JSON is not an object")),
        Err(err) => Err(PyValueError::new_err(err.to_string())),
    }
}

/// Whether `event.get_pdu_json()` certainly passes `validate_canonicaljson`:
/// every number is an integer in the canonical JSON range.
#[pyfunction]
pub fn pdu_json_is_canonical(event: &Bound<'_, Event>) -> PyResult<bool> {
    let pdu = pdu_json(event.get())?;
    Ok(pdu.values().all(is_canonical_value))
}

/// The length in bytes of `encode_canonical_json(event.get_pdu_json())`, or
/// `None` if the event holds a number Python may print differently.
///
/// Numbers keep the text they were parsed from, while Python prints them
/// from its own value. The two agree for integers written the plain way,
/// but not always for floats (`1e3` against `1000.0`).
#[pyfunction]
pub fn pdu_json_size(event: &Bound<'_, Event>) -> PyResult<Option<usize>> {
    let pdu = pdu_json(event.get())?;
    if !pdu.values().all(has_plain_integers_only) {
        return Ok(None);
    }
    to_vec_canonical(&pdu, CanonicalizationOptions::relaxed())
        .map(|json| Some(json.len()))
        .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Whether power levels `content` certainly matches `POWER_LEVELS_SCHEMA`.
#[pyfunction]
pub fn power_levels_content_is_valid(content: JsonObject) -> bool {
    power_levels_are_valid(&content)
}

/// Whether the `m.mentions` in `content`, if any, certainly matches
/// `MENTIONS_SCHEMA`.
#[pyfunction]
pub fn mentions_content_is_valid(content: JsonObject) -> bool {
    mentions_are_valid(&content)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn object(value: Value) -> JsonObject {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn power_levels() {
        assert!(power_levels_are_valid(&object(json!({}))));
        assert!(power_levels_are_valid(&object(json!({
            "ban": 50,
            "users": {"@a:b": 100},
            "events": {},
            "historical": "ignored",
        }))));
        assert!(!power_levels_are_valid(&object(json!({"ban": "50"}))));
        assert!(!power_levels_are_valid(&object(json!({"ban": true}))));
        assert!(!power_levels_are_valid(&object(
            json!({"ban": 2i64.pow(53)})
        )));
        assert!(!power_levels_are_valid(&object(json!({"users": []}))));
        assert!(!power_levels_are_valid(&object(
            json!({"users": {"@a:b": null}})
        )));
    }

    #[test]
    fn power_levels_floats_are_not_sure() {
        let content: JsonObject = serde_json::from_str(r#"{"ban": 50.0}"#).unwrap();
        assert!(!power_levels_are_valid(&content));
    }

    #[test]
    fn mentions() {
        assert!(mentions_are_valid(&object(json!({"body": "hi"}))));
        assert!(mentions_are_valid(&object(json!({"m.mentions": {}}))));
        assert!(mentions_are_valid(&object(
            json!({"m.mentions": {"user_ids": ["@a:b"], "room": true}})
        )));
        assert!(!mentions_are_valid(&object(json!({"m.mentions": []}))));
        assert!(!mentions_are_valid(&object(
            json!({"m.mentions": {"user_ids": [1]}})
        )));
        assert!(!mentions_are_valid(&object(
            json!({"m.mentions": {"room": "yes"}})
        )));
    }
}
