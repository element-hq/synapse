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

//! The `rooms` section of `_generate_sync_entry_for_rooms`, from the room
//! changes on.
//!
//! [`room_changes`] ports `_get_room_changes_for_incremental_sync` and
//! `_get_room_changes_for_initial_sync`: which rooms the response reports,
//! and how. Python then fetches the sticky events of the newly joined
//! rooms, and [`generate_room_entries`] computes the entry of every joined
//! or archived room, ten at a time, as `concurrently_execute` does.
//!
//! What Python computes before the call stays in Python: the joined rooms
//! and membership changes of `get_sync_result_builder`, the ephemeral
//! events, account data, tags and sticky events of each room, and the
//! no-op check, which reads the in-memory stream change cache. Python also
//! drops the invites of ignored users and those the user's invite rules
//! ignore.

use std::collections::{HashMap, HashSet};
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex};

use futures::{stream, FutureExt, StreamExt, TryStreamExt};
use pyo3::{
    exceptions::{PyKeyError, PyRuntimeError},
    intern,
    prelude::*,
    types::{PyBool, PyList, PyTuple},
};
use pythonize::depythonize;

use crate::caches::lock_ignoring_poison;
use crate::clock::Clock;
use crate::events::constants::event_type::M_ROOM_MEMBER;
use crate::events::Event;
use crate::state::StateKey;
use crate::storage::events::RedactBehaviour;
use crate::storage::sync_store::{PaginationOrder, SyncReader};
use crate::sync::client_events::ClientFilterContext;
use crate::sync::ll_members::LazyLoadedMembersCaches;
use crate::sync::request::{
    rtype_str, RoomSyncInputData, RoomSyncResult, RoomSyncResultData, RoomType, SyncRequestData,
};
use crate::sync::room::{generate_room_entry, Fallback, RoomContext};
use crate::types::state_filter::StateFilter;
use crate::types::stream_token::{persisted_after, Direction, RoomStreamToken};
use crate::visibility::membership_of;

/// How many rooms' timelines are fetched at once, as in
/// `get_room_events_stream_for_rooms`.
const TIMELINE_FETCH_CONCURRENCY: usize = 20;

/// How many rooms' entries are computed at once, as in
/// `_generate_sync_entry_for_rooms`.
const ROOM_ENTRY_CONCURRENCY: usize = 10;

/// Which of the request's tokens a room's token is built on. A room's
/// tokens differ from the request's only in their room key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenBase {
    Since,
    Now,
}

/// A room's `RoomSyncResultBuilder` token: the request token it is built
/// on, with its room key replaced.
#[derive(Debug, Clone)]
pub struct RoomToken {
    pub base: TokenBase,
    pub room_key: RoomStreamToken,
}

/// A joined or archived room to compute the entry of: a
/// `RoomSyncResultBuilder`.
pub struct RoomEntryPlan {
    pub room_id: Arc<str>,
    pub rtype: RoomType,
    /// The events between the since and upto tokens, if fetched.
    pub events: Option<Vec<Arc<Event>>>,
    pub newly_joined: bool,
    pub full_state: bool,
    /// Whether the room's `since_token` is the request's, rather than None.
    pub has_since: bool,
    pub upto: RoomToken,
    pub end: RoomToken,
    pub out_of_band: bool,
}

/// One of the user's rooms, as `get_rooms_for_local_user_where_membership_is`
/// returns it: a `RoomsForUser`.
pub struct RoomForUser {
    pub room_id: Arc<str>,
    pub sender: Arc<str>,
    pub membership: Arc<str>,
    pub event_id: Arc<str>,
    pub stream: i64,
}

