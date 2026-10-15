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

//! `_generate_room_entry`: one joined or archived room's entry in a sync
//! response, in the order of the Python.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};

use crate::caches::lock_ignoring_poison;
use crate::clock::Clock;
use crate::events::constants::event_type::M_ROOM_MEMBER;
use crate::events::Event;
use crate::state::{lookup, StateKey};
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::SyncReader;
use crate::sync::client_events::{filter_events_for_client, ClientFilterContext};
use crate::sync::ll_members::{LazyLoadedMembersCache, LazyLoadedMembersCaches};
use crate::sync::request::{RoomSyncInputData, RoomSyncResultData, RoomType, SyncRequestData};
use crate::sync::room_state::{compute_state_delta, RoomState};
use crate::sync::summary::extract_heroes_from_room_summary;
use crate::sync::timeline::{load_filtered_recents, TimelineBatch};
use crate::types::state_filter::StateFilter;

const M_TAG: &str = "m.tag";
const M_ROOM_NAME: &str = "m.room.name";
const M_ROOM_CANONICAL_ALIAS: &str = "m.room.canonical_alias";

/// The reason a room is left to Python. Raised to Python as
/// `RustSyncFallback`.
#[derive(Debug)]
pub struct Fallback(pub String);

impl std::fmt::Display for Fallback {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Fallback {}

/// What computing one room's entry reads, shared by its steps.
pub struct RoomContext<'r, 'a> {
    pub reader: &'r SyncReader<'a>,
    pub request: &'r SyncRequestData,
    pub client_filter: ClientFilterContext,
    pub ll_members: &'r Mutex<LazyLoadedMembersCaches>,
    /// The members this room's entry sends, recorded in `ll_members` only
    /// once the entry is computed. See [`RoomContext::commit_ll_members`].
    pub ll_pending: Mutex<HashMap<Arc<str>, Arc<str>>>,
    pub clock: Clock,
}

/// The device's lazy-loaded members cache, as seen by a room being
/// computed: the cache, overlaid with the members the room sends.
pub struct LlMembersView<'c> {
    cache: &'c mut LazyLoadedMembersCache,
    pending: &'c mut HashMap<Arc<str>, Arc<str>>,
}

impl LlMembersView<'_> {
    /// Whether `event_id` is the membership of `user_id` last sent, by this
    /// room or an earlier response.
    pub fn was_sent(&mut self, user_id: &str, event_id: &str) -> bool {
        match self.pending.get(user_id) {
            Some(pending) => &**pending == event_id,
            None => self.cache.was_sent(user_id, event_id),
        }
    }

    /// Record that this room sends `event_id` as `user_id`'s membership.
    pub fn mark_sent(&mut self, user_id: &str, event_id: &str) {
        self.pending.insert(Arc::from(user_id), Arc::from(event_id));
    }
}

impl RoomContext<'_, '_> {
    pub fn now_ms(&self) -> i64 {
        self.clock.now_millis() as i64
    }

