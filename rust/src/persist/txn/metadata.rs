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

//! `_update_metadata_tables_txn`: the tables about the events, written once
//! the events themselves are stored. Push actions, redactions, relations
//! and threads, search, retention, labels, expiry and room members.
//!
//! The event cache prefill at its end, `_add_to_cache`, is chosen here and
//! done by Python, which holds the events.

use anyhow::anyhow;
use serde_json::{Map, Value};
use tokio_postgres::types::ToSql;

use super::{caches::invalidate_cache_and_stream, edges, redacts, relation, Persisting, TxnInput};
use crate::events::constants::event_type::{
    M_ROOM_CREATE as CREATE, M_ROOM_ENCRYPTION as ENCRYPTION, M_ROOM_MESSAGE as MESSAGE,
    M_ROOM_NAME as NAME, M_ROOM_RETENTION as RETENTION, M_ROOM_TOMBSTONE as TOMBSTONE,
    M_ROOM_TOPIC as TOPIC,
};
use crate::{
    database::postgres::write::{Column, WriteError, WriteTxn},
    events::{
        constants::{event_type, relation_type},
        json_object::JsonObject,
        Event,
    },
    persist::{effects::PersistEffects, extremities::is_mine_id},
};

/// `EventContentFields.LABELS`.
const LABELS: &str = "org.matrix.labels";
/// `EventContentFields.SELF_DESTRUCT_AFTER`.
const SELF_DESTRUCT_AFTER: &str = "org.matrix.self_destruct_after";

/// `_update_metadata_tables_txn` over `events`, the events left once the
/// rejected ones are stored. `input.events` is the whole chunk.
pub async fn update_metadata_tables(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    set_push_actions(txn, input, events).await?;
    if events.is_empty() {
        return Ok(());
    }

    for persisting in events {
        let event = persisting.event();
        if event.type_() != event_type::M_ROOM_REDACTION {
            continue;
        }
        let Some(redacted) = redacts(event) else {
            continue;
        };
        txn.execute(
            "DELETE FROM event_push_actions WHERE room_id = $1 AND event_id = $2",
            &[&event.room_id(), &redacted],
        )
        .await?;
        handle_redact_relations(txn, input, effects, redacted).await?;
    }

    edges::handle_mult_prev_events(txn, input.instance_name, events).await?;

    store_search(txn, input, events).await?;
    store_redactions(txn, input, events).await?;
    for persisting in events {
        store_retention_policy(txn, input, effects, persisting.event()).await?;
    }
    store_relations(txn, events).await?;
    edges::store_state_dag_edges(txn, events).await?;
    store_labels(txn, events).await?;
    if input.enable_ephemeral_messages {
        store_expiry(txn, events).await?;
    }

    store_room_members(txn, input, events).await?;
    add_to_cache(txn, events, effects).await
}

/// `_add_to_cache`: the events to put in the event cache once the
/// transaction commits, those neither rejected nor redacted. Python puts
/// them in, from the events it holds.
async fn add_to_cache(
    txn: &WriteTxn,
    events: &[Persisting<'_>],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let event_ids: Vec<&str> = events.iter().map(|e| e.event().event_id()).collect();
    let rows = txn
        .query(
            "SELECT e.event_id, r.redacts, rej.event_id
            FROM events AS e
            LEFT JOIN rejections AS rej USING (event_id)
            LEFT JOIN redactions AS r ON e.event_id = r.redacts
            WHERE e.event_id = ANY($1)",
            &[&event_ids],
        )
        .await?;
    for row in rows {
        let event_id: String = row.try_get(0).map_err(anyhow::Error::new)?;
        let redacts: Option<&str> = row.try_get(1).map_err(anyhow::Error::new)?;
        let rejects: Option<&str> = row.try_get(2).map_err(anyhow::Error::new)?;
        if redacts.is_none() && rejects.is_none() {
            effects.add_event_cache_prefill(event_id);
        }
    }
    Ok(())
}

/// `_set_push_actions_for_event_and_users_txn`: move the push actions of
/// `events` from the staging table, and clear the staging rows of the whole
/// chunk, `input.events`. Only notifiable events have push actions.
async fn set_push_actions(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut stream_orderings = Vec::new();
    let mut depths = Vec::new();
    for persisting in events {
        let event = persisting.event();
        if event
            .internal_metadata()
            .is_notifiable()
            .map_err(py_error)?
        {
            event_ids.push(event.event_id());
            stream_orderings.push(persisting.stream_ordering());
            depths.push(event.depth());
        }
    }
    if !event_ids.is_empty() {
        txn.execute(
            "INSERT INTO event_push_actions (
                room_id, event_id, user_id, actions, stream_ordering,
                topological_ordering, notif, highlight, unread, thread_id
            )
            SELECT $1, event_id, s.user_id, s.actions, e.stream_ordering,
                e.depth, s.notif, s.highlight, s.unread, s.thread_id
            FROM unnest($2::text[], $3::bigint[], $4::bigint[])
                AS e(event_id, stream_ordering, depth)
            INNER JOIN event_push_actions_staging AS s USING (event_id)",
            &[&input.room_id, &event_ids, &stream_orderings, &depths],
        )
        .await?;
    }

    let mut staged = Vec::new();
    for event in input.events {
        if event
            .event
            .internal_metadata()
            .is_notifiable()
            .map_err(py_error)?
        {
            staged.push(event.event_id());
        }
    }
    if !staged.is_empty() {
        txn.execute(
            "DELETE FROM event_push_actions_staging WHERE event_id = ANY($1)",
            &[&staged],
        )
        .await?;
    }
    Ok(())
}