/// What [`room_changes`] reads besides the request, from Python's
/// `SyncResultBuilder`.
pub struct RoomsInputData {
    /// The room key of the request's since token, `None` for an initial
    /// sync.
    pub since: Option<RoomStreamToken>,
    /// The request's `full_state`.
    pub full_state: bool,
    pub joined_room_ids: Vec<Arc<str>>,
    /// The joined rooms that may have events after `since`, by the stream
    /// change cache. Incremental syncs only.
    pub changed_room_ids: HashSet<Arc<str>>,
    pub forced_newly_joined_room_ids: Vec<Arc<str>>,
    /// The user's membership events after `since`, oldest first.
    pub membership_change_events: Vec<Arc<Event>>,
    /// The user's rooms, in a known room version and not excluded. Initial
    /// syncs only.
    pub initial_rooms: Vec<RoomForUser>,
}

/// A `_RoomChanges`, less the invites Python drops.
pub struct RoomChangesData {
    pub since: Option<RoomStreamToken>,
    pub full_state: bool,
    pub entries: Vec<RoomEntryPlan>,
    /// The index in `entries` of each room.
    pub entry_index: HashMap<Arc<str>, usize>,
    pub invited: Vec<(Arc<str>, Arc<Event>)>,
    pub knocked: Vec<(Arc<str>, Arc<Event>)>,
    pub newly_joined_rooms: Vec<Arc<str>>,
    pub newly_left_rooms: Vec<Arc<str>>,
}

fn entry_index(entries: &[RoomEntryPlan]) -> HashMap<Arc<str>, usize> {
    entries
        .iter()
        .enumerate()
        .map(|(index, plan)| (Arc::clone(&plan.room_id), index))
        .collect()
}

/// `_get_room_changes_for_incremental_sync` or
/// `_get_room_changes_for_initial_sync`.
pub async fn room_changes(
    reader: &SyncReader<'_>,
    request: &SyncRequestData,
    input: RoomsInputData,
) -> anyhow::Result<RoomChangesData> {
    match input.since.clone() {
        Some(since) => incremental_room_changes(reader, request, input, &since).await,
        None => initial_room_changes(reader, request, input).await,
    }
}

/// The user's membership in the room at `since`: the event, if any.
async fn membership_at(
    reader: &SyncReader<'_>,
    room_id: &str,
    user_id: &str,
    since: &RoomStreamToken,
) -> anyhow::Result<Option<Event>> {
    let state_filter = StateFilter::from_types([(M_ROOM_MEMBER, Some(user_id))]);
    let state = reader.state_ids_at(room_id, since, &state_filter).await?;
    let Some(event_id) = state.get(&StateKey::new(M_ROOM_MEMBER, user_id)) else {
        return Ok(None);
    };
    // `get_event(allow_none=True)`.
    let mut events = reader
        .events(
            std::slice::from_ref(event_id),
            RedactBehaviour::Redact,
            false,
            false,
        )
        .await?;
    Ok(events.pop())
}

fn is_join(event: Option<&Event>) -> bool {
    event.and_then(membership_of) == Some("join")
}

