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

//! The room's DAG: the extremities (`_update_forward_extremities_txn`,
//! `_set_state_dag_extremities_txn` and `_update_backward_extremeties`) and
//! the edges (`_handle_mult_prev_events` and `_store_state_dag_edges`).

use std::{collections::HashSet, sync::Arc};

use anyhow::anyhow;

use super::Persisting;
use crate::{
    database::postgres::write::{Column, WriteError, WriteTxn},
    events::{constants::event_type, Event},
};

/// `_update_forward_extremities_txn`: replace the room's forward
/// extremities, and record them at `max_stream_order`.
pub async fn update_forward_extremities(
    txn: &WriteTxn,
    room_id: &str,
    new_forward_extremities: &HashSet<Arc<str>>,
    max_stream_order: i64,
) -> Result<(), WriteError> {
    txn.execute(
        "DELETE FROM event_forward_extremities WHERE room_id = $1",
        &[&room_id],
    )
    .await?;

    let event_ids: Vec<&str> = new_forward_extremities.iter().map(|id| &**id).collect();
    let room_ids = vec![room_id; event_ids.len()];
    txn.insert_many(
        "event_forward_extremities",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("room_id", "text", &room_ids),
        ],
        None,
    )
    .await?;

    let stream_orderings = vec![max_stream_order; event_ids.len()];
    txn.insert_many(
        "stream_ordering_to_exterm",
        &[
            Column::new("room_id", "text", &room_ids),
            Column::new("event_id", "text", &event_ids),
            Column::new("stream_ordering", "bigint", &stream_orderings),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// `_set_state_dag_extremities_txn`: replace the room's MSC4242 state DAG
/// forward extremities.
pub async fn set_state_dag_extremities(
    txn: &WriteTxn,
    room_id: &str,
    new_extremities: &HashSet<Arc<str>>,
) -> Result<(), WriteError> {
    txn.execute(
        "DELETE FROM msc4242_state_dag_forward_extremities WHERE room_id = $1",
        &[&room_id],
    )
    .await?;
    let event_ids: Vec<&str> = new_extremities.iter().map(|id| &**id).collect();
    let room_ids = vec![room_id; event_ids.len()];
    txn.insert_many(
        "msc4242_state_dag_forward_extremities",
        &[
            Column::new("room_id", "text", &room_ids),
            Column::new("event_id", "text", &event_ids),
        ],
        None,
    )
    .await?;
    Ok(())
}

/// An event for [`update_backward_extremities`].
pub struct BackwardEvent<'a> {
    pub event: &'a Event,
    pub outlier: bool,
    pub stream_ordering: i64,
}

/// `_update_backward_extremeties`: the prev events of `events` that we do
/// not have, other than as outliers, become backward extremities, with a
/// timeline gap at each event that points at one. `events` themselves stop
/// being backward extremities, and their failed pull attempts go.
///
/// For new events, and for outliers that are now persisted as
/// non-outliers. `events` must be of one room.
pub async fn update_backward_extremities(
    txn: &WriteTxn,
    instance_name: &str,
    events: &[BackwardEvent<'_>],
) -> Result<(), WriteError> {
    let Some(first) = events.first() else {
        return Ok(());
    };
    let room_id = first.event.room_id();
    let prev_event_ids: Vec<Vec<String>> =
        events.iter().map(|e| e.event.prev_event_ids()).collect();

    let mut potential: HashSet<&str> = events
        .iter()
        .zip(&prev_event_ids)
        .filter(|(e, _)| !e.outlier)
        .flat_map(|(_, prevs)| prevs.iter().map(String::as_str))
        .collect();
    if potential.is_empty() {
        return Ok(());
    }

    let candidates: Vec<&str> = potential.iter().copied().collect();
    let rows = txn
        .query(
            "SELECT event_id FROM events WHERE event_id = ANY($1) AND outlier = FALSE",
            &[&candidates],
        )
        .await?;
    for row in rows {
        let event_id: &str = row.try_get(0).map_err(anyhow::Error::new)?;
        potential.remove(event_id);
    }

    if !potential.is_empty() {
        let event_ids: Vec<&str> = potential.iter().copied().collect();
        let room_ids = vec![room_id; event_ids.len()];
        txn.upsert_many(
            "event_backward_extremities",
            &[
                Column::new("room_id", "text", &room_ids),
                Column::new("event_id", "text", &event_ids),
            ],
            &[],
        )
        .await?;

        // Record the stream orderings where we have new gaps.
        let gap_orderings: Vec<i64> = events
            .iter()
            .zip(&prev_event_ids)
            .filter(|(_, prevs)| prevs.iter().any(|prev| potential.contains(prev.as_str())))
            .map(|(e, _)| e.stream_ordering)
            .collect();
        let room_ids = vec![room_id; gap_orderings.len()];
        let instance_names = vec![instance_name; gap_orderings.len()];
        txn.insert_many(
            "timeline_gaps",
            &[
                Column::new("room_id", "text", &room_ids),
                Column::new("instance_name", "text", &instance_names),
                Column::new("stream_ordering", "bigint", &gap_orderings),
            ],
            None,
        )
        .await?;
    }

    // An event with no prev events will never have anything to backfill
    // from, so it stops being a backward extremity even as an outlier.
    let to_remove: Vec<&str> = events
        .iter()
        .zip(&prev_event_ids)
        .filter(|(e, prevs)| !e.outlier || prevs.is_empty())
        .map(|(e, _)| e.event.event_id())
        .collect();
    if !to_remove.is_empty() {
        txn.execute(
            "DELETE FROM event_backward_extremities WHERE event_id = ANY($1) AND room_id = $2",
            &[&to_remove, &room_id],
        )
        .await?;
        txn.execute(
            "DELETE FROM event_failed_pull_attempts WHERE event_id = ANY($1) AND room_id = $2",
            &[&to_remove, &room_id],
        )
        .await?;
    }
    Ok(())
}

/// `_handle_mult_prev_events`: the `event_edges` of `events`, then their
/// backward extremities. `events` must be of one room.
pub async fn handle_mult_prev_events(
    txn: &WriteTxn,
    instance_name: &str,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let mut event_ids = Vec::new();
    let mut prev_event_ids = Vec::new();
    let mut backward = Vec::with_capacity(events.len());
    for persisting in events {
        let event = persisting.event();
        for prev in event.prev_event_ids() {
            event_ids.push(event.event_id());
            prev_event_ids.push(prev);
        }
        backward.push(BackwardEvent {
            event,
            outlier: persisting.source.is_outlier()?,
            stream_ordering: persisting.stream_ordering(),
        });
    }
    txn.insert_many(
        "event_edges",
        &[
            Column::new("event_id", "text", &event_ids),
            Column::new("prev_event_id", "text", &prev_event_ids),
        ],
        None,
    )
    .await?;
    update_backward_extremities(txn, instance_name, &backward).await
}

/// `_store_state_dag_edges`: the MSC4242 state DAG edges of the state
/// events of `events` in a room version with a state DAG.
pub async fn store_state_dag_edges(
    txn: &WriteTxn,
    events: &[Persisting<'_>],
) -> Result<(), WriteError> {
    let mut room_ids = Vec::new();
    let mut event_ids = Vec::new();
    let mut prev_state_event_ids: Vec<Option<&str>> = Vec::new();
    for persisting in events {
        let event = persisting.event();
        if !event.room_version().msc4242_state_dags || event.state_key().is_none() {
            continue;
        }
        let prevs = event.prev_state_event_ids().unwrap_or_default();
        if prevs.is_empty() {
            // `get_state_dag` reads every row, so the create event, which has
            // no edge, gets one to nothing.
            if event.type_() != event_type::M_ROOM_CREATE {
                return Err(anyhow!(
                    "state DAG event {} has no prev_state_events",
                    event.event_id()
                )
                .into());
            }
            room_ids.push(event.room_id());
            event_ids.push(event.event_id());
            prev_state_event_ids.push(None);
            continue;
        }
        for prev in prevs {
            room_ids.push(event.room_id());
            event_ids.push(event.event_id());
            prev_state_event_ids.push(Some(prev));
        }
    }
    // Python inserts the create event's row and upserts the rest. Only the
    // create event has a NULL edge, and the key of a NULL never conflicts,
    // so one upsert does both.
    txn.upsert_many(
        "msc4242_state_dag_edges",
        &[
            Column::new("room_id", "text", &room_ids),
            Column::new("event_id", "text", &event_ids),
            Column::new("prev_state_event_id", "text", &prev_state_event_ids),
        ],
        &[],
    )
    .await?;
    Ok(())
}
