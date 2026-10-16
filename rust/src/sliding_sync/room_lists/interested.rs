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

//! `compute_interested_rooms` on the sliding sync tables
//! (`_compute_interested_rooms_new_tables`): the user's rooms at the
//! `to_token`, the lists over them, the room subscriptions, and which of
//! the rooms may have updates to send.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use anyhow::bail;
use indexmap::IndexMap;
use pyo3::{exceptions::PyKeyError, prelude::*};
use regex::Regex;

use crate::push::utils::{glob_to_regex, GlobMatchType};
use crate::sliding_sync::config::RoomSyncConfig;
use crate::sliding_sync::connection::PerConnectionState;
use crate::sliding_sync::request::SlidingSyncRequestData;
use crate::sliding_sync::room_lists::filter::{
    filter_membership_for_sync, filter_rooms_using_tables,
};
use crate::sliding_sync::room_lists::lists::{
    add_room_subscription, needs_sort, rooms_to_send, sort_limit, sync_list_ops, RelevantRoomMap,
    SyncOp,
};
use crate::sliding_sync::room_lists::sort::SortPlan;
use crate::sliding_sync::room_lists::RoomForUser;
use crate::sliding_sync::SlidingSyncStats;
use crate::storage::sliding_sync_store::{
    MembershipChange, RoomType, SlidingSyncReader, SnapshotRooms,
};
use crate::types::stream_token::RoomStreamToken;

/// `AccountDataTypes.DIRECT`.
const DIRECT: &str = "m.direct";
/// `AccountDataTypes.INVITE_PERMISSION_CONFIG` (MSC4380).
const INVITE_PERMISSION_CONFIG: &str = "m.invite_permission_config";
/// `AccountDataTypes.MSC4155_INVITE_PERMISSION_CONFIG`.
const MSC4155_INVITE_PERMISSION_CONFIG: &str = "org.matrix.msc4155.invite_permission_config";

/// What Python knows that the room lists need: the in-memory stream
/// change caches at the time of the request, and server config.
pub struct ListsSnapshot {
    /// Ours, for `is_mine_id`.
    pub server_name: Arc<str>,
    /// `msc4155_enabled`, for the invite rules.
    pub msc4155_enabled: bool,
    /// `NUM_ROOMS_THRESHOLD`.
    pub num_rooms_threshold: usize,
    /// `_membership_stream_cache.has_entity_changed(user_id, from.stream)`,
    /// true without a `from` token.
    pub membership_changed_since_from: bool,
    /// The same at the `to_token`'s stream.
    pub membership_changed_since_to: bool,
    /// `_events_stream_cache.get_all_entities_changed(from.stream)`.
    pub events_changed_since_from: Option<HashSet<Arc<str>>>,
    /// `get_room_max_token()`.
    pub room_max_token: RoomStreamToken,
    /// `_events_stream_cache`, read for the rooms a sort needs.
    pub events_stream_cache: Py<PyAny>,
}

/// `SlidingSyncInterestedRooms`.
#[derive(Debug, Default)]
pub struct InterestedRooms {
    /// Whether the request has any lists or room subscriptions.
    pub has_lists_or_subscriptions: bool,
    /// Each list's name, room count and operations, in the request's order.
    pub lists: Vec<(String, usize, Vec<SyncOp>)>,
    pub relevant_room_map: RelevantRoomMap,
    /// The rooms of `relevant_room_map` that may have updates, in its
    /// order.
    pub rooms_to_send: Vec<Arc<str>>,
    /// Every room that could appear in a list, in range or not.
    pub all_rooms: HashSet<Arc<str>>,
    /// The user's membership at the `to_token` of each relevant room.
    pub memberships: HashMap<Arc<str>, RoomForUser>,
    pub newly_joined: HashSet<Arc<str>>,
    pub newly_left: HashSet<Arc<str>>,
    pub dm_room_ids: HashSet<String>,
    /// The snapshot's `events_changed_since_from`, for the rooms out of
    /// range at the end of the request.
    pub events_changed_since_from: Option<HashSet<Arc<str>>>,
}

