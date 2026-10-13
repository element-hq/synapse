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

//! Power level lookups and the `m.room.power_levels` auth rule.
//!
//! Power level values are coerced the way Python's `int()` coerces them, since
//! rooms before v10 accept stringy and float levels and Synapse has always
//! read them with `int()`.

use std::collections::BTreeSet;

use serde_json::{Map, Value};

use super::{is_creator, AuthFailure, AuthMap, CREATE_KEY, CREATOR_POWER_LEVEL, POWER_LEVELS_KEY};
use crate::events::{json_object::JsonObject, Event};
use crate::identifier::UserID;
use crate::room_versions::RoomVersion;

/// The keys of `m.room.power_levels` content that hold a single level.
const TOP_LEVEL_KEYS: [&str; 7] = [
    "users_default",
    "events_default",
    "state_default",
    "ban",
    "redact",
    "kick",
    "invite",
];

/// Coerce a JSON value to an integer the way Python's `int()` does.
///
/// * integers are returned as is (saturating at the `i64` bounds);
/// * floats are truncated towards zero;
/// * `true`/`false` are 1 and 0;
/// * strings are parsed as base-10 integers, allowing surrounding whitespace,
///   a sign and single underscores between digits.
///
/// Anything else (null, arrays, objects, non-finite floats, malformed
/// strings) is an error, as `int()` raises.
pub fn py_int(value: &Value) -> Result<i64, AuthFailure> {
    match value {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                return Ok(i);
            }
            let text = n.to_string();
            if is_integer_literal(&text) {
                // An integer outside the `i64` range.
                return Ok(saturate(text.parse::<i128>().unwrap_or(
                    if text.starts_with('-') {
                        i128::MIN
                    } else {
                        i128::MAX
                    },
                )));
            }
            match n.as_f64() {
                Some(f) if f.is_finite() => {
                    Ok(f.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64)
                }
                _ => Err(AuthFailure::invalid(format!(
                    "cannot convert float {text} to integer"
                ))),
            }
        }
        Value::String(s) => parse_py_int_str(s).ok_or_else(|| {
            AuthFailure::invalid(format!("invalid literal for int() with base 10: {s:?}"))
        }),
        Value::Null => Err(AuthFailure::invalid(
            "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
        )),
        Value::Array(_) | Value::Object(_) => Err(AuthFailure::invalid(
            "int() argument must be a string, a bytes-like object or a real number",
        )),
    }
}

/// Whether a JSON value is an integer in the sense of Python's
/// `type(v) is int`: a JSON number with no fraction or exponent. Booleans
/// are excluded, as `type(True) is bool`.
pub fn is_json_int(value: &Value) -> bool {
    match value {
        Value::Number(n) => n.is_i64() || n.is_u64() || is_integer_literal(&n.to_string()),
        _ => false,
    }
}

fn is_integer_literal(text: &str) -> bool {
    let digits = text.strip_prefix('-').unwrap_or(text);
    !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
}

fn saturate(i: i128) -> i64 {
    i.clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64
}

/// Parse a string the way `int(s)` does for base 10.
fn parse_py_int_str(s: &str) -> Option<i64> {
    let s = s.trim();
    let (negative, rest) = match s.as_bytes().first()? {
        b'-' => (true, &s[1..]),
        b'+' => (false, &s[1..]),
        _ => (false, s),
    };

    // Digits, with single underscores allowed only between digits.
    let mut digits = String::with_capacity(rest.len());
    let mut prev_underscore = true; // disallows a leading underscore
    for c in rest.chars() {
        match c {
            '0'..='9' => {
                digits.push(c);
                prev_underscore = false;
            }
            '_' if !prev_underscore => prev_underscore = true,
            _ => return None,
        }
    }
    if digits.is_empty() || prev_underscore {
        return None;
    }

    let magnitude = digits.parse::<i128>().unwrap_or(i128::MAX);
    Some(saturate(if negative { -magnitude } else { magnitude }))
}

/// `content.get(key, {})` where Python then calls a mapping method on the
/// result.
///
/// Returns `None` when the key is absent. A present value that is not an
/// object (including `null`) is an error, as the Python raises
/// `AttributeError` when it calls `.get` or `.items` on it.
pub fn sub_mapping<'a>(
    value: Option<&'a Value>,
    key: &str,
) -> Result<Option<&'a Map<String, Value>>, AuthFailure> {
    match value {
        None => Ok(None),
        Some(Value::Object(map)) => Ok(Some(map)),
        Some(other) => Err(AuthFailure::invalid(format!(
            "power levels {key:?} is not a mapping: {other}"
        ))),
    }
}

/// Treat a JSON `null` as absent, matching Python's `x.get(k) is None`.
fn non_null(value: Option<&Value>) -> Option<&Value> {
    value.filter(|v| !v.is_null())
}

/// The `m.room.power_levels` event in `auth_events`, if any.
pub fn get_power_level_event<'a>(auth_events: &AuthMap<'a>) -> Option<&'a Event> {
    auth_events.get(&POWER_LEVELS_KEY).copied()
}

