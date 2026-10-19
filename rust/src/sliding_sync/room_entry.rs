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

//! `get_room_sync_data`: one room's entry in a sliding sync response, in
//! the order of the Python, and the batch of rooms Python hands over in
//! one call.
//!
//! The pure decisions are in [`crate::sliding_sync::room`] and
//! [`crate::sliding_sync::required_state`]. This module does the reads
//! between them.

use std::collections::{HashMap, HashSet};
use std::panic::AssertUnwindSafe;
use std::sync::Arc;

use anyhow::{bail, Context};
use futures::{stream, FutureExt, StreamExt};
use indexmap::IndexMap;
use serde_json::{Map, Value};
use tracing::Instrument;

use crate::events::Event;
use crate::logging::opentracing::record_error;
use crate::sliding_sync::config::{RoomSyncConfig, MEMBER};
use crate::sliding_sync::connection::{HaveSentRoom, RoomLazyMembershipChanges};
use crate::sliding_sync::request::SlidingSyncRequestData;
use crate::sliding_sync::required_state::{
    required_state_changes, required_state_request, state_filter_with_meta, TimelineMember,
};
use crate::sliding_sync::room::{
    always_return_bump_stamp, bump_stamp_from_last_event, final_bump_stamp, needs_heroes, num_live,
    room_config_to_persist, store_bump_stamp, strip_event, stripped_state, timeline_bounds,
    timeline_bump_stamp, StoreBumpStamp, StrippedHero, TimelineBounds, BUMP_EVENT_TYPES,
};
use crate::sliding_sync::room_lists::RoomForUser;
use crate::state::{lookup, StateKey, StateMap};
use crate::storage::events::RedactBehaviour;
use crate::storage::sliding_sync_store::SlidingSyncReader;
use crate::storage::sync_store::{PaginationOrder, RoomSummary};
use crate::sync::client_events::{filter_events_for_client, ClientFilterContext};
use crate::sync::relations::{bundled_aggregations, Bundle};
use crate::sync::room::Fallback;
use crate::sync::rooms::panic_message;
use crate::sync::summary::extract_heroes_from_room_summary;
use crate::types::state_filter::StateFilter;
use crate::types::stream_token::{Direction, RoomStreamToken};
use crate::visibility::FilteredEvent;

const M_ROOM_NAME: &str = "m.room.name";
const M_ROOM_AVATAR: &str = "m.room.avatar";

/// How many rooms are computed at once, as `concurrently_execute` does.
const ROOM_ENTRY_CONCURRENCY: usize = 20;

/// What `current_sync_for_user` passes `get_room_sync_data` for one room,
/// besides the request.
#[derive(Debug, Clone)]
pub struct RoomInputData {
    /// The user's membership in the room at the `to_token`.
    pub membership: RoomForUser,
    pub room_sync_config: RoomSyncConfig,
    /// What the connection last recorded for the room.
    pub prev_room_sync_config: Option<RoomSyncConfig>,
    /// Whether the connection sent the room before.
    pub room_status: HaveSentRoom<RoomStreamToken>,
    pub newly_joined: bool,
    pub newly_left: bool,
    pub is_dm: bool,
}

/// `SlidingSyncResult.RoomResult`, holding the events Rust loaded.
pub struct RoomEntry {
    pub room_id: Arc<str>,
    /// May be empty, which counts as an update but is not sent.
    pub name: Option<String>,
    pub avatar: Option<String>,
    pub heroes: Vec<StrippedHero>,
    pub is_dm: bool,
    pub initial: bool,
    pub unstable_expanded_timeline: bool,
    pub required_state: Vec<Event>,
    pub timeline: Vec<FilteredEvent>,
    /// `None` unless the timeline is limited.
    pub bundles: Option<HashMap<Arc<str>, Bundle>>,
    /// The redactions of the redacted events among the room's, which the
    /// serializer adds to their unsigned data.
    pub redactions: HashMap<Arc<str>, Event>,
    pub stripped_state: Vec<Value>,
    /// The `prev_batch` token as a string.
    pub prev_batch: Option<String>,
    pub limited: Option<bool>,
    pub num_live: Option<i64>,
    pub bump_stamp: Option<i64>,
    pub joined_count: Option<i64>,
    pub invited_count: Option<i64>,
}

