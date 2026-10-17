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

//! The room's new current state after a chunk of events: ports of
//! `_get_new_state_after_events`, `_calculate_state_delta`,
//! `_is_server_still_joined` and the two steps that call them with the
//! extremity calculations, `_calculate_new_forward_extremities_and_state_delta`
//! and `_process_state_dag_forward_extremities_and_state_delta`, from
//! `synapse/storage/controllers/persist_events.py`.
//!
//! The reads go through [`StateDeltaReads`], and state resolution through a
//! [`StateResolver`], so that the unit tests can fake both.
//!
//! State resolution always runs in Rust, with the conflict cache of the
//! Rust state resolution handler. It differs from the Python
//! `StateResolutionHandler.resolve_state_groups` in these ways:
//!
//! - There is no `_state_cache` of earlier resolutions by group set and no
//!   linearizer. Every call resolves, through the conflict cache, and checks
//!   and bumps the input groups as a cache miss does in Python.
//! - Rooms whose state resolution is v1, and rooms without a chain cover
//!   index, fail with [`StateResFallback`]. The caller should run the
//!   Python step instead.
//! - When two input groups have the same state, the one the pruning keeps
//!   is the lowest group ID. Python takes the first in its set's order.
//! - [`NativeStateResolver`] reads and bumps the state groups on the main
//!   database's pool. Python uses the state database. Like the Rust state
//!   resolution store, this needs the two to be the same database, which
//!   `Persister::for_homeserver` checks.
//!
//! A step can fail with [`StateResFallback`] after it has added to the
//! caller's [`PersistCounts`] and bumped the input groups. A caller that
//! falls back to Python should drop those counts, or the chunk is counted
//! twice. The second bump is harmless.

use std::{
    collections::{HashMap, HashSet},
    fmt,
    sync::Arc,
};

use anyhow::{anyhow, bail};
use async_trait::async_trait;
use futures::FutureExt;
use log::info;
use tokio::runtime::Handle;

use super::{
    extremities::{
        calculate_new_extremities, calculate_new_state_dag_extremities, is_mine_id,
        prune_extremities, ExtremityReads, NewEvent,
    },
    metrics::PersistCounts,
    reader::PersistReader,
};
use crate::{
    clock::Clock,
    database::postgres::{
        pool::ConnectionPool,
        write::{run_write_interaction, WriteError},
    },
    room_versions::{RoomVersion, StateResolutionVersions},
    state::{
        matching_group, resolve_on_pool, store::NoChainCoverIndex, v2, ConflictCache, EventMap,
        StateKey, StateMap, StateResStats,
    },
    storage::caches::StoreCaches,
    types::state_filter::StateFilter,
};

/// `EventTypes.Member`.
const MEMBER: &str = "m.room.member";
/// `EventTypes.Create`.
const CREATE: &str = "m.room.create";

/// `DeltaState`: how the room's current state changes.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DeltaState {
    /// The keys to remove, in key order.
    pub to_delete: Vec<StateKey>,
    /// The entries to add or replace.
    pub to_insert: StateMap,
    /// The server has left the room, so its current state goes.
    pub no_longer_in_room: bool,
}

/// What `_get_new_state_after_events` returns. If both state maps are
/// `None`, the state has not changed.
#[derive(Debug, Default)]
pub struct NewState {
    /// The full new current state, if it was worked out.
    pub current_state: Option<StateMap>,
    /// The change to the current state, if the contexts knew it.
    pub delta_ids: Option<StateMap>,
    /// The new forward extremities, possibly pruned.
    pub new_latest_event_ids: HashSet<Arc<str>>,
    /// What the state resolution recorded, if one ran.
    pub state_res_stats: Option<StateResStats>,
}

/// What the two orchestrating steps return: the inputs of the write
/// transaction.
#[derive(Debug, Default)]
pub struct StateAndExtremities {
    /// The room's new forward extremities, or `None` if they do not change.
    pub new_forward_extremities: Option<HashSet<Arc<str>>>,
    /// The change to the room's current state, if any.
    pub state_delta: Option<DeltaState>,
    /// The new MSC4242 state DAG forward extremities. Only for a state DAG
    /// room.
    pub new_state_dag_extremities: Option<HashSet<Arc<str>>>,
    /// What the state resolution recorded, if one ran.
    pub state_res_stats: Option<StateResStats>,
}

/// The Rust state resolution cannot resolve this room: the caller should
/// run the Python step.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StateResFallback(pub String);

impl fmt::Display for StateResFallback {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "the Rust state resolution cannot resolve this: {}",
            self.0
        )
    }
}

impl std::error::Error for StateResFallback {}

/// The reads the state delta makes, on top of the extremity reads.
#[async_trait]
pub trait StateDeltaReads: ExtremityReads {
    /// See [`PersistReader::latest_event_ids_in_room`].
    async fn latest_event_ids_in_room(&self, room_id: &str) -> anyhow::Result<HashSet<Arc<str>>>;

    /// See [`PersistReader::state_dag_extremities`].
    async fn state_dag_extremities(&self, room_id: &str) -> anyhow::Result<HashSet<Arc<str>>>;

    /// `_get_state_group_for_events`: the state group of each event that
    /// has one.
    async fn state_group_for_events(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, i64>>;

    /// `_get_state_for_groups`: the full state of each group.
    async fn state_for_groups(&self, groups: &[i64])
        -> anyhow::Result<HashMap<i64, Arc<StateMap>>>;

    /// See [`PersistReader::room_version_id`].
    async fn room_version_id(&self, room_id: &str) -> anyhow::Result<Option<Arc<str>>>;

    /// `get_partial_current_state_ids`: the room's current state, partial
    /// or not.
    async fn current_state_ids(&self, room_id: &str) -> anyhow::Result<StateMap>;

    /// `get_membership_from_event_ids`: the membership of each of the
    /// events that is a membership event.
    async fn membership_from_event_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, Arc<str>>>;

    /// See [`PersistReader::is_local_host_in_room_ignoring_users`].
    async fn is_local_host_in_room_ignoring_users(
        &self,
        room_id: &str,
        ignore_users: &[Arc<str>],
    ) -> anyhow::Result<bool>;
}

#[async_trait]
impl StateDeltaReads for PersistReader<'_> {
    async fn latest_event_ids_in_room(&self, room_id: &str) -> anyhow::Result<HashSet<Arc<str>>> {
        PersistReader::latest_event_ids_in_room(self, room_id).await
    }