async fn incremental_room_changes(
    reader: &SyncReader<'_>,
    request: &SyncRequestData,
    input: RoomsInputData,
    since: &RoomStreamToken,
) -> anyhow::Result<RoomChangesData> {
    let user_id = &*request.user_id;
    let now = &request.now_room_key;
    let joined: HashSet<&str> = input.joined_room_ids.iter().map(|r| &**r).collect();

    // The membership events by room, rooms in the order of their first.
    let mut rooms_in_order: Vec<Arc<str>> = Vec::new();
    let mut by_room: HashMap<Arc<str>, Vec<&Arc<Event>>> = HashMap::new();
    for event in &input.membership_change_events {
        let room_id: Arc<str> = Arc::from(event.room_id());
        by_room
            .entry(Arc::clone(&room_id))
            .or_insert_with(|| {
                rooms_in_order.push(Arc::clone(&room_id));
                Vec::new()
            })
            .push(event);
    }

    let mut newly_joined_rooms: Vec<Arc<str>> = input.forced_newly_joined_room_ids.clone();
    // The same rooms, to look them up.
    let mut newly_joined: HashSet<Arc<str>> = newly_joined_rooms.iter().cloned().collect();
    let mut newly_left_rooms: Vec<Arc<str>> = Vec::new();
    let mut entries: Vec<RoomEntryPlan> = Vec::new();
    let mut invited = Vec::new();
    let mut knocked = Vec::new();

    for room_id in rooms_in_order {
        let events = &by_room[&room_id];
        let non_joins: Vec<&Arc<Event>> = events
            .iter()
            .copied()
            .filter(|event| membership_of(event) != Some("join"))
            .collect();
        let has_join = non_joins.len() != events.len();
        let in_room = joined.contains(&*room_id);

        // Joined again after leaving: always included, so that device
        // list changes are calculated correctly.
        if in_room && !non_joins.is_empty() {
            newly_joined.insert(Arc::clone(&room_id));
            newly_joined_rooms.push(room_id);
            continue;
        }

        if in_room || has_join {
            let old_membership = membership_at(reader, &room_id, user_id, since).await?;
            if !is_join(old_membership.as_ref()) {
                newly_joined.insert(Arc::clone(&room_id));
                newly_joined_rooms.push(Arc::clone(&room_id));
            }
        }

        if in_room {
            continue;
        }
        let Some(&last_non_join) = non_joins.last() else {
            continue;
        };

        // Left the room: either joined before, or joined since and left.
        let last = events.last().expect("a room has at least one event");
        if membership_of(last) != Some("join") {
            if has_join {
                newly_left_rooms.push(Arc::clone(&room_id));
            } else {
                let old_membership = membership_at(reader, &room_id, user_id, since).await?;
                if is_join(old_membership.as_ref()) {
                    newly_left_rooms.push(Arc::clone(&room_id));
                }
            }
        }

        match membership_of(last_non_join) {
            Some("invite") => invited.push((Arc::clone(&room_id), Arc::clone(last_non_join))),
            Some("knock") => knocked.push((Arc::clone(&room_id), Arc::clone(last_non_join))),
            _ => {}
        }

        // The last leave or ban.
        let Some(leave_event) = non_joins
            .iter()
            .rev()
            .find(|event| matches!(membership_of(event), Some("leave" | "ban")))
        else {
            continue;
        };
        let position = reader
            .event_position(leave_event.event_id())
            .await?
            .ok_or_else(|| anyhow::anyhow!("no position for {}", leave_event.event_id()))?;
        if !persisted_after(&position.instance_name, position.stream, since) {
            continue;
        }
        let leave_key = RoomStreamToken::live(position.stream);
        // An out of band leave, like a remote invite rejection, is the
        // timeline. Otherwise the timeline is loaded as usual.
        let out_of_band = leave_event
            .internal_metadata()
            .is_out_of_band_membership()
            .map_err(anyhow::Error::from)?;
        entries.push(RoomEntryPlan {
            newly_joined: newly_joined.contains(&room_id),
            room_id,
            rtype: RoomType::Archived,
            events: out_of_band.then(|| vec![Arc::clone(leave_event)]),
            full_state: false,
            has_since: true,
            upto: RoomToken {
                base: TokenBase::Since,
                room_key: leave_key.clone(),
            },
            end: RoomToken {
                base: TokenBase::Since,
                room_key: leave_key,
            },
            out_of_band,
        });
    }

    // The events since `since` in the joined rooms that may have any, the
    // most recent if there are too many: a gap.
    let limit = request.filter.timeline_limit() + 1;
    let to_fetch: Vec<&Arc<str>> = input
        .joined_room_ids
        .iter()
        .filter(|room_id| input.changed_room_ids.contains(*room_id))
        .collect();
    let fetches: Vec<_> = to_fetch
        .into_iter()
        .map(|room_id| recent_events(reader, room_id, now, since, limit))
        .collect();
    let fetched: HashMap<Arc<str>, (Vec<Event>, RoomStreamToken)> = stream::iter(fetches)
        .buffer_unordered(TIMELINE_FETCH_CONCURRENCY)
        .try_collect()
        .await?;

    // Every joined room, even without new events, for its other updates.
    let mut fetched = fetched;
    for room_id in &input.joined_room_ids {
        let is_newly_joined = newly_joined.contains(room_id);
        let entry = match fetched.remove(room_id) {
            Some((mut events, start_key)) => {
                // Oldest first.
                events.reverse();
                RoomEntryPlan {
                    room_id: Arc::clone(room_id),
                    rtype: RoomType::Joined,
                    events: Some(events.into_iter().map(Arc::new).collect()),
                    newly_joined: is_newly_joined,
                    full_state: false,
                    has_since: !is_newly_joined,
                    upto: RoomToken {
                        base: TokenBase::Now,
                        room_key: start_key,
                    },
                    end: RoomToken {
                        base: TokenBase::Now,
                        room_key: now.clone(),
                    },
                    out_of_band: false,
                }
            }
            None => RoomEntryPlan {
                room_id: Arc::clone(room_id),
                rtype: RoomType::Joined,
                events: Some(Vec::new()),
                newly_joined: is_newly_joined,
                full_state: false,
                has_since: true,
                upto: RoomToken {
                    base: TokenBase::Since,
                    room_key: since.clone(),
                },
                end: RoomToken {
                    base: TokenBase::Now,
                    room_key: now.clone(),
                },
                out_of_band: false,
            },
        };
        entries.push(entry);
    }

    Ok(RoomChangesData {
        since: input.since,
        full_state: input.full_state,
        entry_index: entry_index(&entries),
        entries,
        invited,
        knocked,
        newly_joined_rooms,
        newly_left_rooms,
    })
}

