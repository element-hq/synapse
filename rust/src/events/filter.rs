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
 */

use std::collections::HashMap;

use pyo3::{exceptions::PyValueError, pyfunction, PyResult};

use crate::{
    identifier::UserID,
    matrix_const::{
        HISTORY_VISIBILITY_INVITED, HISTORY_VISIBILITY_JOINED, MEMBERSHIP_INVITE, MEMBERSHIP_JOIN,
    },
};

#[pyfunction(name = "event_visible_to_server")]
pub fn event_visible_to_server_py(
    sender: String,
    target_server_name: String,
    history_visibility: String,
    erased_senders: HashMap<String, bool>,
    partial_state_invisible: bool,
    memberships: Vec<(String, String)>, // (state_key, membership)
) -> PyResult<bool> {
    event_visible_to_server(
        sender,
        target_server_name,
        history_visibility,
        erased_senders,
        partial_state_invisible,
        memberships,
    )
    .map_err(|e| PyValueError::new_err(format!("{e}")))
}

/// Return whether the target server is allowed to see the event.
///
/// For a fully stated room, the target server is allowed to see an event E if:
///   - the state at E has world readable or shared history vis, OR
///   - the state at E says that the target server is in the room.
///
/// For a partially stated room, the target server is allowed to see E if:
///   - E was created by this homeserver, AND:
///       - the partial state at E has world readable or shared history vis, OR
///       - the partial state at E says that the target server is in the room.
pub fn event_visible_to_server(
    sender: String,
    target_server_name: String,
    history_visibility: String,
    erased_senders: HashMap<String, bool>,
    partial_state_invisible: bool,
    memberships: Vec<(String, String)>, // (state_key, membership)
) -> anyhow::Result<bool> {
    if let Some(&erased) = erased_senders.get(&sender) {
        if erased {
            return Ok(false);
        }
    }

    if partial_state_invisible {
        return Ok(false);
    }

    if history_visibility != HISTORY_VISIBILITY_INVITED
        && history_visibility != HISTORY_VISIBILITY_JOINED
    {
        return Ok(true);
    }

    let mut visible = false;
    for (state_key, membership) in memberships {
        let state_key = UserID::try_from(state_key.as_ref())
            .map_err(|e| anyhow::anyhow!(format!("invalid user_id ({state_key}): {e}")))?;
        if state_key.server_name() != target_server_name {
            return Err(anyhow::anyhow!(
                "state_key.server_name ({}) does not match target_server_name ({target_server_name})",
                state_key.server_name()
            ));
        }

        match membership.as_str() {
            MEMBERSHIP_INVITE => {
                if history_visibility == HISTORY_VISIBILITY_INVITED {
                    visible = true;
                    break;
                }
            }
            MEMBERSHIP_JOIN => {
                visible = true;
                break;
            }
            _ => continue,
        }
    }

    Ok(visible)
}
