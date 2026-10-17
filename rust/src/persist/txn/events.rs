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

//! The steps that store the events themselves, from `_update_room_depths_txn`
//! to `_store_rejected_events_txn`, as `_persist_events_txn` runs them.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use anyhow::{anyhow, Context};
use log::{debug, info};

use super::{
    edges::{update_backward_extremities, BackwardEvent},
    json, metadata_error, redacts, PartialStateConflict, Persisting, TxnEvent, TxnInput,
};
use crate::{
    database::postgres::write::{Column, WriteError, WriteTxn},
    event_auth::get_domain_from_id,
    events::{constants::event_type, redact_event_py},
    persist::{
        chain_cover::NewEventChainLinks,
        effects::{PersistEffects, StreamChangeCache},
    },
    room_versions::{EventFormatVersions, RoomVersion},
};

/// `_update_room_depths_txn`: move the room's front in the events stream
/// cache to the chunk's highest stream ordering, unless it was backfilled,
/// and lower its `min_depth` to the deepest new event if that is lower.
pub async fn update_room_depths(
    txn: &WriteTxn,
    room_id: &str,
    events: &[Persisting<'_>],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let mut stream_ordering: Option<i64> = None;
    let mut depth_update = 0;
    for event in events {
        // Backfilled events have negative orderings and are in the past.
        if event.stream_ordering() >= 0 {
            stream_ordering = stream_ordering.max(Some(event.stream_ordering()));
        }
        if !event.source.is_outlier()? && !event.context().is_rejected() {
            depth_update = depth_update.max(event.event().depth());
        }
    }
    if let Some(stream_ordering) = stream_ordering {
        effects.add_stream_change(
            StreamChangeCache::Events,
            room_id.to_owned(),
            stream_ordering,
        );
    }

    // `_update_min_depth_for_room_txn`.
    let rows = txn
        .query(
            "SELECT min_depth FROM room_depth WHERE room_id = $1",
            &[&room_id],
        )
        .await?;
    let min_depth: Option<i64> = match rows.first() {
        Some(row) => row.try_get(0).map_err(anyhow::Error::new)?,
        None => None,
    };
    if min_depth.is_some_and(|min_depth| depth_update >= min_depth) {
        return Ok(());
    }
    txn.upsert_many(
        "room_depth",
        &[Column::new("room_id", "text", &vec![room_id])],
        &[Column::new("min_depth", "bigint", &vec![depth_update])],
    )
    .await?;
    Ok(())
}

/// `_update_outliers_txn`: drop the events we have already persisted. An
/// event we have as an outlier, now persisted as a non-outlier, stops being
/// one: it gets its state group, a row in `ex_outlier_stream`, and its
/// backward extremities.
///
/// Returns the events left, and adds to `de_outliered` the events that
/// stopped being outliers.
pub async fn update_outliers<'a>(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: Vec<Persisting<'a>>,
    effects: &mut PersistEffects,
    de_outliered: &mut Vec<&'a TxnEvent>,
) -> Result<Vec<Persisting<'a>>, WriteError> {
    let event_ids: Vec<&str> = events.iter().map(|e| e.event().event_id()).collect();
    let rows = txn
        .query(
            "SELECT event_id, outlier FROM events WHERE event_id = ANY($1)",
            &[&event_ids],
        )
        .await?;
    let mut have_persisted: HashMap<String, bool> = HashMap::with_capacity(rows.len());
    for row in rows {
        have_persisted.insert(
            row.try_get(0).map_err(anyhow::Error::new)?,
            row.try_get(1).map_err(anyhow::Error::new)?,
        );
    }

    let mut left = Vec::with_capacity(events.len());
    for event in events {
        let Some(&outlier_persisted) = have_persisted.get(event.event().event_id()) else {
            left.push(event);
            continue;
        };
        // What we have is at least as good as a rejected copy.
        if event.context().is_rejected() {
            continue;
        }
        if event.source.is_outlier()? || !outlier_persisted {
            continue;
        }

        // We have the event as an outlier, and now have state at it. Its
        // `events` row keeps the stream ordering it was first persisted
        // with. `ex_outlier_stream` and any timeline gap get this chunk's.
        info!(
            "_update_outliers_txn: Updating state for ex-outlier event {}",
            event.event().event_id()
        );
        store_state_mappings(txn, std::slice::from_ref(&event), effects).await?;

        txn.insert_many(
            "ex_outlier_stream",
            &[
                Column::new(
                    "event_stream_ordering",
                    "bigint",
                    &vec![event.stream_ordering()],
                ),
                Column::new("event_id", "text", &vec![event.event().event_id()]),
                Column::new("state_group", "bigint", &vec![event.context().state_group]),
                Column::new("instance_name", "text", &vec![input.instance_name]),
            ],
            None,
        )
        .await?;
        txn.execute(
            "UPDATE events SET outlier = FALSE WHERE event_id = $1",
            &[&event.event().event_id()],
        )
        .await?;
        update_backward_extremities(
            txn,
            input.instance_name,
            &[BackwardEvent {
                event: event.event(),
                outlier: false,
                stream_ordering: event.stream_ordering(),
            }],
        )
        .await?;
        de_outliered.push(event.source);
    }
    Ok(left)
}

