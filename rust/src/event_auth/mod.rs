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

//! The state-dependent event auth rules.
//!
//! This is the one implementation of the rules, used both by Python (through
//! the pyfunctions registered here) and by the Rust state resolution in
//! [`crate::state`].
//!
//! Each rule reports one of two failures (see [`AuthFailure`]). The split
//! mirrors the Python, where most failures raise `AuthError` but malformed
//! input raises something else (`SynapseError(400)` from power level
//! validation, `ValueError` from `int()`, `KeyError` from a missing field).
//! State resolution skips an event on the first kind and aborts on the second,
//! because `_iterative_auth_checks` only catches `AuthError`.

use std::collections::{BTreeMap, HashMap};

use log::debug;
use pyo3::{
    exceptions::PyAttributeError,
    prelude::*,
    types::{PyModule, PySet, PyTuple},
};
use pythonize::{depythonize, pythonize};
use serde_json::Value;

use crate::errors::{AuthError, SynapseError, UnstableSpecAuthError};
use crate::events::{
    constants::{
        event_type::{
            M_ROOM_ALIASES, M_ROOM_CREATE, M_ROOM_JOIN_RULES, M_ROOM_MEMBER, M_ROOM_POWER_LEVELS,
            M_ROOM_REDACTION,
        },
        membership_field::{JOIN_AUTHORISED_VIA_USERS_SERVER, MEMBERSHIP, THIRD_PARTY_INVITE},
    },
    Event,
};
use crate::identifier::UserID;
use crate::room_versions::{EventFormatVersions, RoomVersion};

pub mod membership;
pub mod power_levels;

pub use power_levels::{get_named_level, get_send_level, get_user_power_level};

/// `m.room.third_party_invite`.
pub const M_ROOM_THIRD_PARTY_INVITE: &str = "m.room.third_party_invite";

/// The power level of a room creator under MSC4289. Matches
/// `synapse.api.constants.CREATOR_POWER_LEVEL`.
pub const CREATOR_POWER_LEVEL: i64 = 1 << 53;

pub const CREATE_KEY: (&str, &str) = (M_ROOM_CREATE, "");
pub const POWER_LEVELS_KEY: (&str, &str) = (M_ROOM_POWER_LEVELS, "");
pub const JOIN_RULES_KEY: (&str, &str) = (M_ROOM_JOIN_RULES, "");

const INSUFFICIENT_POWER: &str = "ORG.MATRIX.MSC3848.INSUFFICIENT_POWER";
const M_UNKNOWN: &str = "M_UNKNOWN";
const M_BAD_JSON: &str = "M_BAD_JSON";

/// The auth events of an event, keyed by `(type, state_key)`.
pub type AuthMap<'a> = HashMap<(&'a str, &'a str), &'a Event>;

/// Why an event failed the auth rules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthFailure {
    /// The event is not allowed: Python's `AuthError` (or its subclass
    /// `UnstableSpecAuthError`, when `errcode` is set). State resolution
    /// skips the event.
    Rejected {
        code: u16,
        msg: String,
        errcode: Option<&'static str>,
    },
    /// The event (or one of its auth events) is malformed in a way the
    /// Python does not report as `AuthError`. State resolution aborts.
    Invalid { msg: String, errcode: &'static str },
}

impl AuthFailure {
    /// A 403 `AuthError`.
    pub fn rejected(msg: impl Into<String>) -> Self {
        AuthFailure::Rejected {
            code: 403,
            msg: msg.into(),
            errcode: None,
        }
    }

    /// A 403 `UnstableSpecAuthError` with the given errcode.
    pub fn unstable(msg: impl Into<String>, errcode: &'static str) -> Self {
        AuthFailure::Rejected {
            code: 403,
            msg: msg.into(),
            errcode: Some(errcode),
        }
    }

    /// A malformed event, raised as `SynapseError(400)`.
    pub fn invalid(msg: impl Into<String>) -> Self {
        AuthFailure::Invalid {
            msg: msg.into(),
            errcode: M_UNKNOWN,
        }
    }

