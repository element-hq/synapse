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

//! `compute_state_delta`: the `state` (or `state_after`) of one room's
//! entry. The arithmetic is in [`crate::sync::state_delta`]; this reads
//! the state it works on.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use crate::events::constants::event_type::{M_ROOM_ALIASES, M_ROOM_MEMBER};
use crate::events::Event;
use crate::state::{StateKey, StateMap};
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::StateDelta as StoredDelta;
use crate::sync::request::RoomSyncInputData;
use crate::sync::room::RoomContext;
use crate::sync::state_delta::{apply_state_deltas, calculate_state, roll_back_state, StateDelta};
use crate::sync::timeline::TimelineBatch;
use crate::types::state_filter::StateFilter;

/// A room's state in a sync response, by `(type, state_key)`.
pub type RoomState = HashMap<StateKey, Event>;

fn to_deltas(deltas: Vec<StoredDelta>) -> Vec<StateDelta> {
    deltas
        .into_iter()
        .map(|delta| StateDelta {
            event_type: delta.event_type,
            state_key: delta.state_key,
            event_id: delta.event_id,
            prev_event_id: delta.prev_event_id,
        })
        .collect()
}

/// `StateFilter.from_lazy_load_member_list`: every state event but the
/// memberships, and the memberships of `members`.
fn lazy_load_member_filter<'a>(members: impl IntoIterator<Item = &'a Arc<str>>) -> StateFilter {
    let members: HashSet<Arc<str>> = members.into_iter().cloned().collect();
    StateFilter::new(
        HashMap::from([(Arc::from(M_ROOM_MEMBER), Some(members))]),
        true,
    )
}

/// `StateFilter.from_types` over the memberships of `members`.
fn members_filter(members: &HashSet<Arc<str>>) -> StateFilter {
    StateFilter::from_types(
        members
            .iter()
            .map(|member| (M_ROOM_MEMBER, Some(&**member))),
    )
}

/// `compute_state_delta`. The room is not partial state: those fall back
/// to Python before this point.
pub async fn compute_state_delta(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
    batch: &TimelineBatch,
    full_state: bool,
    joined: bool,
) -> anyhow::Result<RoomState> {
    let request = context.request;
    let reader = context.reader;
    let lazy_load_members = request.filter.lazy_load_members();
    let include_redundant_members = request.filter.include_redundant_members();

    // The memberships needed for the timeline, with lazy loading, and the
    // timeline's own contribution to the state (the last event of each
    // key).
    let mut members_to_fetch: Option<HashSet<Arc<str>>> = None;
    let mut timeline_state = StateMap::new();
    if lazy_load_members {
        let mut members = HashSet::new();
        for filtered in &batch.events {
            let event = &filtered.event;
            // With `state_after`, the senders' memberships are fetched
            // whether or not the timeline has them. With `state`, the
            // client layers the timeline on the state, so a membership in
            // the timeline already is not needed.
            if request.use_state_after
                || !timeline_state.contains_key(&StateKey::new(M_ROOM_MEMBER, event.sender()))
            {
                members.insert(Arc::from(event.sender()));
            }
            if let Some(state_key) = event.state_key() {
                timeline_state.insert(
                    StateKey::new(event.type_(), state_key),
                    Arc::from(event.event_id()),
                );
            }
        }
        members_to_fetch = Some(members);
    } else {
        for filtered in &batch.events {
            let event = &filtered.event;
            if let Some(state_key) = event.state_key() {
                timeline_state.insert(
                    StateKey::new(event.type_(), state_key),
                    Arc::from(event.event_id()),
                );
            }
        }
    }

    let (mut state_ids, changed_keys) = if full_state {
        let state_ids = state_for_full_sync(
            context,
            room,
            batch,
            members_to_fetch.as_ref(),
            &timeline_state,
            joined,
        )
        .await?;
        (state_ids, HashSet::new())
    } else {
        let since = room
            .since
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("an incremental sync without a since token"))?;
        state_for_incremental_sync(
            context,
            room,
            batch,
            since,
            members_to_fetch.as_ref(),
            &timeline_state,
            joined,
        )
        .await?
    };

    // Without `include_redundant_members`, send only the memberships this
    // device has not been sent. A state change since the last sync is sent
    // regardless, in case the client is retrying.
    if lazy_load_members && !include_redundant_members {
        context.with_ll_cache(|cache| {
            state_ids.retain(|key, event_id| {
                changed_keys.contains(key) || !cache.was_sent(&key.1, event_id)
            });
            for (key, event_id) in state_ids.iter().chain(timeline_state.iter()) {
                if &*key.0 == M_ROOM_MEMBER {
                    cache.mark_sent(&key.1, event_id);
                }
            }
        });
    }

    let event_ids: Vec<Arc<str>> = state_ids.values().cloned().collect();
    let events: Vec<Arc<Event>> = reader
        .events(&event_ids, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(Arc::new)
        .collect();
    let events = request.filter.filter_room_state(events, reader).await?;

    let mut state = RoomState::new();
    for event in events {
        // Until MSC2261 or an alternative.
        if event.type_() == M_ROOM_ALIASES {
            continue;
        }
        let Some(state_key) = event.state_key() else {
            continue;
        };
        state.insert(
            StateKey::new(event.type_(), state_key),
            event.shallow_clone(),
        );
    }
    Ok(state)
}

