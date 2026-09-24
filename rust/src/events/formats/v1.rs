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

//! Event format v1 (room versions 1 and 2).
//!
//! Distinguishing features compared to later formats:
//!
//! - `auth_events` and `prev_events` are `[event_id, hashes]` pairs
//!   rather than flat lists of IDs.
//! - `event_id` is carried explicitly in the event JSON, rather than
//!   being derived from the canonical-JSON hash.
//! - `room_id` is always present.

use std::{collections::HashMap, sync::Arc};

use anyhow::Error;
use serde::{Deserialize, Serialize};

use crate::events::formats::EventCommonFields;

/// Version-specific fields for room versions 1 and 2.
#[derive(Serialize, Deserialize)]
pub struct EventFormatV1 {
    pub auth_events: Vec<(String, HashMap<String, String>)>,
    pub prev_events: Vec<(String, HashMap<String, String>)>,
    pub room_id: Arc<str>,
    pub event_id: Arc<str>,
}

impl EventFormatV1 {
    pub fn validate(&self, _common_fields: &EventCommonFields) -> Result<(), Error> {
        Ok(())
    }

    pub fn auth_event_ids(&self) -> Vec<String> {
        self.auth_events.iter().map(|(id, _)| id.clone()).collect()
    }

    pub fn prev_event_ids(&self) -> Vec<String> {
        self.prev_events.iter().map(|(id, _)| id.clone()).collect()
    }
}
