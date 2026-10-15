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

//! The JSON of the joined and archived rooms of a sync response: the
//! servlet's `encode_room` over the Rust room entries, so that their events
//! never become Python objects.
//!
//! Python builds one [`RoomsEncoder`] per response, with what the room
//! entries do not hold: the parts of the response's tokens other than the
//! room key, and which fields the request asked for.

use std::collections::HashMap;

use log::warn;
use pyo3::{exceptions::PyValueError, prelude::*};
use pythonize::pythonize;
use serde_json::{json, Map, Value};

use crate::events::json_object::JsonObject;
use crate::events::relations::BundledAggregations;
use crate::events::serialize::{
    serialize_event, BundleRef, PyLookups, SerializeEventConfig, SerializeLookups,
};
use crate::events::Event;
use crate::sync::request::{PrevBatchBase, RoomSyncResult, RoomSyncResultData, RoomType};
use crate::sync::rooms::TokenBase;
use crate::types::stream_token::RoomStreamToken;

/// The lookups of one room: the bundles and redactions the Rust handler
/// loaded for it, then Python's maps.
struct RoomLookups<'a, 'py> {
    room: &'a RoomSyncResultData,
    python: &'a PyLookups<'py>,
}

impl SerializeLookups for RoomLookups<'_, '_> {
    fn redaction(&self, event_id: &str) -> Option<&Event> {
        self.room
            .redactions
            .get(event_id)
            .or_else(|| self.python.redaction(event_id))
    }

    fn bundle(&self, event_id: &str) -> Option<BundleRef<'_>> {
        match self.room.bundles.get(event_id) {
            Some(bundle) => Some(bundle.as_ref()),
            None => self.python.bundle(event_id),
        }
    }

    fn unsigned_additions(&self, event_id: &str) -> Option<&JsonObject> {
        self.python.unsigned_additions(event_id)
    }
}

/// Encodes the joined and archived rooms of one response.
#[pyclass(frozen)]
pub struct RoomsEncoder {
    now_room_key: RoomStreamToken,
    /// The response's now token after its room key: everything after the
    /// first `_`.
    now_suffix: String,
    /// The same for the since token, if there is one.
    since_suffix: Option<String>,
    /// `get_id_for_instance` for the writers the tokens name.
    instance_ids: HashMap<String, i64>,
    /// `state`, `state_after`, or MSC4222's unstable name for it.
    state_key_name: String,
    msc3773_enabled: bool,
    msc2654_enabled: bool,
}

#[pymethods]
impl RoomsEncoder {
    #[new]
    #[pyo3(signature = (*, now_room_key, now_suffix, since_suffix, instance_ids, state_key_name, msc3773_enabled, msc2654_enabled))]
    fn py_new(
        now_room_key: RoomStreamToken,
        now_suffix: String,
        since_suffix: Option<String>,
        instance_ids: HashMap<String, i64>,
        state_key_name: String,
        msc3773_enabled: bool,
        msc2654_enabled: bool,
    ) -> Self {
        RoomsEncoder {
            now_room_key,
            now_suffix,
            since_suffix,
            instance_ids,
            state_key_name,
            msc3773_enabled,
            msc2654_enabled,
        }
    }

    /// The encoded `rooms`, in order, as `encode_room` returns them.
    /// `bundle_aggregations`, `redaction_map` and `unsigned_additions` are
    /// as for `serialize_events`, across all the rooms.
    #[pyo3(signature = (rooms, time_now_ms, config, *, bundle_aggregations = None, redaction_map = None, unsigned_additions = None))]
    #[allow(clippy::too_many_arguments)]
    fn encode<'py>(
        &self,
        py: Python<'py>,
        rooms: Vec<Bound<'py, RoomSyncResult>>,
        time_now_ms: i64,
        config: &SerializeEventConfig,
        bundle_aggregations: Option<HashMap<String, Bound<'py, BundledAggregations>>>,
        redaction_map: Option<HashMap<String, Bound<'py, Event>>>,
        unsigned_additions: Option<HashMap<String, JsonObject>>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let python = PyLookups {
            bundles: bundle_aggregations.unwrap_or_default(),
            redactions: redaction_map.unwrap_or_default(),
            unsigned_additions: unsigned_additions.unwrap_or_default(),
        };
        rooms
            .iter()
            .map(|room| {
                let encoded = self.encode_room(&room.get().data, time_now_ms, config, &python)?;
                Ok(pythonize(py, &encoded)?)
            })
            .collect()
    }
}

