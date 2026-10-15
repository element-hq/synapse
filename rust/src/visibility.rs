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

//! Whether a user may see an event: the per-event checks of
//! `synapse.visibility.filter_and_transform_events_for_client`.
//!
//! Everything here is a function of the event, the state at it and a few
//! facts about the user and room. Loading those (the state at each event,
//! the ignored users, who is erased, the retention policy) is the caller's
//! job, so that this can serve the sync handler and any later port alike.
//! [`check_event_for_client`] is the whole per-event decision; the
//! functions below it are the steps, named as the Python ones are.
//!
//! [`filter_events_for_server`] is the server half,
//! `synapse.visibility.filter_events_for_server`. It loads what it needs
//! itself, through a [`SyncReader`], and leaves the per-event decision to
//! [`event_visible_to_server`].

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use pyo3::{
    prelude::*,
    types::{PyDict, PyModule, PyModuleMethods},
};

use crate::events::{
    constants::{
        event_type::{M_ROOM_ALIASES, M_ROOM_HISTORY_VISIBILITY, M_ROOM_JOIN_RULES, M_ROOM_MEMBER},
        history_visibility_field::HISTORY_VISIBILITY,
        join_rules_field::JOIN_RULE,
        membership_field::MEMBERSHIP,
    },
    filter::event_visible_to_server,
    redact_event_py, Event,
};
use crate::state::StateKey;
use crate::storage::{events::RedactBehaviour, sync_store::SyncReader};

/// `EventTypes.Dummy`.
const M_DUMMY: &str = "org.matrix.dummy_event";
/// `EventTypes.CallInvite`.
const M_CALL_INVITE: &str = "m.call.invite";

/// History visibilities from the least to the most restrictive.
pub const VISIBILITY_PRIORITY: [&str; 4] = ["world_readable", "shared", "invited", "joined"];

/// Memberships from the "most joined" down.
pub const MEMBERSHIP_PRIORITY: [&str; 5] = ["join", "invite", "knock", "leave", "ban"];

const SHARED: &str = "shared";
const WORLD_READABLE: &str = "world_readable";
const INVITED: &str = "invited";
const JOINED: &str = "joined";
const JOIN: &str = "join";
const INVITE: &str = "invite";
const LEAVE: &str = "leave";
const PUBLIC: &str = "public";

/// The state at an event that the checks read: the room's history
/// visibility, the user's own membership, and the join rules (only read
/// for call invites).
#[derive(Clone, Copy, Default)]
pub struct StateAtEvent<'a> {
    pub history_visibility: Option<&'a Event>,
    pub user_membership: Option<&'a Event>,
    pub join_rules: Option<&'a Event>,
}

/// What `filter_and_transform_events_for_client` knows about the user and
/// room when it checks an event.
#[derive(Debug, Clone)]
pub struct VisibilityInputs<'a> {
    pub user_id: &'a str,
    /// The time now, for sticky events and retention.
    pub now_ms: i64,
    pub msc4354_enabled: bool,
    /// Whether the event is going to a client, as opposed to being checked
    /// for what state the user may see. Some checks only apply to the
    /// former.
    pub filter_send_to_client: bool,
    /// The user is not a member of the room and has not been since the
    /// event.
    pub is_peeking: bool,
    /// Events to include if present, unless their sender is ignored.
    pub always_include_ids: &'a HashSet<Arc<str>>,
    pub sender_ignored: bool,
    /// The room's retention policy's `max_lifetime`, if it has one.
    pub retention_max_lifetime: Option<i64>,
    pub sender_erased: bool,
}

/// An event the user may see, with their membership at it, for MSC4115.
pub struct FilteredEvent {
    /// The event, or a pruned copy of it when the user may only see that.
    pub event: Event,
    pub membership: Arc<str>,
}

/// The `membership` of a membership event's content.
pub(crate) fn membership_of(event: &Event) -> Option<&str> {
    event
        .content()
        .get_field(MEMBERSHIP)
        .and_then(|m| m.as_str())
}

