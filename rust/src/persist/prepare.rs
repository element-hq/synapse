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

//! What `_persist_event_batch` computes for a chunk before its write
//! transaction: the forward extremities and state delta, then the sliding
//! sync table changes and the sticky events to un-soft-fail, then the chain
//! cover index.
//!
//! Python computes the sliding sync changes and the sticky events inside
//! `_persist_events_and_state_updates`, once the chunk's stream orderings
//! are allocated. Neither reads them, so here they come before the
//! allocation, where they cannot hold up other rooms' events.
//!
//! The chain cover step comes last because it is the only one that writes:
//! it puts off events whose auth events are missing. A failure in an
//! earlier step leaves nothing behind for Python to find when it computes
//! the chunk again. A dry run, for shadow mode, rolls that write back.
//!
//! Python's computation fills two Python caches on the way: the state
//! resolution handler's `_state_cache` of resolved state group sets, and
//! the state group cache. This does not, so when Rust's result is used the
//! next Python read of the same groups misses. Creating a room costs one
//! more `_get_state_groups_from_groups` transaction, and the first local
//! event after a fork resolves state again.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use super::{
    chain_cover::{self, ChainCoverEvent, NewEventChainLinks},
    context::PersistContext,
    extremities::NewEvent,
    metrics::PersistCounts,
    reader::{self, PersistReader},
    sliding_sync::{self, SlidingSyncEvent, SlidingSyncTableChanges},
    state_delta::{self, NativeStateResolver, StateAndExtremities, StateResFallback},
    sticky,
};
use crate::{
    clock::Clock,
    database::postgres::pool::ConnectionPool,
    events::Event,
    state::EventMap,
};

/// A chunk of one room's events, in the shapes the steps read.
pub struct PrepareInput {
    pub room_id: String,
    pub backfilled: bool,
    /// Whether every event is in a room version with MSC4242 state DAGs.
    pub state_dag: bool,
    pub events: Vec<NewEvent>,
    pub event_map: EventMap,
    pub chain_cover_events: Vec<ChainCoverEvent>,
    pub sliding_sync_events: Vec<SlidingSyncEvent>,
    pub sticky_events: Vec<Event>,
}

impl PrepareInput {
    /// The chunk `events_and_contexts`, which share their data with the
    /// Python events they were taken from.
    pub fn new(
        room_id: String,
        events_and_contexts: &[(Event, PersistContext)],
        backfilled: bool,
    ) -> anyhow::Result<Self> {
        let (events, event_map) = reader::chunk(events_and_contexts)?;
        let chunk_events: Vec<&Event> =
            events_and_contexts.iter().map(|(event, _)| event).collect();
        Ok(PrepareInput {
            room_id,
            backfilled,
            state_dag: chunk_events
                .iter()
                .all(|event| event.room_version().msc4242_state_dags),
            events,
            event_map,
            chain_cover_events: chunk_events
                .iter()
                .filter_map(|event| ChainCoverEvent::new(event))
                .collect(),
            sliding_sync_events: chunk_events
                .iter()
                .filter_map(|event| SlidingSyncEvent::new(event))
                .collect(),
            sticky_events: chunk_events
                .iter()
                .map(|event| event.shallow_clone())
                .collect(),
        })
    }
}

/// What the chunk's write transaction takes.
#[derive(Debug)]
pub struct PreparedChunk {
    /// The extremities and state delta. All `None` for a backfilled chunk.
    pub state: StateAndExtremities,
    pub new_event_links: HashMap<Arc<str>, NewEventChainLinks>,
    /// `None` when the state does not change.
    pub sliding_sync: Option<SlidingSyncTableChanges>,
    pub sticky_events_to_un_soft_fail: HashSet<Arc<str>>,
}

/// The outcome of [`prepare_chunk`].
#[derive(Debug)]
pub enum Prepared {
    Chunk(Box<PreparedChunk>),
    /// The Rust state resolution cannot resolve the room: Python should
    /// compute the chunk. Nothing was written.
    StateResFallback(String),
}

/// What [`prepare_chunk`] needs besides the chunk.
pub struct PrepareOptions<'a> {
    pub server_name: &'a str,
    /// Read when a step needs the time, as Python reads its clock.
    pub clock: &'a Clock,
    /// `experimental_features.msc4354_enabled`: whether to look for sticky
    /// events to un-soft-fail.
    pub sticky: bool,
    /// Roll back the chain cover step's write.
    pub dry_run: bool,
}

/// Compute what the chunk's write transaction takes, as
/// `_persist_event_batch` does. Adds to `counts` what Python counts on the
/// way. The caller should drop them on a [`Prepared::StateResFallback`],
/// since Python counts the chunk again.
pub async fn prepare_chunk(
    reader: &PersistReader<'_>,
    resolver: &NativeStateResolver,
    pool: &ConnectionPool,
    input: &PrepareInput,
    options: &PrepareOptions<'_>,
    counts: &mut PersistCounts,
) -> anyhow::Result<Prepared> {
    let room_id = input.room_id.as_str();
    let now_ms = || i64::try_from(options.clock.now_millis());

    // The state and the chain cover index need nothing from each other, so
    // they are worked out at the same time, on their own connections.
    let state_part = async {
        let state = if input.backfilled {
            StateAndExtremities::default()
        } else {
            let result = if input.state_dag {
                state_delta::process_state_dag_forward_extremities_and_state_delta(
                    reader,
                    resolver,
                    room_id,
                    options.server_name,
                    &input.events,
                    &input.event_map,
                    now_ms()?,
                    counts,
                )
                .await
            } else {
                state_delta::calculate_new_forward_extremities_and_state_delta(
                    reader,
                    resolver,
                    room_id,
                    options.server_name,
                    &input.events,
                    &input.event_map,
                    now_ms()?,
                    counts,
                )
                .await
            };
            match result {
                Ok(state) => state,
                Err(e) => match e.downcast::<StateResFallback>() {
                    Ok(fallback) => return Ok(Err(Prepared::StateResFallback(fallback.0))),
                    Err(e) => return Err(e),
                },
            }
        };

        let mut sliding_sync = None;
        let mut sticky_events_to_un_soft_fail = HashSet::new();
        if let Some(delta) = &state.state_delta {
            sliding_sync = Some(
                sliding_sync::calculate_sliding_sync_table_changes(
                    reader,
                    room_id,
                    options.server_name,
                    &input.sliding_sync_events,
                    delta,
                )
                .await?,
            );
            if options.sticky {
                sticky_events_to_un_soft_fail = sticky::compute_sticky_events_to_un_soft_fail(
                    reader,
                    room_id,
                    &input.sticky_events,
                    delta,
                    now_ms()?,
                )
                .await?;
            }
        }
        anyhow::Ok(Ok((state, sliding_sync, sticky_events_to_un_soft_fail)))
    };

    let chain_cover = chain_cover::calculate_chain_cover_index_for_events(
        pool,
        room_id,
        &input.chain_cover_events,
        options.dry_run,
    );

    let (state_part, new_event_links) = futures::future::join(state_part, chain_cover).await;
    // The state's outcome first, as when it was worked out first.
    let (state, sliding_sync, sticky_events_to_un_soft_fail) = match state_part? {
        Ok(state_part) => state_part,
        Err(fallback) => return Ok(fallback),
    };
    let new_event_links = new_event_links?;

    Ok(Prepared::Chunk(Box::new(PreparedChunk {
        state,
        new_event_links,
        sliding_sync,
        sticky_events_to_un_soft_fail,
    })))
}