/// `MutableOverlayMapping` over the snapshot rooms: changes go to an
/// overlay, which iterates first, as Python's does.
struct OverlayMap {
    base: Arc<SnapshotRooms>,
    overlay: IndexMap<Arc<str>, RoomForUser>,
    deletions: HashSet<Arc<str>>,
}

impl OverlayMap {
    fn new(base: Arc<SnapshotRooms>) -> Self {
        OverlayMap {
            base,
            overlay: IndexMap::new(),
            deletions: HashSet::new(),
        }
    }

    fn get(&self, room_id: &str) -> Option<&RoomForUser> {
        if self.deletions.contains(room_id) {
            return None;
        }
        self.overlay.get(room_id).or_else(|| self.base.get(room_id))
    }

    fn contains_key(&self, room_id: &str) -> bool {
        self.get(room_id).is_some()
    }

    fn insert(&mut self, room: RoomForUser) {
        self.deletions.remove(&room.room_id);
        self.overlay.insert(Arc::clone(&room.room_id), room);
    }

    /// `pop(room_id, None)`.
    fn remove(&mut self, room_id: &str) {
        if !self.contains_key(room_id) {
            return;
        }
        // `dict.pop`, so a key set again goes to the end.
        self.overlay.shift_remove(room_id);
        self.deletions.insert(Arc::from(room_id));
    }

    fn values(&self) -> impl Iterator<Item = &RoomForUser> {
        let overlay = self
            .overlay
            .values()
            .filter(|room| !self.deletions.contains(&room.room_id));
        let base = self.base.values().filter(|room| {
            !self.deletions.contains(&room.room_id) && !self.overlay.contains_key(&room.room_id)
        });
        overlay.chain(base)
    }

    fn keys(&self) -> Vec<Arc<str>> {
        self.values()
            .map(|room| Arc::clone(&room.room_id))
            .collect()
    }
}

/// `InviteRule`, as far as the room lists read it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InviteRule {
    Allow,
    Ignore,
    Block,
}

/// `InviteRulesConfig`: the user's choices about which invites to accept.
#[derive(Debug, Default)]
pub enum InviteRules {
    /// `AllowAllInviteRulesConfig`.
    #[default]
    AllowAll,
    /// `MSC4380InviteRulesConfig`: one rule for every invite.
    Default(InviteRule),
    /// `MSC4155InviteRulesConfig`: globs over user IDs, then over servers,
    /// each in allow, ignore, block order.
    Msc4155 {
        users: Vec<(Vec<Regex>, InviteRule)>,
        servers: Vec<(Vec<Regex>, InviteRule)>,
    },
}

impl InviteRules {
    /// `get_invite_config_for_user`.
    pub async fn load(
        reader: &SlidingSyncReader<'_>,
        user_id: &str,
        msc4155_enabled: bool,
    ) -> anyhow::Result<Self> {
        if let Some(data) = reader
            .global_account_data_by_type(user_id, INVITE_PERMISSION_CONFIG)
            .await?
        {
            let rule = match data.get("default_action").and_then(|v| v.as_str()) {
                Some("block") => InviteRule::Block,
                _ => InviteRule::Allow,
            };
            return Ok(InviteRules::Default(rule));
        }
        if msc4155_enabled {
            if let Some(data) = reader
                .global_account_data_by_type(user_id, MSC4155_INVITE_PERMISSION_CONFIG)
                .await?
            {
                return Ok(Self::msc4155(&data));
            }
        }
        Ok(InviteRules::AllowAll)
    }

    /// `MSC4155InviteRulesConfig(account_data)`.
    pub fn msc4155(data: &serde_json::Value) -> Self {
        let field = |name: &str| -> Vec<Regex> {
            let Some(values) = data.get(name).and_then(|v| v.as_array()) else {
                return Vec::new();
            };
            values
                .iter()
                .filter_map(|value| value.as_str())
                // User IDs cannot exceed 255 bytes, so longer globs are
                // skipped, as are ones that do not compile.
                .filter(|glob| !glob.is_empty() && glob.len() <= 255)
                .filter_map(|glob| glob_to_regex(glob, GlobMatchType::Whole).ok())
                .collect()
        };
        InviteRules::Msc4155 {
            users: vec![
                (field("allowed_users"), InviteRule::Allow),
                (field("ignored_users"), InviteRule::Ignore),
                (field("blocked_users"), InviteRule::Block),
            ],
            servers: vec![
                (field("allowed_servers"), InviteRule::Allow),
                (field("ignored_servers"), InviteRule::Ignore),
                (field("blocked_servers"), InviteRule::Block),
            ],
        }
    }

