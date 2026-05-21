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

//! Event format for [MSC4242] (prev-state events).
//!
//! Adds `prev_state_events` and removes `auth_events` from the v4 layout
//! — auth chains are derived implicitly from the state DAG rather than
//! carried on each event. `room_id`, `prev_events` and the create-event
//! derivation rules carry over unchanged from v4 and are delegated to
//! [`EventFormatV4::validate`] via a shim that supplies an empty
//! explicit auth list.
//!
//! [MSC4242]: https://github.com/matrix-org/matrix-spec-proposals/pull/4242

use std::borrow::Cow;

use anyhow::Error;
use pyo3::exceptions::PyRuntimeError;
use pyo3::PyResult;
use serde::{Deserialize, Serialize};

use crate::events::constants::event_type::M_ROOM_CREATE;
use crate::events::formats::v4::get_room_id_for_optional_room_id;
use crate::events::formats::v4::validate_optional_room_id;
use crate::events::formats::EventCommonFields;
use crate::events::Event;

/// Version-specific fields for the MSC4242 event format.
#[derive(Serialize, Deserialize)]
pub struct EventFormatVMSC4242 {
    pub prev_state_events: Vec<String>,
    pub prev_events: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub room_id: Option<Box<str>>,
}

impl EventFormatVMSC4242 {
    pub fn validate(&self, common_fields: &EventCommonFields) -> Result<(), Error> {
        validate_optional_room_id(self.room_id.as_deref(), common_fields)?;

        Ok(())
    }

    pub fn room_id(
        &self,
        event_id: &str,
        common_fields: &EventCommonFields,
    ) -> Result<Cow<'_, str>, Error> {
        get_room_id_for_optional_room_id(self.room_id.as_deref(), event_id, common_fields)
    }

    pub fn auth_event_ids(&self, event: &Event) -> PyResult<Vec<String>> {
        // In the MSC4242 format, the auth events are calculated and stored in
        // internal metadata.
        let auth_event_ids = event.internal_metadata.get_calculated_auth_event_ids()?;

        // Catches cases where we accidentally call auth_event_ids() prior to calculating what they
        // actually are. The exception being the m.room.create event which has no auth events.
        if event.fields.common_fields.type_state_key_tuple() != Some((M_ROOM_CREATE, ""))
            && auth_event_ids.is_empty()
        {
            return Err(PyRuntimeError::new_err(format!(
                "auth_event_ids has not been calculated: {}",
                event.event_id
            )));
        }

        Ok(auth_event_ids)
    }
}