/// `_handle_redact_relations`: the redacted event no longer relates to
/// anything. Invalidate what was computed from its relation, and if it was
/// in a thread, find the thread's new latest event, or delete the thread.
async fn handle_redact_relations(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
    redacted_event_id: &str,
) -> Result<(), WriteError> {
    let room_id = input.room_id;
    let rows = txn
        .query(
            "SELECT relates_to_id, relation_type FROM event_relations WHERE event_id = $1",
            &[&redacted_event_id],
        )
        .await?;
    let Some(row) = rows.first() else {
        return Ok(());
    };
    let relates_to: String = row.try_get(0).map_err(anyhow::Error::new)?;
    let rel_type: String = row.try_get(1).map_err(anyhow::Error::new)?;
    txn.execute(
        "DELETE FROM event_relations WHERE event_id = $1",
        &[&redacted_event_id],
    )
    .await?;

    // Any relation information for the related event must be cleared.
    let mut invalidations = vec![(
        "get_relations_for_event",
        vec![room_id.to_owned(), relates_to.clone()],
    )];
    match rel_type.as_str() {
        relation_type::REFERENCE => {
            invalidations.push(("get_references_for_event", vec![relates_to.clone()]));
        }
        relation_type::REPLACE => {
            invalidations.push(("get_applicable_edit", vec![relates_to.clone()]));
        }
        relation_type::THREAD => {
            invalidations.push(("get_thread_summary", vec![relates_to.clone()]));
            invalidations.push(("get_thread_participated", vec![relates_to.clone()]));
            invalidations.push(("get_threads", vec![room_id.to_owned()]));
        }
        _ => {}
    }
    for (cache_name, keys) in invalidations {
        invalidate_cache_and_stream(txn, input, effects, cache_name, keys).await?;
    }
    if rel_type != relation_type::THREAD {
        return Ok(());
    }

    let rows = txn
        .query(
            "SELECT event_id, topological_ordering, stream_ordering
            FROM event_relations
            INNER JOIN events USING (event_id)
            WHERE relates_to_id = $1 AND relation_type = $2
            ORDER BY topological_ordering DESC, stream_ordering DESC
            LIMIT 1",
            &[&relates_to, &relation_type::THREAD],
        )
        .await?;
    if let Some(latest) = rows.first() {
        // This may be the thread's current latest event, if an earlier
        // event of the thread was redacted.
        let event_id: String = latest.try_get(0).map_err(anyhow::Error::new)?;
        let topological_ordering: i64 = latest.try_get(1).map_err(anyhow::Error::new)?;
        let stream_ordering: i64 = latest.try_get(2).map_err(anyhow::Error::new)?;
        txn.execute(
            "INSERT INTO threads
                (room_id, thread_id, latest_event_id, topological_ordering, stream_ordering)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (room_id, thread_id) DO UPDATE SET
                latest_event_id = EXCLUDED.latest_event_id,
                topological_ordering = EXCLUDED.topological_ordering,
                stream_ordering = EXCLUDED.stream_ordering",
            &[
                &room_id,
                &relates_to,
                &event_id,
                &topological_ordering,
                &stream_ordering,
            ],
        )
        .await?;
    } else {
        // The thread no longer exists. As in Python's
        // `simple_delete_one_txn`, by thread ID alone, and exactly one row.
        let deleted = txn
            .execute("DELETE FROM threads WHERE thread_id = $1", &[&relates_to])
            .await?;
        if deleted != 1 {
            return Err(anyhow!("deleting thread {relates_to}: {deleted} rows matched").into());
        }
    }
    Ok(())
}