fn priority(list: &[&str], value: &str) -> Option<usize> {
    list.iter().position(|v| *v == value)
}

/// `value` as the entry of `list` it equals, so that it outlives the
/// borrow it came from, or `fallback` if it is not in the list.
fn known<'l>(list: &[&'l str], value: Option<&str>, fallback: &'l str) -> &'l str {
    value
        .and_then(|value| list.iter().copied().find(|v| *v == value))
        .unwrap_or(fallback)
}

/// The `allowed` closure of `filter_and_transform_events_for_client`:
/// whether the user may see `event`, what form of it, and their membership
/// at it. `state` is `None` for an outlier.
///
/// Fails only if pruning the event does, which a persisted event cannot.
pub fn check_event_for_client(
    inputs: &VisibilityInputs<'_>,
    event: &Event,
    state: Option<&StateAtEvent<'_>>,
) -> PyResult<Option<FilteredEvent>> {
    let Some(filtered) = check_client_allowed_to_see_event(inputs, event, state)? else {
        return Ok(None);
    };

    let is_own_membership =
        event.type_() == M_ROOM_MEMBER && event.state_key() == Some(inputs.user_id);

    // Call invites in public rooms would ring a lot of users.
    if event.type_() == M_CALL_INVITE && !event.is_state() {
        // The state is only absent for an outlier, and the only outliers
        // that get this far are the user's own memberships.
        let state = state.expect("a call invite is not an outlier");
        let join_rule = state
            .join_rules
            .and_then(|rules| rules.content().get_field(JOIN_RULE))
            .and_then(|rule| rule.as_str());
        if join_rule == Some(PUBLIC) {
            return Ok(None);
        }
    }

    // The user's membership after the event. An outlier has no state, but
    // the only outliers returned are the user's own membership events, so
    // the event itself says.
    let membership_event = if is_own_membership {
        Some(event)
    } else {
        state
            .expect("an event that is not the user's own membership has state")
            .user_membership
    };
    let membership = membership_event.and_then(membership_of).unwrap_or(LEAVE);

    Ok(Some(FilteredEvent {
        event: filtered,
        membership: Arc::from(membership),
    }))
}

/// `_check_client_allowed_to_see_event`: `None` if the user cannot see the
/// event at all, a pruned copy if they may only see that, or the event as
/// it is.
pub fn check_client_allowed_to_see_event(
    inputs: &VisibilityInputs<'_>,
    event: &Event,
    state: Option<&StateAtEvent<'_>>,
) -> PyResult<Option<Event>> {
    if inputs.filter_send_to_client
        && !check_filter_send_to_client(
            event,
            inputs.now_ms,
            inputs.retention_max_lifetime,
            inputs.sender_ignored,
        )
    {
        return Ok(None);
    }

    if inputs.always_include_ids.contains(event.event_id()) {
        return Ok(Some(event.shallow_clone()));
    }

    // Outliers have no room state. Normally clients can't see them, but
    // out-of-band membership events (incoming invites, rejections of them)
    // are shown to the user they are about.
    if event.internal_metadata().is_outlier()? {
        if event.type_() == M_ROOM_MEMBER && event.state_key() == Some(inputs.user_id) {
            return Ok(Some(event.shallow_clone()));
        }
        return Ok(None);
    }

    let state = state.expect("state for a non-outlier event");

    if inputs.msc4354_enabled && !inputs.is_peeking {
        // Sticky events are exempt from the membership part of the history
        // visibility rules while they are sticky: any joined user may see
        // them, as for state events (MSC4354).
        if let Some(sticky_until) = event.locally_sticky_until_ts()? {
            if inputs.now_ms < sticky_until {
                return Ok(Some(event.shallow_clone()));
            }
        }
    }

    let visibility = get_effective_room_visibility(state.history_visibility);

    // Lax history visibility means no membership check, unless the sender
    // was erased: then whether the user was joined decides the form.
    if !inputs.sender_erased && check_history_visibility(event, visibility, inputs.is_peeking) {
        return Ok(Some(event.shallow_clone()));
    }

    let (allowed, joined) = check_membership(
        inputs.user_id,
        event,
        visibility,
        state.user_membership,
        inputs.is_peeking,
    );
    if !allowed {
        return Ok(None);
    }

    // An erased sender's events are only shown in full to users who were
    // joined at the time.
    if inputs.sender_erased && !joined {
        return Ok(Some(redact_event_py(event)?));
    }

    Ok(Some(event.shallow_clone()))
}