/// A user's power level. Port of `synapse.event_auth.get_user_power_level`.
pub fn get_user_power_level(user_id: &str, auth_events: &AuthMap<'_>) -> Result<i64, AuthFailure> {
    let Some(create) = auth_events.get(&CREATE_KEY) else {
        return Err(AuthFailure::invalid(
            "A create event in the auth events chain is required to calculate user power level \
             correctly, but was not found. This indicates a bug",
        ));
    };

    if create.room_version().msc4289_creator_power_enabled && is_creator(create, user_id)? {
        return Ok(CREATOR_POWER_LEVEL);
    }

    if let Some(pl) = get_power_level_event(auth_events) {
        let content = pl.content();
        let users = sub_mapping(content.get_field("users"), "users")?;
        let level = non_null(users.and_then(|users| users.get(user_id)))
            .or_else(|| non_null(content.get_field("users_default")));
        return match level {
            None => Ok(0),
            Some(level) => py_int(level),
        };
    }

    // If there is no power levels event, the creator gets 100 and everyone
    // else gets 0.
    let is_room_creator = if create.room_version().implicit_room_creator {
        create.sender() == user_id
    } else {
        match create.content().get_field("creator") {
            Some(creator) => creator.as_str() == Some(user_id),
            None => return Err(AuthFailure::invalid("create event has no 'creator'")),
        }
    };
    Ok(if is_room_creator { 100 } else { 0 })
}

/// The level named `name` (e.g. `"ban"`), or `default` if there is no power
/// levels event or it does not set one. Port of `get_named_level`.
pub fn get_named_level(
    auth_events: &AuthMap<'_>,
    name: &str,
    default: i64,
) -> Result<i64, AuthFailure> {
    let Some(pl) = get_power_level_event(auth_events) else {
        return Ok(default);
    };
    match non_null(pl.content().get_field(name)) {
        Some(level) => py_int(level),
        None => Ok(default),
    }
}

/// The level required to send an event of type `etype`. Port of
/// `get_send_level`.
pub fn get_send_level(
    etype: &str,
    state_key: Option<&str>,
    power_levels_event: Option<&Event>,
) -> Result<i64, AuthFailure> {
    let Some(pl) = power_levels_event else {
        return Ok(if state_key.is_some() { 50 } else { 0 });
    };
    let content = pl.content();

    let events = sub_mapping(content.get_field("events"), "events")?;
    if let Some(level) = non_null(events.and_then(|events| events.get(etype))) {
        return py_int(level);
    }

    // `content.get("state_default", 50)`: a present `null` is passed to
    // `int()`, which raises.
    let (key, default) = if state_key.is_some() {
        ("state_default", 50)
    } else {
        ("events_default", 0)
    };
    match content.get_field(key) {
        None => Ok(default),
        Some(level) => py_int(level),
    }
}

/// One of the places a level can be looked up in power levels content.
#[derive(Clone, Copy)]
enum Location<'a> {
    TopLevel(&'a JsonObject),
    Nested(Option<&'a Map<String, Value>>),
}

impl<'a> Location<'a> {
    fn new(content: &'a JsonObject, dir: Option<&str>) -> Result<Self, AuthFailure> {
        Ok(match dir {
            None => Location::TopLevel(content),
            Some(dir) => Location::Nested(sub_mapping(content.get_field(dir), dir)?),
        })
    }

    fn get(&self, key: &str) -> Option<&'a Value> {
        match self {
            Location::TopLevel(content) => content.get_field(key),
            Location::Nested(map) => map.and_then(|map| map.get(key)),
        }
    }
}

/// Validate that `value` is a valid user ID for a power levels `users` key,
/// as `UserID.from_string` does.
fn check_user_id(user_id: &str) -> Result<(), AuthFailure> {
    if UserID::try_from(user_id).is_err() {
        return Err(AuthFailure::invalid(format!(
            "Not a valid user_id: {user_id}"
        )));
    }
    Ok(())
}

/// The keys of a nested mapping, for building the set of levels to compare.
fn keys_of(map: Option<&Map<String, Value>>) -> impl Iterator<Item = &str> {
    map.into_iter().flat_map(|m| m.keys().map(String::as_str))
}

