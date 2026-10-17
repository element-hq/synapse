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

//! What follows writing a chunk, before its stream orderings are
//! finished: `_after_persisting_events`. The metrics it counts, and the
//! MSC4293 `room_ban_redactions` rows of the bans and kicks that redact a
//! user's events, with the joins and leaves that end them.

use std::sync::Arc;

use anyhow::{anyhow, bail};
use futures::FutureExt;
use serde_json::Value;

use super::{context::PersistContext, extremities::is_mine_id, metrics::PersistCounts};
use crate::{
    database::postgres::{
        pool::ConnectionPool,
        write::{run_write_interaction, WriteError},
    },
    event_auth::is_creator,
    events::Event,
    state::StateKey,
    storage::{events::RedactBehaviour, sync_store::SyncReader},
    types::state_filter::StateFilter,
};

/// `TRACKED_EVENT_TYPES`: the event types counted by name in
/// `synapse_storage_events_persisted_events_sep`. Others count as
/// `*other*`.
const TRACKED_EVENT_TYPES: [&str; 7] = [
    "m.room.message",
    "m.room.encrypted",
    "m.room.member",
    "m.room.third_party_invite",
    "m.room.redaction",
    "m.room.create",
    "m.room.tombstone",
];

/// MSC4293's flag on a ban or kick: redact the user's events.
const REDACT_EVENTS: &str = "org.matrix.msc4293.redact_events";

/// What `_after_persisting_events` counts for the chunk `events`, every
/// entry of it, as Python counts them.
pub fn count_chunk(
    events: &[(Event, PersistContext)],
    server_name: &str,
    backfilled: bool,
) -> PersistCounts {
    let mut counts = PersistCounts {
        persisted_events: events.len() as u64,
        ..Default::default()
    };
    for (event, context) in events {
        let origin_type = if context.app_service {
            "application_service"
        } else if is_mine_id(event.sender(), server_name) {
            "local"
        } else {
            "remote"
        };
        let event_type = if TRACKED_EVENT_TYPES.contains(&event.type_()) {
            event.type_()
        } else {
            "*other*"
        };
        *counts
            .persisted_events_by_type
            .entry((event_type.to_owned(), origin_type))
            .or_default() += 1;
    }
    // Not to a negative position.
    if !backfilled {
        counts.persisted_position = events
            .last()
            .and_then(|(event, _)| event.internal_metadata().stream_ordering())
            .map(|ordering| ordering.get());
    }
    counts
}

/// The MSC4293 part of `_after_persisting_events`, for each event of the
/// chunk in order: a join, or a leave by someone else, ends the
/// redactions of the target's events, at its stream ordering (the events
/// stream's current token for a backfilled one). A ban, or a kick, with
/// `redact_events` set, by a sender who may redact, starts them.
///
/// Sets `started` once any starts, so that Python drops the room's events
/// from its event cache, even if a later event fails: Python drops them
/// right after each start.
pub async fn record_room_ban_redactions(
    pool: &ConnectionPool,
    reader: &SyncReader<'_>,
    room_id: &str,
    events: &[(Event, PersistContext)],
    events_token: impl Fn() -> i64,
    started: &mut bool,
) -> anyhow::Result<()> {
    for (event, _) in events {
        let Some(target) = event.state_key() else {
            continue;
        };
        if event.type_() != "m.room.member" {
            continue;
        }
        let membership = event
            .content()
            .get_field("membership")
            .and_then(Value::as_str);
        if matches!(membership, Some("leave" | "join")) {
            // A user leaving of their own accord changes nothing.
            if membership == Some("leave") && event.sender() == target {
                continue;
            }
            let mut ordering = event
                .internal_metadata()
                .stream_ordering()
                .ok_or_else(|| anyhow!("{} has no stream ordering", event.event_id()))?
                .get();
            if ordering < 0 {
                ordering = events_token();
            }
            end_redactions(pool, room_id, target, ordering).await?;
        }

        if !matches!(membership, Some("leave" | "ban")) {
            continue;
        }
        // Only a literal `true`.
        if event.content().get_field(REDACT_EVENTS) != Some(&Value::Bool(true)) {
            continue;
        }
        // Self-bans are not allowed.
        if membership == Some("ban") && event.sender() == target {
            continue;
        }
        if can_sender_redact(reader, room_id, event.sender()).await? {
            start_redactions(pool, room_id, target, event.event_id()).await?;
            *started = true;
        }
    }
    Ok(())
}