    pub fn is_rejected(&self) -> bool {
        matches!(self, AuthFailure::Rejected { .. })
    }

    /// Convert to the Python exception the rules used to raise.
    pub fn into_py_err(self) -> PyErr {
        match self {
            AuthFailure::Rejected {
                code,
                msg,
                errcode: None,
            } => AuthError::new_err((code, msg)),
            AuthFailure::Rejected {
                code,
                msg,
                errcode: Some(errcode),
            } => UnstableSpecAuthError::new_err((code, msg, errcode)),
            AuthFailure::Invalid { msg, errcode } => SynapseError::new_err((400, msg, errcode)),
        }
    }
}

impl std::fmt::Display for AuthFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AuthFailure::Rejected { code, msg, .. } => write!(f, "{code}: {msg}"),
            AuthFailure::Invalid { msg, .. } => write!(f, "400: {msg}"),
        }
    }
}

impl std::error::Error for AuthFailure {}

/// Build the `(type, state_key) -> event` map Python builds with
/// `{(e.type, e.state_key): e for e in auth_events}`. Later events win.
pub fn build_auth_map<'a>(
    auth_events: impl IntoIterator<Item = &'a Event>,
) -> Result<AuthMap<'a>, AuthFailure> {
    let mut map = HashMap::new();
    for event in auth_events {
        let Some(state_key) = event.state_key() else {
            return Err(AuthFailure::invalid(format!(
                "auth event {} is not a state event",
                event.event_id()
            )));
        };
        map.insert((event.type_(), state_key), event);
    }
    Ok(map)
}

/// The part of an ID after the first colon. Port of `get_domain_from_id`.
pub fn get_domain_from_id(id: &str) -> Result<&str, AuthFailure> {
    match id.find(':') {
        Some(idx) => Ok(&id[idx + 1..]),
        None => Err(AuthFailure::invalid(format!("Invalid ID: {id:?}"))),
    }
}

/// Port of `UserID.is_valid`: `from_string` plus server name validation.
pub fn user_id_is_valid(s: &str) -> bool {
    match UserID::try_from(s) {
        Ok(user_id) => is_valid_server_name(user_id.server_name()),
        Err(_) => false,
    }
}