/// A redaction that applies to an event of the chunk.
struct RedactionInfo<'a> {
    event_id: &'a str,
    redacts: &'a str,
    sender: &'a str,
    needs_v3_recheck: bool,
}

/// `_apply_existing_redaction_txn`: store redacted the events of the chunk
/// that an earlier event or another event of the chunk redacts. Redaction
/// events are never redacted here, so that circular redactions work.
pub async fn apply_existing_redactions<'a>(
    txn: &WriteTxn,
    room_id: &str,
    room_version: &RoomVersion,
    events: Vec<Persisting<'a>>,
) -> Result<Vec<Persisting<'a>>, WriteError> {
    let event_ids: HashSet<&str> = events
        .iter()
        .filter(|e| e.event().type_() != event_type::M_ROOM_REDACTION)
        .map(|e| e.event().event_id())
        .collect();
    if event_ids.is_empty() {
        return Ok(events);
    }

    let ids: Vec<&str> = event_ids.iter().copied().collect();
    let rows = txn
        .query(
            "SELECT redactions.event_id, redactions.redacts, redaction_events.sender,
                redactions.recheck
            FROM redactions
            INNER JOIN events AS redaction_events
                ON redactions.event_id = redaction_events.event_id
                AND redaction_events.room_id = $1
            WHERE redactions.redacts = ANY($2)",
            &[&room_id, &ids],
        )
        .await?;
    let mut redactions = Vec::with_capacity(rows.len());
    for row in &rows {
        redactions.push(RedactionInfo {
            event_id: row.try_get(0).map_err(anyhow::Error::new)?,
            redacts: row.try_get(1).map_err(anyhow::Error::new)?,
            sender: row.try_get(2).map_err(anyhow::Error::new)?,
            needs_v3_recheck: row
                .try_get::<_, Option<bool>>(3)
                .map_err(anyhow::Error::new)?
                .unwrap_or(false),
        });
    }
    for event in &events {
        let event_ = event.event();
        let Some(redacts) = redacts(event_) else {
            continue;
        };
        if event_.type_() == event_type::M_ROOM_REDACTION
            && event_ids.contains(redacts)
            && !event.context().is_rejected()
        {
            redactions.push(RedactionInfo {
                event_id: event_.event_id(),
                redacts,
                sender: event_.sender(),
                needs_v3_recheck: event_
                    .internal_metadata()
                    .need_to_check_redaction()
                    .map_err(metadata_error)?,
            });
        }
    }

    let events_by_id: HashMap<&str, &Persisting<'a>> =
        events.iter().map(|e| (e.event().event_id(), e)).collect();
    // From each redacted event to the event that redacts it.
    let mut redacted_by: HashMap<String, String> = HashMap::new();
    for redaction in &redactions {
        if redacted_by.contains_key(redaction.redacts) {
            continue;
        }
        let event = events_by_id
            .get(redaction.redacts)
            .ok_or_else(|| anyhow!("redaction of {} is not in the chunk", redaction.redacts))?
            .event();
        // The recheck column defaults to true, so v1 and v2 rooms can have
        // it set, but their redactions never need one.
        if redaction.needs_v3_recheck
            && room_version.event_format != EventFormatVersions::ROOM_V1_V2
        {
            let redaction_domain = get_domain_from_id(redaction.sender).map_err(domain_error)?;
            let event_domain = get_domain_from_id(event.sender()).map_err(domain_error)?;
            if redaction_domain != event_domain {
                debug!(
                    "redaction of {} by {} skipped as it required a v3 recheck and sender servers differ",
                    redaction.redacts, redaction.event_id
                );
                continue;
            }
        }
        redacted_by.insert(redaction.redacts.to_owned(), redaction.event_id.to_owned());
    }
    drop(events_by_id);

    events
        .into_iter()
        .map(|mut event| {
            if let Some(by) = redacted_by.get(event.event().event_id()) {
                let redacted = redact_event_py(event.event()).map_err(metadata_error)?;
                redacted
                    .internal_metadata()
                    .set_redacted_by(Some(by.clone()))
                    .map_err(metadata_error)?;
                event.redacted = Some(redacted);
            }
            Ok(event)
        })
        .collect()
}