/// `_check_membership`: `(allowed, joined)`, whether the user's membership
/// at the event lets them see it, and whether they were joined.
pub fn check_membership(
    user_id: &str,
    event: &Event,
    visibility: &str,
    membership_event: Option<&Event>,
    is_peeking: bool,
) -> (bool, bool) {
    let mut membership: Option<&str> = None;

    // The user's own membership event is judged by its "most joined" side.
    if event.type_() == M_ROOM_MEMBER && event.state_key() == Some(user_id) {
        let new = known(&MEMBERSHIP_PRIORITY, membership_of(event), LEAVE);
        let prev_content = event.unsigned_ref().prev_content().ok().flatten();
        let prev = known(
            &MEMBERSHIP_PRIORITY,
            prev_content
                .as_ref()
                .and_then(|content| content.get(MEMBERSHIP))
                .and_then(|m| m.as_str()),
            LEAVE,
        );

        // Always let the user see their own leave, so that they see the
        // room go when they reject an invite. (Out-of-band rejections have
        // no prev content; they are outliers and handled before this.)
        if new == LEAVE && (prev == JOIN || prev == INVITE) {
            return (true, false);
        }

        let new_priority = priority(&MEMBERSHIP_PRIORITY, new).expect("checked above");
        let old_priority = priority(&MEMBERSHIP_PRIORITY, prev).expect("checked above");
        membership = Some(if old_priority < new_priority {
            prev
        } else {
            new
        });
    }

    // Otherwise, their membership at the time of the event.
    if membership.is_none() {
        membership = membership_event.and_then(membership_of);
    }

    if membership == Some(JOIN) {
        return (true, true);
    }

    match visibility {
        JOINED => (false, false),
        INVITED => (membership == Some(INVITE), false),
        SHARED if is_peeking => (false, false),
        // Shared or world readable, and the user was not a member.
        _ => (true, false),
    }
}

/// `_check_filter_send_to_client`: `false` if the event is definitely not
/// to be sent to a client: a dummy event, a non-state event from an ignored
/// sender, an aliases event, or a non-state event past the room's
/// retention.
pub fn check_filter_send_to_client(
    event: &Event,
    now_ms: i64,
    retention_max_lifetime: Option<i64>,
    sender_ignored: bool,
) -> bool {
    if event.type_() == M_DUMMY {
        return false;
    }
    if !event.is_state() && sender_ignored {
        return false;
    }
    // Until MSC2261 lands, alias events cannot be redacted when malicious,
    // so they are filtered out entirely.
    if event.type_() == M_ROOM_ALIASES {
        return false;
    }
    // Retention only applies to non-state events (MSC1763).
    if !event.is_state() {
        if let Some(max_lifetime) = retention_max_lifetime {
            if event.origin_server_ts() < now_ms - max_lifetime {
                return false;
            }
        }
    }
    true
}

/// `_check_history_visibility`: `true` if the user can definitely see the
/// event by the room's history visibility alone.
///
/// A history visibility event is judged by the laxer of the old and new
/// settings, so that the change itself is visible on both sides.
pub fn check_history_visibility(event: &Event, visibility: &str, is_peeking: bool) -> bool {
    let mut visibility = visibility;
    if event.type_() == M_ROOM_HISTORY_VISIBILITY {
        let prev_content = event.unsigned_ref().prev_content().ok().flatten();
        let prev = known(
            &VISIBILITY_PRIORITY,
            prev_content
                .as_ref()
                .and_then(|content| content.get(HISTORY_VISIBILITY))
                .and_then(|v| v.as_str()),
            SHARED,
        );
        let new_priority =
            priority(&VISIBILITY_PRIORITY, visibility).expect("effective visibility");
        let old_priority = priority(&VISIBILITY_PRIORITY, prev).expect("checked above");
        if old_priority < new_priority {
            visibility = prev;
        }
    }

    (visibility == SHARED && !is_peeking) || visibility == WORLD_READABLE
}