/// Port of `parse_and_validate_server_name`, returning whether it succeeds.
pub(crate) fn is_valid_server_name(server_name: &str) -> bool {
    // `parse_server_name`.
    let host = if server_name.ends_with(']') {
        server_name
    } else {
        match server_name.rsplit_once(':') {
            Some((host, port)) => {
                if power_levels::py_int(&Value::String(port.to_owned())).is_err() {
                    return false;
                }
                host
            }
            None => server_name,
        }
    };

    if let Some(rest) = host.strip_prefix('[') {
        let Some(addr) = rest.strip_suffix(']') else {
            return false;
        };
        return !addr.is_empty() && addr.parse::<std::net::Ipv6Addr>().is_ok();
    }

    // `\A[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*\Z`
    !host.is_empty()
        && host.split('.').all(|label| {
            !label.is_empty()
                && label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
}

/// Port of `synapse.events.is_creator`: whether `user_id` created the room,
/// including the MSC4289 additional creators.
pub fn is_creator(create: &Event, user_id: &str) -> Result<bool, AuthFailure> {
    if create.sender() == user_id {
        return Ok(true);
    }
    if !create.room_version().msc4289_creator_power_enabled {
        return Ok(false);
    }
    // `user_id in set(content.get("additional_creators", []))`
    Ok(match create.content().get_field("additional_creators") {
        None => false,
        Some(Value::Array(creators)) => creators.iter().any(|c| c.as_str() == Some(user_id)),
        Some(Value::Object(creators)) => creators.contains_key(user_id),
        Some(Value::String(_)) => false,
        Some(other) => {
            return Err(AuthFailure::invalid(format!(
                "additional_creators is not iterable: {other}"
            )))
        }
    })
}

/// The `membership` of a member event. Python's `event.membership` raises
/// `KeyError` when the field is missing.
pub(crate) fn membership_of(event: &Event) -> Result<&Value, AuthFailure> {
    event.content().get_field(MEMBERSHIP).ok_or_else(|| {
        AuthFailure::invalid(format!("event {} has no membership", event.event_id()))
    })
}

fn can_federate(auth_events: &AuthMap<'_>) -> bool {
    // There should always be a creation event, but if not don't federate.
    let Some(create) = auth_events.get(&CREATE_KEY) else {
        return false;
    };
    match create.content().get_field("m.federate") {
        None => true,
        Some(value) => *value == Value::Bool(true),
    }
}

fn check_joined_room(
    member: Option<&Event>,
    user_id: &str,
    room_id: &str,
) -> Result<(), AuthFailure> {
    let joined = match member {
        Some(member) => membership_of(member)?.as_str() == Some(membership::JOIN),
        None => false,
    };
    if !joined {
        return Err(AuthFailure::rejected(format!(
            "User {user_id} not in room {room_id} ({})",
            member.map_or("None", Event::event_id)
        )));
    }
    Ok(())
}

fn check_event_sender_in_room(event: &Event, auth_events: &AuthMap<'_>) -> Result<(), AuthFailure> {
    let member = auth_events.get(&(M_ROOM_MEMBER, event.sender())).copied();
    check_joined_room(member, event.sender(), event.room_id())
}

/// Port of `_can_send_event`.
fn can_send_event(event: &Event, auth_events: &AuthMap<'_>) -> Result<(), AuthFailure> {
    let state_key = event.state_key();
    let power_levels_event = power_levels::get_power_level_event(auth_events);

    let send_level = get_send_level(event.type_(), state_key, power_levels_event)?;
    let user_level = get_user_power_level(event.sender(), auth_events)?;

    if user_level < send_level {
        return Err(AuthFailure::unstable(
            format!(
                "You don't have permission to post that to the room. \
                 user_level ({user_level}) < send_level ({send_level})"
            ),
            INSUFFICIENT_POWER,
        ));
    }

    if let Some(state_key) = state_key {
        if state_key.starts_with('@') && state_key != event.sender() {
            if event.room_version().msc3757_enabled {
                let state_key_user_id =
                    msc3757_state_key_owner(state_key).ok_or_else(|| AuthFailure::Invalid {
                        msg: "State key neither equals a valid user ID, nor starts with one \
                              plus an underscore"
                            .to_owned(),
                        errcode: M_BAD_JSON,
                    })?;
                if state_key_user_id == event.sender()
                    || user_level > get_user_power_level(state_key_user_id, auth_events)?
                {
                    return Ok(());
                }
            }
            return Err(AuthFailure::rejected(
                "You are not allowed to set others state",
            ));
        }
    }

    Ok(())
}

/// The user that owns an MSC3757 state key: the state key up to the first
/// underscore after the server name's colon, if that is a valid user ID.
fn msc3757_state_key_owner(state_key: &str) -> Option<&str> {
    // `state_key.index(":", 1)`; the state key starts with the one-byte "@",
    // so byte and character offsets agree.
    let colon_idx = 1 + state_key.get(1..)?.find(':')?;
    let user_id = match state_key[colon_idx + 1..].find('_') {
        Some(offset) => &state_key[..colon_idx + 1 + offset],
        None => state_key,
    };
    user_id_is_valid(user_id).then_some(user_id)
}

/// Check whether the event sender is allowed to redact the target event.
/// Port of `check_redaction`.
///
/// Returns `true` if the sender may redact the target only if they sent it
/// (and sets `recheck_redaction` on the event for room v3+), `false` if they
/// may redact it outright.
pub fn check_redaction(
    room_version: &RoomVersion,
    event: &Event,
    auth_events: &AuthMap<'_>,
) -> Result<bool, AuthFailure> {
    let user_level = get_user_power_level(event.sender(), auth_events)?;
    let redact_level = get_named_level(auth_events, "redact", 50)?;

    if user_level >= redact_level {
        return Ok(false);
    }

    if room_version.event_format == EventFormatVersions::ROOM_V1_V2 {
        let redacter_domain = get_domain_from_id(event.event_id())?;
        let Some(Value::String(redacts)) = event.redacts() else {
            return Ok(false);
        };
        if redacter_domain == get_domain_from_id(redacts)? {
            return Ok(true);
        }
    } else {
        event
            .internal_metadata()
            .set_recheck_redaction(true)
            .map_err(|e| AuthFailure::invalid(e.to_string()))?;
        return Ok(true);
    }

    Err(AuthFailure::rejected(
        "You don't have permission to redact events",
    ))
}

/// Check that an event complies with the auth rules that depend on room
/// state. Port of `synapse.event_auth.check_state_dependent_auth_rules`.
///
/// This is suitable for state resolution, where whether to accept the event
/// has already been decided. For the initial auth of an event it is only part
/// of the checks.
pub fn check_state_dependent_auth_rules<'a>(
    event: &Event,
    auth_events: impl IntoIterator<Item = &'a Event>,
) -> Result<(), AuthFailure> {
    // There are no state-dependent auth rules for create events.
    if event.type_() == M_ROOM_CREATE {
        return Ok(());
    }

    let auth_dict = build_auth_map(auth_events)?;
    check_state_dependent_auth_rules_with_map(event, &auth_dict)
}

/// As [`check_state_dependent_auth_rules`], with the auth events already
/// keyed by `(type, state_key)`.
pub fn check_state_dependent_auth_rules_with_map(
    event: &Event,
    auth_dict: &AuthMap<'_>,
) -> Result<(), AuthFailure> {
    if event.type_() == M_ROOM_CREATE {
        return Ok(());
    }

    // Later rules rely on there being a create event, so produce a more
    // intelligible error if we don't have one.
    let Some(create_event) = auth_dict.get(&CREATE_KEY) else {
        return Err(AuthFailure::rejected(format!(
            "Event {} is missing a create event in auth_events.",
            event.event_id()
        )));
    };

    // Additional check for m.federate.
    let creating_domain = get_domain_from_id(create_event.sender())?;
    let originating_domain = get_domain_from_id(event.sender())?;
    if creating_domain != originating_domain && !can_federate(auth_dict) {
        return Err(AuthFailure::rejected(
            "This room has been marked as unfederatable.",
        ));
    }

    let room_version = event.room_version();

    // 4. If type is m.room.aliases
    if event.type_() == M_ROOM_ALIASES && room_version.special_case_aliases_auth {
        // 4a. If event has no state_key, reject
        let Some(state_key) = event.state_key() else {
            return Err(AuthFailure::rejected("Alias event must be a state event"));
        };
        if state_key.is_empty() {
            return Err(AuthFailure::rejected(
                "Alias event must have non-empty state_key",
            ));
        }

        // 4b. If sender's domain doesn't match state_key, reject
        if state_key != get_domain_from_id(event.sender())? {
            return Err(AuthFailure::rejected(
                "Alias event's state_key does not match sender's domain",
            ));
        }

        // 4c. Otherwise, allow.
        return Ok(());
    }

    // 5. If type is m.room.member
    if event.type_() == M_ROOM_MEMBER {
        membership::is_membership_change_allowed(room_version, event, auth_dict)?;
        debug!("Allowing! {}", event.event_id());
        return Ok(());
    }

    check_event_sender_in_room(event, auth_dict)?;

    // Special case to allow m.room.third_party_invite events wherever a user
    // is allowed to issue invites.
    if event.type_() == M_ROOM_THIRD_PARTY_INVITE {
        let user_level = get_user_power_level(event.sender(), auth_dict)?;
        let invite_level = get_named_level(auth_dict, "invite", 0)?;

        if user_level < invite_level {
            return Err(AuthFailure::unstable(
                "You don't have permission to invite users",
                INSUFFICIENT_POWER,
            ));
        }
        return Ok(());
    }

    can_send_event(event, auth_dict)?;

    if event.type_() == M_ROOM_POWER_LEVELS {
        power_levels::check_power_levels(room_version, event, auth_dict)?;
    }

    if event.type_() == M_ROOM_REDACTION {
        check_redaction(room_version, event, auth_dict)?;
    }

    debug!("Allowing! {}", event.event_id());
    Ok(())
}

/// Validate a third-party invite against the `m.room.third_party_invite`
/// event it claims. Port of `_verify_third_party_invite`.
///
/// The structural checks run here. The ed25519 signature check calls back
/// into Python (`synapse.event_auth._verify_third_party_invite_signatures`)
/// under the GIL, as there is no ed25519 implementation in Rust yet.
fn verify_third_party_invite(
    event: &Event,
    auth_events: &AuthMap<'_>,
) -> Result<bool, AuthFailure> {
    let Some(Value::Object(third_party_invite)) = event.content().get_field(THIRD_PARTY_INVITE)
    else {
        return Ok(false);
    };
    let Some(Value::Object(signed)) = third_party_invite.get("signed") else {
        return Ok(false);
    };
    if ["mxid", "token", "signatures"]
        .iter()
        .any(|key| !signed.contains_key(*key))
    {
        return Ok(false);
    }

    let Some(token) = signed["token"].as_str() else {
        return Ok(false);
    };
    let Some(invite_event) = auth_events.get(&(M_ROOM_THIRD_PARTY_INVITE, token)) else {
        return Ok(false);
    };

    if invite_event.sender() != event.sender() {
        return Ok(false);
    }

    if signed["mxid"].as_str() != event.state_key() {
        return Ok(false);
    }

    Python::attach(|py| -> PyResult<bool> {
        let verify = py
            .import("synapse.event_auth")?
            .getattr("_verify_third_party_invite_signatures")?;
        let signed = pythonize(py, signed)?;
        let invite_content = pythonize(py, invite_event.content())?;
        verify.call1((signed, invite_content))?.extract()
    })
    .map_err(|e| AuthFailure::invalid(format!("verifying third party invite: {e}")))
}

/// The `(type, state_key)` pairs that may be needed to auth an event. Port
/// of `synapse.event_auth.auth_types_for_event`.
///
/// The result may be a superset of what is needed, depending on the room
/// state. A non-string `third_party_invite` token or authorising user is left
/// out, as no state key can match it.
///
/// The pairs are distinct and in the order the Python adds them to its set.
/// The order matters to Python callers: building the same set in the same
/// order gives the same iteration order, and event creation lists the auth
/// events in that order, which feeds into the event ID.
pub fn auth_types_for_event<'a>(
    room_version: &RoomVersion,
    event: &'a Event,
) -> Result<Vec<(&'a str, &'a str)>, AuthFailure> {
    auth_types_for(
        room_version,
        event.type_(),
        event.sender(),
        event.state_key(),
        event.content().as_map(),
    )
}