/// `_compute_state_delta_for_full_sync`.
async fn state_for_full_sync(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
    batch: &TimelineBatch,
    members_to_fetch: Option<&HashSet<Arc<str>>>,
    timeline_state: &StateMap,
    joined: bool,
) -> anyhow::Result<StateMap> {
    let reader = context.reader;
    let request = context.request;
    let room_id = &*room.room_id;

    let (state_filter, lazy_load_members) = match members_to_fetch {
        // The user's own membership too, so the client knows it is in the
        // room.
        Some(members) => {
            let own = Arc::clone(&request.user_id);
            (
                lazy_load_member_filter(members.iter().chain(std::iter::once(&own))),
                true,
            )
        }
        None => (StateFilter::all(), false),
    };

    if request.use_state_after {
        if joined {
            // The current state, rolled back to the end of the timeline.
            // The deltas are read with no upper bound, after the current
            // state, so that they cover every change the current state may
            // hold. Rolling back a change made after it was read is still
            // right: it is after the end of the timeline too.
            let mut state_ids = reader.current_state_ids(room_id, &state_filter).await?;
            let deltas = reader.state_deltas(room_id, Some(&room.end), None).await?;
            if !deltas.is_empty() {
                roll_back_state(&mut state_ids, &to_deltas(deltas));
            }
            return Ok(state_ids);
        }
        // After a leave there may be many deltas since: read the state at
        // the leave from state groups instead.
        return reader.state_ids_at(room_id, &room.end, &state_filter).await;
    }

    let state_at_timeline_end = reader
        .state_ids_at(room_id, &room.end, &state_filter)
        .await?;
    let state_at_timeline_start = match batch.events.first() {
        // The state after the first event, which is close enough.
        Some(first) => {
            reader
                .state_ids_for_event(first.event.event_id(), &state_filter)
                .await?
        }
        None => state_at_timeline_end.clone(),
    };

    Ok(calculate_state(
        timeline_state,
        &state_at_timeline_start,
        &state_at_timeline_end,
        &StateMap::new(),
        lazy_load_members,
    ))
}