    async fn state_dag_extremities(&self, room_id: &str) -> anyhow::Result<HashSet<Arc<str>>> {
        PersistReader::state_dag_extremities(self, room_id).await
    }

    async fn state_group_for_events(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
        (**self).state_group_for_events(event_ids).await
    }

    async fn state_for_groups(
        &self,
        groups: &[i64],
    ) -> anyhow::Result<HashMap<i64, Arc<StateMap>>> {
        (**self).state_for_groups(groups).await
    }

    async fn room_version_id(&self, room_id: &str) -> anyhow::Result<Option<Arc<str>>> {
        PersistReader::room_version_id(self, room_id).await
    }

    async fn current_state_ids(&self, room_id: &str) -> anyhow::Result<StateMap> {
        (**self)
            .current_state_ids(room_id, &StateFilter::all())
            .await
    }

    async fn membership_from_event_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashMap<Arc<str>, Arc<str>>> {
        let memberships = PersistReader::membership_from_event_ids(self, event_ids).await?;
        Ok(memberships
            .into_iter()
            .map(|(event_id, membership)| (event_id, membership.membership))
            .collect())
    }

    async fn is_local_host_in_room_ignoring_users(
        &self,
        room_id: &str,
        ignore_users: &[Arc<str>],
    ) -> anyhow::Result<bool> {
        PersistReader::is_local_host_in_room_ignoring_users(self, room_id, ignore_users).await
    }
}

/// What the state delta needs from state resolution.
#[async_trait]
pub trait StateResolver: Sync {
    /// `check_state_groups_and_bump_deletion`: those of the groups that do
    /// not exist. The deletion of the others, if pending, is put off.
    async fn check_state_groups_and_bump_deletion(
        &self,
        groups: &[i64],
    ) -> anyhow::Result<Vec<i64>>;

    /// Resolve the state sets. `event_map` holds the events being
    /// persisted. Fails with [`StateResFallback`] if the Rust state
    /// resolution cannot resolve the room.
    async fn resolve(
        &self,
        room_id: &str,
        room_version: &'static RoomVersion,
        state_sets: &[Arc<StateMap>],
        event_map: &EventMap,
    ) -> anyhow::Result<(StateMap, StateResStats)>;
}

/// The [`StateResolver`] over the native pool.
pub struct NativeStateResolver {
    pub pool: ConnectionPool,
    pub handle: Handle,
    pub caches: Arc<StoreCaches>,
    pub conflict_cache: Arc<ConflictCache>,
}

#[async_trait]
impl StateResolver for NativeStateResolver {
    async fn check_state_groups_and_bump_deletion(
        &self,
        groups: &[i64],
    ) -> anyhow::Result<Vec<i64>> {
        let existing = run_write_interaction(
            &self.pool,
            "check_state_groups_and_bump_deletion",
            |txn, _| {
                async move {
                    // Read on each attempt, as Python reads the clock inside
                    // the transaction.
                    let now_ms = Clock::new().now_millis() as i64;
                    let rows = txn
                        .query("SELECT id FROM state_groups WHERE id = ANY($1)", &[&groups])
                        .await?;
                    let existing = rows
                        .iter()
                        .map(|row| row.try_get::<_, i64>(0))
                        .collect::<Result<Vec<i64>, _>>()
                        .map_err(|e| WriteError::Other(e.into()))?;
                    if !existing.is_empty() {
                        txn.execute(
                            "UPDATE state_groups_pending_deletion
                            SET sequence_number = DEFAULT, insertion_ts = $1
                            WHERE state_group = ANY($2)",
                            &[&now_ms, &existing],
                        )
                        .await?;
                    }
                    Ok(existing)
                }
                .boxed()
            },
        )
        .await?;
        let existing: HashSet<i64> = existing.into_iter().collect();
        Ok(groups
            .iter()
            .copied()
            .filter(|group| !existing.contains(group))
            .collect())
    }

    async fn resolve(
        &self,
        room_id: &str,
        room_version: &'static RoomVersion,
        state_sets: &[Arc<StateMap>],
        event_map: &EventMap,
    ) -> anyhow::Result<(StateMap, StateResStats)> {
        if room_version.state_res == StateResolutionVersions::V1 {
            return Err(v1_fallback(room_version));
        }
        let (unconflicted, conflicted) = v2::separate(state_sets);
        if conflicted.is_empty() {
            return Ok((unconflicted, StateResStats::default()));
        }
        let output = resolve_on_pool(
            &self.pool,
            self.handle.clone(),
            Arc::clone(&self.caches),
            &self.conflict_cache,
            room_id,
            room_version,
            state_sets,
            (unconflicted, conflicted),
            event_map.clone(),
            StateResStats::default(),
        )
        .await
        .map_err(|e| match e.downcast::<NoChainCoverIndex>() {
            Ok(no_index) => StateResFallback(no_index.to_string()).into(),
            Err(e) => e,
        })?;
        Ok((output.state, output.stats))
    }
}

/// The [`StateResFallback`] for a room on state resolution v1.
fn v1_fallback(room_version: &RoomVersion) -> anyhow::Error {
    StateResFallback(format!(
        "room version {} uses state resolution v1",
        room_version.identifier
    ))
    .into()
}

/// Look up the state group of each event, failing for one without, as
/// Python's dict lookup does.
fn groups_of<'a>(
    event_ids: impl IntoIterator<Item = &'a Arc<str>>,
    event_id_to_state_group: &HashMap<Arc<str>, i64>,
) -> anyhow::Result<HashSet<i64>> {
    event_ids
        .into_iter()
        .map(|event_id| {
            event_id_to_state_group
                .get(event_id)
                .copied()
                .ok_or_else(|| anyhow!("no state group for {event_id}"))
        })
        .collect()
}

