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

//! `filter_and_transform_events_for_client` over the [`SyncReader`]: load
//! what the per-event checks in [`crate::visibility`] read, then run them.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use anyhow::Context;

use crate::events::constants::event_type::{
    M_ROOM_HISTORY_VISIBILITY, M_ROOM_JOIN_RULES, M_ROOM_MEMBER,
};
use crate::events::Event;
use crate::state::{lookup, StateMap};
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::SyncReader;
use crate::visibility::{
    check_event_for_client, keep_soft_failed, FilteredEvent, SoftFailedPolicy, StateAtEvent,
    VisibilityInputs,
};

/// `EventTypes.CallInvite`.
const M_CALL_INVITE: &str = "m.call.invite";

/// What the checks need to know about the requesting user, fetched once
/// per request.
#[derive(Debug, Clone)]
pub struct ClientFilterContext {
    pub user_id: Arc<str>,
    pub ignored_users: Arc<HashSet<Arc<str>>>,
    /// `Some` for a server admin: what they asked to see of soft-failed
    /// events.
    pub admin_policy: Option<SoftFailedPolicy>,
    pub msc4354_enabled: bool,
}

/// `filter_and_transform_events_for_client` with `filter_send_to_client`
/// and not peeking: the events the user may see, in order, each pruned if
/// its sender was erased and with the user's membership at it.
///
/// `now_ms` is the time the sticky and retention checks compare with.
pub async fn filter_events_for_client(
    reader: &SyncReader<'_>,
    context: &ClientFilterContext,
    events: &[Arc<Event>],
    always_include_ids: &HashSet<Arc<str>>,
    now_ms: i64,
) -> anyhow::Result<Vec<FilteredEvent>> {
    if events.is_empty() {
        return Ok(Vec::new());
    }

    // Soft-failed events are left out unless a server admin asked for them.
    // A call invite among the events left by default is what decides
    // whether the join rules are read, as in the Python.
    let mut found_call_invite = false;
    for event in events {
        if !event.internal_metadata().soft_failed().unwrap_or(false)
            && event.type_() == M_CALL_INVITE
            && !event.is_state()
        {
            found_call_invite = true;
        }
    }
    let events: Vec<&Arc<Event>> = events
        .iter()
        .filter(|event| keep_soft_failed(event, context.admin_policy.as_ref()))
        .collect();

    // The state after each event that is not an outlier: the history
    // visibility, the user's membership and, for call invites, the join
    // rules.
    let wanted_event_ids: Vec<Arc<str>> = events
        .iter()
        .filter(|event| !event.internal_metadata().is_outlier().unwrap_or(false))
        .map(|event| Arc::from(event.event_id()))
        .collect();
    let groups = reader.state_group_for_events(&wanted_event_ids).await?;
    for event_id in &wanted_event_ids {
        if !groups.contains_key(event_id) {
            // `get_state_for_events` fails the same way.
            anyhow::bail!("no state group for event {event_id}");
        }
    }
    let mut unique_groups: Vec<i64> = groups.values().copied().collect();
    unique_groups.sort_unstable();
    unique_groups.dedup();
    let group_state = reader.state_for_groups(&unique_groups).await?;

    let user_id = &*context.user_id;
    let pick = |state: &StateMap| -> [Option<Arc<str>>; 3] {
        [
            lookup(state, (M_ROOM_HISTORY_VISIBILITY, "")).cloned(),
            lookup(state, (M_ROOM_MEMBER, user_id)).cloned(),
            if found_call_invite {
                lookup(state, (M_ROOM_JOIN_RULES, "")).cloned()
            } else {
                None
            },
        ]
    };
    let mut picked: HashMap<i64, [Option<Arc<str>>; 3]> = HashMap::new();
    for group in &unique_groups {
        let state = group_state
            .get(group)
            .with_context(|| format!("no state for state group {group}"))?;
        picked.insert(*group, pick(state));
    }

    let mut state_event_ids: Vec<Arc<str>> = picked.values().flatten().flatten().cloned().collect();
    state_event_ids.sort_unstable();
    state_event_ids.dedup();
    let state_events: HashMap<Arc<str>, Event> = reader
        .events(&state_event_ids, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(|event| (Arc::from(event.event_id()), event))
        .collect();

    let senders: Vec<Arc<str>> = {
        let mut senders: Vec<Arc<str>> = events
            .iter()
            .map(|event| Arc::from(event.sender()))
            .collect();
        senders.sort_unstable();
        senders.dedup();
        senders
    };
    let erased = reader.are_users_erased(&senders).await?;

    let mut retention: HashMap<&str, Option<i64>> = HashMap::new();
    for event in &events {
        if !retention.contains_key(event.room_id()) {
            let policy = reader.retention_policy(event.room_id()).await?;
            retention.insert(event.room_id(), policy.max_lifetime);
        }
    }

    let mut filtered = Vec::with_capacity(events.len());
    for event in events {
        let state = groups.get(event.event_id()).map(|group| {
            let [vis, member, rules] = &picked[group];
            let get = |id: &Option<Arc<str>>| id.as_ref().and_then(|id| state_events.get(id));
            StateAtEvent {
                history_visibility: get(vis),
                user_membership: get(member),
                join_rules: get(rules),
            }
        });
        let inputs = VisibilityInputs {
            user_id,
            now_ms,
            msc4354_enabled: context.msc4354_enabled,
            filter_send_to_client: true,
            is_peeking: false,
            always_include_ids,
            sender_ignored: context.ignored_users.contains(event.sender()),
            retention_max_lifetime: retention[event.room_id()],
            sender_erased: erased.get(event.sender()).copied().unwrap_or(false),
        };
        if let Some(event) = check_event_for_client(&inputs, event, state.as_ref())? {
            filtered.push(event);
        }
    }
    Ok(filtered)
}