/// The `simple_update` that ends the target's redactions.
async fn end_redactions(
    pool: &ConnectionPool,
    room_id: &str,
    user_id: &str,
    ordering: i64,
) -> Result<(), WriteError> {
    run_write_interaction(
        pool,
        "room_ban_redactions update redact_end_ordering",
        |txn, _| {
            async move {
                txn.execute(
                    "UPDATE room_ban_redactions SET redact_end_ordering = $3
                WHERE room_id = $1 AND user_id = $2",
                    &[&room_id, &user_id, &ordering],
                )
                .await?;
                Ok(())
            }
            .boxed()
        },
    )
    .await
}

/// The `simple_upsert` that starts redacting the target's events.
async fn start_redactions(
    pool: &ConnectionPool,
    room_id: &str,
    user_id: &str,
    redacting_event_id: &str,
) -> Result<(), WriteError> {
    run_write_interaction(pool, "room_ban_redactions upsert", |txn, _| {
        async move {
            txn.execute(
                "INSERT INTO room_ban_redactions
                    (room_id, user_id, redacting_event_id, redact_end_ordering)
                VALUES ($1, $2, $3, NULL)
                ON CONFLICT (room_id, user_id) DO UPDATE SET
                    redacting_event_id = EXCLUDED.redacting_event_id,
                    redact_end_ordering = NULL",
                &[&room_id, &user_id, &redacting_event_id],
            )
            .await?;
            Ok(())
        }
        .boxed()
    })
    .await
}

/// `_can_sender_redact`: whether `sender` may redact others' events in the
/// room, by its current power levels and create event.
async fn can_sender_redact(
    reader: &SyncReader<'_>,
    room_id: &str,
    sender: &str,
) -> anyhow::Result<bool> {
    let filter = StateFilter::from_types([
        ("m.room.power_levels", Some("")),
        ("m.room.create", Some("")),
    ]);
    let state = reader.current_state_ids(room_id, &filter).await?;
    // Python indexes the state, so a room without either fails.
    let event_id = |event_type: &str| -> anyhow::Result<Arc<str>> {
        state
            .get(&StateKey::new(event_type, ""))
            .cloned()
            .ok_or_else(|| anyhow!("{room_id} has no {event_type} in its current state"))
    };
    let ids = [event_id("m.room.power_levels")?, event_id("m.room.create")?];
    let events = reader
        .events(&ids, RedactBehaviour::Redact, false, false)
        .await?;
    let find = |id: &Arc<str>| events.iter().find(|e| e.event_id() == &**id);
    let power_levels = find(&ids[0]);
    let Some(create) = find(&ids[1]) else {
        log::warn!("No create event found for room {room_id}");
        return Ok(false);
    };

    let power_levels = if create.room_version().msc4289_creator_power_enabled {
        // The creators have infinite power, everyone else 0 without power
        // levels, which is less than the default to redact (50).
        if is_creator(create, sender).map_err(|e| anyhow!("{e}"))? {
            return Ok(true);
        }
        match power_levels {
            Some(power_levels) => power_levels,
            None => return Ok(false),
        }
    } else {
        // Without power levels the creator has 100, everyone else 0.
        match power_levels {
            Some(power_levels) => power_levels,
            None => return Ok(create.sender() == sender),
        }
    };

    let content = power_levels.content();
    let sender_level = match content
        .get_field("users")
        .and_then(|users| users.get(sender))
        .filter(|level| !level.is_null())
    {
        Some(level) => level_of(level)?,
        None => field_level(content.get_field("users_default"))?,
    };
    let redact_level = match content.get_field("redact").filter(|l| !l.is_null()) {
        Some(level) => level_of(level)?,
        None => field_level(content.get_field("events_default"))?,
    };
    if let Some(level) = content
        .get_field("events")
        .and_then(|events| events.get("m.room.redaction"))
        .filter(|level| !level.is_null())
    {
        if sender_level < level_of(level)? {
            return Ok(false);
        }
    }
    Ok(sender_level >= redact_level)
}