/// `_get_new_state_after_events`: the room's state after `events`, given
/// that its forward extremities go from `old_latest_event_ids` to
/// `new_latest_event_ids`.
///
/// If the extremities' state groups do not change, both state maps are
/// `None`. If a context knows the delta between the old and new group, that
/// comes back as `delta_ids`. Otherwise the full state comes back, resolved
/// if there are several new groups. After a resolution whose result is one
/// of the new groups, and if `should_prune`, the extremities in other groups
/// may be pruned: see [`prune_extremities`].
#[allow(clippy::too_many_arguments)]
pub async fn get_new_state_after_events(
    reads: &dyn StateDeltaReads,
    resolver: &dyn StateResolver,
    room_id: &str,
    server_name: &str,
    events: &[NewEvent],
    event_map: &EventMap,
    old_latest_event_ids: &HashSet<Arc<str>>,
    new_latest_event_ids: HashSet<Arc<str>>,
    should_prune: bool,
    now_ms: i64,
    counts: &mut PersistCounts,
) -> anyhow::Result<NewState> {
    // The deltas the contexts know, from (prev group, new group). A later
    // event's replaces an earlier one's.
    let mut state_group_deltas: HashMap<(i64, i64), &StateMap> = HashMap::new();
    for event in events {
        // Python's `context.state_group` raises for a rejected event.
        if event.rejected {
            bail!("Attempt to access state_group of rejected event");
        }
        if event.state_group.is_none() {
            // This should only happen for outlier events.
            if !event.outlier {
                bail!(
                    "Context for new event {} has no state group",
                    event.event_id
                );
            }
            continue;
        }
        state_group_deltas.extend(event.state_group_deltas.iter().map(|(k, v)| (*k, v)));
    }

    // The state group of each extremity: from the chunk if the event is
    // being persisted, and otherwise from the database. None of the old
    // extremities are in the chunk.
    let mut missing: HashSet<Arc<str>> = old_latest_event_ids.clone();
    let mut event_id_to_state_group: HashMap<Arc<str>, i64> = HashMap::new();
    for event_id in &new_latest_event_ids {
        let in_chunk = events
            .iter()
            .find_map(|event| event.state_group.filter(|_| event.event_id == *event_id));
        match in_chunk {
            Some(group) => {
                event_id_to_state_group.insert(Arc::clone(event_id), group);
            }
            None => {
                missing.insert(Arc::clone(event_id));
            }
        }
    }
    if !missing.is_empty() {
        let missing: Vec<Arc<str>> = missing.into_iter().collect();
        event_id_to_state_group.extend(reads.state_group_for_events(&missing).await?);
    }

    let old_state_groups = groups_of(old_latest_event_ids, &event_id_to_state_group)?;
    let new_state_groups = groups_of(&new_latest_event_ids, &event_id_to_state_group)?;

    if old_state_groups == new_state_groups {
        return Ok(NewState {
            new_latest_event_ids,
            ..NewState::default()
        });
    }

    if new_state_groups.len() == 1 && old_state_groups.len() == 1 {
        // Going from one group to another: a context may know the delta.
        let new_group = *new_state_groups.iter().next().expect("one group");
        let old_group = *old_state_groups.iter().next().expect("one group");
        if let Some(delta) = state_group_deltas.get(&(old_group, new_group)) {
            return Ok(NewState {
                delta_ids: Some((*delta).clone()),
                new_latest_event_ids,
                ..NewState::default()
            });
        }
    }

    let mut new_groups: Vec<i64> = new_state_groups.iter().copied().collect();
    new_groups.sort_unstable();
    let mut state_groups_map = reads.state_for_groups(&new_groups).await?;

    if new_groups.len() == 1 {
        let state = state_groups_map.remove(&new_groups[0]).unwrap_or_default();
        return Ok(NewState {
            current_state: Some((*state).clone()),
            new_latest_event_ids,
            ..NewState::default()
        });
    }

    // Several groups: resolve them. The room version is in the create
    // event, which may be in the chunk.
    let room_version = match events
        .iter()
        .find(|event| &*event.event_type == CREATE && event.state_key.as_deref() == Some(""))
    {
        Some(create) => create.create_room_version.clone(),
        None => None,
    };
    let room_version = match room_version {
        Some(version) => version,
        None => reads
            .room_version_id(room_id)
            .await?
            .ok_or_else(|| anyhow!("Room {room_id} not found"))?,
    };
    let room_version: &'static RoomVersion = room_version
        .parse()
        .map_err(|e| anyhow!("{e}: {room_version:?}"))?;
    if room_version.state_res == StateResolutionVersions::V1 {
        return Err(v1_fallback(room_version));
    }

    let groups: Vec<(i64, Arc<StateMap>)> = new_groups
        .iter()
        .map(|group| {
            let state = state_groups_map.get(group).cloned().unwrap_or_default();
            (*group, state)
        })
        .collect();

    info!("Resolving state for {room_id} with groups {new_groups:?}");
    let missing_groups = resolver
        .check_state_groups_and_bump_deletion(&new_groups)
        .await?;
    if !missing_groups.is_empty() {
        bail!("State groups have been deleted: {missing_groups:?}");
    }
    let state_sets: Vec<Arc<StateMap>> =
        groups.iter().map(|(_, state)| Arc::clone(state)).collect();
    let (state, stats) = resolver
        .resolve(room_id, room_version, &state_sets, event_map)
        .await?;
    counts.state_resolutions += 1;

    // If the resolved state is one of the new groups, the extremities in
    // the others may be stuck ones to prune.
    let mut new_latest_event_ids = new_latest_event_ids;
    if should_prune {
        if let Some(resolved_group) = matching_group(&state, &groups) {
            new_latest_event_ids = prune_extremities(
                reads,
                room_id,
                server_name,
                new_latest_event_ids,
                resolved_group,
                &event_id_to_state_group,
                events,
                now_ms,
                counts,
            )
            .await?;
        }
    }

    Ok(NewState {
        current_state: Some(state),
        delta_ids: None,
        new_latest_event_ids,
        state_res_stats: Some(stats),
    })
}

/// `_calculate_state_delta`: the change from the room's current state to
/// `current_state`.
pub async fn calculate_state_delta(
    reads: &dyn StateDeltaReads,
    room_id: &str,
    current_state: &StateMap,
) -> anyhow::Result<DeltaState> {
    let existing_state = reads.current_state_ids(room_id).await?;

    let mut to_delete: Vec<StateKey> = existing_state
        .keys()
        .filter(|key| !current_state.contains_key(*key))
        .cloned()
        .collect();
    to_delete.sort_unstable();

    let to_insert: StateMap = current_state
        .iter()
        .filter(|(key, event_id)| existing_state.get(*key) != Some(event_id))
        .map(|(key, event_id)| (key.clone(), Arc::clone(event_id)))
        .collect();

    Ok(DeltaState {
        to_delete,
        to_insert,
        no_longer_in_room: false,
    })
}