/// `paginate_room_events_by_stream_ordering` backwards from `now` to
/// `since`, for `get_room_events_stream_for_rooms`: the events, newest
/// first, and the token of the start of the page.
async fn recent_events(
    reader: &SyncReader<'_>,
    room_id: &Arc<str>,
    now: &RoomStreamToken,
    since: &RoomStreamToken,
    limit: i64,
) -> anyhow::Result<(Arc<str>, (Vec<Event>, RoomStreamToken))> {
    let (events, start_key, _) = reader
        .paginate_room_events(
            room_id,
            now,
            Some(since),
            Direction::Backwards,
            limit,
            PaginationOrder::Stream,
        )
        .await?;
    Ok((Arc::clone(room_id), (events, start_key)))
}

async fn initial_room_changes(
    reader: &SyncReader<'_>,
    request: &SyncRequestData,
    input: RoomsInputData,
) -> anyhow::Result<RoomChangesData> {
    let now = &request.now_room_key;
    let mut entries = Vec::new();
    // Room, event ID, and whether it is a knock rather than an invite.
    let mut to_load: Vec<(Arc<str>, Arc<str>, bool)> = Vec::new();

    for room in &input.initial_rooms {
        match &*room.membership {
            "join" => entries.push(RoomEntryPlan {
                room_id: Arc::clone(&room.room_id),
                rtype: RoomType::Joined,
                events: None,
                newly_joined: false,
                full_state: true,
                has_since: false,
                upto: RoomToken {
                    base: TokenBase::Now,
                    room_key: now.clone(),
                },
                end: RoomToken {
                    base: TokenBase::Now,
                    room_key: now.clone(),
                },
                out_of_band: false,
            }),
            // Python drops these too, but they need not be loaded.
            "invite" if request.ignored_users.contains(&room.sender) => {}
            "invite" => {
                to_load.push((Arc::clone(&room.room_id), Arc::clone(&room.event_id), false))
            }
            "knock" => to_load.push((Arc::clone(&room.room_id), Arc::clone(&room.event_id), true)),
            "leave" | "ban" => {
                // Rooms the user was banned or kicked from are always sent.
                if !request.filter.include_leave
                    && &*room.membership == "leave"
                    && room.sender == request.user_id
                {
                    continue;
                }
                let leave_key = RoomStreamToken::live(room.stream);
                entries.push(RoomEntryPlan {
                    room_id: Arc::clone(&room.room_id),
                    rtype: RoomType::Archived,
                    events: None,
                    newly_joined: false,
                    full_state: true,
                    has_since: false,
                    upto: RoomToken {
                        base: TokenBase::Now,
                        room_key: leave_key.clone(),
                    },
                    end: RoomToken {
                        base: TokenBase::Now,
                        room_key: leave_key,
                    },
                    out_of_band: false,
                });
            }
            _ => {}
        }
    }

    // `get_event`, for each invite and knock.
    let ids: Vec<Arc<str>> = to_load.iter().map(|(_, id, _)| Arc::clone(id)).collect();
    let loaded: HashMap<Arc<str>, Arc<Event>> = reader
        .events(&ids, RedactBehaviour::Redact, false, false)
        .await?
        .into_iter()
        .map(|event| (Arc::from(event.event_id()), Arc::new(event)))
        .collect();
    let mut invited = Vec::new();
    let mut knocked = Vec::new();
    for (room_id, event_id, is_knock) in to_load {
        let event = loaded
            .get(&event_id)
            .ok_or_else(|| anyhow::anyhow!("could not find event {event_id}"))?;
        let list = if is_knock { &mut knocked } else { &mut invited };
        list.push((room_id, Arc::clone(event)));
    }

    Ok(RoomChangesData {
        since: input.since,
        full_state: input.full_state,
        entry_index: entry_index(&entries),
        entries,
        invited,
        knocked,
        newly_joined_rooms: Vec::new(),
        newly_left_rooms: Vec::new(),
    })
}