impl RoomsEncoder {
    /// `prev_batch`: the token Python's `TimelineBatch` holds, as a string.
    fn prev_batch(&self, room: &RoomSyncResultData) -> PyResult<String> {
        let upto = room
            .upto
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("the room's upto token is not set"))?;
        let (base, base_room_key) = match room.prev_batch_base {
            PrevBatchBase::Now => (TokenBase::Now, &self.now_room_key),
            PrevBatchBase::Upto => (upto.base, &upto.room_key),
        };
        let room_key = room.prev_batch_room_key.as_ref().unwrap_or(base_room_key);
        let suffix = match base {
            TokenBase::Now => &self.now_suffix,
            TokenBase::Since => self
                .since_suffix
                .as_ref()
                .ok_or_else(|| PyValueError::new_err("prev_batch needs a since token"))?,
        };
        let room_key = room_key
            .to_string_with_instances(|name| self.instance_ids.get(name).copied())
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(format!("{room_key}_{suffix}"))
    }

    fn encode_room(
        &self,
        room: &RoomSyncResultData,
        time_now_ms: i64,
        config: &SerializeEventConfig,
        python: &PyLookups<'_>,
    ) -> PyResult<Value> {
        let lookups = RoomLookups { room, python };

        // We've had bug reports that events were coming down under the wrong
        // room.
        let events = room
            .state
            .iter()
            .chain(room.timeline.iter().map(|filtered| &filtered.event));
        for event in events {
            if event.room_id() != &*room.room_id {
                warn!(
                    "Event {:?} is under room {:?} instead of {:?}",
                    event.event_id(),
                    room.room_id,
                    event.room_id(),
                );
            }
        }

        let state = room
            .state
            .iter()
            .map(|event| serialize_event(event, time_now_ms, config, None, false, &lookups))
            .map(|event| event.map(Value::Object))
            .collect::<PyResult<Vec<_>>>()?;
        let timeline = room
            .timeline
            .iter()
            .map(|filtered| {
                serialize_event(
                    &filtered.event,
                    time_now_ms,
                    config,
                    Some(&filtered.membership),
                    true,
                    &lookups,
                )
            })
            .map(|event| event.map(Value::Object))
            .collect::<PyResult<Vec<_>>>()?;

        let mut result = Map::new();
        result.insert(
            "timeline".to_owned(),
            json!({
                "events": timeline,
                "prev_batch": self.prev_batch(room)?,
                "limited": room.limited,
            }),
        );
        result.insert(self.state_key_name.clone(), json!({ "events": state }));
        result.insert(
            "account_data".to_owned(),
            json!({ "events": room.account_data }),
        );

        if room.rtype == RoomType::Joined {
            result.insert("ephemeral".to_owned(), json!({ "events": room.ephemeral }));
            result.insert(
                "unread_notifications".to_owned(),
                json!(room.unread_notifications),
            );
            if !room.sticky.is_empty() {
                // The sticky events have already been deduplicated so that
                // events appearing in the timeline won't appear again here.
                let sticky = room
                    .sticky
                    .iter()
                    .map(|filtered| {
                        serialize_event(
                            &filtered.event,
                            time_now_ms,
                            config,
                            Some(&filtered.membership),
                            false,
                            &lookups,
                        )
                    })
                    .map(|event| event.map(Value::Object))
                    .collect::<PyResult<Vec<_>>>()?;
                result.insert("msc4354_sticky".to_owned(), json!({ "events": sticky }));
            }
            if !room.unread_thread_notifications.is_empty() {
                let threads: Map<String, Value> = room
                    .unread_thread_notifications
                    .iter()
                    .map(|(thread_id, notification_count, highlight_count)| {
                        (
                            thread_id.to_string(),
                            json!({
                                "notification_count": notification_count,
                                "highlight_count": highlight_count,
                            }),
                        )
                    })
                    .collect();
                if self.msc3773_enabled {
                    result.insert(
                        "org.matrix.msc3773.unread_thread_notifications".to_owned(),
                        Value::Object(threads.clone()),
                    );
                }
                result.insert(
                    "unread_thread_notifications".to_owned(),
                    Value::Object(threads),
                );
            }
            result.insert(
                "summary".to_owned(),
                room.summary.clone().unwrap_or(Value::Null),
            );
            if self.msc2654_enabled {
                result.insert(
                    "org.matrix.msc2654.unread_count".to_owned(),
                    json!(room.unread_count),
                );
            }
        }

        Ok(Value::Object(result))
    }
}