    /// `get_invite_rule`.
    pub fn invite_rule(&self, inviter: &str) -> InviteRule {
        match self {
            InviteRules::AllowAll => InviteRule::Allow,
            InviteRules::Default(rule) => *rule,
            InviteRules::Msc4155 { users, servers } => {
                for (patterns, rule) in users {
                    if patterns.iter().any(|regex| regex.is_match(inviter)) {
                        return *rule;
                    }
                }
                let domain = inviter.split_once(':').map_or("", |(_, domain)| domain);
                for (patterns, rule) in servers {
                    if patterns.iter().any(|regex| regex.is_match(domain)) {
                        return *rule;
                    }
                }
                InviteRule::Allow
            }
        }
    }
}

/// `_get_dm_rooms_for_user`: the rooms of the user's `m.direct`, ignoring
/// whatever is not a list of strings.
fn dm_room_ids(direct: Option<&serde_json::Value>) -> HashSet<String> {
    let Some(serde_json::Value::Object(map)) = direct else {
        return HashSet::new();
    };
    map.values()
        .filter_map(|room_ids| room_ids.as_array())
        .flatten()
        .filter_map(|room_id| room_id.as_str())
        .map(str::to_owned)
        .collect()
}

/// A rewound membership with the room's state from `state`: Python's
/// `RoomsForUserSlidingSync(...)` "we keep the state of the room though".
fn rewound(change: &MembershipChange, state: &RoomForUser) -> RoomForUser {
    RoomForUser {
        room_id: Arc::clone(&change.room_id),
        sender: change.sender.clone(),
        membership: Arc::clone(&change.membership),
        event_id: change.event_id.clone(),
        event_pos: change.event_pos.clone(),
        room_version_id: change.room_version_id.clone(),
        has_known_state: state.has_known_state,
        room_type: state.room_type.clone(),
        is_encrypted: state.is_encrypted,
    }
}

/// Keep `room` if the sync covers it, else drop the room:
/// `filter_membership_for_sync` and the assignment or `pop` after it.
fn keep_if_covered(rooms: &mut OverlayMap, user_id: &str, room: RoomForUser, newly_left: bool) {
    if filter_membership_for_sync(user_id, &room, newly_left) {
        rooms.insert(room);
    } else {
        rooms.remove(&room.room_id);
    }
}