/// A power level field Python reads with a default of 0.
fn field_level(value: Option<&Value>) -> anyhow::Result<f64> {
    match value.filter(|v| !v.is_null()) {
        Some(level) => level_of(level),
        None => Ok(0.0),
    }
}

/// A power level as Python compares it: any number, a bool as 0 or 1.
/// Python compares a string level only with another string, and raises
/// otherwise. Here a string of an integer is read as a number, and any
/// other string fails.
fn level_of(value: &Value) -> anyhow::Result<f64> {
    match value {
        Value::Number(n) => n
            .as_f64()
            .ok_or_else(|| anyhow!("power level {n} is not a number")),
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map(|level| level as f64)
            .map_err(|_| anyhow!("power level {s:?} is not an integer")),
        other => bail!("power level {other} is not a number"),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::{events::internal_metadata::EventInternalMetadata, room_versions::RoomVersion};

    #[test]
    fn levels_read_as_python_compares_them() {
        assert_eq!(level_of(&json!(50)).unwrap(), 50.0);
        assert_eq!(level_of(&json!(5.5)).unwrap(), 5.5);
        assert_eq!(level_of(&json!(true)).unwrap(), 1.0);
        assert_eq!(level_of(&json!("50")).unwrap(), 50.0);
        assert!(level_of(&json!("high")).is_err());
        assert!(level_of(&json!([])).is_err());
        assert_eq!(field_level(None).unwrap(), 0.0);
        assert_eq!(field_level(Some(&Value::Null)).unwrap(), 0.0);
    }

    fn event(event_type: &str, sender: &str, stream_ordering: i64) -> Event {
        let json = json!({
            "auth_events": [], "prev_events": [], "type": event_type,
            "sender": sender, "content": {}, "depth": 3,
            "room_id": "!r:here", "origin_server_ts": 5,
            "hashes": {"sha256": event_type}, "signatures": {}, "unsigned": {},
        });
        let metadata = EventInternalMetadata::from_json_str("{}").unwrap();
        metadata
            .set_persisted_position(Some(stream_ordering), None, None)
            .unwrap();
        Event::from_json_str(&json.to_string(), &RoomVersion::V10, metadata, None).unwrap()
    }

    #[test]
    fn chunks_are_counted_as_python_counts_them() {
        let app_service = PersistContext {
            app_service: true,
            ..Default::default()
        };
        let events = [
            (
                event("m.room.message", "@a:here", 4),
                PersistContext::default(),
            ),
            (
                event("m.room.message", "@b:there", 5),
                PersistContext::default(),
            ),
            (
                event("m.room.topic", "@a:here", 6),
                PersistContext::default(),
            ),
            (event("m.room.member", "@bot:here", 7), app_service),
        ];
        let counts = count_chunk(&events, "here", false);
        assert_eq!(counts.persisted_events, 4);
        let by_type = |event_type: &str, origin: &'static str| {
            counts
                .persisted_events_by_type
                .get(&(event_type.to_owned(), origin))
                .copied()
        };
        assert_eq!(by_type("m.room.message", "local"), Some(1));
        assert_eq!(by_type("m.room.message", "remote"), Some(1));
        assert_eq!(by_type("*other*", "local"), Some(1));
        assert_eq!(by_type("m.room.member", "application_service"), Some(1));
        assert_eq!(counts.persisted_events_by_type.len(), 4);
        assert_eq!(counts.persisted_position, Some(7));

        // No position for a backfilled chunk.
        let counts = count_chunk(&events, "here", true);
        assert_eq!(counts.persisted_position, None);
    }
}