/// What `handle_room_entries` passes `_generate_room_entry` besides the
/// room: the room's ephemeral events, tags, account data and sticky events.
pub struct RoomExtras {
    pub ephemeral: Vec<serde_json::Value>,
    pub tags: Option<serde_json::Value>,
    pub account_data: Vec<(String, serde_json::Value)>,
    pub sticky_event_ids: Vec<Arc<str>>,
}

impl RoomExtras {
    /// `room_id`'s extras, read from the mappings Python passes, as
    /// `handle_room_entries` reads them.
    pub fn from_py(
        room_id: &str,
        ephemeral_by_room: &Bound<'_, PyAny>,
        tags_by_room: &Bound<'_, PyAny>,
        account_data_by_room: &Bound<'_, PyAny>,
        sticky_by_room: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let py = ephemeral_by_room.py();
        fn get<'py>(
            mapping: &Bound<'py, PyAny>,
            room_id: &str,
        ) -> PyResult<Option<Bound<'py, PyAny>>> {
            let value = mapping.call_method1(intern!(mapping.py(), "get"), (room_id,))?;
            Ok((!value.is_none()).then_some(value))
        }

        let mut ephemeral = Vec::new();
        if let Some(events) = get(ephemeral_by_room, room_id)? {
            for event in events.try_iter()? {
                ephemeral.push(depythonize(&event?)?);
            }
        }
        let tags = get(tags_by_room, room_id)?
            .map(|tags| depythonize(&tags))
            .transpose()?;
        let mut account_data = Vec::new();
        if let Some(items) = get(account_data_by_room, room_id)? {
            for item in items.call_method0(intern!(py, "items"))?.try_iter()? {
                let (account_data_type, content): (String, Bound<'_, PyAny>) = item?.extract()?;
                account_data.push((account_data_type, depythonize(&content)?));
            }
        }
        let sticky_event_ids = match get(sticky_by_room, room_id)? {
            Some(ids) => ids
                .extract::<Vec<String>>()?
                .into_iter()
                .map(Arc::from)
                .collect(),
            None => Vec::new(),
        };
        Ok(RoomExtras {
            ephemeral,
            tags,
            account_data,
            sticky_event_ids,
        })
    }
}

/// How one room's entry came out.
pub enum RoomOutcome {
    /// The entry, or `None` when the room has nothing to send.
    Entry(Option<Box<RoomSyncResultData>>),
    /// Python must compute the room: [`Fallback`].
    Fallback(String),
    /// Computing the room failed.
    Error(String),
}