pub(crate) fn auth_types_for<'a>(
    room_version: &RoomVersion,
    event_type: &'a str,
    sender: &'a str,
    state_key: Option<&'a str>,
    content: &'a BTreeMap<Box<str>, Value>,
) -> Result<Vec<(&'a str, &'a str)>, AuthFailure> {
    let mut auth_types = OrderedSet::default();
    if event_type == M_ROOM_CREATE {
        return Ok(auth_types.0);
    }

    auth_types.insert(POWER_LEVELS_KEY);
    auth_types.insert((M_ROOM_MEMBER, sender));
    auth_types.insert(CREATE_KEY);

    if event_type == M_ROOM_MEMBER {
        let Some(membership) = content.get(MEMBERSHIP) else {
            return Err(AuthFailure::invalid("member event has no membership"));
        };
        let membership = membership.as_str();
        if matches!(
            membership,
            Some(membership::JOIN | membership::INVITE | membership::KNOCK)
        ) {
            auth_types.insert(JOIN_RULES_KEY);
        }

        let Some(state_key) = state_key else {
            return Err(AuthFailure::invalid("member event has no state_key"));
        };
        auth_types.insert((M_ROOM_MEMBER, state_key));

        if membership == Some(membership::INVITE) {
            if let Some(third_party_invite) = content.get(THIRD_PARTY_INVITE) {
                // `content["third_party_invite"]["signed"]["token"]`
                let token = third_party_invite
                    .get("signed")
                    .and_then(|signed| signed.get("token"))
                    .ok_or_else(|| AuthFailure::invalid("malformed third_party_invite"))?;
                if let Some(token) = token.as_str() {
                    auth_types.insert((M_ROOM_THIRD_PARTY_INVITE, token));
                }
            }
        }

        if room_version.restricted_join_rule && membership == Some(membership::JOIN) {
            if let Some(Value::String(user)) = content.get(JOIN_AUTHORISED_VIA_USERS_SERVER) {
                auth_types.insert((M_ROOM_MEMBER, user));
            }
        }
    }

    Ok(auth_types.0)
}

