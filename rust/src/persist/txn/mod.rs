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

//! The write transaction of a chunk: `_persist_events_txn`.
//!
//! [`persist_events_txn`] runs inside a [`WriteTxn`] and takes what
//! [`super::prepare`] computed. It writes the rows Python's transaction
//! writes, in the same order, and collects in a [`PersistEffects`] the
//! cache updates Python queues with `txn.call_after`.
//!
//! This is being ported a group of steps at a time, from the start of the
//! transaction. So far it covers the steps up to dropping duplicate events,
//! and returns the events left at that point. The Python steps after it run
//! only in `tests/storage/test_rust_persist_txn.py`, which persists the
//! rest of a chunk in Python and compares the rows with Python's.

use std::collections::HashMap;

use anyhow::anyhow;
use serde_json::Value;

use super::{context::PersistContext, effects::EventInvalidation, effects::PersistEffects};
use crate::{
    database::postgres::write::{WriteError, WriteTxn},
    events::{
        constants::{event_field::M_RELATES_TO, relation_type},
        Event,
    },
};

/// One event of the chunk, with its stream ordering already set on its
/// internal metadata.
pub struct TxnEvent {
    pub event: Event,
    pub context: PersistContext,
    pub stream_ordering: i64,
}

impl TxnEvent {
    pub fn new(event: Event, context: PersistContext) -> anyhow::Result<Self> {
        let stream_ordering = event
            .internal_metadata()
            .stream_ordering()
            .ok_or_else(|| anyhow!("{} has no stream ordering", event.event_id()))?
            .get();
        Ok(TxnEvent {
            event,
            context,
            stream_ordering,
        })
    }

    fn event_id(&self) -> &str {
        self.event.event_id()
    }

    fn is_outlier(&self) -> Result<bool, WriteError> {
        self.event.internal_metadata().is_outlier().map_err(|e| {
            anyhow::Error::new(e)
                .context(format!("reading {}", self.event_id()))
                .into()
        })
    }
}

/// What the transaction takes besides the chunk.
pub struct TxnInput<'a> {
    pub room_id: &'a str,
    /// The chunk, in order. It may hold the same event twice.
    pub events: &'a [TxnEvent],
}

/// Write the chunk, as `_persist_events_txn` does, adding to `effects` what
/// Python would queue with `txn.call_after`. Returns the events left once
/// duplicates are dropped, which the steps still to be ported take.
///
/// `effects` must start empty: a retried transaction starts again.
pub async fn persist_events_txn<'a>(
    txn: &WriteTxn,
    input: &TxnInput<'a>,
    effects: &mut PersistEffects,
) -> Result<Vec<&'a TxnEvent>, WriteError> {
    // Protect against a race with deleting the room: the share lock holds
    // off a delete until we commit.
    let rows = txn
        .query(
            "SELECT room_version FROM rooms WHERE room_id = $1 FOR SHARE",
            &[&input.room_id],
        )
        .await?;
    if rows.is_empty() {
        return Err(anyhow!("Room does not exist {}", input.room_id).into());
    }

    // Recorded for every event of the chunk, before any are dropped.
    for event in input.events {
        add_event_effects(event, effects);
    }

    filter_duplicates(input.events)
}

/// The cache updates Python queues for each event of the chunk at the start
/// of the transaction.
fn add_event_effects(event: &TxnEvent, effects: &mut PersistEffects) {
    let redacts = redacts(&event.event);
    effects.add_event_cache_invalidation(event.event_id().to_owned());
    if let Some(redacts) = redacts.filter(|r| !r.is_empty()) {
        effects.add_event_cache_invalidation(redacts.to_owned());
    }
    effects.add_event(EventInvalidation {
        stream_ordering: event.stream_ordering,
        event_id: event.event_id().to_owned(),
        room_id: event.event.room_id().to_owned(),
        event_type: event.event.type_().to_owned(),
        state_key: event.event.state_key().map(str::to_owned),
        redacts: redacts.map(str::to_owned),
        relates_to: relation(&event.event).map(|r| r.parent_id.to_owned()),
        // Python passes `False` for backfilled events too.
        backfilled: false,
    });
}

/// `_filter_events_and_contexts_for_duplicates`: one of each event, the
/// first that is not an outlier if there is one, else the first, in the
/// order each event was first seen.
fn filter_duplicates(events: &[TxnEvent]) -> Result<Vec<&TxnEvent>, WriteError> {
    // Slots in first-seen order. A replaced event leaves an empty slot.
    let mut slots: Vec<Option<&TxnEvent>> = Vec::with_capacity(events.len());
    let mut index_of: HashMap<&str, usize> = HashMap::with_capacity(events.len());
    for event in events {
        match index_of.get(event.event_id()) {
            None => {
                index_of.insert(event.event_id(), slots.len());
                slots.push(Some(event));
            }
            Some(&index) => {
                let kept = slots[index].expect("the slot of a kept event is full");
                // Python pops the outlier and appends the event, so it moves
                // to the end.
                if !event.is_outlier()? && kept.is_outlier()? {
                    slots[index] = None;
                    index_of.insert(event.event_id(), slots.len());
                    slots.push(Some(event));
                }
            }
        }
    }
    Ok(slots.into_iter().flatten().collect())
}