/// The `event_search` rows of the events: room names, topics and message
/// bodies, as `_store_room_name_txn`, `_store_room_topic_txn` and
/// `_store_room_message_txn` add them with `store_search_entries_txn`.
async fn store_search(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    if !input.enable_search {
        return Ok(());
    }
    let mut event_ids = Vec::new();
    let mut room_ids = Vec::new();
    let mut keys = Vec::new();
    let mut values = Vec::new();
    let mut stream_orderings = Vec::new();
    let mut origin_server_tss = Vec::new();
    for persisting in events {
        let event = persisting.event();
        let Some((key, value)) = search_entry(event) else {
            continue;
        };
        event_ids.push(event.event_id());
        room_ids.push(event.room_id());
        keys.push(key);
        // `_clean_value_for_search`: Postgres takes no NUL in text.
        values.push(value.replace('\0', " "));
        stream_orderings.push(persisting.stream_ordering());
        origin_server_tss.push(event.origin_server_ts());
    }
    if event_ids.is_empty() {
        return Ok(());
    }
    txn.execute(
        "INSERT INTO event_search
            (event_id, room_id, key, vector, stream_ordering, origin_server_ts)
        SELECT event_id, room_id, key, to_tsvector('english', value),
            stream_ordering, origin_server_ts
        FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::bigint[], $6::bigint[])
            AS t(event_id, room_id, key, value, stream_ordering, origin_server_ts)",
        &[
            &event_ids,
            &room_ids,
            &keys,
            &values,
            &stream_orderings,
            &origin_server_tss,
        ],
    )
    .await?;
    Ok(())
}

/// The search key and text of an event, if it has one.
fn search_entry(event: &Event) -> Option<(&'static str, &str)> {
    let content = event.content();
    match event.type_() {
        NAME => Some(("content.name", content.get_field("name")?.as_str()?)),
        TOPIC => {
            content.get_field("topic")?.as_str()?;
            Some(("content.topic", plain_text_topic(content).unwrap_or("")))
        }
        MESSAGE => Some(("content.body", content.get_field("body")?.as_str()?)),
        _ => None,
    }
}

/// `get_plain_text_topic_from_event_content`: the first plain text
/// representation in `m.topic`, else `topic`. A malformed `m.topic` is
/// ignored, as are its malformed representations.
fn plain_text_topic(content: &JsonObject) -> Option<&str> {
    let topic = content.get_field("topic")?.as_str()?;
    let representations = content
        .get_field("m.topic")
        .and_then(Value::as_object)
        .and_then(|m_topic| match m_topic.get("m.text") {
            Some(Value::Array(representations)) => Some(representations.as_slice()),
            // Without `m.text` the `m.topic` is valid but has no text.
            None => Some(&[][..]),
            // Anything else makes the whole `m.topic` invalid.
            Some(_) => None,
        })
        .unwrap_or_default();
    for representation in representations {
        let Some(representation) = representation.as_object() else {
            continue;
        };
        let Some(Value::String(body)) = representation.get("body") else {
            continue;
        };
        match representation.get("mimetype") {
            // The mimetype defaults to `text/plain`, and Python takes an
            // empty one as missing.
            None | Some(Value::Null) => return Some(body),
            Some(Value::String(mimetype)) if mimetype.is_empty() || mimetype == "text/plain" => {
                return Some(body)
            }
            // A representation of another type, or a malformed one.
            Some(_) => continue,
        }
    }
    Some(topic)
}