/// `_get_rewind_changes_to_current_membership_to_token`: the changes that
/// take the current memberships in `rooms` back to the `to_token`, by
/// room. `None` drops the room: the user joined it after the token, or the
/// previous membership is gone.
async fn rewind_changes(
    reader: &SlidingSyncReader<'_>,
    request: &SlidingSyncRequestData,
    rooms: &OverlayMap,
    membership_changed_since_to: bool,
) -> anyhow::Result<IndexMap<Arc<str>, Option<MembershipChange>>> {
    let mut changes = IndexMap::new();

    // The token the snapshot was read at: each writer's latest membership.
    let mut max_by_instance: IndexMap<Arc<str>, i64> = IndexMap::new();
    for room in rooms.values() {
        let max = max_by_instance
            .entry(Arc::clone(&room.event_pos.instance_name))
            .or_insert(room.event_pos.stream);
        *max = (*max).max(room.event_pos.stream);
    }
    let Some(min_stream) = max_by_instance.values().copied().min() else {
        return Ok(changes);
    };
    let snapshot_token = RoomStreamToken::with_instance_map(
        min_stream,
        max_by_instance
            .into_iter()
            .filter(|(_, stream)| *stream > min_stream)
            .collect(),
    )?;
    if snapshot_token.is_before_or_eq(&request.to_room_key) {
        return Ok(changes);
    }

    let deltas = reader
        .membership_delta_changes(
            &request.user_id,
            &request.to_room_key,
            &snapshot_token,
            &request.rooms_to_exclude,
            membership_changed_since_to,
        )
        .await?;

    // The first change after the token in each room.
    let mut first_changes = IndexMap::new();
    for delta in deltas {
        first_changes
            .entry(Arc::clone(&delta.room_id))
            .or_insert(delta);
    }

    for (room_id, first) in first_changes {
        if first.prev_event_id.is_none() {
            changes.insert(room_id, None);
            continue;
        }
        // The previous event may have been culled from the database.
        let (Some(prev_event_pos), Some(prev_membership), Some(prev_sender)) = (
            first.prev_event_pos,
            first.prev_membership,
            first.prev_sender,
        ) else {
            changes.insert(room_id, None);
            continue;
        };
        let room_version_id = match rooms.get(&room_id) {
            Some(current) => current.room_version_id.clone(),
            None => reader.room_version_id(&room_id).await?,
        };
        changes.insert(
            Arc::clone(&room_id),
            Some(MembershipChange {
                room_id,
                sender: Some(prev_sender),
                membership: prev_membership,
                event_id: first.prev_event_id,
                event_pos: prev_event_pos,
                room_version_id,
            }),
        );
    }
    Ok(changes)
}

/// The positions the events stream change cache has for `room_ids`, and
/// the earliest it knows: `get_max_pos_of_last_change` and
/// `get_earliest_known_position`, read under the GIL.
fn stream_cache_positions(
    cache: &Py<PyAny>,
    room_ids: &[Arc<str>],
) -> anyhow::Result<(i64, HashMap<Arc<str>, i64>)> {
    Python::attach(|py| -> PyResult<_> {
        let cache = cache.bind(py);
        let earliest: i64 = cache
            .call_method0(pyo3::intern!(py, "get_earliest_known_position"))?
            .extract()?;
        let method = cache.getattr(pyo3::intern!(py, "get_max_pos_of_last_change"))?;
        let mut positions = HashMap::with_capacity(room_ids.len());
        for room_id in room_ids {
            let position: Option<i64> = method.call1((&**room_id,))?.extract()?;
            if let Some(position) = position {
                positions.insert(Arc::clone(room_id), position);
            }
        }
        Ok((earliest, positions))
    })
    .map_err(|e| anyhow::anyhow!("reading the events stream cache: {e}"))
}

/// `sort_rooms`, with a `limit`.
async fn sort_rooms<'a>(
    reader: &SlidingSyncReader<'_>,
    rooms: &[&'a RoomForUser],
    to_token: &RoomStreamToken,
    limit: usize,
    snapshot: &ListsSnapshot,
) -> anyhow::Result<Vec<&'a RoomForUser>> {
    let joined = SortPlan::joined_rooms(rooms);
    let (earliest, positions) = stream_cache_positions(&snapshot.events_stream_cache, &joined)?;
    let mut plan = SortPlan::new(rooms, earliest, |room_id| positions.get(room_id).copied());
    for room_id in plan.rooms_to_recheck(Some(limit), to_token.stream) {
        let last = reader.last_event_before(&room_id, to_token).await?;
        plan.apply_recheck(&room_id, last.map(|(_, position)| position.stream));
    }
    if let Some(sorted) = plan.early_result(rooms, Some(limit)) {
        return Ok(sorted);
    }
    let joined_positions = reader
        .last_event_pos_before(&joined, to_token, &snapshot.room_max_token, &positions)
        .await?;
    plan.sort_all(rooms, &joined_positions)
}