/// `get_effective_room_visibility_from_state`: the room's history
/// visibility from its event, `shared` when there is none or it is invalid.
pub fn get_effective_room_visibility(history_visibility: Option<&Event>) -> &'static str {
    let visibility = history_visibility
        .and_then(|event| event.content().get_field(HISTORY_VISIBILITY))
        .and_then(|v| v.as_str())
        .unwrap_or(SHARED);
    VISIBILITY_PRIORITY
        .iter()
        .copied()
        .find(|v| *v == visibility)
        .unwrap_or(SHARED)
}

/// What a server admin asked to see of soft-failed events, from
/// `AdminClientConfig`.
#[derive(Debug, Clone, Copy, Default)]
pub struct SoftFailedPolicy {
    pub return_soft_failed_events: bool,
    pub return_policy_server_spammy_events: bool,
}

/// Whether a soft-failed event is kept before the visibility checks: never,
/// unless the user is a server admin who asked for soft-failed events, or
/// for the ones a policy server marked as spam. `policy` is `None` for a
/// user who is not an admin, or when the events are not going to a client.
pub fn keep_soft_failed(event: &Event, policy: Option<&SoftFailedPolicy>) -> bool {
    if !event.internal_metadata().soft_failed().unwrap_or(false) {
        return true;
    }
    match policy {
        Some(policy) if policy.return_soft_failed_events => true,
        Some(policy) if policy.return_policy_server_spammy_events => event
            .internal_metadata()
            .policy_server_spammy()
            .unwrap_or(false),
        _ => false,
    }
}

// The server half.

/// What [`filter_events_for_server`] is asked to do: its arguments, less
/// the events.
#[derive(Debug, Clone, Copy)]
pub struct ServerFilter<'a> {
    pub target_server_name: &'a str,
    pub local_server_name: &'a str,
    /// Include the redacted form of an event the server may not see, rather
    /// than leave it out.
    pub redact: bool,
    /// Also leave out the events of senders who asked to be erased.
    pub filter_out_erased_senders: bool,
    /// Also leave out remote events in rooms we only have partial state
    /// for.
    pub filter_out_remote_partial_state_events: bool,
}

/// What [`filter_events_for_server`] found.
pub enum ServerFiltered {
    Events(Vec<Event>),
    /// An event needs the memberships at it and has only partial state.
    /// Python waits for the full state there. We do not, and leave it to
    /// the caller.
    NeedsFullState,
}

/// The server name of a user ID, as `get_domain_from_id`.
fn domain_of(user_id: &str) -> anyhow::Result<&str> {
    user_id
        .split_once(':')
        .map(|(_, domain)| domain)
        .ok_or_else(|| anyhow::anyhow!("Invalid ID: {user_id}"))
}

