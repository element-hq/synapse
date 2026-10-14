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

//! `_load_filtered_recents`: the timeline of one room's entry.

use std::collections::HashSet;
use std::sync::Arc;

use crate::events::Event;
use crate::storage::sync_store::{PaginationOrder, SyncReader};
use crate::sync::client_events::filter_events_for_client;
use crate::sync::filter::RelationLookup;
use crate::sync::request::{PrevBatchBase, RoomSyncInputData};
use crate::sync::room::RoomContext;
use crate::types::stream_token::{Direction, RoomStreamToken};
use crate::visibility::FilteredEvent;

/// How many times the timeline is paginated before giving up on filling
/// it, when visibility keeps taking events out.
const MAX_REPEAT: u32 = 5;

/// A room's timeline in a sync response. See `TimelineBatch`.
pub struct TimelineBatch {
    pub events: Vec<FilteredEvent>,
    pub prev_batch_base: PrevBatchBase,
    pub prev_batch_room_key: Option<RoomStreamToken>,
    pub limited: bool,
    pub bundle_aggregations: bool,
}

impl RelationLookup for SyncReader<'_> {
    type Error = anyhow::Error;

    fn events_have_relations(
        &self,
        event_ids: &[&str],
        related_by_senders: Option<&[String]>,
        related_by_rel_types: Option<&[String]>,
    ) -> impl std::future::Future<Output = Result<HashSet<String>, Self::Error>> + Send {
        SyncReader::events_have_relations(self, event_ids, related_by_senders, related_by_rel_types)
    }
}

/// The token just before `event` in the stream, as `prev_batch` points
/// there.
fn token_before(event: &FilteredEvent) -> anyhow::Result<RoomStreamToken> {
    let stream_ordering = event
        .event
        .internal_metadata()
        .stream_ordering()
        .ok_or_else(|| {
            anyhow::anyhow!("event {} has no stream ordering", event.event.event_id())
        })?;
    Ok(RoomStreamToken::live(stream_ordering.get() - 1))
}

/// Where Python's `list[-limit:]` starts, for a list of `len` items.
fn python_tail_start(len: usize, limit: i64) -> usize {
    match limit {
        0 => 0,
        limit if limit > 0 => len.saturating_sub(limit as usize),
        limit => (limit.unsigned_abs() as usize).min(len),
    }
}

/// Filter loaded events for the timeline: the request's filter, then
/// visibility, with the events in the room's current state always kept.
async fn filter_recents(
    context: &RoomContext<'_, '_>,
    events: Vec<Arc<Event>>,
) -> anyhow::Result<Vec<FilteredEvent>> {
    let reader = context.reader;
    let recents = context
        .request
        .filter
        .filter_room_timeline(events, reader)
        .await?;

    let state_event_ids: Vec<Arc<str>> = recents
        .iter()
        .filter(|event| event.is_state())
        .map(|event| Arc::from(event.event_id()))
        .collect();
    let current_state_ids = if state_event_ids.is_empty() {
        HashSet::new()
    } else {
        reader.events_in_current_state(&state_event_ids).await?
    };

    filter_events_for_client(
        reader,
        &context.client_filter,
        &recents,
        &current_state_ids,
        context.now_ms(),
    )
    .await
}

/// `_load_filtered_recents`, less the bundled aggregations, which Python
/// adds when `bundle_aggregations` is set.
pub async fn load_filtered_recents(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
) -> anyhow::Result<TimelineBatch> {
    let reader = context.reader;
    let filter = &context.request.filter;
    let timeline_limit = filter.timeline_limit();
    let block_all_timeline = filter.blocks_all_room_timeline();

    let mut potential_recents = room.events.clone();
    let mut limited = match &potential_recents {
        None => true,
        Some(events) => room.newly_joined || timeline_limit < events.len() as i64,
    };

    // A gap in the timeline means the passed in events cannot be trusted:
    // the timeline is loaded again from the now token.
    let gap_token = reader
        .timeline_gap(
            &room.room_id,
            room.since.as_ref(),
            &context.request.now_room_key,
        )
        .await?;
    let mut prev_batch_base = PrevBatchBase::Upto;
    let mut upto_key = room.upto.clone();
    if gap_token.is_some() {
        potential_recents = None;
        upto_key = context.request.now_room_key.clone();
        prev_batch_base = PrevBatchBase::Now;
        limited = true;
    }

    let mut filtered_recents = match potential_recents {
        Some(events) if !events.is_empty() => filter_recents(context, events).await?,
        _ => Vec::new(),
    };

    if !limited || block_all_timeline {
        let prev_batch_room_key = filtered_recents.first().map(token_before).transpose()?;
        return Ok(TimelineBatch {
            events: filtered_recents,
            prev_batch_base,
            prev_batch_room_key,
            limited: false,
            bundle_aggregations: false,
        });
    }

    let load_limit = (timeline_limit * 2).max(10);
    let mut max_repeat = MAX_REPEAT;
    let mut end_key = upto_key;

    let since_key = match (&room.since, &gap_token) {
        // After a gap, only the events after it.
        (Some(_), Some(gap)) => Some(gap.clone()),
        (Some(since), None) if !room.newly_joined => Some(since.clone()),
        _ => None,
    };
    // Initial syncs view a historical section of the timeline, in
    // topological order, as `/messages` does. Incremental ones want every
    // update since the last sync, in the order the server received them.
    let order = if since_key.is_none() {
        PaginationOrder::Topological
    } else {
        PaginationOrder::Stream
    };

    while limited && (filtered_recents.len() as i64) < timeline_limit && max_repeat > 0 {
        let (mut events, new_end_key, page_limited) = reader
            .paginate_room_events(
                &room.room_id,
                &end_key,
                since_key.as_ref(),
                Direction::Backwards,
                load_limit,
                order,
            )
            .await?;
        end_key = new_end_key;
        limited = page_limited;
        events.reverse();

        let mut loaded =
            filter_recents(context, events.into_iter().map(Arc::new).collect()).await?;
        loaded.extend(filtered_recents);
        filtered_recents = loaded;

        max_repeat -= 1;
    }

    let mut prev_batch_room_key = None;
    if (filtered_recents.len() as i64) > timeline_limit {
        limited = true;
        let start = python_tail_start(filtered_recents.len(), timeline_limit);
        filtered_recents.drain(..start);
        prev_batch_room_key = filtered_recents.first().map(token_before).transpose()?;
    }

    Ok(TimelineBatch {
        events: filtered_recents,
        prev_batch_base,
        prev_batch_room_key,
        bundle_aggregations: limited || room.newly_joined,
        // Also limited for a new room or after a gap, to make the client
        // paginate.
        limited: limited || room.newly_joined || gap_token.is_some(),
    })
}

#[cfg(test)]
mod tests {
    use super::python_tail_start;

    #[test]
    fn tail_start_matches_python_slicing() {
        // `list(range(5))[-limit:]` in Python.
        assert_eq!(python_tail_start(5, 3), 2);
        assert_eq!(python_tail_start(5, 7), 0);
        assert_eq!(python_tail_start(5, 0), 0);
        assert_eq!(python_tail_start(5, -2), 2);
        assert_eq!(python_tail_start(5, -9), 5);
    }
}