/// `_compute_interested_rooms_new_tables`. Fails with
/// [`NotImplementedFilter`](crate::sliding_sync::room_lists::filter::NotImplementedFilter)
/// where Python raises `NotImplementedError`, and with
/// [`UnknownPosition`](crate::sliding_sync::room_lists::lists::UnknownPosition)
/// for a connection with too many rooms to catch up on.
pub async fn compute_interested_rooms(
    reader: &SlidingSyncReader<'_>,
    request: &SlidingSyncRequestData,
    previous: &PerConnectionState<String>,
    snapshot: ListsSnapshot,
    now_ms: i64,
) -> anyhow::Result<InterestedRooms> {
    let body = &request.body;
    let body_lists = body.lists.as_ref().filter(|lists| !lists.is_empty());
    let subscriptions = body
        .room_subscriptions
        .as_ref()
        .filter(|subscriptions| !subscriptions.is_empty());
    if body_lists.is_none() && subscriptions.is_none() {
        return Ok(InterestedRooms::default());
    }

    let user_id = &*request.user_id;
    let to_token = &request.to_room_key;

    // The current memberships, less the rooms the user left themselves,
    // plus those they left after the `to_token`, which the rewind may
    // bring back.
    let mut rooms = OverlayMap::new(reader.rooms_from_snapshots(user_id).await?);
    for (_, room) in reader.self_leave_rooms_after(user_id, to_token).await? {
        rooms.insert(room);
    }

    // Invites from ignored users. As in Python, the invite rules only
    // apply when the user ignores someone.
    let ignored_users = reader.ignored_users(user_id).await?;
    if !ignored_users.is_empty() {
        let invite_rules = InviteRules::load(reader, user_id, snapshot.msc4155_enabled).await?;
        for room_id in rooms.keys() {
            let Some(room) = rooms.get(&room_id) else {
                continue;
            };
            let ignored = &*room.membership == "invite"
                && room.sender.as_deref().is_some_and(|sender| {
                    !sender.is_empty()
                        && (ignored_users.contains(sender)
                            || invite_rules.invite_rule(sender) == InviteRule::Ignore)
                });
            if ignored {
                rooms.remove(&room_id);
            }
        }
    }

    let mut newly_joined = HashSet::new();
    let mut newly_left: IndexMap<Arc<str>, MembershipChange> = IndexMap::new();
    if let Some(from) = &request.from {
        let changes = reader
            .snapshot_membership_changes(
                user_id,
                &from.room_key,
                to_token,
                &request.rooms_to_exclude,
                snapshot.membership_changed_since_from,
            )
            .await?;
        for (room_id, change) in changes {
            match &*change.membership {
                "join" => {
                    newly_joined.insert(room_id);
                }
                "leave" => {
                    newly_left.insert(room_id, change);
                }
                _ => {}
            }
        }
    }

    let changes = rewind_changes(
        reader,
        request,
        &rooms,
        snapshot.membership_changed_since_to,
    )
    .await?;
    for (room_id, change) in &changes {
        let Some(change) = change else {
            rooms.remove(room_id);
            continue;
        };
        if let Some(existing) = rooms.get(room_id) {
            let room = rewound(change, existing);
            keep_if_covered(&mut rooms, user_id, room, newly_left.contains_key(room_id));
        }
    }

    // Add back the rooms left in the token range, which the snapshots
    // leave out when the user left themselves.
    let missing_newly_left: Vec<Arc<str>> = newly_left
        .keys()
        .filter(|room_id| !rooms.contains_key(room_id))
        .cloned()
        .collect();
    for room_id in missing_newly_left {
        let newly_left_room = &newly_left[&room_id];
        if &*newly_left_room.membership != "leave" {
            bail!(
                "newly left room {room_id} has membership {}",
                newly_left_room.membership
            );
        }
        match reader.snapshot_room(user_id, &room_id).await? {
            // A normal leave.
            Some(snapshot_room) => {
                keep_if_covered(&mut rooms, user_id, snapshot_room.clone(), true);
                if let Some(Some(change)) = changes.get(&room_id) {
                    let room = rewound(change, &snapshot_room);
                    keep_if_covered(&mut rooms, user_id, room, true);
                }
            }
            // No membership: a state reset out of the room, whose state
            // the tables no longer hold.
            None => {
                let (has_known_state, room_type) = match reader.room_type(&room_id).await? {
                    RoomType::Unknown => (false, None),
                    RoomType::Known(room_type) => (true, room_type),
                };
                let at = RoomStreamToken::live(newly_left_room.event_pos.stream);
                let is_encrypted = reader
                    .is_encrypted_at(&room_id, &at, &snapshot.room_max_token)
                    .await?;
                let room = RoomForUser {
                    room_id: Arc::clone(&room_id),
                    sender: newly_left_room.sender.clone(),
                    membership: Arc::clone(&newly_left_room.membership),
                    event_id: newly_left_room.event_id.clone(),
                    event_pos: newly_left_room.event_pos.clone(),
                    room_version_id: newly_left_room.room_version_id.clone(),
                    has_known_state,
                    room_type,
                    is_encrypted,
                };
                keep_if_covered(&mut rooms, user_id, room, true);
            }
        }
    }

    for room_id in request.rooms_to_exclude.iter() {
        rooms.remove(room_id);
    }

    let direct = reader.global_account_data_by_type(user_id, DIRECT).await?;
    let dm_room_ids = dm_room_ids(direct.as_deref());

    let mut lists = Vec::new();
    let mut relevant_room_map = RelevantRoomMap::new();
    let mut all_rooms: HashSet<Arc<str>> = HashSet::new();

    if let Some(body_lists) = body_lists {
        let sync_rooms: Vec<&RoomForUser> = rooms.values().collect();
        let mut room_tags: Option<HashMap<String, HashSet<String>>> = None;
        for (list_key, list) in body_lists.iter() {
            let mut filtered = match &list.filters {
                Some(filters) => {
                    if (filters.tags.is_some() || filters.not_tags.is_some()) && room_tags.is_none()
                    {
                        let tags = reader.tags_for_user(user_id).await?;
                        room_tags = Some(
                            tags.iter()
                                .map(|(room_id, tags)| {
                                    (room_id.to_string(), tags.keys().cloned().collect())
                                })
                                .collect(),
                        );
                    }
                    let no_tags = HashMap::new();
                    filter_rooms_using_tables(
                        &sync_rooms,
                        filters,
                        &dm_room_ids,
                        room_tags.as_ref().unwrap_or(&no_tags),
                        |room_id| !previous.rooms.have_sent_room(room_id).is_never(),
                    )?
                }
                None => sync_rooms.clone(),
            };

            let partial_state_rooms = reader.partial_rooms().await?;
            let room_sync_config =
                RoomSyncConfig::from_room_config(list.timeline_limit, &list.required_state);
            if room_sync_config.must_await_full_state(&snapshot.server_name)? {
                filtered.retain(|room| !partial_state_rooms.contains(&room.room_id));
            }

            all_rooms.extend(filtered.iter().map(|room| Arc::clone(&room.room_id)));

            let mut ops = Vec::new();
            if let Some(ranges) = list.ranges.as_deref().filter(|ranges| !ranges.is_empty()) {
                let sorted = if needs_sort(ranges, filtered.len()) {
                    sort_rooms(reader, &filtered, to_token, sort_limit(ranges), &snapshot).await?
                } else {
                    filtered.clone()
                };
                let sorted_ids: Vec<Arc<str>> = sorted
                    .iter()
                    .map(|room| Arc::clone(&room.room_id))
                    .collect();
                ops = sync_list_ops(
                    ranges,
                    &sorted_ids,
                    &room_sync_config,
                    &mut relevant_room_map,
                );
            }
            lists.push((list_key.to_owned(), filtered.len(), ops));
        }
    }

    if let Some(subscriptions) = subscriptions {
        let partial_state_rooms = reader.partial_rooms().await?;
        let missing: Vec<Arc<str>> = subscriptions
            .iter()
            .map(|(room_id, _)| room_id)
            .filter(|room_id| !rooms.contains_key(room_id))
            .map(Arc::from)
            .collect();
        for (_, room) in reader.snapshot_rooms(user_id, &missing).await? {
            rooms.insert(room);
        }
        for (room_id, subscription) in subscriptions.iter() {
            // TODO, as in Python: rooms the user is not in.
            if !rooms.contains_key(room_id) {
                continue;
            }
            let room_id: Arc<str> = Arc::from(room_id);
            all_rooms.insert(Arc::clone(&room_id));
            let room_sync_config = RoomSyncConfig::from_room_config(
                subscription.timeline_limit,
                &subscription.required_state,
            );
            if room_sync_config.must_await_full_state(&snapshot.server_name)?
                && partial_state_rooms.contains(&room_id)
            {
                continue;
            }
            add_room_subscription(&mut relevant_room_map, &room_id, room_sync_config);
        }
    }

    let rooms_to_send = match &request.from {
        Some(from) if !relevant_room_map.is_empty() => {
            let relevant: Vec<Arc<str>> = relevant_room_map.keys().cloned().collect();
            let rooms_that_have_updates = reader
                .rooms_with_updates_since(
                    &relevant,
                    &from.room_key,
                    snapshot.events_changed_since_from.as_ref(),
                )
                .await?;
            rooms_to_send(
                &relevant_room_map,
                previous,
                &rooms_that_have_updates,
                snapshot.num_rooms_threshold,
                now_ms,
            )?
        }
        _ => relevant_room_map.keys().cloned().collect(),
    };

    let memberships = relevant_room_map
        .keys()
        .filter_map(|room_id| rooms.get(room_id))
        .map(|room| (Arc::clone(&room.room_id), room.clone()))
        .collect();

    Ok(InterestedRooms {
        has_lists_or_subscriptions: true,
        lists,
        relevant_room_map,
        rooms_to_send,
        all_rooms,
        memberships,
        newly_joined,
        newly_left: newly_left.into_keys().collect(),
        dm_room_ids,
        events_changed_since_from: snapshot.events_changed_since_from,
    })
}

