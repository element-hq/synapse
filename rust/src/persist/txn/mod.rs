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
//! The steps are the events themselves ([`events`]), the room's DAG
//! ([`edges`]), the tables about the events ([`metadata`]), the room's
//! current state ([`current_state`]) with its profile updates
//! ([`profile`]), sticky events ([`sticky`]) and the sliding sync tables
//! ([`sliding_sync`]), with the rows of the cache stream ([`caches`]) and
//! the IDs of the streams the transaction takes from ([`streams`]).
//! `tests/storage/test_rust_persist_txn.py` persists a chunk through Rust
//! and compares the rows with Python's.

pub mod caches;
pub mod current_state;
pub mod edges;
pub mod events;
pub mod json;
pub mod metadata;
pub mod profile;
pub mod sliding_sync;
pub mod sticky;
pub mod streams;

use std::collections::HashMap;

use anyhow::anyhow;
use pyo3::PyErr;
use serde_json::Value;

use self::streams::TxnStream;
use super::{
    context::PersistContext,
    effects::{EventInvalidation, PersistEffects},
    prepare::PreparedChunk,
    sliding_sync::SlidingSyncTableChanges,
    state_delta::DeltaState,
};
use crate::{
    clock::Clock,
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

/// An event on its way through the transaction: an event of the chunk, or
/// its redacted copy once [`events::apply_existing_redactions`] finds it
/// already redacted.
pub struct Persisting<'a> {
    pub source: &'a TxnEvent,
    /// The event as it is stored, if it is redacted.
    pub redacted: Option<Event>,
}

impl Persisting<'_> {
    pub fn event(&self) -> &Event {
        self.redacted.as_ref().unwrap_or(&self.source.event)
    }

    pub fn context(&self) -> &PersistContext {
        &self.source.context
    }

    pub fn stream_ordering(&self) -> i64 {
        self.source.stream_ordering
    }
}

/// What the transaction takes besides the chunk.
pub struct TxnInput<'a> {
    pub room_id: &'a str,
    /// The chunk, in order. It may hold the same event twice.
    pub events: &'a [TxnEvent],
    /// What [`super::prepare`] computed for the chunk.
    pub prepared: &'a PreparedChunk,
    /// This instance, which writes the events.
    pub instance_name: &'a str,
    /// This server's name, for which users are ours.
    pub server_name: &'a str,
    pub clock: &'a Clock,
    /// The chunk was backfilled, so its memberships are not current.
    pub inhibit_local_membership_updates: bool,
    /// `server.enable_search`.
    pub enable_search: bool,
    /// `server.enable_ephemeral_messages`.
    pub enable_ephemeral_messages: bool,
    /// `experimental_features.msc4354_enabled`: sticky events.
    pub msc4354_enabled: bool,
    /// `server.include_profile_updates_in_sync`.
    pub include_profile_updates_in_sync: bool,
    /// The cache stream IDs of this persist, over its attempts.
    pub cache_stream: &'a TxnStream,
    /// The sticky events stream IDs of this persist, over its attempts.
    pub sticky_stream: &'a TxnStream,
    /// The profile updates stream IDs of this persist, over its attempts.
    pub profile_stream: &'a TxnStream,
}

impl TxnInput<'_> {
    /// The time, read when a step needs it, as Python reads its clock.
    fn now_ms(&self) -> Result<i64, WriteError> {
        Ok(i64::try_from(self.clock.now_millis()).map_err(anyhow::Error::new)?)
    }

    /// The streams the transaction takes IDs from.
    fn streams(&self) -> [&TxnStream; 3] {
        [self.cache_stream, self.sticky_stream, self.profile_stream]
    }
}

/// The chunk has an event with partial state, but the room no longer has
/// partial state. Python raises `PartialStateConflictError`, and the caller
/// computes the event's context again.
#[derive(Debug)]
pub struct PartialStateConflict;

impl std::fmt::Display for PartialStateConflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Cannot persist partial state event in un-partial stated room")
    }
}

impl std::error::Error for PartialStateConflict {}

/// An error reading an event's fields through the Python-facing accessors.
fn metadata_error(e: PyErr) -> WriteError {
    WriteError::Other(anyhow::Error::new(e))
}

/// Write the chunk, as `_persist_events_txn` does, adding to `effects` what
/// Python would queue with `txn.call_after`.
///
/// `effects` must start empty: a retried transaction starts again. Once
/// it commits, hand the stream IDs in `effects` to the streams of `input`.
pub async fn persist_events_txn(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    // An earlier attempt whose commit failed left its stream IDs.
    for stream in input.streams() {
        stream.finish_pending();
    }
    let result = persist_events_txn_steps(txn, input, effects).await;
    if result.is_err() {
        // The rows are rolled back, so their IDs are done with, as Python's
        // `call_on_exception` has it.
        for stream in input.streams() {
            stream.finish_pending();
        }
    }
    result
}