/// `_is_server_still_joined`: whether a local user is still joined after
/// `delta`.
pub async fn is_server_still_joined(
    reads: &dyn StateDeltaReads,
    room_id: &str,
    server_name: &str,
    events: &[NewEvent],
    delta: &DeltaState,
) -> anyhow::Result<bool> {
    let is_local_member = |key: &StateKey| &*key.0 == MEMBER && is_mine_id(&key.1, server_name);

    if !delta
        .to_delete
        .iter()
        .chain(delta.to_insert.keys())
        .any(is_local_member)
    {
        // No local user's membership changed.
        return Ok(true);
    }

    // A local join in the new state, among the events being persisted or
    // in the database.
    let mut events_to_check = Vec::new();
    for (key, event_id) in &delta.to_insert {
        if !is_local_member(key) {
            continue;
        }
        for event in events {
            if *event_id == event.event_id && event.membership.as_deref() == Some("join") {
                return Ok(true);
            }
        }
        // Python reads the event from the database whether or not it was in
        // the chunk.
        events_to_check.push(Arc::clone(event_id));
    }
    if !events_to_check.is_empty() {
        let memberships = reads.membership_from_event_ids(&events_to_check).await?;
        if memberships
            .values()
            .any(|membership| &**membership == "join")
        {
            return Ok(true);
        }
    }

    // Otherwise, any other local user still joined. The users whose state
    // changed are covered above.
    let users_to_ignore: Vec<Arc<str>> = delta
        .to_insert
        .keys()
        .chain(delta.to_delete.iter())
        .filter(|key| is_local_member(key))
        .map(|key| Arc::clone(&key.1))
        .collect();
    reads
        .is_local_host_in_room_ignoring_users(room_id, &users_to_ignore)
        .await
}

/// The delta from what [`get_new_state_after_events`] found, with
/// `no_longer_in_room` set if the last local user left.
async fn delta_from_new_state(
    reads: &dyn StateDeltaReads,
    room_id: &str,
    server_name: &str,
    events: &[NewEvent],
    new_state: NewState,
) -> anyhow::Result<Option<DeltaState>> {
    let mut delta = if let Some(delta_ids) = new_state.delta_ids {
        // A known delta only adds or replaces state.
        DeltaState {
            to_insert: delta_ids,
            ..DeltaState::default()
        }
    } else if let Some(current_state) = &new_state.current_state {
        calculate_state_delta(reads, room_id, current_state).await?
    } else {
        return Ok(None);
    };

    if !is_server_still_joined(reads, room_id, server_name, events, &delta).await? {
        info!("Server no longer in room {room_id}");
        delta.no_longer_in_room = true;
    }
    Ok(Some(delta))
}

/// `_calculate_new_forward_extremities_and_state_delta`: the new forward
/// extremities and the change to the current state after `events`.
///
/// Fails if no forward extremity is left, where Python's `assert` raises
/// an `AssertionError`.
#[allow(clippy::too_many_arguments)]
pub async fn calculate_new_forward_extremities_and_state_delta(
    reads: &dyn StateDeltaReads,
    resolver: &dyn StateResolver,
    room_id: &str,
    server_name: &str,
    events: &[NewEvent],
    event_map: &EventMap,
    now_ms: i64,
    counts: &mut PersistCounts,
) -> anyhow::Result<StateAndExtremities> {
    let latest_event_ids = reads.latest_event_ids_in_room(room_id).await?;
    let new_latest_event_ids =
        calculate_new_extremities(reads, events, &latest_event_ids, counts).await?;

    if new_latest_event_ids == latest_event_ids {
        // No change in extremities, so no change in state.
        return Ok(StateAndExtremities::default());
    }

    if new_latest_event_ids.is_empty() {
        bail!("No forward extremities left!");
    }

    // A chain of single-prev non-state events cannot change the state.
    if latest_event_ids.len() == 1
        && new_latest_event_ids.len() == 1
        && events
            .iter()
            .all(|event| event.prev_event_ids.len() == 1 && !event.is_state())
    {
        return Ok(StateAndExtremities {
            new_forward_extremities: Some(new_latest_event_ids),
            ..StateAndExtremities::default()
        });
    }

    counts.state_delta += 1;
    if new_latest_event_ids.len() == 1 {
        counts.state_delta_single_event += 1;

        // A rough guess at whether the delta could have been known when one
        // of the events was created: a local event takes the forward
        // extremities as its prev events.
        if events.iter().any(|event| {
            let prevs: HashSet<&Arc<str>> = event.prev_event_ids.iter().collect();
            prevs.len() == latest_event_ids.len()
                && latest_event_ids.iter().all(|id| prevs.contains(id))
        }) {
            counts.state_delta_reuse_delta += 1;
        }
    }

    let mut new_state = get_new_state_after_events(
        reads,
        resolver,
        room_id,
        server_name,
        events,
        event_map,
        &latest_event_ids,
        new_latest_event_ids,
        true,
        now_ms,
        counts,
    )
    .await?;
    if new_state.new_latest_event_ids.is_empty() {
        bail!("No forward extremities left!");
    }
    let new_forward_extremities = std::mem::take(&mut new_state.new_latest_event_ids);
    let state_res_stats = new_state.state_res_stats.take();

    let state_delta = delta_from_new_state(reads, room_id, server_name, events, new_state).await?;

    Ok(StateAndExtremities {
        new_forward_extremities: Some(new_forward_extremities),
        state_delta,
        new_state_dag_extremities: None,
        state_res_stats,
    })
}