/// The input of the room's `_generate_room_entry`.
pub fn room_input(
    changes: &RoomChangesData,
    plan: &RoomEntryPlan,
    extras: RoomExtras,
) -> RoomSyncInputData {
    RoomSyncInputData {
        room_id: Arc::clone(&plan.room_id),
        rtype: plan.rtype,
        events: plan.events.clone(),
        newly_joined: plan.newly_joined,
        full_state: plan.full_state || plan.newly_joined || changes.full_state,
        since: if plan.has_since {
            changes.since.clone()
        } else {
            None
        },
        upto: plan.upto.room_key.clone(),
        end: plan.end.room_key.clone(),
        out_of_band: plan.out_of_band,
        ephemeral: extras.ephemeral,
        tags: extras.tags,
        account_data: extras.account_data,
        sticky_event_ids: extras.sticky_event_ids,
        always_include: changes.full_state,
    }
}

/// One room's entry. Its lazy-loaded members are recorded once it is
/// computed.
async fn room_entry(
    reader: &SyncReader<'_>,
    request: &SyncRequestData,
    ll_members: &Mutex<LazyLoadedMembersCaches>,
    room: RoomSyncInputData,
) -> (Arc<str>, RoomOutcome) {
    let context = RoomContext {
        reader,
        request,
        client_filter: ClientFilterContext {
            user_id: Arc::clone(&request.user_id),
            ignored_users: Arc::clone(&request.ignored_users),
            admin_policy: request.admin_policy,
            msc4354_enabled: request.msc4354_enabled,
        },
        ll_members,
        ll_pending: Mutex::default(),
        clock: Clock::new(),
    };
    // A panic is this room's error. It would otherwise fail every room of
    // the request, including those that have recorded the members they
    // sent, which Python then could not compute.
    let outcome = match AssertUnwindSafe(generate_room_entry(&context, &room))
        .catch_unwind()
        .await
    {
        Ok(Ok(result)) => {
            context.commit_ll_members();
            RoomOutcome::Entry(result.map(Box::new))
        }
        Ok(Err(err)) => match err.downcast::<Fallback>() {
            Ok(fallback) => RoomOutcome::Fallback(fallback.0),
            Err(err) => RoomOutcome::Error(format!("{err:#}")),
        },
        Err(panic) => RoomOutcome::Error(format!("panicked: {}", panic_message(&*panic))),
    };
    (Arc::clone(&room.room_id), outcome)
}

/// The message of a panic's payload.
fn panic_message(panic: &(dyn std::any::Any + Send)) -> &str {
    if let Some(message) = panic.downcast_ref::<&str>() {
        message
    } else if let Some(message) = panic.downcast_ref::<String>() {
        message
    } else {
        "unknown error"
    }
}

/// The entries of `rooms`, ten at a time, in the order they finish. A
/// room's lazy-loaded members are recorded once its entry is computed.
pub async fn generate_room_entries(
    reader: &SyncReader<'_>,
    request: &SyncRequestData,
    ll_members: &Mutex<LazyLoadedMembersCaches>,
    rooms: Vec<RoomSyncInputData>,
) -> Vec<(Arc<str>, RoomOutcome)> {
    let rooms: Vec<_> = rooms
        .into_iter()
        .map(|room| room_entry(reader, request, ll_members, room))
        .collect();
    stream::iter(rooms)
        .buffer_unordered(ROOM_ENTRY_CONCURRENCY)
        .collect()
        .await
}

/// What [`room_changes`] reads besides the request. See
/// [`RoomsInputData`].
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomsInput {
    pub(crate) data: Mutex<Option<RoomsInputData>>,
}