/// `_compute_state_delta_for_incremental_sync`: the state to send, and the
/// keys of it that changed since the last sync.
async fn state_for_incremental_sync(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
    batch: &TimelineBatch,
    since: &crate::types::stream_token::RoomStreamToken,
    members_to_fetch: Option<&HashSet<Arc<str>>>,
    timeline_state: &StateMap,
    joined: bool,
) -> anyhow::Result<(StateMap, HashSet<StateKey>)> {
    let reader = context.reader;
    let request = context.request;
    let room_id = &*room.room_id;

    let (mut state_filter, lazy_load_members) = match members_to_fetch {
        Some(members) => (lazy_load_member_filter(members), true),
        None => (StateFilter::all(), false),
    };

    if request.use_state_after {
        let mut initial = StateMap::new();
        if let Some(members) = members_to_fetch.filter(|members| !members.is_empty()) {
            // The memberships of the timeline's senders, which the caller
            // dedupes against what the device was sent.
            let member_filter = members_filter(members);
            let member_ids = if joined {
                reader.current_state_ids(room_id, &member_filter).await?
            } else {
                // The current state may be from after the user left.
                reader
                    .state_ids_at(room_id, &room.end, &member_filter)
                    .await?
            };
            initial.extend(member_ids);
        }

        // Every delta, membership changes included: incremental syncs are
        // not lazy loaded.
        let deltas = reader
            .state_deltas(room_id, Some(since), Some(&room.end))
            .await?;
        let mut applied = apply_state_deltas(
            initial,
            &to_deltas(deltas),
            timeline_state,
            &request.user_id,
        );

        if !applied.cleared.is_empty() {
            let cleared_filter =
                StateFilter::from_types(applied.cleared.iter().map(|key| (&*key.0, Some(&*key.1))));
            // Partial state has every state event but remote memberships,
            // and those looked up here are from timeline events we have,
            // so there is no waiting for full state.
            let state_at_end = reader
                .state_ids_at(room_id, &room.end, &cleared_filter)
                .await?;
            for (key, event_id) in state_at_end {
                applied.changed.insert(key.clone());
                applied.state.insert(key, event_id);
            }
        }
        return Ok((applied.state, applied.changed));
    }

    // A timeline that is a linear chain from the last sync's last event,
    // and not limited, changed no state outside itself (#16941 explains
    // why this needs the chain).
    let mut is_linear_timeline = true;
    if !batch.events.is_empty() {
        let mut prev_event_id: Option<String> = reader
            .last_event_before(room_id, since)
            .await?
            .map(|(event_id, _)| event_id.to_string());
        for filtered in &batch.events {
            let prev_events = filtered.event.prev_event_ids();
            let follows = match (&prev_event_id, prev_events.as_slice()) {
                (Some(prev), [only]) => only == prev,
                _ => false,
            };
            if !follows {
                is_linear_timeline = false;
                break;
            }
            prev_event_id = Some(filtered.event.event_id().to_owned());
        }
    }

    if is_linear_timeline && !batch.limited {
        let mut state_ids = StateMap::new();
        if lazy_load_members {
            if let (Some(members), Some(first)) = (
                members_to_fetch.filter(|members| !members.is_empty()),
                batch.events.first(),
            ) {
                // Only the memberships, which the caller dedupes.
                state_ids = reader
                    .state_ids_for_event(first.event.event_id(), &members_filter(members))
                    .await?;
            }
        }
        return Ok((state_ids, HashSet::new()));
    }

    let state_at_timeline_start = match batch.events.first() {
        Some(first) => {
            reader
                .state_ids_for_event(first.event.event_id(), &state_filter)
                .await?
        }
        // Possible when the user ignores the senders of every recent
        // event.
        None => {
            reader
                .state_ids_at(room_id, &room.end, &state_filter)
                .await?
        }
    };

    if batch.limited {
        // Gappy syncs are not lazy loaded. `timeline_start` stays filtered
        // to the senders' memberships, which `calculate_state` then sends
        // even if the previous sync had them.
        state_filter = StateFilter::all();
    }

    let state_at_previous_sync = reader.state_ids_at(room_id, since, &state_filter).await?;
    let state_at_timeline_end = reader
        .state_ids_at(room_id, &room.end, &state_filter)
        .await?;

    let state_ids = calculate_state(
        timeline_state,
        &state_at_timeline_start,
        &state_at_timeline_end,
        &state_at_previous_sync,
        lazy_load_members,
    );

    // What differs from the previous sync's state changed. The rest is the
    // senders' memberships, added back for lazy loading.
    let changed_keys = state_ids
        .iter()
        .filter(|(key, event_id)| state_at_previous_sync.get(*key) != Some(*event_id))
        .map(|(key, _)| key.clone())
        .collect();

    Ok((state_ids, changed_keys))
}
