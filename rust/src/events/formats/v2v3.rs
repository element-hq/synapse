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

//! Event format v2/v3 (room versions 3 through 10).
//!
//! Differences from v1:
//!
//! - `auth_events` and `prev_events` are flat `Vec<String>` lists of event IDs
//!   rather than `[id, hashes]` pairs.
//! - `event_id` is no longer in the event JSON; it is derived from the
//!   canonical-JSON hash at parse time.
//!
//! Note that the difference between event format v2 and v3 is purely in the
//! base64 encoding of the event ID, so the same struct can be used for both
//! formats.
//!
//! [`SimpleAuthPrevEvents`] is shared with [`v4`](super::v4) since the
//! flat-list encoding carries forward unchanged.

use anyhow::{bail, Error};
use serde::{Deserialize, Serialize};

use crate::events::formats::EventCommonFields;

/// Shared flat-list encoding of `auth_events` and `prev_events`, reused
/// by every format from v2/v3 onwards.
#[derive(Serialize, Deserialize)]
pub struct SimpleAuthPrevEvents {
    pub auth_events: Vec<String>,
    pub prev_events: Vec<String>,
}

/// Version-specific fields for room versions 3-10.
#[derive(Serialize, Deserialize)]
pub struct EventFormatV2V3 {
    pub room_id: Box<str>,
    #[serde(flatten)]
    pub auth_prev_events: SimpleAuthPrevEvents,
}

impl EventFormatV2V3 {
    pub fn validate(&self, common_fields: &EventCommonFields) -> Result<(), Error> {
        // Ensure that we don't have an event_id set.
        if common_fields.other_fields.contains_key("event_id") {
            bail!("v2/v3 events must not have an explicit event_id");
        }

        Ok(())
    }

    pub fn auth_event_ids(&self) -> Vec<String> {
        self.auth_prev_events.auth_events.clone()
    }
}
