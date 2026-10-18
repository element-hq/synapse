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

//! The MSC4354 sticky events of the chunk: `insert_sticky_events_txn` and
//! `un_soft_fail_sticky_events_txn`. Their rows take IDs from the sticky
//! events stream inside the transaction. Removing the stickiness of
//! redacted events is in [`super::events`].

use log::info;

use super::{metadata_error, TxnInput};
use crate::{
    database::postgres::write::{OwnedColumn, OwnedParam, WriteError, WriteTxn},
    events::Event,
    persist::effects::PersistEffects,
};

/// `insert_sticky_events_txn`: the events that are sticky now become rows
/// of `sticky_events`. Outliers, events that are not sticky, and events
/// whose stickiness has expired are skipped. Soft-failed events are not,
/// as their soft failure may be lifted.
///
/// Each event's stream ordering is the one it is persisted with in this
/// chunk, even for an event persisted before as an outlier.
pub async fn insert_sticky_events(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    events: &[(&Event, i64)],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    let now_ms = input.now_ms()?;
    let mut sticky = Vec::new();
    for &(event, stream_ordering) in events {
        // An outlier has no state, so it cannot be sticky until it is
        // de-outliered.
        if event
            .internal_metadata()
            .is_outlier()
            .map_err(metadata_error)?
        {
            continue;
        }
        let Some(expires_at) = event.locally_sticky_until_ts().map_err(metadata_error)? else {
            continue;
        };
        if expires_at <= now_ms {
            continue;
        }
        sticky.push((event, stream_ordering, expires_at));
    }
    let Some((first, _, _)) = sticky.first() else {
        return Ok(());
    };
    info!(
        "inserting {} sticky events in room {}",
        sticky.len(),
        first.room_id()
    );

    let stream_ids = input.sticky_stream.next_ids(txn, sticky.len()).await?;
    effects.add_sticky_stream_ids(&stream_ids);
    let instance_names = vec![input.instance_name; sticky.len()];
    let room_ids: Vec<&str> = sticky.iter().map(|(e, _, _)| e.room_id()).collect();
    let event_ids: Vec<&str> = sticky.iter().map(|(e, _, _)| e.event_id()).collect();
    let stream_orderings: Vec<i64> = sticky.iter().map(|&(_, o, _)| o).collect();
    let senders: Vec<&str> = sticky.iter().map(|(e, _, _)| e.sender()).collect();
    let expiries: Vec<i64> = sticky.iter().map(|&(_, _, at)| at).collect();
    // `stream_id` and `event_stream_ordering` are `INTEGER`, which a
    // `bigint` casts to on assignment.
    txn.insert_many_later(
        "sticky_events",
        vec![
            OwnedColumn::copy("instance_name", "text", &instance_names),
            OwnedColumn::copy("stream_id", "bigint", &stream_ids),
            OwnedColumn::copy("room_id", "text", &room_ids),
            OwnedColumn::copy("event_id", "text", &event_ids),
            OwnedColumn::copy("event_stream_ordering", "bigint", &stream_orderings),
            OwnedColumn::copy("sender", "text", &senders),
            OwnedColumn::copy("expires_at", "bigint", &expiries),
        ],
        None,
    )?;
    Ok(())
}

/// `un_soft_fail_sticky_events_txn`: the events stop being soft failed,
/// and move to the end of the sticky events stream, so clients are told
/// about them. They keep their order in the stream. Any without a row,
/// which the update leaves alone, go last, by event ID.
pub async fn un_soft_fail_sticky_events(
    txn: &WriteTxn,
    input: &TxnInput<'_>,
    event_ids: &[&str],
    effects: &mut PersistEffects,
) -> Result<(), WriteError> {
    if event_ids.is_empty() {
        return Ok(());
    }
    txn.execute_later(
        "UPDATE event_json
        SET internal_metadata = (
            jsonb_set(internal_metadata::jsonb, '{soft_failed}', 'false'::jsonb)
        )::text
        WHERE event_id = ANY($1)",
        crate::owned_params![event_ids],
    );
    for event_id in event_ids {
        effects.add_event_cache_invalidation((*event_id).to_owned());
    }

    let rows = txn
        .query(
            "SELECT event_id FROM sticky_events WHERE event_id = ANY($1)
            ORDER BY event_stream_ordering",
            &[&event_ids],
        )
        .await?;
    let mut ordered: Vec<String> = rows
        .iter()
        .map(|row| row.try_get(0))
        .collect::<Result<_, _>>()
        .map_err(anyhow::Error::new)?;
    let mut without_rows: Vec<&str> = event_ids
        .iter()
        .copied()
        .filter(|id| !ordered.iter().any(|o| o == id))
        .collect();
    without_rows.sort_unstable();
    ordered.extend(without_rows.into_iter().map(str::to_owned));

    let stream_ids = input.sticky_stream.next_ids(txn, event_ids.len()).await?;
    effects.add_sticky_stream_ids(&stream_ids);
    let rows: Vec<Vec<OwnedParam>> = stream_ids
        .iter()
        .zip(&ordered)
        .map(|(stream_id, event_id)| crate::owned_params![stream_id, input.instance_name, event_id])
        .collect();
    // `sticky_events` has no index on `event_id`, as in Python.
    txn.execute_batch_later(
        "UPDATE sticky_events SET stream_id = $1::bigint, instance_name = $2
        WHERE event_id = $3",
        rows,
    );
    Ok(())
}