/// `_store_redaction`: a `redactions` row for each redaction.
async fn store_redactions(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut redacted = Vec::new();
    let mut rechecks = Vec::new();
    for persisting in events {
        let event = persisting.event();
        if event.type_() != event_type::M_ROOM_REDACTION {
            continue;
        }
        let Some(redacts) = redacts(event) else {
            continue;
        };
        event_ids.push(event.event_id());
        redacted.push(redacts);
        rechecks.push(
            event
                .internal_metadata()
                .need_to_check_redaction()
                .map_err(py_error)?,
        );
    }
    if event_ids.is_empty() {
        return Ok(());
    }
    let received_tss = vec![input.now_ms()?; event_ids.len()];
    txn.upsert_many(
        "redactions",
        &[Column::new("event_id", "text", &event_ids)],
        &[
            Column::new("redacts", "text", &redacted),
            Column::new("received_ts", "bigint", &received_tss),
            Column::new("recheck", "boolean", &rechecks),
        ],
    )
    .await?;
    Ok(())
}

/// `_store_retention_policy_for_room_txn`: a `room_retention` row for a
/// retention state event whose lifetimes are integers, if it has either.
async fn store_retention_policy(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    effects: &mut PersistEffects,
    event: &Event,
) -> Result<(), WriteError> {
    if event.type_() != RETENTION || !event.is_state() {
        return Ok(());
    }
    let content = event.content();
    let min_lifetime = content.get_field("min_lifetime");
    let max_lifetime = content.get_field("max_lifetime");
    if min_lifetime.is_none() && max_lifetime.is_none() {
        return Ok(());
    }
    // Python ignores the event if either is present but not an integer.
    let (Some(min_lifetime), Some(max_lifetime)) = (
        min_lifetime.map_or(Ok(Some(None)), |v| integer(v).map(|i| i.map(Some)))?,
        max_lifetime.map_or(Ok(Some(None)), |v| integer(v).map(|i| i.map(Some)))?,
    ) else {
        return Ok(());
    };
    txn.execute(
        "INSERT INTO room_retention (room_id, event_id, min_lifetime, max_lifetime)
        VALUES ($1, $2, $3, $4)",
        &[
            &event.room_id(),
            &event.event_id(),
            &min_lifetime,
            &max_lifetime,
        ],
    )
    .await?;
    invalidate_cache_and_stream(
        txn,
        input,
        effects,
        "get_retention_policy_for_room",
        vec![event.room_id().to_owned()],
    )
    .await
}

/// A JSON integer, as Python's `type(value) is int` takes it, or `None`.
/// An integer too big for a `bigint` fails, as Python's insert does.
///
/// serde reads an integer beyond `u64` as a float, which Python reads as an
/// integer. Python's insert then fails, while here it is not an integer.
fn integer(value: &Value) -> anyhow::Result<Option<i64>> {
    match value {
        Value::Number(number) if !number.is_f64() => number
            .as_i64()
            .map(Some)
            .ok_or_else(|| anyhow!("{number} is out of range for a bigint")),
        _ => Ok(None),
    }
}

/// `_handle_event_relations`: an `event_relations` row for each event with
/// a relation, and for a thread reply, the thread's latest event, unless
/// the thread already has a later one.
async fn store_relations(txn: &WriteTxn, events: &[Persisting<'_>]) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut parent_ids = Vec::new();
    let mut rel_types = Vec::new();
    let mut aggregation_keys = Vec::new();
    // The thread replies, as `(room_id, thread_id, event_id, depth,
    // stream_ordering)`.
    let mut replies: Vec<(&str, &str, &str, i64, i64)> = Vec::new();
    for persisting in events {
        let event = persisting.event();
        let Some(relation) = relation(event) else {
            continue;
        };
        event_ids.push(event.event_id());
        parent_ids.push(relation.parent_id);
        rel_types.push(relation.rel_type);
        aggregation_keys.push(relation.aggregation_key);
        if relation.rel_type == relation_type::THREAD {
            replies.push((
                event.room_id(),
                relation.parent_id,
                event.event_id(),
                event.depth(),
                persisting.stream_ordering(),
            ));
        }
    }
    txn.insert_many(
        "event_relations",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("relates_to_id", "text", &parent_ids),
            Column::new("relation_type", "text", &rel_types),
            Column::new("aggregation_key", "text", &aggregation_keys),
        ],
        None,
    )
    .await?;

    // Only a later event becomes the latest. The stream ordering is always
    // later, unless the event was backfilled. In order, as each reply
    // compares with the one before.
    let thread_rows: Vec<Vec<&(dyn ToSql + Sync)>> = replies
        .iter()
        .map(|(room_id, thread_id, event_id, depth, stream_ordering)| {
            vec![
                room_id as &(dyn ToSql + Sync),
                thread_id,
                event_id,
                depth,
                stream_ordering,
            ]
        })
        .collect();
    txn.execute_batch(
        "INSERT INTO threads
            (room_id, thread_id, latest_event_id, topological_ordering, stream_ordering)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (room_id, thread_id) DO UPDATE SET
            latest_event_id = EXCLUDED.latest_event_id,
            topological_ordering = EXCLUDED.topological_ordering,
            stream_ordering = EXCLUDED.stream_ordering
        WHERE
            threads.topological_ordering <= EXCLUDED.topological_ordering AND
            threads.stream_ordering < EXCLUDED.stream_ordering",
        &thread_rows,
    )
    .await?;
    Ok(())
}