/// `filter_events_for_server`: the events the target server may see, in
/// order. Each event it may not see is left out, or redacted if
/// `filter.redact`.
pub async fn filter_events_for_server(
    reader: &SyncReader<'_>,
    filter: &ServerFilter<'_>,
    events: Vec<Event>,
) -> anyhow::Result<ServerFiltered> {
    let erased = if filter.filter_out_erased_senders {
        let mut senders: Vec<Arc<str>> = events.iter().map(|e| Arc::from(e.sender())).collect();
        senders.sort();
        senders.dedup();
        reader.are_users_erased(&senders).await?
    } else {
        HashMap::new()
    };

    // Python checks this first, so that a room fully joined while we look
    // cannot let its events through on an old view of the memberships.
    let mut partial_state_invisible: HashSet<&str> = HashSet::new();
    if filter.filter_out_remote_partial_state_events {
        for event in &events {
            if domain_of(event.sender())? != filter.local_server_name
                && reader.is_partial_state_room(event.room_id()).await?
            {
                partial_state_invisible.insert(event.event_id());
            }
        }
    }

    // `_event_to_history_vis`. An outlier has no state, and counts as
    // shared.
    let mut with_state: Vec<Arc<str>> = Vec::with_capacity(events.len());
    for event in &events {
        if !event.internal_metadata().is_outlier()? {
            with_state.push(Arc::from(event.event_id()));
        }
    }
    let event_to_group = reader.state_group_for_events(&with_state).await?;
    if let Some(missing) = with_state.iter().find(|id| !event_to_group.contains_key(*id)) {
        anyhow::bail!("No state group for event {missing}");
    }
    let mut groups: Vec<i64> = event_to_group.values().copied().collect();
    groups.sort_unstable();
    groups.dedup();
    let group_state = reader.state_for_groups(&groups).await?;
    let state_of = |event_id: &str| {
        event_to_group
            .get(event_id)
            .and_then(|group| group_state.get(group))
    };

    let vis_key = StateKey::new(M_ROOM_HISTORY_VISIBILITY, "");
    let mut vis_ids: Vec<Arc<str>> = group_state
        .values()
        .filter_map(|state| state.get(&vis_key).cloned())
        .collect();
    vis_ids.sort();
    vis_ids.dedup();
    let vis_events = reader
        .events(&vis_ids, RedactBehaviour::Redact, false, false)
        .await?;
    let vis_events: HashMap<&str, &Event> = vis_events
        .iter()
        .map(|event| (event.event_id(), event))
        .collect();

    let mut history_visibility: HashMap<&str, &str> = HashMap::with_capacity(events.len());
    for event in &events {
        let mut vis = SHARED;
        if let Some(vis_id) = state_of(event.event_id()).and_then(|state| state.get(&vis_key)) {
            let vis_event = vis_events
                .get(&**vis_id)
                .ok_or_else(|| anyhow::anyhow!("Missing history visibility event {vis_id}"))?;
            if let Some(value) = vis_event.content().get_field(HISTORY_VISIBILITY) {
                vis = value.as_str().ok_or_else(|| {
                    anyhow::anyhow!("Invalid history visibility in {vis_id}")
                })?;
            }
        }
        history_visibility.insert(event.event_id(), vis);
    }

    // `_event_to_memberships`, for the events with restricted visibility:
    // the memberships at each of the target server's users.
    let restricted: Vec<Arc<str>> = events
        .iter()
        .filter(|event| {
            !matches!(
                history_visibility[event.event_id()],
                SHARED | WORLD_READABLE
            )
        })
        .map(|event| Arc::from(event.event_id()))
        .collect();
    let mut member_events: HashMap<Arc<str>, Event> = HashMap::new();
    if !restricted.is_empty() {
        // Python waits for the full state of these.
        if !reader.partial_state_events(&restricted).await?.is_empty() {
            return Ok(ServerFiltered::NeedsFullState);
        }
        let mut member_ids: Vec<Arc<str>> = restricted
            .iter()
            .filter_map(|id| state_of(id))
            .flat_map(|state| state.iter())
            .filter(|(key, _)| is_target_member(key, filter.target_server_name))
            .map(|(_, event_id)| Arc::clone(event_id))
            .collect();
        member_ids.sort();
        member_ids.dedup();
        for event in reader
            .events(&member_ids, RedactBehaviour::Redact, false, false)
            .await?
        {
            member_events.insert(Arc::from(event.event_id()), event);
        }
    }

    let mut visible = Vec::with_capacity(events.len());
    for event in &events {
        let event_id = event.event_id();
        let mut memberships: Vec<(&str, &str)> = Vec::new();
        if restricted.iter().any(|id| &**id == event_id) {
            for (key, member_id) in state_of(event_id).into_iter().flat_map(|state| state.iter()) {
                if !is_target_member(key, filter.target_server_name) {
                    continue;
                }
                let Some(member) = member_events.get(member_id) else {
                    continue;
                };
                let membership = membership_of(member).ok_or_else(|| {
                    anyhow::anyhow!("Membership event {member_id} has no membership")
                })?;
                memberships.push((&key.1, membership));
            }
        }
        let sender_erased = erased.get(event.sender()).copied().unwrap_or(false);
        visible.push(event_visible_to_server(
            filter.target_server_name,
            history_visibility[event_id],
            sender_erased,
            partial_state_invisible.contains(event_id),
            memberships,
        )?);
    }

    let mut filtered = Vec::with_capacity(events.len());
    for (event, visible) in events.into_iter().zip(visible) {
        if visible {
            filtered.push(event);
        } else if filter.redact {
            filtered.push(redact_event_py(&event).map_err(|err| anyhow::anyhow!("{err}"))?);
        }
    }
    Ok(ServerFiltered::Events(filtered))
}