/// `event.redacts`, if it is a string. Python passes on any value, but an
/// event whose `redacts` is not a string fails validation before it gets
/// here.
pub(super) fn redacts(event: &Event) -> Option<&str> {
    event.redacts().and_then(Value::as_str)
}

/// What `relation_from_event` parses from an event's `m.relates_to`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct Relation<'a> {
    pub parent_id: &'a str,
    pub rel_type: &'a str,
    /// For an annotation, its `key` if it is a string.
    pub aggregation_key: Option<&'a str>,
}

/// `relation_from_event`: the event's relation, if it is well formed.
pub(super) fn relation(event: &Event) -> Option<Relation<'_>> {
    let relation = event.content().get_field(M_RELATES_TO)?.as_object()?;
    let rel_type = relation.get("rel_type")?.as_str()?;
    let parent_id = relation.get("event_id")?.as_str()?;
    let aggregation_key = if rel_type == relation_type::ANNOTATION {
        relation.get("key").and_then(Value::as_str)
    } else {
        None
    };
    Some(Relation {
        parent_id,
        rel_type,
        aggregation_key,
    })
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::{events::internal_metadata::EventInternalMetadata, room_versions::RoomVersion};

    fn event(body: &str, content: Value, outlier: bool) -> Event {
        let json = json!({
            "auth_events": [], "prev_events": [], "type": "m.room.message",
            "sender": "@a:example.com", "content": content, "depth": 3,
            "room_id": "!r:example.com", "origin_server_ts": 5,
            "hashes": {"sha256": body}, "signatures": {}, "unsigned": {},
        });
        let metadata = EventInternalMetadata::from_json_str("{}").unwrap();
        metadata.set_outlier_flag(outlier).unwrap();
        metadata
            .set_persisted_position(Some(1), None, None)
            .unwrap();
        Event::from_json_str(&json.to_string(), &RoomVersion::V10, metadata, None).unwrap()
    }

    fn txn_event(body: &str, outlier: bool) -> TxnEvent {
        TxnEvent::new(event(body, json!({}), outlier), PersistContext::default()).unwrap()
    }

    #[test]
    fn duplicates_keep_the_first_non_outlier() {
        // The same event three times, as an outlier first, and another.
        let events = [
            txn_event("a", true),
            txn_event("b", false),
            txn_event("a", false),
            txn_event("a", false),
        ];
        let kept = filter_duplicates(&events).unwrap();
        // The first non-outlier copy of `a` replaces the outlier and moves
        // to the end.
        assert_eq!(kept.len(), 2);
        assert!(std::ptr::eq(kept[0], &events[1]));
        assert!(std::ptr::eq(kept[1], &events[2]));

        // Without a non-outlier copy, the first is kept in place.
        let events = [txn_event("a", true), txn_event("a", true)];
        let kept = filter_duplicates(&events).unwrap();
        assert_eq!(kept.len(), 1);
        assert!(std::ptr::eq(kept[0], &events[0]));
    }

    #[test]
    fn relation_is_parsed_as_python_does() {
        let relation_of = |relates_to: Value| {
            let event = event("a", json!({ "m.relates_to": relates_to }), false);
            relation(&event).map(|r| {
                (
                    r.parent_id.to_owned(),
                    r.rel_type.to_owned(),
                    r.aggregation_key.map(str::to_owned),
                )
            })
        };
        assert_eq!(
            relation_of(json!({"rel_type": "m.thread", "event_id": "$p"})),
            Some(("$p".into(), "m.thread".into(), None))
        );
        assert_eq!(
            relation_of(json!({"rel_type": "m.annotation", "event_id": "$p", "key": "👍"})),
            Some(("$p".into(), "m.annotation".into(), Some("👍".into())))
        );
        // Only an annotation has a key, and only a string one.
        assert_eq!(
            relation_of(json!({"rel_type": "m.reference", "event_id": "$p", "key": "k"})),
            Some(("$p".into(), "m.reference".into(), None))
        );
        assert_eq!(
            relation_of(json!({"rel_type": "m.annotation", "event_id": "$p", "key": 1})),
            Some(("$p".into(), "m.annotation".into(), None))
        );
        // Malformed relations are ignored.
        assert_eq!(relation_of(json!({"rel_type": "m.thread"})), None);
        assert_eq!(relation_of(json!({"rel_type": 1, "event_id": "$p"})), None);
        assert_eq!(
            relation_of(json!({"rel_type": "m.thread", "event_id": 1})),
            None
        );
        assert_eq!(relation_of(json!(["$p"])), None);
        assert_eq!(relation_of(json!({})), None);
    }
}