impl RoomEntry {
    /// `RoomResult.__bool__`: whether the entry holds an update to return
    /// at once.
    pub fn has_updates(&self) -> bool {
        self.initial
            || self.name.is_some()
            || self.avatar.is_some()
            || !self.heroes.is_empty()
            || self.joined_count.is_some()
            || self.invited_count.is_some()
            || !self.required_state.is_empty()
            || !self.timeline.is_empty()
            || !self.stripped_state.is_empty()
    }
}

/// What `get_room_sync_data` writes to the new connection state for the
/// room.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RoomMutations {
    /// `room_configs[room_id]`, if it changes.
    pub room_config: Option<RoomSyncConfig>,
    /// `room_lazy_membership[room_id]`, always set.
    pub lazy_membership: RoomLazyMembershipChanges,
}

/// One room computed.
pub struct RoomComputed {
    pub entry: RoomEntry,
    pub mutations: RoomMutations,
    /// Whether the room's lazy-loaded members were looked up in the
    /// connection state.
    pub lazy_member_lookup: bool,
}

/// How one room came out.
pub enum RoomOutcome {
    Computed(Box<RoomComputed>),
    /// Python must compute the room: [`Fallback`].
    Fallback(String),
    /// Computing the room failed.
    Error(String),
}

/// What computing one room reads, shared by the rooms of a request.
pub struct RoomContext<'r, 'a> {
    pub reader: &'r SlidingSyncReader<'a>,
    pub request: &'r SlidingSyncRequestData,
    pub client_filter: ClientFilterContext,
    /// `get_id_for_instance` for the writers the rooms' tokens may name.
    pub instance_ids: &'r HashMap<String, i64>,
    /// The time the visibility checks compare with.
    pub now_ms: i64,
}

/// One room's view of the request: the room, and where the user's view of
/// it ends.
struct Room<'r> {
    input: &'r RoomInputData,
    room_id: &'r str,
    membership: &'r str,
    /// Where a leave or ban stops what the user sees: the membership's
    /// position, as `event_pos.to_room_stream_token()`.
    membership_token: RoomStreamToken,
}

impl Room<'_> {
    fn has_left(&self) -> bool {
        matches!(self.membership, "leave" | "ban")
    }
}

/// The events of `event_ids`, by state key. Events that are not found or
/// rejected are left out.
///
/// An event of the room's timeline is that event, with the previous content
/// the timeline's load gave it. Python's loads find the same objects through
/// its map of the events still held, whatever the event cache holds.
async fn state_events(
    reader: &SlidingSyncReader<'_>,
    timeline: &HashMap<&str, &Arc<Event>>,
    event_ids: &[Arc<str>],
) -> anyhow::Result<HashMap<StateKey, Event>> {
    let (held, to_load): (Vec<&Arc<str>>, Vec<&Arc<str>>) = event_ids
        .iter()
        .partition(|event_id| timeline.contains_key(&***event_id));
    let to_load: Vec<Arc<str>> = to_load.into_iter().cloned().collect();
    let loaded = reader
        .events(&to_load, RedactBehaviour::Redact, false, false)
        .await?;
    let held = held
        .into_iter()
        .map(|event_id| timeline[&**event_id].shallow_clone());
    Ok(loaded
        .into_iter()
        .chain(held)
        .filter_map(|event| {
            let key = StateKey::new(event.type_(), event.state_key()?);
            Some((key, event))
        })
        .collect())
}

/// `get_event`: the event, which must exist.
async fn one_event(reader: &SlidingSyncReader<'_>, event_id: &Arc<str>) -> anyhow::Result<Event> {
    reader
        .events(
            std::slice::from_ref(event_id),
            RedactBehaviour::Redact,
            false,
            false,
        )
        .await?
        .into_iter()
        .next()
        .with_context(|| format!("Could not find event {event_id}"))
}