fn domain_error(e: impl std::fmt::Display) -> WriteError {
    WriteError::Other(anyhow!("{e}"))
}

/// `_compute_newly_redacted_event_ids_txn`: the events of the room already
/// persisted that the chunk's redactions redact, where we can confirm the
/// redaction. A redaction that needed a recheck and passes it no longer
/// needs one, which is stored with it.
pub async fn newly_redacted_event_ids(
    txn: &WriteTxn,
    room_id: &str,
    events: &[Persisting<'_>],
) -> Result<HashSet<String>, WriteError> {
    let redactions: Vec<(&Persisting<'_>, &str)> = events
        .iter()
        // As in Python. A new event's rejection is in its context, not
        // here, so a rejected redaction still counts.
        .filter(|e| e.event().rejected_reason().is_none())
        // Python takes any `redacts`, but only a string can match.
        .filter_map(|e| Some((e, redacts(e.event())?)))
        .collect();
    if redactions.is_empty() {
        return Ok(HashSet::new());
    }

    let targets: Vec<&str> = redactions.iter().map(|(_, redacts)| *redacts).collect();
    // The room ID keeps redactions from reaching into other rooms.
    let rows = txn
        .query(
            "SELECT event_id, sender, type FROM events
            WHERE event_id = ANY($1) AND room_id = $2",
            &[&targets, &room_id],
        )
        .await?;
    let mut redacted_events: HashMap<String, (String, String)> = HashMap::new();
    for row in rows {
        let sender: &str = row.try_get(1).map_err(anyhow::Error::new)?;
        redacted_events.insert(
            row.try_get(0).map_err(anyhow::Error::new)?,
            (
                get_domain_from_id(sender).map_err(domain_error)?.to_owned(),
                row.try_get(2).map_err(anyhow::Error::new)?,
            ),
        );
    }

    let mut confirmed = HashSet::new();
    for (redaction, redacts) in redactions {
        let Some((sender_domain, event_type)) = redacted_events.get(redacts) else {
            // We cannot confirm a redaction of an event we do not have.
            continue;
        };
        // As `_maybe_redact_event_row`, create events are never redacted.
        if event_type == event_type::M_ROOM_CREATE {
            continue;
        }
        let metadata = redaction.event().internal_metadata();
        if metadata.need_to_check_redaction().map_err(metadata_error)? {
            let domain = get_domain_from_id(redaction.event().sender()).map_err(domain_error)?;
            if domain != sender_domain {
                continue;
            }
            metadata
                .set_recheck_redaction(false)
                .map_err(metadata_error)?;
        }
        confirmed.insert(redacts.to_owned());
    }
    Ok(confirmed)
}

/// `delete_sticky_events_txn`: the events stop being sticky.
pub async fn delete_sticky_events(
    txn: &WriteTxn,
    event_ids: &HashSet<String>,
) -> Result<(), WriteError> {
    if event_ids.is_empty() {
        return Ok(());
    }
    let event_ids: Vec<&str> = event_ids.iter().map(String::as_str).collect();
    // `sticky_events` has no index on `event_id`, but has one on the
    // stream ordering.
    txn.execute(
        "DELETE FROM sticky_events WHERE event_stream_ordering IN (
            SELECT stream_ordering FROM events WHERE event_id = ANY($1)
        )",
        &[&event_ids],
    )
    .await?;
    Ok(())
}

/// `_store_event_txn`: the `event_json`, `events` and `state_events` rows,
/// and the redactions of these events need censoring again.
pub async fn store_events(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    if events.is_empty() {
        return Ok(());
    }

    let n = events.len();
    let mut event_ids = Vec::with_capacity(n);
    let mut room_ids = Vec::with_capacity(n);
    let mut internal_metadata = Vec::with_capacity(n);
    let mut jsons = Vec::with_capacity(n);
    let mut format_versions = Vec::with_capacity(n);
    let mut instance_names = Vec::with_capacity(n);
    let mut stream_orderings = Vec::with_capacity(n);
    let mut depths = Vec::with_capacity(n);
    let mut types = Vec::with_capacity(n);
    let mut outliers = Vec::with_capacity(n);
    let mut origin_server_tss = Vec::with_capacity(n);
    let mut received_tss = Vec::with_capacity(n);
    let mut senders = Vec::with_capacity(n);
    let mut contains_urls = Vec::with_capacity(n);
    let mut state_keys = Vec::with_capacity(n);
    let mut rejection_reasons = Vec::with_capacity(n);
    let mut unredacted = Vec::new();
    for persisting in events {
        let event = persisting.event();
        let metadata = event.internal_metadata();
        event_ids.push(event.event_id());
        room_ids.push(event.room_id());
        internal_metadata.push(
            json::encode_object(&metadata.to_persistence_pairs().map_err(metadata_error)?)
                .map_err(anyhow::Error::new)?,
        );
        jsons.push(
            event
                .with_persistence_dict(|dict| json::encode(dict))
                .map_err(metadata_error)?
                .map_err(anyhow::Error::new)?,
        );
        format_versions.push(i64::from(event.room_version().event_format));
        instance_names.push(input.instance_name);
        stream_orderings.push(persisting.stream_ordering());
        depths.push(event.depth());
        types.push(event.type_());
        outliers.push(persisting.source.is_outlier()?);
        origin_server_tss.push(event.origin_server_ts());
        received_tss.push(metadata.get_received_ts().map_err(metadata_error)?);
        senders.push(event.sender());
        contains_urls.push(
            event
                .content()
                .get_field("url")
                .is_some_and(serde_json::Value::is_string),
        );
        state_keys.push(event.state_key());
        rejection_reasons.push(persisting.context().rejected.as_deref());
        if !metadata.redacted().map_err(metadata_error)? {
            unredacted.push(event.event_id());
        }
    }

    txn.insert_many(
        "event_json",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
            Column::new("internal_metadata", "text", &internal_metadata),
            Column::new("json", "text", &jsons),
            Column::new("format_version", "bigint", &format_versions),
        ],
        None,
    )
    .await?;

    let processed = vec![true; n];
    txn.insert_many(
        "events",
        &[
            Column::new("instance_name", "text", &instance_names),
            Column::new("stream_ordering", "bigint", &stream_orderings),
            Column::new("topological_ordering", "bigint", &depths),
            Column::new("depth", "bigint", &depths),
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
            Column::new("type", "text", &types),
            Column::new("processed", "boolean", &processed),
            Column::new("outlier", "boolean", &outliers),
            Column::new("origin_server_ts", "bigint", &origin_server_tss),
            Column::new("received_ts", "bigint", &received_tss),
            Column::new("sender", "text", &senders),
            Column::new("contains_url", "boolean", &contains_urls),
            Column::new("state_key", "text", &state_keys),
            Column::new("rejection_reason", "text", &rejection_reasons),
        ],
        None,
    )
    .await?;

    // A redaction of an event we now have unredacted needs to censor it.
    if !unredacted.is_empty() {
        txn.execute(
            "UPDATE redactions SET have_censored = FALSE WHERE redacts = ANY($1)",
            &[&unredacted],
        )
        .await?;
    }

    let state: Vec<_> = events.iter().filter(|e| e.event().is_state()).collect();
    let event_ids: Vec<&str> = state.iter().map(|e| e.event().event_id()).collect();
    let room_ids: Vec<&str> = state.iter().map(|e| e.event().room_id()).collect();
    let types: Vec<&str> = state.iter().map(|e| e.event().type_()).collect();
    let state_keys: Vec<Option<&str>> = state.iter().map(|e| e.event().state_key()).collect();
    txn.insert_many(
        "state_events",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
            Column::new("type", "text", &types),
            Column::new("state_key", "text", &state_keys),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `_persist_transaction_ids_txn`: map the client's transaction ID to each
/// event that has one and a device ID, for deduplicating its retries.
pub async fn persist_transaction_ids(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let mut rows = Vec::new();
    for persisting in events {
        let event = persisting.event();
        let metadata = event.internal_metadata();
        let (Some(txn_id), Some(device_id)) = (
            metadata.txn_id().map_err(metadata_error)?,
            metadata.device_id().map_err(metadata_error)?,
        ) else {
            continue;
        };
        rows.push((event, device_id, txn_id));
    }
    if rows.is_empty() {
        return Ok(());
    }
    let inserted_ts = input.now_ms()?;
    let event_ids: Vec<&str> = rows.iter().map(|(e, _, _)| e.event_id()).collect();
    let room_ids: Vec<&str> = rows.iter().map(|(e, _, _)| e.room_id()).collect();
    let user_ids: Vec<&str> = rows.iter().map(|(e, _, _)| e.sender()).collect();
    let device_ids: Vec<&str> = rows.iter().map(|(_, d, _)| d.as_str()).collect();
    let txn_ids: Vec<&str> = rows.iter().map(|(_, _, t)| t.as_str()).collect();
    let inserted_tss = vec![inserted_ts; rows.len()];
    txn.insert_many(
        "event_txn_id_device_id",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
            Column::new("user_id", "text", &user_ids),
            Column::new("device_id", "text", &device_ids),
            Column::new("txn_id", "text", &txn_ids),
            Column::new("inserted_ts", "bigint", &inserted_tss),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `_store_event_state_mappings_txn`: the state group of each event, and
/// which events have partial state.
///
/// Fails with [`PartialStateConflict`] when an event has partial state in
/// a room that no longer has.
pub async fn store_state_mappings(
    txn: &WriteTxn,
    events: &[Persisting<'_>],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    // Event ID to state group, in first-seen order.
    let mut state_groups: Vec<(&str, Option<i64>)> = Vec::new();
    let mut index_of: HashMap<&str, usize> = HashMap::new();
    for persisting in events {
        let event = persisting.event();
        let context = persisting.context();
        // State DAG rooms give outliers state: the events
        // `/get_missing_events` returns are nominally outliers but need
        // state for their auth events.
        if !event.room_version().msc4242_state_dags && persisting.source.is_outlier()? {
            if context.partial_state {
                return Err(anyhow!(
                    "Outlier event {} claims to have partial state",
                    event.event_id()
                )
                .into());
            }
            continue;
        }
        // A rejected event gets the state of its predecessor.
        let group = if context.is_rejected() {
            context.state_group_before_event
        } else {
            context.state_group
        };
        match index_of.get(event.event_id()) {
            Some(&i) => state_groups[i].1 = group,
            None => {
                index_of.insert(event.event_id(), state_groups.len());
                state_groups.push((event.event_id(), group));
            }
        }
    }

    let partial: Vec<&Persisting<'_>> = events
        .iter()
        .filter(|e| e.context().partial_state)
        .collect();
    let event_ids: Vec<&str> = partial.iter().map(|e| e.event().event_id()).collect();
    let room_ids: Vec<&str> = partial.iter().map(|e| e.event().room_id()).collect();
    let inserted = txn
        .insert_many(
            "partial_state_events",
            &[
                Column::new("room_id", "text", &room_ids),
                Column::new("event_id", "text", &event_ids),
            ],
            None,
        )
        .await;
    if let Err(WriteError::Constraint { .. }) = inserted {
        // The room's partial state rows are gone: it was un-partial stated.
        let rooms: HashSet<&str> = room_ids.iter().copied().collect();
        info!(
            "Cannot persist events {event_ids:?} in rooms {rooms:?}: room has been un-partial stated"
        );
        return Err(WriteError::Other(anyhow::Error::new(PartialStateConflict)));
    }
    inserted?;

    let event_ids: Vec<&str> = state_groups.iter().map(|(id, _)| *id).collect();
    let groups: Vec<Option<i64>> = state_groups.iter().map(|(_, group)| *group).collect();
    txn.upsert_many(
        "event_to_state_groups",
        &[Column::new("event_id", "text", &event_ids)],
        &[Column::new("state_group", "bigint", &groups)],
    )
    .await?;

    for (event_id, group) in state_groups {
        // Python would prefill a missing group too, but the insert above
        // has failed by then.
        if let Some(group) = group {
            effects.add_state_group_prefill(event_id.to_owned(), group);
        }
    }
    Ok(())
}

/// `_persist_event_auth_chain_txn`: the chain cover index of the new state
/// events, which [`crate::persist::chain_cover`] calculated, and their
/// auth events, rejected ones included as state resolution reads them.
pub async fn persist_auth_chain(
    txn: &WriteTxn,
    events: &[Persisting<'_>],
    new_event_links: &HashMap<Arc<str>, NewEventChainLinks>,
) -> Result<(), WriteError> {
    if !new_event_links.is_empty() {
        // `_persist_chain_cover_index`.
        let event_ids: Vec<&str> = new_event_links.keys().map(|id| &**id).collect();
        let chain_ids: Vec<i64> = new_event_links.values().map(|l| l.chain_id).collect();
        let sequence_numbers: Vec<i64> = new_event_links
            .values()
            .map(|l| l.sequence_number)
            .collect();
        txn.insert_many(
            "event_auth_chains",
            &[
                Column::new("event_id", "text", &event_ids),
                Column::new("chain_id", "bigint", &chain_ids),
                Column::new("sequence_number", "bigint", &sequence_numbers),
            ],
            None,
        )
        .await?;
        txn.execute(
            "DELETE FROM event_auth_chain_to_calculate WHERE event_id = ANY($1)",
            &[&event_ids],
        )
        .await?;

        let (mut origin_chains, mut origin_seqs) = (Vec::new(), Vec::new());
        let (mut target_chains, mut target_seqs) = (Vec::new(), Vec::new());
        for l in new_event_links.values() {
            for &(chain, seq) in &l.links {
                origin_chains.push(l.chain_id);
                origin_seqs.push(l.sequence_number);
                target_chains.push(chain);
                target_seqs.push(seq);
            }
        }
        txn.insert_many(
            "event_auth_chain_links",
            &[
                Column::new("origin_chain_id", "bigint", &origin_chains),
                Column::new("origin_sequence_number", "bigint", &origin_seqs),
                Column::new("target_chain_id", "bigint", &target_chains),
                Column::new("target_sequence_number", "bigint", &target_seqs),
            ],
            None,
        )
        .await?;
    }

    let mut event_ids = Vec::new();
    let mut room_ids = Vec::new();
    let mut auth_ids = Vec::new();
    for persisting in events {
        let event = persisting.event();
        if !event.is_state() {
            continue;
        }
        let auth_events = event
            .auth_event_ids()
            .map_err(metadata_error)
            .with_context(|| format!("reading the auth events of {}", event.event_id()))?;
        for auth_id in auth_events {
            event_ids.push(event.event_id());
            room_ids.push(event.room_id());
            auth_ids.push(auth_id);
        }
    }
    txn.insert_many(
        "event_auth",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
            Column::new("auth_id", "text", &auth_ids),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `_store_rejected_events_txn`: a `rejections` row for each rejected
/// event, which goes no further. Returns the events left.
pub async fn store_rejections<'a>(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: Vec<Persisting<'a>>,
) -> Result<Vec<Persisting<'a>>, WriteError> {
    let (rejected, left): (Vec<_>, Vec<_>) =
        events.into_iter().partition(|e| e.context().is_rejected());
    if rejected.is_empty() {
        return Ok(left);
    }
    let event_ids: Vec<&str> = rejected.iter().map(|e| e.event().event_id()).collect();
    let reasons: Vec<&str> = rejected
        .iter()
        .map(|e| e.context().rejected.as_deref().unwrap_or_default())
        .collect();
    // `last_check` is a text column.
    let last_checks = vec![input.now_ms()?.to_string(); rejected.len()];
    txn.insert_many(
        "rejections",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("reason", "text", &reasons),
            Column::new("last_check", "text", &last_checks),
        ],
        None,
    )
    .await?;
    Ok(left)
}