/// The `m.room.power_levels` auth rule. Port of `_check_power_levels`.
///
/// Malformed content is reported as [`AuthFailure::Invalid`], which the
/// Python raises as `SynapseError(400)` rather than `AuthError`.
pub fn check_power_levels(
    room_version: &RoomVersion,
    event: &Event,
    auth_events: &AuthMap<'_>,
) -> Result<(), AuthFailure> {
    let content = event.content();
    let user_list = sub_mapping(content.get_field("users"), "users")?;

    // Validate users.
    for (user, level) in user_list.into_iter().flatten() {
        check_user_id(user)?;
        if py_int(level).is_err() {
            return Err(AuthFailure::invalid(format!(
                "Not a valid power level: {level}"
            )));
        }
    }

    if room_version.msc4289_creator_power_enabled {
        // Enforce the creator does not appear in the users map.
        let Some(create) = auth_events.get(&CREATE_KEY) else {
            return Err(AuthFailure::invalid(
                "Cannot check power levels without a create event in auth_events",
            ));
        };
        let users = user_list.map(|m| m.keys().map(String::as_str).collect::<BTreeSet<_>>());
        let users = users.unwrap_or_default();
        if users.contains(create.sender()) {
            return Err(AuthFailure::invalid(format!(
                "Creator user {} must not appear in content.users",
                create.sender()
            )));
        }
        match create.content().get_field("additional_creators") {
            None | Some(Value::Null) => {}
            Some(Value::Array(creators)) => {
                if creators
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|creator| users.contains(creator))
                {
                    return Err(AuthFailure::invalid(
                        "Additional creators users must not appear in content.users",
                    ));
                }
            }
            Some(other) => {
                return Err(AuthFailure::invalid(format!(
                    "additional_creators is not a list: {other}"
                )))
            }
        }
    }

    // Reject events with stringy power levels if required by the room version.
    if room_version.enforce_int_power_levels {
        for (key, value) in content.iter() {
            if TOP_LEVEL_KEYS.contains(&&**key) && !is_json_int(value) {
                return Err(AuthFailure::invalid(format!("{value} must be an integer.")));
            }
            if matches!(&**key, "events" | "notifications" | "users") {
                let all_ints = match value {
                    Value::Object(map) => map.values().all(is_json_int),
                    _ => false,
                };
                if !all_ints {
                    return Err(AuthFailure::invalid(format!(
                        "{value} must be a dict wherein all the values are integers."
                    )));
                }
            }
        }
    }

    let Some(state_key) = event.state_key() else {
        return Err(AuthFailure::invalid("power levels event has no state_key"));
    };
    let Some(current_state) = auth_events.get(&(event.type_(), state_key)) else {
        return Ok(());
    };

    let user_level = get_user_power_level(event.sender(), auth_events)?;

    let old_state = current_state.content();
    let new_state = content;

    // The levels to compare, as (name, the content key they live under).
    let mut levels_to_check: Vec<(&str, Option<&str>)> =
        TOP_LEVEL_KEYS.iter().map(|key| (*key, None)).collect();

    let mut dirs = vec!["users", "events"];
    if room_version.limit_notifications_power_levels {
        // MSC2209 specifies these checks should also be done for the
        // "notifications" key.
        dirs.push("notifications");
    }
    for dir in dirs {
        let old = sub_mapping(old_state.get_field(dir), dir)?;
        let new = sub_mapping(new_state.get_field(dir), dir)?;
        let names: BTreeSet<&str> = keys_of(old).chain(keys_of(new)).collect();
        levels_to_check.extend(names.into_iter().map(|name| (name, Some(dir))));
    }

    for (level_to_check, dir) in levels_to_check {
        let old_loc = Location::new(old_state, dir)?;
        let new_loc = Location::new(new_state, dir)?;

        let old_level = old_loc.get(level_to_check).map(py_int).transpose()?;
        let new_level = new_loc.get(level_to_check).map(py_int).transpose()?;

        if old_level.is_some() && new_level.is_some() && old_level == new_level {
            continue;
        }

        if dir == Some("users") && level_to_check != event.sender() && old_level == Some(user_level)
        {
            return Err(AuthFailure::rejected(
                "You don't have permission to remove ops level equal to your own",
            ));
        }

        // Check if the old and new levels are greater than the user level
        // (if defined).
        let too_big = |level: Option<i64>| level.is_some_and(|level| level > user_level);
        if too_big(old_level) || too_big(new_level) {
            return Err(AuthFailure::rejected(
                "You don't have permission to add ops level greater than your own",
            ));
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn py_int_matches_python() {
        assert_eq!(py_int(&json!(50)).unwrap(), 50);
        assert_eq!(py_int(&json!(-3)).unwrap(), -3);
        assert_eq!(py_int(&json!(50.9)).unwrap(), 50);
        assert_eq!(py_int(&json!(-50.9)).unwrap(), -50);
        assert_eq!(py_int(&json!(true)).unwrap(), 1);
        assert_eq!(py_int(&json!(false)).unwrap(), 0);
        assert_eq!(py_int(&json!("50")).unwrap(), 50);
        assert_eq!(py_int(&json!(" +50\n")).unwrap(), 50);
        assert_eq!(py_int(&json!("-050")).unwrap(), -50);
        assert_eq!(py_int(&json!("1_000")).unwrap(), 1000);
        assert_eq!(
            py_int(&serde_json::from_str::<Value>("100000000000000000000").unwrap()).unwrap(),
            i64::MAX
        );

        for bad in [
            json!(null),
            json!("50.0"),
            json!(""),
            json!("_1"),
            json!("1_"),
            json!("1__0"),
            json!("abc"),
            json!([]),
            json!({}),
        ] {
            assert!(py_int(&bad).is_err(), "{bad} should not coerce");
        }
    }

    #[test]
    fn is_json_int_matches_type_is_int() {
        assert!(is_json_int(&json!(50)));
        assert!(is_json_int(&json!(-50)));
        assert!(!is_json_int(&json!(50.0)));
        assert!(!is_json_int(&json!("50")));
        assert!(!is_json_int(&json!(true)));
        assert!(!is_json_int(&json!(null)));
    }
}