/// `get_current_state_ids_at`: the room's state as the user sees it, at
/// their leave or ban, or the current state.
async fn state_ids_at(
    context: &RoomContext<'_, '_>,
    room: &Room<'_>,
    state_filter: &StateFilter,
) -> anyhow::Result<StateMap> {
    if room.has_left() {
        context
            .reader
            .state_ids_at(room.room_id, &room.membership_token, state_filter)
            .await
    } else {
        context
            .reader
            .current_state_ids(room.room_id, state_filter)
            .await
    }
}

/// `get_current_state_at`: as [`state_ids_at`], with the events.
async fn state_at(
    context: &RoomContext<'_, '_>,
    room: &Room<'_>,
    timeline: &HashMap<&str, &Arc<Event>>,
    state_filter: &StateFilter,
) -> anyhow::Result<HashMap<StateKey, Event>> {
    let state_ids = state_ids_at(context, room, state_filter).await?;
    let event_ids: Vec<Arc<str>> = state_ids.into_values().collect();
    state_events(context.reader, timeline, &event_ids).await
}

/// `filter` with the room name added, as the first send reads it.
fn with_room_name(filter: StateFilter) -> StateFilter {
    if filter.matches(M_ROOM_NAME, "") {
        return filter;
    }
    let mut types = filter.types().clone();
    types
        .entry(Arc::from(M_ROOM_NAME))
        .or_insert_with(|| Some(HashSet::new()))
        .get_or_insert_with(HashSet::new)
        .insert(Arc::from(""));
    StateFilter::new(types, filter.include_others())
}

/// `_stripped_state` of an invite or knock event.
fn stripped_state_of(event: &Event) -> anyhow::Result<Vec<Value>> {
    let mut unsigned = Map::new();
    let unsigned_ref = event.unsigned_ref();
    if let Some(state) = unsigned_ref.invite_room_state()? {
        unsigned.insert("invite_room_state".to_owned(), Value::Array(state));
    }
    if let Some(state) = unsigned_ref.knock_room_state()? {
        unsigned.insert("knock_room_state".to_owned(), Value::Array(state));
    }
    let content = Value::Object(
        event
            .content()
            .as_map()
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect(),
    );
    let membership = event
        .content()
        .get_field("membership")
        .and_then(Value::as_str);
    let stripped = strip_event(
        event.type_(),
        event.state_key().unwrap_or_default(),
        &content,
        event.sender(),
    );
    Ok(stripped_state(membership, &unsigned, stripped))
}