/// A room's `RoomsForUserSlidingSync` for Python: `(sender, membership,
/// event_id, instance_name, stream, room_version_id, has_known_state,
/// room_type, is_encrypted)`.
type PyMembership<'a> = (
    Option<&'a str>,
    &'a str,
    Option<&'a str>,
    &'a str,
    i64,
    Option<&'a str>,
    bool,
    Option<&'a str>,
    bool,
);

/// One room's input to Python's `get_room_sync_data`: `(room_id,
/// (timeline_limit, required_state_map), membership, newly_joined,
/// newly_left, is_dm)`.
type PyRoomInput<'a> = (
    &'a str,
    (i64, BTreeMap<String, BTreeSet<String>>),
    PyMembership<'a>,
    bool,
    bool,
    bool,
);

/// `SlidingSyncInterestedRooms`, as Python reads it.
#[pyclass(frozen, skip_from_py_object, name = "InterestedRooms")]
pub struct PyInterestedRooms {
    pub(crate) data: Arc<InterestedRooms>,
    pub(crate) stats: SlidingSyncStats,
}

#[pymethods]
impl PyInterestedRooms {
    /// `(name, count, [((start, end), room_ids)])` for each list, in the
    /// request's order.
    #[getter]
    #[allow(clippy::type_complexity)]
    fn lists(&self) -> Vec<(&str, usize, Vec<((u64, u64), Vec<&str>)>)> {
        self.data
            .lists
            .iter()
            .map(|(name, count, ops)| {
                let ops = ops
                    .iter()
                    .map(|op| (op.range, op.room_ids.iter().map(|r| &**r).collect()))
                    .collect();
                (name.as_str(), *count, ops)
            })
            .collect()
    }