#[pymethods]
impl RoomsInput {
    #[new]
    #[pyo3(signature = (*, since, full_state, joined_room_ids, changed_room_ids, forced_newly_joined_room_ids, membership_change_events, initial_rooms))]
    #[allow(clippy::too_many_arguments)]
    fn py_new(
        since: Option<RoomStreamToken>,
        full_state: bool,
        joined_room_ids: Vec<String>,
        changed_room_ids: HashSet<String>,
        forced_newly_joined_room_ids: Vec<String>,
        membership_change_events: Vec<Bound<'_, Event>>,
        initial_rooms: Vec<(String, String, String, String, i64)>,
    ) -> Self {
        RoomsInput {
            data: Mutex::new(Some(RoomsInputData {
                since,
                full_state,
                joined_room_ids: joined_room_ids.into_iter().map(Arc::from).collect(),
                changed_room_ids: changed_room_ids.into_iter().map(Arc::from).collect(),
                forced_newly_joined_room_ids: forced_newly_joined_room_ids
                    .into_iter()
                    .map(Arc::from)
                    .collect(),
                membership_change_events: membership_change_events
                    .iter()
                    .map(|event| Arc::new(event.get().shallow_clone()))
                    .collect(),
                initial_rooms: initial_rooms
                    .into_iter()
                    .map(
                        |(room_id, sender, membership, event_id, stream)| RoomForUser {
                            room_id: Arc::from(room_id),
                            sender: Arc::from(sender),
                            membership: Arc::from(membership),
                            event_id: Arc::from(event_id),
                            stream,
                        },
                    )
                    .collect(),
            })),
        }
    }
}

impl RoomsInput {
    /// The input, which a [`room_changes`] call consumes.
    pub(crate) fn take(&self) -> PyResult<RoomsInputData> {
        lock_ignoring_poison(&self.data)
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("RoomsInput already used"))
    }
}

fn events_to_py<'py>(
    py: Python<'py>,
    events: &[(Arc<str>, Arc<Event>)],
) -> PyResult<Bound<'py, PyList>> {
    let items = events
        .iter()
        .map(|(room_id, event)| {
            let event = Bound::new(py, event.shallow_clone())?;
            PyTuple::new(
                py,
                [room_id.into_pyobject(py)?.into_any(), event.into_any()],
            )
        })
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, items)
}

fn room_ids_to_py<'py>(py: Python<'py>, room_ids: &[Arc<str>]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, room_ids.iter().map(|room_id| &**room_id))
}

/// A `_RoomChanges`. See [`RoomChangesData`].
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomChanges {
    pub(crate) data: Arc<RoomChangesData>,
}

#[pymethods]
impl RoomChanges {
    #[getter]
    fn newly_joined_rooms<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        room_ids_to_py(py, &self.data.newly_joined_rooms)
    }

    #[getter]
    fn newly_left_rooms<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        room_ids_to_py(py, &self.data.newly_left_rooms)
    }

    /// The invites as `(room_id, event)`, including those Python drops.
    #[getter]
    fn invited<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        events_to_py(py, &self.data.invited)
    }

    /// The knocks as `(room_id, event)`.
    #[getter]
    fn knocked<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        events_to_py(py, &self.data.knocked)
    }

    /// The number of joined and archived rooms to compute.
    fn __len__(&self) -> usize {
        self.data.entries.len()
    }

    /// The `RoomSyncResultBuilder` fields of a room, for Python to compute
    /// it: `(rtype, events, newly_joined, full_state, has_since,
    /// upto_from_now, upto_room_key, end_from_now, end_room_key,
    /// out_of_band)`.
    fn room_entry<'py>(&self, py: Python<'py>, room_id: &str) -> PyResult<Bound<'py, PyTuple>> {
        let plan = self
            .data
            .entry_index
            .get(room_id)
            .map(|&index| &self.data.entries[index])
            .ok_or_else(|| PyKeyError::new_err(room_id.to_owned()))?;
        let events = match &plan.events {
            Some(events) => PyList::new(
                py,
                events
                    .iter()
                    .map(|event| Bound::new(py, event.shallow_clone()))
                    .collect::<PyResult<Vec<_>>>()?,
            )?
            .into_any(),
            None => py.None().into_bound(py),
        };
        PyTuple::new(
            py,
            [
                rtype_str(plan.rtype).into_pyobject(py)?.into_any(),
                events,
                PyBool::new(py, plan.newly_joined).to_owned().into_any(),
                PyBool::new(py, plan.full_state).to_owned().into_any(),
                PyBool::new(py, plan.has_since).to_owned().into_any(),
                PyBool::new(py, plan.upto.base == TokenBase::Now)
                    .to_owned()
                    .into_any(),
                (&plan.upto.room_key).into_pyobject(py)?.into_any(),
                PyBool::new(py, plan.end.base == TokenBase::Now)
                    .to_owned()
                    .into_any(),
                (&plan.end.room_key).into_pyobject(py)?.into_any(),
                PyBool::new(py, plan.out_of_band).to_owned().into_any(),
            ],
        )
    }
}