    /// Run `f` on the lazy-loaded members cache of the request's device.
    pub fn with_ll_cache<R>(&self, f: impl FnOnce(&mut LlMembersView<'_>) -> R) -> R {
        let mut caches = lock_ignoring_poison(self.ll_members);
        let mut pending = lock_ignoring_poison(&self.ll_pending);
        f(&mut LlMembersView {
            cache: caches.get_or_create(
                &self.request.user_id,
                self.request.device_id.as_deref(),
                self.clock.now(),
            ),
            pending: &mut pending,
        })
    }

    /// Record the members this room's entry sends in the device's cache,
    /// as sent in the response ending at `now_room_key`. Called once the
    /// entry is computed: a room that fails part way sends nothing, and
    /// Python may compute it instead from the same cache.
    pub fn commit_ll_members(&self) {
        let pending = std::mem::take(&mut *lock_ignoring_poison(&self.ll_pending));
        if pending.is_empty() {
            return;
        }
        let mut caches = lock_ignoring_poison(self.ll_members);
        let cache = caches.get_or_create(
            &self.request.user_id,
            self.request.device_id.as_deref(),
            self.clock.now(),
        );
        for (user_id, event_id) in &pending {
            cache.mark_sent(user_id, event_id, &self.request.now_room_key);
        }
    }
}

/// Whether a JSON value is truthy, as Python reads a decoded one.
fn py_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|n| n != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `_generate_room_entry`: the room's entry, or `None` when it has
/// nothing to send. Fails with [`Fallback`] for a room Python must
/// compute.
pub async fn generate_room_entry(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
) -> anyhow::Result<Option<RoomSyncResultData>> {
    let request = context.request;
    let reader = context.reader;
    let filter = &request.filter;
    let full_state = room.full_state;

    // Out as early as possible.
    if !(room.always_include
        || !room.account_data.is_empty()
        || !room.ephemeral.is_empty()
        || full_state
        || !room.sticky_event_ids.is_empty())
        && room.events.as_ref().is_some_and(Vec::is_empty)
        && room.tags.is_none()
    {
        return Ok(None);
    }

    // The Python waits for full state and digs memberships out of auth
    // events for these, through in-memory state Rust does not see.
    if reader.is_partial_state_room(&room.room_id).await? {
        return Err(Fallback(format!("{} is a partial state room", room.room_id)).into());
    }

    let batch = load_filtered_recents(context, room).await?;

    // On a join (or with `full_state`), send the existing tags.
    let mut tags = room.tags.clone();
    if full_state {
        let room_tags = reader
            .tags_for_room(&request.user_id, &room.room_id)
            .await?;
        tags = if room_tags.is_empty() {
            None
        } else {
            Some(Value::Object(room_tags.into_iter().collect::<Map<_, _>>()))
        };
    }

    let mut account_data_events = Vec::new();
    if let Some(tags) = tags {
        account_data_events.push(json!({"type": M_TAG, "content": {"tags": tags}}));
    }
    for (account_data_type, content) in &room.account_data {
        account_data_events.push(json!({"type": account_data_type, "content": content}));
    }
    account_data_events.retain(|event| filter.check_room_account_data(event));

    // Ephemeral events go to clients without a `room_id`.
    let ephemeral: Vec<Value> = room
        .ephemeral
        .iter()
        .filter(|event| filter.check_room_ephemeral(event))
        .map(|event| match event {
            Value::Object(object) => Value::Object(
                object
                    .iter()
                    .filter(|(key, _)| *key != "room_id")
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            ),
            other => other.clone(),
        })
        .collect();

    if !(room.always_include
        || !batch.events.is_empty()
        || !account_data_events.is_empty()
        || !ephemeral.is_empty()
        || full_state
        || !room.sticky_event_ids.is_empty())
    {
        return Ok(None);
    }

    let mut state = if room.out_of_band {
        RoomState::new()
    } else {
        compute_state_delta(
            context,
            room,
            &batch,
            full_state,
            room.rtype == RoomType::Joined,
        )
        .await?
    };

    // A summary when lazy loading, so that the client can name the room:
    // when the timeline has membership changes, when a gappy sync's state
    // does, or on an initial sync.
    let mut summary = Some(json!({}));
    if !room.out_of_band
        && filter.lazy_load_members()
        && (batch
            .events
            .iter()
            .any(|e| e.event.type_() == M_ROOM_MEMBER)
            || (batch.limited && state.keys().any(|key| &*key.0 == M_ROOM_MEMBER))
            || room.since.is_none())
    {
        summary = compute_summary(context, room, &batch, &mut state).await?;
    }

    let has_state = !state.is_empty();
    let state: Vec<Event> = state.into_values().collect();

    let mut result = RoomSyncResultData {
        room_id: Arc::clone(&room.room_id),
        rtype: room.rtype,
        timeline: Vec::new(),
        prev_batch_base: batch.prev_batch_base,
        prev_batch_room_key: batch.prev_batch_room_key,
        limited: batch.limited,
        bundle_aggregations: batch.bundle_aggregations,
        state,
        ephemeral,
        account_data: account_data_events,
        summary,
        unread_notifications: HashMap::new(),
        unread_thread_notifications: Vec::new(),
        unread_count: 0,
        sticky: Vec::new(),
        redactions: HashMap::new(),
        upto: None,
    };
    let timeline = batch.events;

    match room.rtype {
        RoomType::Joined => {
            // MSC4354: sticky events already in the timeline are not sent
            // twice.
            let timeline_ids: HashSet<&str> = timeline.iter().map(|e| e.event.event_id()).collect();
            let sticky_ids: Vec<Arc<str>> = room
                .sticky_event_ids
                .iter()
                .filter(|id| !timeline_ids.contains(&***id))
                .cloned()
                .collect();
            if !sticky_ids.is_empty() {
                let events: Vec<Arc<Event>> = reader
                    .events(&sticky_ids, RedactBehaviour::Redact, false, false)
                    .await?
                    .into_iter()
                    .map(Arc::new)
                    .collect();
                result.sticky = filter_events_for_client(
                    reader,
                    &context.client_filter,
                    &events,
                    &HashSet::new(),
                    context.now_ms(),
                )
                .await?;
            }

            let non_empty = !timeline.is_empty()
                || has_state
                || !result.ephemeral.is_empty()
                || !result.account_data.is_empty()
                || !result.sticky.is_empty();
            if !(non_empty || room.always_include) {
                return Ok(None);
            }
            add_unread_counts(context, &room.room_id, &mut result).await?;
        }
        RoomType::Archived => {
            let non_empty = !timeline.is_empty() || has_state || !result.account_data.is_empty();
            if !(non_empty || room.always_include) {
                return Ok(None);
            }
        }
    }
    result.timeline = timeline;
    result.redactions = load_redactions(reader, &result).await?;
    Ok(Some(result))
}

/// The redactions of the redacted events of `result`, which the serializer
/// adds to their unsigned data, as `_prepare_serialization` does.
async fn load_redactions(
    reader: &SyncReader<'_>,
    result: &RoomSyncResultData,
) -> anyhow::Result<HashMap<Arc<str>, Event>> {
    let mut redaction_ids: Vec<Arc<str>> = Vec::new();
    let events = result
        .timeline
        .iter()
        .chain(&result.sticky)
        .map(|filtered| &filtered.event)
        .chain(&result.state);
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

/// `compute_summary`: the member counts, and the heroes if the room has no
/// name or canonical alias, with the heroes' memberships added to `state`
/// if the device has not been sent them. `None` for a room with no events.
async fn compute_summary(
    context: &RoomContext<'_, '_>,
    room: &RoomSyncInputData,
    batch: &TimelineBatch,
    state: &mut RoomState,
) -> anyhow::Result<Option<Value>> {
    let reader = context.reader;
    let request = context.request;
    let room_id = &*room.room_id;

    let (last_events, _) = reader
        .recent_event_ids(room_id, 1, &request.now_room_key)
        .await?;
    let Some(last_event) = last_events.last() else {
        return Ok(None);
    };
    let state_ids = reader
        .state_ids_for_event(
            &last_event.event_id,
            &StateFilter::from_types([(M_ROOM_NAME, Some("")), (M_ROOM_CANONICAL_ALIAS, Some(""))]),
        )
        .await?;

    let details = reader.room_summary(room_id).await?;
    let count = |membership: &str| details.get(membership).map_or(0, |m| m.count);

    let mut summary = Map::new();
    summary.insert("m.joined_member_count".to_owned(), json!(count("join")));
    summary.insert("m.invited_member_count".to_owned(), json!(count("invite")));

    // A room with a name or canonical alias needs no heroes.
    for (key, field) in [(M_ROOM_NAME, "name"), (M_ROOM_CANONICAL_ALIAS, "alias")] {
        let Some(event_id) = lookup(&state_ids, (key, "")) else {
            continue;
        };
        let events = reader
            .events(
                std::slice::from_ref(event_id),
                RedactBehaviour::Redact,
                false,
                false,
            )
            .await?;
        if events
            .first()
            .is_some_and(|event| py_truthy(event.content().get_field(field)))
        {
            return Ok(Some(Value::Object(summary)));
        }
    }

    let mut member_ids: HashMap<Arc<str>, Arc<str>> = HashMap::new();
    for membership in ["join", "invite", "leave", "ban"] {
        if let Some(members) = details.get(membership) {
            for (user_id, event_id) in &members.members {
                member_ids.insert(Arc::clone(user_id), Arc::clone(event_id));
            }
        }
    }

    let heroes = extract_heroes_from_room_summary(&details, &request.user_id);
    summary.insert(
        "m.heroes".to_owned(),
        Value::Array(
            heroes
                .iter()
                .map(|h| Value::String(h.to_string()))
                .collect(),
        ),
    );

    if !request.filter.lazy_load_members() {
        return Ok(Some(Value::Object(summary)));
    }

    // The members the client already knows of, from the state or the
    // timeline. The heroes' memberships the device was not sent are added.
    let mut existing_members: HashSet<&str> = state
        .keys()
        .filter(|key| &*key.0 == M_ROOM_MEMBER)
        .map(|key| &*key.1)
        .collect();
    for filtered in &batch.events {
        if filtered.event.type_() == M_ROOM_MEMBER {
            if let Some(state_key) = filtered.event.state_key() {
                existing_members.insert(state_key);
            }
        }
    }
    let missing_hero_event_ids: Vec<Arc<str>> = context.with_ll_cache(|cache| {
        heroes
            .iter()
            .filter_map(|hero| {
                let event_id = member_ids.get(hero)?;
                (!cache.was_sent(hero, event_id) && !existing_members.contains(&**hero))
                    .then(|| Arc::clone(event_id))
            })
            .collect()
    });

    let missing_hero_state = reader
        .events(
            &missing_hero_event_ids,
            RedactBehaviour::Redact,
            false,
            false,
        )
        .await?;
    context.with_ll_cache(|cache| {
        for event in &missing_hero_state {
            if let Some(state_key) = event.state_key() {
                cache.mark_sent(state_key, event.event_id());
            }
        }
    });
    for event in missing_hero_state {
        if let Some(state_key) = event.state_key() {
            state.insert(StateKey::new(M_ROOM_MEMBER, state_key), event);
        }
    }

    Ok(Some(Value::Object(summary)))
}

/// The unread counts of a joined room: per thread if the filter asks for
/// thread notifications, folded into the main timeline's otherwise.
async fn add_unread_counts(
    context: &RoomContext<'_, '_>,
    room_id: &str,
    result: &mut RoomSyncResultData,
) -> anyhow::Result<()> {
    let request = context.request;
    // Without push there are no counts in the database to read.
    let counts = if request.enable_push {
        Some(
            context
                .reader
                .unread_counts(room_id, &request.user_id)
                .await?,
        )
    } else {
        None
    };
    let (mut notify_count, mut highlight_count, mut unread_count) = (0, 0, 0);
    if let Some(counts) = &counts {
        notify_count = counts.main_timeline.notify_count;
        highlight_count = counts.main_timeline.highlight_count;
        unread_count = counts.main_timeline.unread_count;
        if request.filter.unread_thread_notifications() {
            result.unread_thread_notifications = counts
                .threads
                .iter()
                .map(|(thread_id, thread)| {
                    (
                        Arc::clone(thread_id),
                        thread.notify_count,
                        thread.highlight_count,
                    )
                })
                .collect();
        } else {
            for thread in counts.threads.values() {
                notify_count += thread.notify_count;
                highlight_count += thread.highlight_count;
                unread_count += thread.unread_count;
            }
        }
    }
    result
        .unread_notifications
        .insert("notification_count", notify_count);
    result
        .unread_notifications
        .insert("highlight_count", highlight_count);
    result.unread_count = unread_count;
    Ok(())
}