/// `_update_current_state_txn`, for `update_current_state`: write the
/// change `delta` to the room's current state, with `stream_id` from the
/// events stream, adding to `effects` what Python would queue. `input`
/// has no events. As [`persist_events_txn`], `effects` must start empty.
pub async fn update_current_state_txn(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    delta: &DeltaState,
    changes: &SlidingSyncTableChanges,
    stream_id: i64,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    for stream in input.streams() {
        stream.finish_pending();
    }
    let result =
        current_state::update_current_state(txn, input, delta, changes, stream_id, effects).await;
    if result.is_err() {
        for stream in input.streams() {
            stream.finish_pending();
        }
    }
    result
}

async fn persist_events_txn_steps<'a>(
    txn: &WriteTxn,
    input: &TxnInput<'a>,
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let room_id = input.room_id;

    // Protect against a race with deleting the room: the share lock holds
    // off a delete until we commit.
    let rows = txn
        .query(
            "SELECT room_version FROM rooms WHERE room_id = $1 FOR SHARE",
            &[&room_id],
        )
        .await?;
    if rows.is_empty() {
        return Err(anyhow!("Room does not exist {room_id}").into());
    }

    let (Some(first), Some(last)) = (input.events.first(), input.events.last()) else {
        return Err(anyhow!("no events to persist in {room_id}").into());
    };
    let min_stream_order = first.stream_ordering;
    let max_stream_order = last.stream_ordering;
    // Python takes the first event's room version for the whole chunk.
    let room_version = first.event.room_version();

    // Recorded for every event of the chunk, before any are dropped.
    for event in input.events {
        add_event_effects(event, effects);
    }

    let events: Vec<Persisting<'a>> = filter_duplicates(input.events)?
        .into_iter()
        .map(|source| Persisting {
            source,
            redacted: None,
        })
        .collect();

    events::update_room_depths(txn, room_id, &events, effects).await?;

    let events = events::update_outliers(txn, input, events, effects).await?;

    // From here on, only events we have not seen before.
    let events = events::apply_existing_redactions(txn, room_id, room_version, events).await?;
    let newly_redacted = events::newly_redacted_event_ids(txn, room_id, &events).await?;
    // Redacted events stop being sticky. A sticky event de-outliered in
    // this chunk does not: its row has this chunk's stream ordering, but its
    // `events` row keeps its first one, which the delete looks for. A Python
    // bug, kept.
    events::delete_sticky_events(txn, &newly_redacted).await?;

    events::store_events(txn, input, &events).await?;

    let state = &input.prepared.state;
    if let Some(extremities) = state
        .new_forward_extremities
        .as_ref()
        .filter(|e| !e.is_empty())
    {
        edges::update_forward_extremities(txn, room_id, extremities, max_stream_order).await?;
    }
    if let Some(extremities) = state
        .new_state_dag_extremities
        .as_ref()
        .filter(|e| !e.is_empty())
    {
        edges::set_state_dag_extremities(txn, room_id, extremities).await?;
    }

    events::persist_transaction_ids(txn, input, &events).await?;
    events::store_state_mappings(txn, &events, effects).await?;
    events::persist_auth_chain(txn, &events, &input.prepared.new_event_links).await?;

    // From here on, only events that were not rejected.
    let events = events::store_rejections(txn, input, events).await?;

    metadata::update_metadata_tables(txn, input, &events, effects).await?;

    // Last, as it reads the memberships the metadata step stored.
    if let Some(delta) = &state.state_delta {
        let changes = input.prepared.sliding_sync.as_ref().ok_or_else(|| {
            anyhow!("a state delta for {room_id} without its sliding sync changes")
        })?;
        current_state::update_current_state(txn, input, delta, changes, min_stream_order, effects)
            .await?;
    }

    if input.msc4354_enabled {
        let new_events: Vec<(&Event, i64)> = events
            .iter()
            .map(|e| (e.event(), e.stream_ordering()))
            .collect();
        sticky::insert_sticky_events(txn, input, &new_events, effects).await?;
        // Those the state delta just applied lets pass auth.
        let un_soft_fail: Vec<&str> = input
            .prepared
            .sticky_events_to_un_soft_fail
            .iter()
            .map(|id| &**id)
            .collect();
        sticky::un_soft_fail_sticky_events(txn, input, &un_soft_fail, effects).await?;
    }

    sliding_sync::update_sliding_sync_joined_room(txn, room_id, &events).await?;

    Ok(())
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