/// What [`generate_room_entries`] made of the rooms.
#[pyclass(frozen, skip_from_py_object)]
pub struct RoomsResult {
    /// `(entry, upto_from_now, upto_room_key)` for each room with an entry:
    /// the room's upto token is the request's now or since token with that
    /// room key. Taken by [`RoomsResult::take_entries`].
    pub(crate) entries: Mutex<Vec<(RoomSyncResultData, bool, RoomStreamToken)>>,
    /// The rooms Rust computed with nothing to send.
    pub(crate) empty: Vec<Arc<str>>,
    pub(crate) fallbacks: Vec<(Arc<str>, String)>,
    pub(crate) errors: Vec<(Arc<str>, String)>,
}

#[pymethods]
impl RoomsResult {
    /// The entries as `(RoomSyncResult, upto_from_now, upto_room_key)`. A
    /// second call returns an empty list.
    fn take_entries<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let entries = std::mem::take(&mut *lock_ignoring_poison(&self.entries));
        let items = entries
            .into_iter()
            .map(|(data, from_now, room_key)| {
                PyTuple::new(
                    py,
                    [
                        Bound::new(py, RoomSyncResult { data })?.into_any(),
                        PyBool::new(py, from_now).to_owned().into_any(),
                        room_key.into_pyobject(py)?.into_any(),
                    ],
                )
            })
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, items)
    }

    #[getter]
    fn empty<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        room_ids_to_py(py, &self.empty)
    }

    /// `(room_id, reason)` for each room Python must compute.
    #[getter]
    fn fallbacks(&self) -> Vec<(String, String)> {
        self.fallbacks
            .iter()
            .map(|(room_id, reason)| (room_id.to_string(), reason.clone()))
            .collect()
    }

    /// `(room_id, error)` for each room Rust failed on.
    #[getter]
    fn errors(&self) -> Vec<(String, String)> {
        self.errors
            .iter()
            .map(|(room_id, error)| (room_id.to_string(), error.clone()))
            .collect()
    }
}

impl RoomsResult {
    /// The result for `outcomes`, the rooms of `changes`.
    pub fn new(changes: &RoomChangesData, outcomes: Vec<(Arc<str>, RoomOutcome)>) -> Self {
        let mut entries = Vec::new();
        let mut empty = Vec::new();
        let mut fallbacks = Vec::new();
        let mut errors = Vec::new();
        for (room_id, outcome) in outcomes {
            match outcome {
                RoomOutcome::Entry(Some(data)) => {
                    let token = &changes.entries[changes.entry_index[&room_id]].upto;
                    entries.push((*data, token.base == TokenBase::Now, token.room_key.clone()));
                }
                RoomOutcome::Entry(None) => empty.push(room_id),
                RoomOutcome::Fallback(reason) => fallbacks.push((room_id, reason)),
                RoomOutcome::Error(error) => errors.push((room_id, error)),
            }
        }
        RoomsResult {
            entries: Mutex::new(entries),
            empty,
            fallbacks,
            errors,
        }
    }
}