/// A string field of an event's content, as the Python's `isinstance(value,
/// str)` checks read it.
fn content_str(event: &Event, key: &str) -> Option<String> {
    event
        .content()
        .get_field(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
}

/// `_get_bump_stamp`.
async fn bump_stamp(
    context: &RoomContext<'_, '_>,
    room_id: &str,
    timeline: &[FilteredEvent],
    check_outside_timeline: bool,
) -> anyhow::Result<Option<i64>> {
    let in_timeline = timeline_bump_stamp(timeline.iter().filter_map(|filtered| {
        let stream = filtered.event.internal_metadata().stream_ordering()?;
        Some((filtered.event.type_(), stream.get()))
    }));
    if in_timeline.is_some() {
        return Ok(in_timeline);
    }
    if !check_outside_timeline {
        return Ok(None);
    }
    let request = context.request;
    let latest = context.reader.latest_bump_stamp(room_id).await?;
    match store_bump_stamp(
        latest,
        request.have_finished_bg_jobs,
        request.to_room_key.stream,
    ) {
        StoreBumpStamp::Done(stamp) => Ok(stamp),
        StoreBumpStamp::LookupLastEvent => {
            let types: Vec<String> = BUMP_EVENT_TYPES.iter().map(|t| t.to_string()).collect();
            let last = context
                .reader
                .last_event_of_types_before(room_id, &request.to_room_key, &types)
                .await?;
            Ok(bump_stamp_from_last_event(last.map(|(_, pos)| pos.stream)))
        }
    }
}

/// The redactions of the redacted events of `entry`.
async fn load_redactions(
    reader: &SlidingSyncReader<'_>,
    entry: &RoomEntry,
) -> anyhow::Result<HashMap<Arc<str>, Event>> {
    let mut redaction_ids: Vec<Arc<str>> = Vec::new();
    let bundled = entry
        .bundles
        .iter()
        .flat_map(|bundles| bundles.values().flat_map(Bundle::events));
    let events = entry
        .timeline
        .iter()
        .map(|filtered| &filtered.event)
        .chain(&entry.required_state)
        .chain(bundled);
    for event in events {
        if let Some(redacted_by) = event.internal_metadata().redacted_by()? {
            let redacted_by: Arc<str> = Arc::from(redacted_by);
            if !redaction_ids.contains(&redacted_by) {
                redaction_ids.push(redacted_by);
            }
        }
    }
    Ok(reader
        .events(&redaction_ids, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(|event| (Arc::from(event.event_id()), event))
        .collect())
}

/// `get_room_sync_data`.
pub async fn room_sync_data(
    context: &RoomContext<'_, '_>,
    input: &RoomInputData,
) -> anyhow::Result<RoomComputed> {
    let reader = context.reader;
    let request = context.request;
    let user_id = &*request.user_id;
    let member = &input.membership;
    let room = Room {
        input,
        room_id: &member.room_id,
        membership: &member.membership,
        membership_token: RoomStreamToken::live(member.event_pos.stream),
    };
    let room_id = room.room_id;
    let membership = room.membership;
    let config = &input.room_sync_config;
    let prev_config = input.prev_room_sync_config.as_ref();
    let from = request.from.as_ref();

    // Only a state reset out of the room leaves a membership with no event.
    let state_reset_out_of_room = member.event_id.is_none();
    if state_reset_out_of_room && membership != "leave" {
        bail!("{room_id}: membership {membership} with no event");
    }

    let TimelineBounds {
        from_bound,
        initial,
        ignore_timeline_bound,
    } = timeline_bounds(
        &room.input.room_status,
        from.map(|from| &from.room_key),
        input.newly_joined || state_reset_out_of_room,
        prev_config,
        config,
    );

    // The timeline. Invites and knocks only have stripped state.
    let mut timeline: Vec<FilteredEvent> = Vec::new();
    // The timeline's events as loaded, before visibility.
    let mut loaded_timeline: Vec<Arc<Event>> = Vec::new();
    let mut bundles = None;
    let mut limited = None;
    let mut prev_batch = None;
    let mut live = None;
    if config.timeline_limit > 0 && !matches!(membership, "invite" | "knock") {
        let to_bound = if room.has_left() {
            &room.membership_token
        } else {
            &request.to_room_key
        };
        let timeline_from_bound = if ignore_timeline_bound {
            None
        } else {
            from_bound.as_ref()
        };
        // A historical section of the timeline in topological order, as
        // `/messages` returns it, or the updates since the bound in the
        // order the server received them.
        let order = if timeline_from_bound.is_none() {
            PaginationOrder::Topological
        } else {
            PaginationOrder::Stream
        };
        let (mut events, new_room_key, page_limited) = reader
            .paginate_room_events(
                room_id,
                to_bound,
                timeline_from_bound,
                Direction::Backwards,
                config.timeline_limit,
                order,
            )
            .await?;
        events.reverse();
        loaded_timeline = events.into_iter().map(Arc::new).collect();
        timeline = filter_events_for_client(
            reader,
            &context.client_filter,
            &loaded_timeline,
            &HashSet::new(),
            membership != "join",
            context.now_ms,
        )
        .await?;

        let mut positions = Vec::new();
        if from.is_some() {
            for filtered in &timeline {
                let metadata = filtered.event.internal_metadata();
                let stream = metadata.stream_ordering().with_context(|| {
                    format!("event {} has no stream ordering", filtered.event.event_id())
                })?;
                let instance_name = metadata.instance_name().with_context(|| {
                    format!("event {} has no instance name", filtered.event.event_id())
                })?;
                positions.push((instance_name, stream.get()));
            }
        }
        live = Some(num_live(
            positions
                .iter()
                .map(|(instance_name, stream)| (instance_name.as_str(), *stream)),
            from.map(|from| &from.room_key),
        ));

        // With a limited timeline, the client lacks the events it would
        // need to aggregate relations itself.
        if page_limited {
            bundles = Some(
                bundled_aggregations(
                    reader,
                    &timeline,
                    user_id,
                    &context.client_filter.ignored_users,
                )
                .await?,
            );
        }
        limited = Some(page_limited);

        let room_key = new_room_key
            .to_string_with_instances(|name| {
                let id = context.instance_ids.get(name).copied();
                if id.is_none() {
                    // A writer added since the table was cached. The next
                    // request reads it again.
                    reader.forget_instance_map();
                }
                id
            })
            .map_err(|e| anyhow::anyhow!("{room_id}: prev_batch: {e}"))?;
        prev_batch = Some(format!("{room_key}_{}", request.to_suffix));
    }

    // Python holds no reference to a redacted event, which is loaded again.
    let held: HashMap<&str, &Arc<Event>> = loaded_timeline
        .iter()
        .filter(|event| matches!(event.internal_metadata().redacted_by(), Ok(None)))
        .map(|event| (event.event_id(), event))
        .collect();

    // Invites and knocks carry the stripped state the potential joiner
    // identifies the room by.
    let mut stripped = Vec::new();
    if matches!(membership, "invite" | "knock") {
        let event_id = member
            .event_id
            .as_ref()
            .with_context(|| format!("{room_id}: {membership} with no event"))?;
        stripped = stripped_state_of(&one_event(reader, event_id).await?)?;
    }

    // The changes to the current state in the token range, in the order of
    // Python's dict.
    let mut deltas: IndexMap<StateKey, Arc<str>> = IndexMap::new();
    let mut membership_changed = false;
    let mut name_changed = false;
    let mut avatar_changed = false;
    if !initial {
        let from_bound = from_bound
            .as_ref()
            .with_context(|| format!("{room_id}: incremental with no from bound"))?;

        // An out-of-band leave, such as a rejected remote invite, is not in
        // the state deltas: it is passed in as newly left.
        if input.newly_left {
            if let Some(event_id) = &member.event_id {
                membership_changed = true;
                let leave = one_event(reader, event_id).await?;
                if let Some(state_key) = leave.state_key() {
                    deltas.insert(
                        StateKey::new(leave.type_(), state_key),
                        Arc::clone(event_id),
                    );
                }
            }
        }

        let to_bound = match membership {
            "leave" | "ban" => Some(&room.membership_token),
            "join" => Some(&request.to_room_key),
            "invite" | "knock" => None,
            other => bail!("Unexpected membership {other} that we don't know how to handle yet"),
        };
        let state_deltas = match to_bound {
            Some(to_bound) => {
                reader
                    .state_deltas_by_stream_id(room_id, Some(from_bound), Some(to_bound))
                    .await?
            }
            None => Vec::new(),
        };
        for delta in state_deltas {
            // A state reset leaves a delta with no event, which is not
            // handled yet.
            if let Some(event_id) = &delta.event_id {
                deltas.insert(
                    StateKey(Arc::clone(&delta.event_type), Arc::clone(&delta.state_key)),
                    Arc::clone(event_id),
                );
            }
            if &*delta.event_type == MEMBER {
                membership_changed = true;
            } else if &*delta.event_type == M_ROOM_NAME && delta.state_key.is_empty() {
                name_changed = true;
            } else if &*delta.event_type == M_ROOM_AVATAR && delta.state_key.is_empty() {
                avatar_changed = true;
            }
        }
    }

    // The state the request asks for, from the timeline and the deltas
    // before any are added below.
    let delta_keys: Vec<(String, String)> = deltas
        .keys()
        .map(|key| (key.0.to_string(), key.1.to_string()))
        .collect();
    let timeline_members: Vec<TimelineMember<'_>> = timeline
        .iter()
        .map(|filtered| TimelineMember {
            sender: filtered.event.sender(),
            event_type: filtered.event.type_(),
            state_key: filtered.event.state_key(),
        })
        .collect();
    let state_request = required_state_request(
        user_id,
        membership,
        &config.required_state_map,
        &timeline_members,
        limited,
        initial,
        delta_keys.iter().map(|(t, k)| (t.as_str(), k.as_str())),
    );
    drop(timeline_members);
    let required_state_filter = &state_request.required_state_filter;
    let lazy_load_user_ids = &state_request.lazy_load_user_ids;

    // On the first send, the name and the required state come from one read
    // of the room's state, split in memory below. Python reads the name and
    // then the required state, each from its cache of the room's state.
    let initial_filter = with_room_name(state_filter_with_meta(required_state_filter, &[], true));
    let initial_state_ids = if initial {
        Some(state_ids_at(context, &room, &initial_filter).await?)
    } else {
        None
    };

    let mut room_name = None;
    if initial || name_changed {
        let name_filter = StateFilter::from_types([(M_ROOM_NAME, Some(""))]);
        let name_state = match &initial_state_ids {
            Some(state_ids) => {
                let ids: Vec<Arc<str>> =
                    name_filter.filter_state(state_ids).into_values().collect();
                state_events(reader, &held, &ids).await?
            }
            None => state_at(context, &room, &held, &name_filter).await?,
        };
        room_name =
            lookup(&name_state, (M_ROOM_NAME, "")).and_then(|event| content_str(event, "name"));
    }

    // Heroes, when the room has no name, on the first send or when the
    // membership changed. The summary also gives the member counts.
    let mut summary: Option<Arc<RoomSummary>> = None;
    let mut hero_user_ids: Vec<Arc<str>> = Vec::new();
    if needs_heroes(room_name.as_deref(), initial, membership_changed) {
        // The summary of a room left, invited to or knocked on is not known.
        let details = if membership == "join" {
            reader.room_summary(room_id).await?
        } else {
            Arc::new(RoomSummary::new())
        };
        hero_user_ids = extract_heroes_from_room_summary(&details, user_id);
        summary = Some(details);
    }

    let mut joined_count = None;
    let mut invited_count = None;
    if (initial || membership_changed) && membership == "join" {
        let counts = match &summary {
            Some(details) => {
                let count = |m: &str| details.get(m).map_or(0, |summary| summary.count);
                (count("join"), count("invite"))
            }
            None => {
                let counts = reader.cached_member_counts(room_id).await?;
                let count = |m: &str| counts.get(m).copied().unwrap_or(0);
                (count("join"), count("invite"))
            }
        };
        joined_count = Some(counts.0);
        invited_count = Some(counts.1);
    }

    let state_filter = state_filter_with_meta(
        required_state_filter,
        &hero_user_ids,
        initial || avatar_changed,
    );

    // Every lazy-loaded member counts as sent first in this request, until
    // the connection says otherwise.
    let mut returned: HashMap<String, Option<i64>> = if state_request.lazy_load_room_members {
        lazy_load_user_ids
            .iter()
            .map(|user_id| (user_id.clone(), None))
            .collect()
    } else {
        HashMap::new()
    };
    let mut invalidated = HashSet::new();
    let mut changed_required_state_map = None;
    let mut lazy_member_lookup = false;

    let room_state: HashMap<StateKey, Event>;
    // The heroes' memberships, when not in `room_state`.
    let mut hero_state: Option<HashMap<StateKey, Event>> = None;
    if initial {
        let Some(state_ids) = &initial_state_ids else {
            anyhow::bail!("{room_id}: first send without its state read");
        };
        let mut ids = state_filter.filter_state(state_ids);
        // The heroes' memberships the first read did not cover.
        let missing_heroes: Vec<(&str, Option<&str>)> = hero_user_ids
            .iter()
            .filter(|user_id| !initial_filter.matches(MEMBER, user_id))
            .map(|user_id| (MEMBER, Some(&**user_id)))
            .collect();
        if !missing_heroes.is_empty() {
            let heroes = StateFilter::from_types(missing_heroes);
            ids.extend(state_ids_at(context, &room, &heroes).await?);
        }
        let ids: Vec<Arc<str>> = ids.into_values().collect();
        room_state = state_events(reader, &held, &ids).await?;
    } else {
        let from = from.with_context(|| format!("{room_id}: incremental with no from token"))?;
        if let Some(prev_config) = prev_config {
            // The members whose state may be sent: explicit, lazy-loaded,
            // and those whose membership changed.
            let mut all_required_user_state: HashSet<&str> = state_request
                .explicit_user_state
                .iter()
                .chain(lazy_load_user_ids)
                .map(String::as_str)
                .collect();
            for (state_type, state_key) in &delta_keys {
                if state_type == MEMBER {
                    all_required_user_state.insert(state_key);
                }
            }

            let mut previously_returned: HashSet<String> = HashSet::new();
            if !all_required_user_state.is_empty() {
                lazy_member_lookup = true;
                let user_ids: Vec<Arc<str>> = all_required_user_state
                    .iter()
                    .map(|u| Arc::from(*u))
                    .collect();
                let last_seen = reader
                    .connection_lazy_members(from.connection_position, room_id, &user_ids)
                    .await?;
                for (user_id, last_seen_ts) in last_seen {
                    if lazy_load_user_ids.contains(&*user_id) {
                        returned.insert(user_id.to_string(), Some(last_seen_ts));
                    }
                    previously_returned.insert(user_id.to_string());
                }
            }

            let changes = required_state_changes(
                user_id,
                &prev_config.required_state_map,
                &config.required_state_map,
                &previously_returned,
                lazy_load_user_ids,
                &delta_keys,
            );
            changed_required_state_map = changes.changed_required_state_map;
            invalidated = changes.lazy_members_invalidated;
            // Explicit members converted to lazy ones: when they were last
            // sent is not known, so a timestamp already there is kept.
            for user_id in changes.extra_users_to_add_to_lazy_cache {
                returned.entry(user_id).or_insert(None);
            }
            if changes.added_state_filter != StateFilter::none() {
                // State the request newly asks for, on top of the deltas.
                let added = state_ids_at(context, &room, &changes.added_state_filter).await?;
                deltas.extend(added);
            }
        }

        let delta_state: StateMap = deltas
            .iter()
            .map(|(key, event_id)| (key.clone(), Arc::clone(event_id)))
            .collect();
        let event_ids: Vec<Arc<str>> = state_filter
            .filter_state(&delta_state)
            .into_values()
            .collect();
        room_state = state_events(reader, &held, &event_ids).await?;

        if !hero_user_ids.is_empty() {
            let hero_filter =
                StateFilter::from_types(hero_user_ids.iter().map(|u| (MEMBER, Some(&**u))));
            hero_state = Some(state_at(context, &room, &held, &hero_filter).await?);
        }
    }

    let room_avatar =
        lookup(&room_state, (M_ROOM_AVATAR, "")).and_then(|event| content_str(event, "url"));

    let hero_membership_state = hero_state.as_ref().unwrap_or(&room_state);
    let heroes: Vec<StrippedHero> = hero_user_ids
        .iter()
        .filter_map(|hero| {
            let event = lookup(hero_membership_state, (MEMBER, hero))?;
            Some(StrippedHero {
                user_id: hero.to_string(),
                display_name: content_str(event, "displayname"),
                avatar_url: content_str(event, "avatar_url"),
            })
        })
        .collect();

    let mut required_state: Vec<Event> = Vec::new();
    if *required_state_filter != StateFilter::none() {
        let mut keys: Vec<&StateKey> = room_state
            .keys()
            .filter(|key| required_state_filter.matches(&key.0, &key.1))
            .collect();
        keys.sort();
        required_state = keys
            .into_iter()
            .map(|key| room_state[key].shallow_clone())
            .collect();
    }

    // The bump stamp: the last bump event for a joined room, else the
    // membership's position if the client needs one.
    let always_return = always_return_bump_stamp(membership, limited, initial);
    let new_bump_stamp = if membership == "join" {
        bump_stamp(context, room_id, &timeline, always_return).await?
    } else {
        None
    };
    let bump_stamp = final_bump_stamp(new_bump_stamp, always_return, member.event_pos.stream);

    let room_config = room_config_to_persist(
        config,
        prev_config,
        changed_required_state_map.as_ref(),
        ignore_timeline_bound,
        limited,
    );

    let mut entry = RoomEntry {
        room_id: Arc::clone(&member.room_id),
        name: room_name,
        avatar: room_avatar,
        heroes,
        is_dm: input.is_dm,
        initial,
        unstable_expanded_timeline: ignore_timeline_bound,
        required_state,
        timeline,
        bundles,
        redactions: HashMap::new(),
        stripped_state: stripped,
        prev_batch,
        limited,
        num_live: live,
        bump_stamp,
        joined_count,
        invited_count,
    };
    entry.redactions = load_redactions(reader, &entry).await?;

    Ok(RoomComputed {
        entry,
        mutations: RoomMutations {
            room_config,
            lazy_membership: RoomLazyMembershipChanges {
                returned_user_id_to_last_seen_ts_map: returned,
                invalidated_user_ids: invalidated,
            },
        },
        lazy_member_lookup,
    })
}

/// One room's outcome. A panic is this room's error, not the request's.
async fn room_outcome(
    context: &RoomContext<'_, '_>,
    input: &RoomInputData,
) -> (Arc<str>, RoomOutcome) {
    let span = tracing::info_span!(
        "sliding_sync.room",
        room_id = &*input.membership.room_id,
        outcome = tracing::field::Empty,
        error = tracing::field::Empty,
    );
    let outcome = match AssertUnwindSafe(room_sync_data(context, input))
        .catch_unwind()
        .instrument(span.clone())
        .await
    {
        Ok(Ok(computed)) => RoomOutcome::Computed(Box::new(computed)),
        Ok(Err(err)) => match err.downcast::<Fallback>() {
            Ok(fallback) => RoomOutcome::Fallback(fallback.0),
            Err(err) => RoomOutcome::Error(format!("{err:#}")),
        },
        Err(panic) => RoomOutcome::Error(format!("panicked: {}", panic_message(&*panic))),
    };
    let outcome_name = match &outcome {
        RoomOutcome::Computed(_) => "entry",
        RoomOutcome::Fallback(_) => "fallback",
        RoomOutcome::Error(_) => "error",
    };
    span.record("outcome", outcome_name);
    if let RoomOutcome::Error(error) = &outcome {
        record_error(&span, error);
    }
    (Arc::clone(&input.membership.room_id), outcome)
}

/// The entries of `rooms`, twenty at a time, in the order they finish.
pub async fn generate_room_entries(
    reader: &SlidingSyncReader<'_>,
    request: &SlidingSyncRequestData,
    instance_ids: &HashMap<String, i64>,
    now_ms: i64,
    rooms: &[RoomInputData],
) -> anyhow::Result<Vec<(Arc<str>, RoomOutcome)>> {
    let ignored_users = reader.ignored_users(&request.user_id).await?;
    let context = RoomContext {
        reader,
        request,
        client_filter: ClientFilterContext {
            user_id: Arc::clone(&request.user_id),
            ignored_users,
            admin_policy: request.admin_policy,
            msc4354_enabled: request.msc4354_enabled,
        },
        instance_ids,
        now_ms,
    };
    let outcomes: Vec<_> = rooms
        .iter()
        .map(|room| room_outcome(&context, room))
        .collect();
    Ok(stream::iter(outcomes)
        .buffer_unordered(ROOM_ENTRY_CONCURRENCY)
        .collect()
        .await)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn empty_entry() -> RoomEntry {
        RoomEntry {
            room_id: Arc::from("!r:test"),
            name: None,
            avatar: None,
            heroes: Vec::new(),
            is_dm: true,
            initial: false,
            unstable_expanded_timeline: true,
            required_state: Vec::new(),
            timeline: Vec::new(),
            bundles: None,
            redactions: HashMap::new(),
            stripped_state: Vec::new(),
            prev_batch: Some("s1_0".to_owned()),
            limited: Some(true),
            num_live: Some(0),
            bump_stamp: Some(5),
            joined_count: None,
            invited_count: None,
        }
    }

    #[test]
    fn what_counts_as_an_update() {
        // As in Python: a DM flag, an expanded timeline, a token or a bump
        // stamp alone are not updates.
        assert!(!empty_entry().has_updates());
        // An empty name is, though it is not sent.
        let entry = RoomEntry {
            name: Some(String::new()),
            ..empty_entry()
        };
        assert!(entry.has_updates());
        let entry = RoomEntry {
            joined_count: Some(0),
            ..empty_entry()
        };
        assert!(entry.has_updates());
        let entry = RoomEntry {
            initial: true,
            ..empty_entry()
        };
        assert!(entry.has_updates());
    }
}