/// A set that keeps insertion order. It holds at most seven pairs, so a
/// linear scan is cheapest.
#[derive(Default)]
struct OrderedSet<'a>(Vec<(&'a str, &'a str)>);

impl<'a> OrderedSet<'a> {
    fn insert(&mut self, pair: (&'a str, &'a str)) {
        if !self.0.contains(&pair) {
            self.0.push(pair);
        }
    }
}

/// Python entry point for [`check_state_dependent_auth_rules`].
///
/// Raises `AuthError` if the event is not allowed and `SynapseError` if it is
/// malformed.
#[pyfunction(name = "check_state_dependent_auth_rules")]
fn check_state_dependent_auth_rules_py(
    event: &Bound<'_, Event>,
    auth_events: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let event = event.get();
    if event.type_() == M_ROOM_CREATE {
        return Ok(());
    }

    let auth_events = auth_events
        .try_iter()?
        .map(|ev| Ok(ev?.cast_into::<Event>()?))
        .collect::<PyResult<Vec<Bound<'_, Event>>>>()?;

    check_state_dependent_auth_rules(event, auth_events.iter().map(|ev| ev.get()))
        .map_err(AuthFailure::into_py_err)
}

/// Python entry point for [`auth_types_for_event`]. Also accepts an
/// `EventBuilder` (or any object with `type`, `sender`, `state_key` and
/// `content` attributes).
#[pyfunction(name = "auth_types_for_event")]
fn auth_types_for_event_py<'py>(
    py: Python<'py>,
    room_version: &RoomVersion,
    event: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PySet>> {
    let to_py = |types: Vec<(&str, &str)>| {
        let tuples = types
            .into_iter()
            .map(|(t, k)| PyTuple::new(py, [t, k]))
            .collect::<PyResult<Vec<_>>>()?;
        PySet::new(py, tuples)
    };

    if let Ok(event) = event.cast::<Event>() {
        let types =
            auth_types_for_event(room_version, event.get()).map_err(AuthFailure::into_py_err)?;
        return to_py(types);
    }

    let event_type: String = event.getattr("type")?.extract()?;
    let sender: String = event.getattr("sender")?.extract()?;
    // `EventBuilder.state_key` raises `AttributeError` for non-state events.
    let state_key: Option<String> = match event.getattr("state_key") {
        Ok(state_key) => state_key.extract()?,
        Err(err) if err.is_instance_of::<PyAttributeError>(py) => None,
        Err(err) => return Err(err),
    };
    let content: BTreeMap<Box<str>, Value> = depythonize(&event.getattr("content")?)?;
    let types = auth_types_for(
        room_version,
        &event_type,
        &sender,
        state_key.as_deref(),
        &content,
    )
    .map_err(AuthFailure::into_py_err)?;
    to_py(types)
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "event_auth")?;
    child_module.add_function(wrap_pyfunction!(check_state_dependent_auth_rules_py, m)?)?;
    child_module.add_function(wrap_pyfunction!(auth_types_for_event_py, m)?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import event_auth` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.event_auth", child_module)?;

    Ok(())
}

#[cfg(test)]
pub(crate) mod tests;
