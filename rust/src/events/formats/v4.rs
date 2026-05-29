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
 *
 */

//! Event format v4 (room version 11).
//!
//! The main change from v2/v3 is that `room_id` becomes optional: an
//! `m.room.create` event no longer carries an explicit room ID, and the
//! room ID is instead *derived* from the create event's ID by replacing
//! the leading `$` with `!`. Conversely, every non-create event still
//! has an explicit `room_id`, and the create event is implicitly
//! included in the auth chain of every non-create event (so it does not
//! need to appear in `auth_events`).
//!
//! [`EventFormatV4::validate`] enforces these invariants at parse time;
//! [`EventFormatV4::room_id`] and [`EventFormatV4::auth_event_ids`]
//! expose the derived values to callers.

use std::borrow::Cow;

use anyhow::{bail, ensure, Error};
use serde::{Deserialize, Serialize};

use crate::{
    events::{constants::event_type::M_ROOM_CREATE, formats::EventCommonFields},
    json::AllowMissing,
};

/// Version-specific fields for room version 11.
#[derive(Serialize, Deserialize)]
pub struct EventFormatV4 {
    #[serde(
        default,
        with = "crate::json::allow_missing",
        skip_serializing_if = "AllowMissing::is_absent"
    )]
    pub room_id: AllowMissing<Box<str>>,
    pub auth_events: Vec<String>,
    pub prev_events: Vec<String>,
}

impl EventFormatV4 {
    pub fn validate(&self, common_fields: &EventCommonFields) -> Result<(), Error> {
        validate_optional_room_id(self.room_id.as_deref_opt(), common_fields)?;

        // Ensure that we don't have an event_id set.
        if common_fields.other_fields.contains_key("event_id") {
            bail!("v4 events must not have an explicit event_id");
        }

        Ok(())
    }

    pub fn room_id(
        &self,
        event_id: &str,
        common_fields: &EventCommonFields,
    ) -> Result<Cow<'_, str>, Error> {
        get_room_id_for_optional_room_id(self.room_id.as_deref_opt(), event_id, common_fields)
    }

    pub fn auth_event_ids(&self, common_fields: &EventCommonFields) -> Result<Vec<String>, Error> {
        let is_create = common_fields.type_state_key_tuple() == Some((M_ROOM_CREATE, ""));

        if is_create {
            // The create event itself has no implicit auth events.
            return Ok(self.auth_events.clone());
        }

        // For non-create events, the create event is implicitly part of
        // the auth chain. Derive its event ID from the room ID by
        // replacing the leading '!' with '$'.
        let room_id = self
            .room_id
            .as_deref_opt()
            .ok_or_else(|| anyhow::anyhow!("non-create event has no room_id"))?;

        let mut create_event_id = String::with_capacity(room_id.len());
        create_event_id.push('$');
        create_event_id.push_str(&room_id[1..]);

        ensure!(
            !self.auth_events.contains(&create_event_id),
            "The create event ID is implicitly part of the auth chain and should not be explicitly be in the auth_events"
        );

        let mut auth_events = self.auth_events.clone();
        auth_events.push(create_event_id);
        Ok(auth_events)
    }
}

/// Validation helper for v4+ events that can have an optional room ID.
pub fn validate_optional_room_id(
    room_id: Option<&str>,
    common_fields: &EventCommonFields,
) -> Result<(), Error> {
    let is_create_event = common_fields.type_state_key_tuple() == Some((M_ROOM_CREATE, ""));

    match (is_create_event, room_id) {
        // For non-create events, room_id must be present.
        (false, None) => bail!("non-create event must have a room ID"),
        (false, Some(room_id)) => {
            // We later derive the create event ID from the room ID by replacing
            // the leading '!' with '$', so we require the room ID to start with
            // '!'.
            ensure!(
                room_id.starts_with("!"),
                "room_id must start with '!': {}",
                room_id
            );
        }

        // For create events, room_id must be absent.
        (true, Some(_)) => bail!("create event must not have a room ID"),
        (true, None) => {}
    }

    Ok(())
}

/// Room ID derivation helper for v4+ events, which can have an optional room
/// ID.
pub fn get_room_id_for_optional_room_id<'a>(
    room_id: Option<&'a str>,
    event_id: &str,
    common_fields: &EventCommonFields,
) -> Result<Cow<'a, str>, Error> {
    let is_create_event = common_fields.type_state_key_tuple() == Some((M_ROOM_CREATE, ""));

    match (is_create_event, room_id) {
        // For non-create events, room_id must be present.
        (false, None) => bail!("Event '{}' has no room ID", event_id),
        (false, Some(room_id)) => Ok(room_id.into()),

        // For create events, room_id must be absent.
        (true, Some(_)) => bail!("Create event '{}' has unexpected room_id", event_id),
        (true, None) => {
            // The room ID is derived from the event ID by replacing the
            // leading '$' with a '!'.
            if !event_id.starts_with('$') {
                bail!("Create event ID does not start with '$': {}", event_id);
            }

            let mut room_id = String::with_capacity(event_id.len());
            room_id.push('!');
            room_id.push_str(&event_id[1..]);

            Ok(room_id.into())
        }
    }
}