/// `_process_state_dag_forward_extremities_and_state_delta`: for an MSC4242
/// state DAG room, the new forward extremities of both DAGs and the change
/// to the current state, which follows the state DAG's extremities. Those
/// are never pruned, as that would lose eventual delivery.
///
/// Fails if no room DAG forward extremity is left, where Python's `assert`
/// raises an `AssertionError`.
#[allow(clippy::too_many_arguments)]
pub async fn process_state_dag_forward_extremities_and_state_delta(
    reads: &dyn StateDeltaReads,
    resolver: &dyn StateResolver,
    room_id: &str,
    server_name: &str,
    events: &[NewEvent],
    event_map: &EventMap,
    now_ms: i64,
    counts: &mut PersistCounts,
) -> anyhow::Result<StateAndExtremities> {
    let existing_state_dag = reads.state_dag_extremities(room_id).await?;
    let new_state_dag =
        calculate_new_state_dag_extremities(reads, room_id, &existing_state_dag, events, counts)
            .await?;

    let existing_room_dag = reads.latest_event_ids_in_room(room_id).await?;
    let new_room_dag = calculate_new_extremities(reads, events, &existing_room_dag, counts).await?;
    if new_room_dag.is_empty() {
        bail!("No room dag forward extremities left in room {room_id}!");
    }

    if new_state_dag == existing_state_dag {
        return Ok(StateAndExtremities {
            new_forward_extremities: Some(new_room_dag),
            state_delta: None,
            new_state_dag_extremities: Some(new_state_dag),
            state_res_stats: None,
        });
    }

    let mut new_state = get_new_state_after_events(
        reads,
        resolver,
        room_id,
        server_name,
        events,
        event_map,
        &existing_state_dag,
        new_state_dag.clone(),
        false,
        now_ms,
        counts,
    )
    .await?;
    let state_res_stats = new_state.state_res_stats.take();

    let state_delta = delta_from_new_state(reads, room_id, server_name, events, new_state).await?;

    Ok(StateAndExtremities {
        new_forward_extremities: Some(new_room_dag),
        state_delta,
        new_state_dag_extremities: Some(new_state_dag),
        state_res_stats,
    })
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;
    use crate::persist::extremities::DagEvent;

    /// The database as the state delta reads it.
    #[derive(Default)]
    struct FakeReads {
        latest: HashSet<Arc<str>>,
        state_dag: HashSet<Arc<str>>,
        event_groups: HashMap<Arc<str>, i64>,
        groups: HashMap<i64, Arc<StateMap>>,
        room_version: Option<Arc<str>>,
        current_state: StateMap,
        memberships: HashMap<Arc<str>, Arc<str>>,
        /// Local users joined to the room.
        local_joined: Vec<Arc<str>>,
        dag_events: HashMap<Arc<str>, DagEvent>,
        /// What `is_local_host_in_room_ignoring_users` was asked to ignore.
        ignored: Mutex<Option<Vec<Arc<str>>>>,
    }

    #[async_trait]
    impl ExtremityReads for FakeReads {
        async fn events_which_are_prevs(
            &self,
            _event_ids: &[Arc<str>],
        ) -> anyhow::Result<Vec<Arc<str>>> {
            Ok(Vec::new())
        }

        async fn prevs_before_rejected(
            &self,
            _event_ids: &[Arc<str>],
            _include_soft_failed: bool,
        ) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(HashSet::new())
        }

        async fn have_seen_events(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(event_ids.iter().cloned().collect())
        }

        async fn dag_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<DagEvent>> {
            Ok(event_ids
                .iter()
                .filter_map(|id| self.dag_events.get(id).cloned())
                .collect())
        }
    }

    #[async_trait]
    impl StateDeltaReads for FakeReads {
        async fn latest_event_ids_in_room(&self, _: &str) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(self.latest.clone())
        }

        async fn state_dag_extremities(&self, _: &str) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(self.state_dag.clone())
        }

        async fn state_group_for_events(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<HashMap<Arc<str>, i64>> {
            Ok(event_ids
                .iter()
                .filter_map(|id| self.event_groups.get(id).map(|g| (Arc::clone(id), *g)))
                .collect())
        }

        async fn state_for_groups(
            &self,
            groups: &[i64],
        ) -> anyhow::Result<HashMap<i64, Arc<StateMap>>> {
            Ok(groups
                .iter()
                .filter_map(|g| self.groups.get(g).map(|s| (*g, Arc::clone(s))))
                .collect())
        }

        async fn room_version_id(&self, _: &str) -> anyhow::Result<Option<Arc<str>>> {
            Ok(self.room_version.clone())
        }

        async fn current_state_ids(&self, _: &str) -> anyhow::Result<StateMap> {
            Ok(self.current_state.clone())
        }

        async fn membership_from_event_ids(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<HashMap<Arc<str>, Arc<str>>> {
            Ok(event_ids
                .iter()
                .filter_map(|id| {
                    self.memberships
                        .get(id)
                        .map(|m| (Arc::clone(id), Arc::clone(m)))
                })
                .collect())
        }

        async fn is_local_host_in_room_ignoring_users(
            &self,
            _: &str,
            ignore_users: &[Arc<str>],
        ) -> anyhow::Result<bool> {
            *self.ignored.lock().unwrap() = Some(ignore_users.to_vec());
            Ok(self
                .local_joined
                .iter()
                .any(|user| !ignore_users.contains(user)))
        }
    }

    /// Resolves to a fixed state, and records what it was asked.
    #[derive(Default)]
    struct FakeResolver {
        result: StateMap,
        deleted_groups: Vec<i64>,
        checked: Mutex<Vec<Vec<i64>>>,
        resolved: Mutex<Vec<(&'static str, usize)>>,
    }

    #[async_trait]
    impl StateResolver for FakeResolver {
        async fn check_state_groups_and_bump_deletion(
            &self,
            groups: &[i64],
        ) -> anyhow::Result<Vec<i64>> {
            self.checked.lock().unwrap().push(groups.to_vec());
            Ok(groups
                .iter()
                .copied()
                .filter(|g| self.deleted_groups.contains(g))
                .collect())
        }

        async fn resolve(
            &self,
            _room_id: &str,
            room_version: &'static RoomVersion,
            state_sets: &[Arc<StateMap>],
            _event_map: &EventMap,
        ) -> anyhow::Result<(StateMap, StateResStats)> {
            self.resolved
                .lock()
                .unwrap()
                .push((room_version.identifier, state_sets.len()));
            Ok((self.result.clone(), StateResStats::default()))
        }
    }

    const ROOM: &str = "!r:here";
    const SERVER: &str = "here";

    fn ids(ids: &[&str]) -> HashSet<Arc<str>> {
        ids.iter().map(|id| Arc::from(*id)).collect()
    }

    fn state(entries: &[(&str, &str, &str)]) -> StateMap {
        entries
            .iter()
            .map(|(t, k, e)| (StateKey::new(t, k), Arc::from(*e)))
            .collect()
    }

    fn event(id: &str, prevs: &[&str], group: i64) -> NewEvent {
        NewEvent {
            event_id: Arc::from(id),
            event_type: Arc::from("m.room.message"),
            sender: Arc::from("@a:remote"),
            depth: 10,
            prev_event_ids: prevs.iter().map(|p| Arc::from(*p)).collect(),
            state_group: Some(group),
            ..Default::default()
        }
    }

    fn member(id: &str, user: &str, membership: &str, group: i64) -> NewEvent {
        NewEvent {
            event_type: Arc::from(MEMBER),
            state_key: Some(Arc::from(user)),
            membership: Some(Arc::from(membership)),
            ..event(id, &["$p"], group)
        }
    }

    fn block_on<F: std::future::Future>(f: F) -> F::Output {
        futures::executor::block_on(f)
    }

    fn new_state(
        reads: &FakeReads,
        resolver: &FakeResolver,
        events: &[NewEvent],
        old: &[&str],
        new: &[&str],
    ) -> anyhow::Result<(NewState, PersistCounts)> {
        let mut counts = PersistCounts::default();
        let result = block_on(get_new_state_after_events(
            reads,
            resolver,
            ROOM,
            SERVER,
            events,
            &EventMap::new(),
            &ids(old),
            ids(new),
            true,
            0,
            &mut counts,
        ))?;
        Ok((result, counts))
    }

    #[test]
    fn same_groups_change_nothing() {
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5)].into(),
            ..Default::default()
        };
        let events = [event("$new", &["$old"], 5)];
        let (result, _) = new_state(
            &reads,
            &FakeResolver::default(),
            &events,
            &["$old"],
            &["$new"],
        )
        .unwrap();
        assert!(result.current_state.is_none());
        assert!(result.delta_ids.is_none());
        assert_eq!(result.new_latest_event_ids, ids(&["$new"]));
    }

    #[test]
    fn a_known_delta_is_reused() {
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5)].into(),
            ..Default::default()
        };
        let delta = state(&[("m.room.topic", "", "$new")]);
        let mut first = event("$new", &["$old"], 6);
        // A delta between other groups is no use.
        first.state_group_deltas = Arc::new([((4, 6), state(&[("x", "", "$y")]))].into());
        let mut second = event("$other", &["$old"], 6);
        second.state_group_deltas = Arc::new([((5, 6), delta.clone())].into());
        let (result, _) = new_state(
            &reads,
            &FakeResolver::default(),
            &[first, second],
            &["$old"],
            &["$new"],
        )
        .unwrap();
        assert_eq!(result.delta_ids, Some(delta));
        assert!(result.current_state.is_none());
    }

    #[test]
    fn one_new_group_is_the_state() {
        let group_state = state(&[("m.room.create", "", "$c"), ("m.room.topic", "", "$new")]);
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5)].into(),
            groups: [(6, Arc::new(group_state.clone()))].into(),
            ..Default::default()
        };
        let resolver = FakeResolver::default();
        let events = [event("$new", &["$old"], 6)];
        let (result, counts) = new_state(&reads, &resolver, &events, &["$old"], &["$new"]).unwrap();
        assert_eq!(result.current_state, Some(group_state));
        assert!(resolver.resolved.lock().unwrap().is_empty());
        assert_eq!(counts.state_resolutions, 0);
    }

    #[test]
    fn extremities_in_the_chunk_without_a_group_come_from_the_database() {
        // `$out` is an outlier in the chunk, so its group is read.
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5), (Arc::from("$out"), 5)].into(),
            ..Default::default()
        };
        let mut outlier = event("$out", &["$old"], 0);
        outlier.state_group = None;
        outlier.outlier = true;
        let (result, _) = new_state(
            &reads,
            &FakeResolver::default(),
            &[outlier],
            &["$old"],
            &["$out"],
        )
        .unwrap();
        assert!(result.current_state.is_none() && result.delta_ids.is_none());
    }

    #[test]
    fn several_groups_are_resolved() {
        let resolved = state(&[("m.room.create", "", "$c"), ("m.room.topic", "", "$a")]);
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5), (Arc::from("$b"), 7)].into(),
            groups: [
                (
                    6,
                    Arc::new(state(&[
                        ("m.room.create", "", "$c"),
                        ("m.room.topic", "", "$x"),
                    ])),
                ),
                (7, Arc::new(state(&[("m.room.create", "", "$c")]))),
            ]
            .into(),
            room_version: Some(Arc::from("10")),
            ..Default::default()
        };
        let resolver = FakeResolver {
            result: resolved.clone(),
            ..Default::default()
        };
        let events = [event("$a", &["$old"], 6)];
        let (result, counts) =
            new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap();
        assert_eq!(result.current_state, Some(resolved));
        assert_eq!(*resolver.checked.lock().unwrap(), vec![vec![6, 7]]);
        assert_eq!(*resolver.resolved.lock().unwrap(), vec![("10", 2)]);
        assert_eq!(counts.state_resolutions, 1);
        // The result is neither group, so nothing is pruned.
        assert_eq!(counts.potential_times_prune_extremities, 0);
        assert_eq!(result.new_latest_event_ids, ids(&["$a", "$b"]));
    }

    /// A resolution that comes out as `$a`'s group, with `$b` an old remote
    /// extremity in another group.
    fn prunable() -> (FakeReads, FakeResolver, Vec<NewEvent>) {
        let group_a = state(&[("m.room.create", "", "$c"), ("m.room.topic", "", "$a")]);
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5), (Arc::from("$b"), 7)].into(),
            groups: [
                (6, Arc::new(group_a.clone())),
                (7, Arc::new(state(&[("m.room.create", "", "$c")]))),
            ]
            .into(),
            room_version: Some(Arc::from("10")),
            dag_events: [(
                Arc::from("$b"),
                DagEvent {
                    // The chunk's server, and over 20 events behind it.
                    sender: Arc::from("@b:remote"),
                    event_type: Arc::from("m.room.message"),
                    origin_server_ts: 0,
                    depth: 1,
                    ..Default::default()
                },
            )]
            .into(),
            ..Default::default()
        };
        let resolver = FakeResolver {
            result: group_a,
            ..Default::default()
        };
        let mut a = event("$a", &["$old"], 6);
        a.depth = 500;
        (reads, resolver, vec![a])
    }

    #[test]
    fn a_resolution_to_a_new_group_prunes() {
        let (reads, resolver, events) = prunable();
        let (result, counts) =
            new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap();
        assert_eq!(result.new_latest_event_ids, ids(&["$a"]));
        assert_eq!(counts.potential_times_prune_extremities, 1);
        assert_eq!(counts.times_pruned_extremities, 1);
    }

    #[test]
    fn the_state_dag_does_not_prune() {
        let (reads, resolver, events) = prunable();
        let mut counts = PersistCounts::default();
        let result = block_on(get_new_state_after_events(
            &reads,
            &resolver,
            ROOM,
            SERVER,
            &events,
            &EventMap::new(),
            &ids(&["$old"]),
            ids(&["$a", "$b"]),
            false,
            86_400_000 * 2,
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result.new_latest_event_ids, ids(&["$a", "$b"]));
        assert_eq!(counts.potential_times_prune_extremities, 0);
    }

    #[test]
    fn the_room_version_comes_from_a_create_event_in_the_chunk() {
        let (reads, resolver, mut events) = prunable();
        let mut create = event("$create", &[], 6);
        create.event_type = Arc::from(CREATE);
        create.state_key = Some(Arc::from(""));
        create.create_room_version = Some(Arc::from("11"));
        events.insert(0, create);
        new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap();
        assert_eq!(*resolver.resolved.lock().unwrap(), vec![("11", 2)]);

        // A falsy version sends the reader to the database.
        let resolver = FakeResolver::default();
        events[0].create_room_version = None;
        new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap();
        assert_eq!(*resolver.resolved.lock().unwrap(), vec![("10", 2)]);
    }

    #[test]
    fn state_res_v1_falls_back() {
        let (mut reads, resolver, events) = prunable();
        reads.room_version = Some(Arc::from("1"));
        let err = new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap_err();
        assert!(err.downcast_ref::<StateResFallback>().is_some(), "{err:#}");
        assert!(resolver.checked.lock().unwrap().is_empty());
    }

    #[test]
    fn deleted_groups_fail() {
        let (reads, mut resolver, events) = prunable();
        resolver.deleted_groups = vec![7];
        let err = new_state(&reads, &resolver, &events, &["$old"], &["$a", "$b"]).unwrap_err();
        assert_eq!(err.to_string(), "State groups have been deleted: [7]");
        assert!(resolver.resolved.lock().unwrap().is_empty());
    }

    #[test]
    fn bad_contexts_fail() {
        let reads = FakeReads {
            event_groups: [(Arc::from("$old"), 5)].into(),
            ..Default::default()
        };
        let mut rejected = event("$new", &["$old"], 6);
        rejected.rejected = true;
        let err = new_state(
            &reads,
            &FakeResolver::default(),
            &[rejected],
            &["$old"],
            &["$x"],
        )
        .unwrap_err();
        assert_eq!(
            err.to_string(),
            "Attempt to access state_group of rejected event"
        );

        let mut no_group = event("$new", &["$old"], 6);
        no_group.state_group = None;
        let err = new_state(
            &reads,
            &FakeResolver::default(),
            &[no_group],
            &["$old"],
            &["$x"],
        )
        .unwrap_err();
        assert_eq!(
            err.to_string(),
            "Context for new event $new has no state group"
        );

        // An extremity with no state group anywhere.
        let err = new_state(
            &reads,
            &FakeResolver::default(),
            &[event("$new", &["$old"], 6)],
            &["$old"],
            &["$unknown"],
        )
        .unwrap_err();
        assert_eq!(err.to_string(), "no state group for $unknown");
    }

    #[test]
    fn state_delta_deletes_and_inserts() {
        let reads = FakeReads {
            current_state: state(&[
                ("m.room.create", "", "$c"),
                ("m.room.topic", "", "$t1"),
                ("m.room.name", "", "$n"),
                ("m.room.avatar", "", "$av"),
            ]),
            ..Default::default()
        };
        let new = state(&[
            ("m.room.create", "", "$c"),
            ("m.room.topic", "", "$t2"),
            ("m.room.join_rules", "", "$j"),
        ]);
        let delta = block_on(calculate_state_delta(&reads, ROOM, &new)).unwrap();
        assert_eq!(
            delta.to_delete,
            vec![
                StateKey::new("m.room.avatar", ""),
                StateKey::new("m.room.name", "")
            ]
        );
        assert_eq!(
            delta.to_insert,
            state(&[("m.room.topic", "", "$t2"), ("m.room.join_rules", "", "$j")])
        );
        assert!(!delta.no_longer_in_room);
    }

    fn joined(reads: &FakeReads, events: &[NewEvent], delta: &DeltaState) -> bool {
        block_on(is_server_still_joined(reads, ROOM, SERVER, events, delta)).unwrap()
    }

    #[test]
    fn still_joined_without_local_membership_changes() {
        // Nobody local is joined, but no local membership changed.
        let reads = FakeReads::default();
        let delta = DeltaState {
            to_delete: vec![StateKey::new(MEMBER, "@x:remote")],
            to_insert: state(&[(MEMBER, "@y:remote", "$y"), ("m.room.topic", "", "$t")]),
            ..Default::default()
        };
        assert!(joined(&reads, &[], &delta));
        assert!(reads.ignored.lock().unwrap().is_none());
    }

    #[test]
    fn still_joined_through_a_local_join() {
        let reads = FakeReads::default();
        let delta = DeltaState {
            to_insert: state(&[(MEMBER, "@me:here", "$join")]),
            ..Default::default()
        };
        // In the chunk.
        assert!(joined(
            &reads,
            &[member("$join", "@me:here", "join", 1)],
            &delta
        ));
        // A leave in the chunk is read from the database too, where it is
        // not found.
        assert!(!joined(
            &reads,
            &[member("$join", "@me:here", "leave", 1)],
            &delta
        ));

        // In the database.
        let reads = FakeReads {
            memberships: [(Arc::from("$join"), Arc::from("join"))].into(),
            ..Default::default()
        };
        assert!(joined(&reads, &[], &delta));
    }

    #[test]
    fn still_joined_through_another_local_user() {
        let delta = DeltaState {
            to_delete: vec![StateKey::new(MEMBER, "@gone:here")],
            to_insert: state(&[(MEMBER, "@me:here", "$leave"), (MEMBER, "@x:remote", "$x")]),
            ..Default::default()
        };
        let reads = FakeReads {
            memberships: [(Arc::from("$leave"), Arc::from("leave"))].into(),
            local_joined: vec![Arc::from("@me:here"), Arc::from("@gone:here")],
            ..Default::default()
        };
        // The users whose membership changed do not count.
        assert!(!joined(&reads, &[], &delta));
        let mut ignored = reads.ignored.lock().unwrap().clone().unwrap();
        ignored.sort();
        assert_eq!(
            ignored,
            vec![Arc::from("@gone:here"), Arc::from("@me:here")]
        );

        let reads = FakeReads {
            local_joined: vec![Arc::from("@other:here")],
            ..reads
        };
        assert!(joined(&reads, &[], &delta));
    }

    fn orchestrate(
        reads: &FakeReads,
        resolver: &FakeResolver,
        events: &[NewEvent],
    ) -> (StateAndExtremities, PersistCounts) {
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_forward_extremities_and_state_delta(
            reads,
            resolver,
            ROOM,
            SERVER,
            events,
            &EventMap::new(),
            0,
            &mut counts,
        ))
        .unwrap();
        (result, counts)
    }

    #[test]
    fn unchanged_extremities_change_nothing() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            ..Default::default()
        };
        let mut outlier = event("$out", &["$old"], 1);
        outlier.outlier = true;
        let (result, counts) = orchestrate(&reads, &FakeResolver::default(), &[outlier]);
        assert!(result.new_forward_extremities.is_none());
        assert!(result.state_delta.is_none());
        assert_eq!(counts, PersistCounts::default());
    }

    #[test]
    fn a_chain_of_messages_needs_no_state() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            ..Default::default()
        };
        let events = [event("$1", &["$old"], 1), event("$2", &["$1"], 1)];
        let (result, counts) = orchestrate(&reads, &FakeResolver::default(), &events);
        assert_eq!(result.new_forward_extremities, Some(ids(&["$2"])));
        assert!(result.state_delta.is_none());
        assert_eq!(counts.state_delta, 0);
    }

    #[test]
    fn a_state_change_is_counted_and_applied() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            event_groups: [(Arc::from("$old"), 5)].into(),
            local_joined: vec![Arc::from("@me:here")],
            ..Default::default()
        };
        let mut topic = event("$t", &["$old"], 6);
        topic.event_type = Arc::from("m.room.topic");
        topic.state_key = Some(Arc::from(""));
        topic.state_group_deltas =
            Arc::new([((5, 6), state(&[("m.room.topic", "", "$t")]))].into());
        let (result, counts) = orchestrate(&reads, &FakeResolver::default(), &[topic]);
        assert_eq!(result.new_forward_extremities, Some(ids(&["$t"])));
        assert_eq!(
            result.state_delta,
            Some(DeltaState {
                to_insert: state(&[("m.room.topic", "", "$t")]),
                ..Default::default()
            })
        );
        assert_eq!(counts.state_delta, 1);
        assert_eq!(counts.state_delta_single_event, 1);
        assert_eq!(counts.state_delta_reuse_delta, 1);
    }

    #[test]
    fn reuse_needs_prevs_equal_to_the_extremities() {
        // Two extremities, merged by an event that only names one.
        let reads = FakeReads {
            latest: ids(&["$o1", "$o2"]),
            event_groups: [(Arc::from("$o1"), 5), (Arc::from("$o2"), 5)].into(),
            ..Default::default()
        };
        let mut events = vec![event("$m", &["$o1"], 5), event("$n", &["$m", "$o2"], 5)];
        let (_, counts) = orchestrate(&reads, &FakeResolver::default(), &events);
        assert_eq!(counts.state_delta, 1);
        assert_eq!(counts.state_delta_single_event, 1);
        assert_eq!(counts.state_delta_reuse_delta, 0);

        events[0].prev_event_ids = vec![Arc::from("$o2"), Arc::from("$o1")];
        let (_, counts) = orchestrate(&reads, &FakeResolver::default(), &events);
        assert_eq!(counts.state_delta_reuse_delta, 1);
    }

    #[test]
    fn the_last_local_leave_leaves_the_room() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            event_groups: [(Arc::from("$old"), 5)].into(),
            groups: [(6, Arc::new(state(&[(MEMBER, "@me:here", "$leave")])))].into(),
            current_state: state(&[(MEMBER, "@me:here", "$join")]),
            local_joined: vec![Arc::from("@me:here")],
            ..Default::default()
        };
        let leave = member("$leave", "@me:here", "leave", 6);
        let leave = NewEvent {
            prev_event_ids: vec![Arc::from("$old")],
            ..leave
        };
        let (result, _) = orchestrate(&reads, &FakeResolver::default(), &[leave]);
        let delta = result.state_delta.unwrap();
        assert_eq!(delta.to_insert, state(&[(MEMBER, "@me:here", "$leave")]));
        assert!(delta.no_longer_in_room);
    }

    fn state_dag_event(id: &str, prev_state: &[&str], group: i64) -> NewEvent {
        NewEvent {
            event_type: Arc::from("m.room.topic"),
            state_key: Some(Arc::from("")),
            prev_state_events: Some(prev_state.iter().map(|p| Arc::from(*p)).collect()),
            ..event(id, prev_state, group)
        }
    }

    fn orchestrate_state_dag(
        reads: &FakeReads,
        resolver: &FakeResolver,
        events: &[NewEvent],
    ) -> (StateAndExtremities, PersistCounts) {
        let mut counts = PersistCounts::default();
        let result = block_on(process_state_dag_forward_extremities_and_state_delta(
            reads,
            resolver,
            ROOM,
            SERVER,
            events,
            &EventMap::new(),
            0,
            &mut counts,
        ))
        .unwrap();
        (result, counts)
    }

    #[test]
    fn state_dag_messages_only_move_the_room_dag() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            state_dag: ids(&["$s"]),
            ..Default::default()
        };
        let (result, counts) = orchestrate_state_dag(
            &reads,
            &FakeResolver::default(),
            &[event("$m", &["$old"], 1)],
        );
        assert_eq!(result.new_forward_extremities, Some(ids(&["$m"])));
        assert_eq!(result.new_state_dag_extremities, Some(ids(&["$s"])));
        assert!(result.state_delta.is_none());
        assert_eq!(counts.state_delta, 0);
    }

    #[test]
    fn state_dag_state_follows_the_state_dag() {
        let reads = FakeReads {
            latest: ids(&["$old"]),
            state_dag: ids(&["$s"]),
            event_groups: [(Arc::from("$s"), 5)].into(),
            groups: [(6, Arc::new(state(&[("m.room.topic", "", "$t")])))].into(),
            current_state: state(&[("m.room.topic", "", "$s")]),
            ..Default::default()
        };
        let topic = NewEvent {
            prev_event_ids: vec![Arc::from("$old")],
            ..state_dag_event("$t", &["$s"], 6)
        };
        let (result, counts) = orchestrate_state_dag(&reads, &FakeResolver::default(), &[topic]);
        assert_eq!(result.new_forward_extremities, Some(ids(&["$t"])));
        assert_eq!(result.new_state_dag_extremities, Some(ids(&["$t"])));
        assert_eq!(
            result.state_delta.unwrap().to_insert,
            state(&[("m.room.topic", "", "$t")])
        );
        // The state DAG path does not count the state delta metrics.
        assert_eq!(counts.state_delta, 0);
        assert_eq!(counts.state_dag_forward_extremities[&1], 1);
    }
}