/// `insert_labels_for_event_txn`: the event's labels, if it has a list of
/// them.
///
/// Python iterates whatever the field holds and stores each element, so it
/// also stores labels from a string or an object, and non-string labels.
/// Only string labels from a list are stored here.
async fn store_labels(txn: &WriteTxn, events: &[Persisting<'_>]) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut labels = Vec::new();
    let mut room_ids = Vec::new();
    let mut depths = Vec::new();
    for persisting in events {
        let event = persisting.event();
        let Some(Value::Array(event_labels)) = event.content().get_field(LABELS) else {
            continue;
        };
        for label in event_labels.iter().filter_map(Value::as_str) {
            event_ids.push(event.event_id());
            labels.push(label);
            room_ids.push(event.room_id());
            depths.push(event.depth());
        }
    }
    txn.insert_many(
        "event_labels",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("label", "text", &labels),
            Column::new("room_id", "text", &room_ids),
            Column::new("topological_ordering", "bigint", &depths),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `_insert_event_expiry_txn`: the expiry of each non-state event that
/// sets an integer one.
async fn store_expiry(txn: &WriteTxn, events: &[Persisting<'_>]) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut expiry_tss = Vec::new();
    for persisting in events {
        let event = persisting.event();
        if event.is_state() {
            continue;
        }
        let Some(expiry_ts) = event
            .content()
            .get_field(SELF_DESTRUCT_AFTER)
            .map(integer)
            .transpose()?
            .flatten()
        else {
            continue;
        };
        event_ids.push(event.event_id());
        expiry_tss.push(expiry_ts);
    }
    txn.insert_many(
        "event_expiry",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("expiry_ts", "bigint", &expiry_tss),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `event.membership`, if it is a string.
fn membership_of(event: &Event) -> Option<&str> {
    event
        .content()
        .get_field("membership")
        .and_then(Value::as_str)
}

/// `non_null_str_or_none`: the value if it is a string without a NUL.
fn non_null_str(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str).filter(|s| !s.contains('\0'))
}

/// `_store_room_members_txn`: a `room_memberships` row for each membership
/// event. An out-of-band membership of one of our users, such as a remote
/// invite or its rejection, is also current, so it updates
/// `local_current_membership` and the user's sliding sync snapshot, unless
/// the chunk was backfilled.
async fn store_room_members(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let members: Vec<&Persisting<'_>> = events
        .iter()
        .filter(|p| p.event().type_() == event_type::M_ROOM_MEMBER)
        .collect();
    if members.is_empty() {
        return Ok(());
    }
    let event_ids: Vec<&str> = members.iter().map(|p| p.event().event_id()).collect();
    let stream_orderings: Vec<i64> = members.iter().map(|p| p.stream_ordering()).collect();
    let user_ids: Vec<Option<&str>> = members.iter().map(|p| p.event().state_key()).collect();
    let senders: Vec<&str> = members.iter().map(|p| p.event().sender()).collect();
    let room_ids: Vec<&str> = members.iter().map(|p| p.event().room_id()).collect();
    let memberships: Vec<Option<&str>> = members.iter().map(|p| membership_of(p.event())).collect();
    let display_names: Vec<Option<&str>> = members
        .iter()
        .map(|p| non_null_str(p.event().content().get_field("displayname")))
        .collect();
    let avatar_urls: Vec<Option<&str>> = members
        .iter()
        .map(|p| non_null_str(p.event().content().get_field("avatar_url")))
        .collect();
    txn.insert_many(
        "room_memberships",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("event_stream_ordering", "bigint", &stream_orderings),
            Column::new("user_id", "text", &user_ids),
            Column::new("sender", "text", &senders),
            Column::new("room_id", "text", &room_ids),
            Column::new("membership", "text", &memberships),
            Column::new("display_name", "text", &display_names),
            Column::new("avatar_url", "text", &avatar_urls),
        ],
        None,
    )
    .await?;

    if input.inhibit_local_membership_updates {
        return Ok(());
    }
    for persisting in members {
        let event = persisting.event();
        let metadata = event.internal_metadata();
        let Some(user_id) = event.state_key() else {
            continue;
        };
        if !is_mine_id(user_id, input.server_name)
            || !persisting.source.is_outlier()?
            || !metadata.is_out_of_band_membership().map_err(py_error)?
        {
            continue;
        }
        let membership = membership_of(event).unwrap_or_default();
        let stripped_state = match membership {
            "invite" => Some(event.unsigned_ref().invite_room_state().map_err(py_error)?),
            "knock" => Some(event.unsigned_ref().knock_room_state().map_err(py_error)?),
            // A rejected or retracted invite, or a rescinded knock, keeps the
            // snapshot's state, so the room does not come and go from
            // sliding sync filters.
            "leave" => None,
            _ => {
                return Err(anyhow!(
                    "Unexpected out-of-band membership {membership} ({}) that we don't know how to handle yet",
                    event.event_id()
                )
                .into())
            }
        };

        txn.execute(
            "INSERT INTO local_current_membership
                (room_id, user_id, event_id, event_stream_ordering, membership)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (room_id, user_id) DO UPDATE SET
                event_id = EXCLUDED.event_id,
                event_stream_ordering = EXCLUDED.event_stream_ordering,
                membership = EXCLUDED.membership",
            &[
                &event.room_id(),
                &user_id,
                &event.event_id(),
                &persisting.stream_ordering(),
                &membership,
            ],
        )
        .await?;

        let snapshot_state = stripped_state
            .map(|state| snapshot_values_from_stripped_state(state.as_deref()))
            .transpose()?;
        let room_id = event.room_id();
        let sender = event.sender();
        let event_id = event.event_id();
        // A new membership is not forgotten.
        let forgotten = 0i32;
        let stream_ordering = persisting.stream_ordering();
        let mut columns: Vec<(&'static str, &(dyn ToSql + Sync))> = vec![
            ("room_id", &room_id),
            ("user_id", &user_id),
            ("sender", &sender),
            ("membership_event_id", &event_id),
            ("membership", &membership),
            ("forgotten", &forgotten),
            ("event_stream_ordering", &stream_ordering),
            ("event_instance_name", &input.instance_name),
        ];
        if let Some(values) = &snapshot_state {
            values.add_columns(&mut columns);
        }
        upsert_one(
            txn,
            "sliding_sync_membership_snapshots",
            &["room_id", "user_id"],
            &columns,
        )
        .await?;
    }
    Ok(())
}

/// Insert a row, or update all but the `keys` columns of the row with the
/// same keys, as Python's `simple_upsert_txn`.
async fn upsert_one(
    txn: &WriteTxn,
    table: &str,
    keys: &[&str],
    columns: &[(&str, &(dyn ToSql + Sync))],
) -> Result<(), WriteError> {
    let names: Vec<&str> = columns.iter().map(|(name, _)| *name).collect();
    let placeholders: Vec<String> = (1..=columns.len()).map(|i| format!("${i}")).collect();
    let updates: Vec<String> = names
        .iter()
        .filter(|name| !keys.contains(name))
        .map(|name| format!("{name} = EXCLUDED.{name}"))
        .collect();
    let sql = format!(
        "INSERT INTO {table} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}",
        names.join(", "),
        placeholders.join(", "),
        keys.join(", "),
        updates.join(", ")
    );
    let params: Vec<&(dyn ToSql + Sync)> = columns.iter().map(|(_, value)| *value).collect();
    txn.execute(&sql, &params).await?;
    Ok(())
}

/// What `_get_sliding_sync_insert_values_from_stripped_state` takes from an
/// invite's or knock's stripped state for its snapshot. A field is `None`
/// where Python leaves the column out.
#[derive(Debug, PartialEq, Eq)]
struct StrippedSnapshotValues {
    has_known_state: bool,
    room_type: Option<Option<String>>,
    is_encrypted: Option<bool>,
    room_name: Option<Option<String>>,
    tombstone_successor_room_id: Option<Option<String>>,
}

impl StrippedSnapshotValues {
    fn add_columns<'a>(&'a self, columns: &mut Vec<(&'static str, &'a (dyn ToSql + Sync))>) {
        columns.push(("has_known_state", &self.has_known_state));
        if let Some(room_type) = &self.room_type {
            columns.push(("room_type", room_type));
        }
        if let Some(is_encrypted) = &self.is_encrypted {
            columns.push(("is_encrypted", is_encrypted));
        }
        if let Some(room_name) = &self.room_name {
            columns.push(("room_name", room_name));
        }
        if let Some(successor) = &self.tombstone_successor_room_id {
            columns.push(("tombstone_successor_room_id", successor));
        }
    }
}

/// `_get_sliding_sync_insert_values_from_stripped_state`.
fn snapshot_values_from_stripped_state(
    stripped_state: Option<&[Value]>,
) -> anyhow::Result<StrippedSnapshotValues> {
    let no_state = StrippedSnapshotValues {
        has_known_state: false,
        room_type: Some(None),
        is_encrypted: Some(false),
        room_name: Some(None),
        tombstone_successor_room_id: None,
    };
    let Some(stripped_state) = stripped_state else {
        return Ok(no_state);
    };

    // `StrippedStateEvent.from_json_dict`, keeping the last of each key.
    let mut contents: std::collections::HashMap<(&str, &str), &Map<String, Value>> =
        std::collections::HashMap::new();
    for raw in stripped_state {
        let Some(raw) = raw.as_object() else {
            continue;
        };
        let field = |name: &str| raw.get(name).and_then(Value::as_str);
        let (Some(event_type), Some(state_key), Some(_sender), Some(Value::Object(content))) = (
            field("type"),
            field("state_key"),
            field("sender"),
            raw.get("content"),
        ) else {
            continue;
        };
        contents.insert((event_type, state_key), content);
    }

    // If there is any stripped state, the remote server sent all of it.
    // There must at least be a create event.
    let Some(create) = contents.get(&(CREATE, "")) else {
        return Ok(no_state);
    };
    let field = |event_type: &str, name: &str| {
        contents
            .get(&(event_type, ""))
            .and_then(|content| content.get(name))
            .filter(|v| !v.is_null())
    };
    Ok(StrippedSnapshotValues {
        has_known_state: true,
        room_type: stripped_text(create.get("type").filter(|v| !v.is_null()))?,
        is_encrypted: Some(field(ENCRYPTION, "algorithm").is_some()),
        room_name: stripped_text(field(NAME, "name"))?,
        tombstone_successor_room_id: stripped_text(field(TOMBSTONE, "replacement_room"))?,
    })
}

/// A text column from stripped state: `Some(None)` for a missing value, and
/// `None`, leaving the column out, where Python's `"\0" in value` holds. That
/// is a string with a NUL, which Postgres does not allow, or a list holding
/// `"\0"`, or an object with that key. Python fails on any other value, in
/// the check or when storing it.
fn stripped_text(value: Option<&Value>) -> anyhow::Result<Option<Option<String>>> {
    match value {
        None => Ok(Some(None)),
        Some(Value::String(s)) if s.contains('\0') => Ok(None),
        Some(Value::String(s)) => Ok(Some(Some(s.clone()))),
        Some(Value::Array(items)) if items.iter().any(|item| item == "\0") => Ok(None),
        Some(Value::Object(object)) if object.contains_key("\0") => Ok(None),
        Some(other) => Err(anyhow!("stripped state value {other} is not a string")),
    }
}

fn py_error(e: pyo3::PyErr) -> WriteError {
    WriteError::Other(anyhow::Error::new(e))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn topic(content: Value) -> Option<String> {
        let content: JsonObject = serde_json::from_value(content).unwrap();
        plain_text_topic(&content).map(str::to_owned)
    }

    #[test]
    fn plain_text_topic_as_python() {
        let plain = |body: &str| Some(body.to_owned());
        assert_eq!(topic(json!({"topic": "t"})), plain("t"));
        assert_eq!(
            topic(json!({"topic": "t", "m.topic": {"m.text": [
                {"mimetype": "text/html", "body": "<b>h</b>"},
                {"body": "p"},
            ]}})),
            plain("p")
        );
        assert_eq!(
            topic(json!({"topic": "t", "m.topic": {"m.text": [
                {"mimetype": "", "body": "empty"},
            ]}})),
            plain("empty")
        );
        // Malformed representations are skipped.
        assert_eq!(
            topic(json!({"topic": "t", "m.topic": {"m.text": [
                "x", {"body": 1}, {"mimetype": 1, "body": "m"}, {"mimetype": null, "body": "n"},
            ]}})),
            plain("n")
        );
        // A malformed `m.topic` falls back to `topic`.
        assert_eq!(
            topic(json!({"topic": "t", "m.topic": {"m.text": {"body": "x"}}})),
            plain("t")
        );
        assert_eq!(topic(json!({"topic": "t", "m.topic": "x"})), plain("t"));
        assert_eq!(topic(json!({"topic": "t", "m.topic": {}})), plain("t"));
        assert_eq!(topic(json!({"topic": 1})), None);
    }

    #[test]
    fn stripped_state_as_python() {
        let values = |state: Value| {
            let state: Vec<Value> = serde_json::from_value(state).unwrap();
            snapshot_values_from_stripped_state(Some(&state))
        };
        let event = |event_type: &str, content: Value| json!({"type": event_type, "state_key": "", "sender": "@s:x", "content": content});

        let no_state = StrippedSnapshotValues {
            has_known_state: false,
            room_type: Some(None),
            is_encrypted: Some(false),
            room_name: Some(None),
            tombstone_successor_room_id: None,
        };
        assert_eq!(snapshot_values_from_stripped_state(None).unwrap(), no_state);
        // Without a create event, the state is unknown.
        assert_eq!(
            values(json!([event(NAME, json!({"name": "n"}))])).unwrap(),
            no_state
        );

        assert_eq!(
            values(json!([
                event(CREATE, json!({"type": "m.space"})),
                event(ENCRYPTION, json!({"algorithm": "a"})),
                event(NAME, json!({"name": "n"})),
                event(TOMBSTONE, json!({"replacement_room": "!r:x"})),
                // Malformed events are ignored.
                {"type": NAME, "state_key": "", "content": {"name": "no sender"}},
            ]))
            .unwrap(),
            StrippedSnapshotValues {
                has_known_state: true,
                room_type: Some(Some("m.space".into())),
                is_encrypted: Some(true),
                room_name: Some(Some("n".into())),
                tombstone_successor_room_id: Some(Some("!r:x".into())),
            }
        );

        // NULs leave the column out, and a null algorithm is unencrypted.
        assert_eq!(
            values(json!([
                event(CREATE, json!({"type": "a\u{0}b"})),
                event(ENCRYPTION, json!({"algorithm": null})),
                event(NAME, json!({"name": "a\u{0}b"})),
            ]))
            .unwrap(),
            StrippedSnapshotValues {
                has_known_state: true,
                room_type: None,
                is_encrypted: Some(false),
                room_name: None,
                tombstone_successor_room_id: Some(None),
            }
        );

        assert!(values(json!([event(CREATE, json!({"type": 1}))])).is_err());
        assert!(values(json!([event(CREATE, json!({"type": ["x"]}))])).is_err());
        // Python's `"\0" in value` also holds for a list or an object.
        assert_eq!(
            values(json!([
                event(CREATE, json!({"type": ["\u{0}"]})),
                event(NAME, json!({"name": {"\u{0}": 1}})),
            ]))
            .unwrap(),
            StrippedSnapshotValues {
                has_known_state: true,
                room_type: None,
                is_encrypted: Some(false),
                room_name: None,
                tombstone_successor_room_id: Some(None),
            }
        );
    }

    #[test]
    fn integers_as_python() {
        assert_eq!(integer(&json!(5)).unwrap(), Some(5));
        assert_eq!(integer(&json!(-5)).unwrap(), Some(-5));
        assert_eq!(integer(&json!(5.0)).unwrap(), None);
        assert_eq!(integer(&json!(true)).unwrap(), None);
        assert_eq!(integer(&json!("5")).unwrap(), None);
        // Too big for a `bigint`.
        assert!(integer(&json!(u64::MAX)).is_err());
    }
}