    /// The keys of `relevant_room_map`.
    #[getter]
    fn relevant_room_ids(&self) -> Vec<&str> {
        self.data.relevant_room_map.keys().map(|r| &**r).collect()
    }

    /// The keys of `relevant_rooms_to_send_map`.
    #[getter]
    fn rooms_to_send(&self) -> Vec<&str> {
        self.data.rooms_to_send.iter().map(|r| &**r).collect()
    }

    #[getter]
    fn all_rooms(&self) -> HashSet<&str> {
        self.data.all_rooms.iter().map(|r| &**r).collect()
    }

    #[getter]
    fn stats(&self) -> SlidingSyncStats {
        self.stats
    }

    /// What Python's `get_room_sync_data` takes for each of `room_ids`, for
    /// the rooms Rust leaves to Python.
    fn python_room_inputs<'a>(&'a self, room_ids: Vec<String>) -> PyResult<Vec<PyRoomInput<'a>>> {
        let data = &*self.data;
        room_ids
            .iter()
            .map(|room_id| {
                let (room_id, config) = data
                    .relevant_room_map
                    .get_key_value(room_id.as_str())
                    .ok_or_else(|| PyKeyError::new_err(room_id.clone()))?;
                let room = data
                    .memberships
                    .get(room_id)
                    .ok_or_else(|| PyKeyError::new_err(room_id.to_string()))?;
                let membership = (
                    room.sender.as_deref(),
                    &*room.membership,
                    room.event_id.as_deref(),
                    &*room.event_pos.instance_name,
                    room.event_pos.stream,
                    room.room_version_id.as_deref(),
                    room.has_known_state,
                    room.room_type.as_deref(),
                    room.is_encrypted,
                );
                Ok((
                    &**room_id,
                    (config.timeline_limit, config.required_state_map.clone()),
                    membership,
                    data.newly_joined.contains(room_id),
                    data.newly_left.contains(room_id),
                    data.dm_room_ids.contains(&**room_id),
                ))
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::sync_store::PersistedEventPosition;

    fn room(room_id: &str, membership: &str) -> RoomForUser {
        RoomForUser {
            room_id: Arc::from(room_id),
            sender: Some(Arc::from("@u:test")),
            membership: Arc::from(membership),
            event_id: Some(Arc::from("$e")),
            event_pos: PersistedEventPosition {
                instance_name: Arc::from("master"),
                stream: 1,
            },
            room_version_id: Some(Arc::from("10")),
            has_known_state: true,
            room_type: None,
            is_encrypted: false,
        }
    }

    #[test]
    fn overlay_iterates_changes_first() {
        let base: SnapshotRooms = ["!a", "!b", "!c"]
            .into_iter()
            .map(|id| (Arc::from(id), room(id, "join")))
            .collect();
        let mut rooms = OverlayMap::new(Arc::new(base));
        rooms.insert(room("!b", "leave"));
        rooms.remove("!a");
        rooms.insert(room("!d", "join"));
        let ids: Vec<String> = rooms.keys().iter().map(|id| id.to_string()).collect();
        assert_eq!(ids, ["!b", "!d", "!c"]);
        assert_eq!(&*rooms.get("!b").unwrap().membership, "leave");
        assert!(rooms.get("!a").is_none());
        // Set again after a pop, the room goes to the end.
        rooms.remove("!b");
        rooms.insert(room("!b", "join"));
        let ids: Vec<String> = rooms.keys().iter().map(|id| id.to_string()).collect();
        assert_eq!(ids, ["!d", "!b", "!c"]);
    }

    #[test]
    fn msc4155_rules() {
        let rules = InviteRules::msc4155(&serde_json::json!({
            "allowed_users": ["@friend:evil.example"],
            "ignored_servers": ["*.example"],
            "blocked_users": [5, "@bad:*"],
        }));
        assert_eq!(rules.invite_rule("@friend:evil.example"), InviteRule::Allow);
        assert_eq!(rules.invite_rule("@other:evil.example"), InviteRule::Ignore);
        assert_eq!(rules.invite_rule("@bad:good.org"), InviteRule::Block);
        assert_eq!(rules.invite_rule("@ok:good.org"), InviteRule::Allow);
    }

    #[test]
    fn dm_rooms_ignore_bad_shapes() {
        let direct = serde_json::json!({"@a:x": ["!1", 2, "!2"], "@b:x": "!3"});
        let mut rooms: Vec<String> = dm_room_ids(Some(&direct)).into_iter().collect();
        rooms.sort();
        assert_eq!(rooms, ["!1", "!2"]);
        assert!(dm_room_ids(Some(&serde_json::json!([]))).is_empty());
    }
}