/// Whether `key` is the membership of one of `server_name`'s users.
fn is_target_member(key: &StateKey, server_name: &str) -> bool {
    &*key.0 == M_ROOM_MEMBER
        && key
            .1
            .split_once(':')
            .is_some_and(|(_, domain)| domain == server_name)
}

// The entry point for the differential test.

/// [`check_event_for_client`] through the Rust implementation. `state` maps
/// `(type, state_key)` to events, as the Python state map does, or is
/// `None` for an outlier. Returns `(event, membership)` or `None`.
#[pyfunction]
#[pyo3(signature = (user_id, event, state, *, now_ms, msc4354_enabled, filter_send_to_client, is_peeking, always_include_ids, sender_ignored, retention_max_lifetime, sender_erased))]
#[allow(clippy::too_many_arguments)]
fn check_event_for_client_for_test(
    user_id: &str,
    event: &Event,
    state: Option<&Bound<'_, PyDict>>,
    now_ms: i64,
    msc4354_enabled: bool,
    filter_send_to_client: bool,
    is_peeking: bool,
    always_include_ids: HashSet<String>,
    sender_ignored: bool,
    retention_max_lifetime: Option<i64>,
    sender_erased: bool,
) -> PyResult<Option<(Event, String)>> {
    let always_include_ids: HashSet<Arc<str>> =
        always_include_ids.into_iter().map(Arc::from).collect();
    let inputs = VisibilityInputs {
        user_id,
        now_ms,
        msc4354_enabled,
        filter_send_to_client,
        is_peeking,
        always_include_ids: &always_include_ids,
        sender_ignored,
        retention_max_lifetime,
        sender_erased,
    };

    let lookup = |dict: &Bound<'_, PyDict>, key: (&str, &str)| -> PyResult<Option<Event>> {
        match dict.get_item(key)? {
            Some(event) => Ok(Some(event.cast::<Event>()?.get().shallow_clone())),
            None => Ok(None),
        }
    };
    let events = match state {
        Some(dict) => Some((
            lookup(dict, (M_ROOM_HISTORY_VISIBILITY, ""))?,
            lookup(dict, (M_ROOM_MEMBER, user_id))?,
            lookup(dict, (M_ROOM_JOIN_RULES, ""))?,
        )),
        None => None,
    };
    let state = events.as_ref().map(|(vis, member, rules)| StateAtEvent {
        history_visibility: vis.as_ref(),
        user_membership: member.as_ref(),
        join_rules: rules.as_ref(),
    });

    Ok(check_event_for_client(&inputs, event, state.as_ref())?
        .map(|filtered| (filtered.event, filtered.membership.to_string())))
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "visibility")?;
    child_module.add_function(wrap_pyfunction!(
        check_event_for_client_for_test,
        &child_module
    )?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import visibility` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.visibility", child_module)?;

    Ok(())
}
