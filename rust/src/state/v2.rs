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

//! State resolution v2 and v2.1. Port of `synapse/state/v2.py`.
//!
//! The work is split in two:
//!
//! - [`separate`] splits the state into unconflicted and conflicted keys, and
//!   [`prepare`] computes the auth chain difference and loads every event the
//!   algorithm can touch (the "prefetch closure", see [`prefetch`]).
//! - [`PendingResolution::run`] does no I/O. It runs the power sort, the
//!   iterative auth checks and the mainline sort over the loaded events (see
//!   [`Core::resolve_conflicted_set`]).
//!
//! The Python fetches events lazily, one at a time, inside the sorts. The
//! core cannot, which is why the async layer loads everything up front.
//!
//! The core runs on the tokio workers, which also pump the database
//! connections' sockets. Like the Python (which sleeps for 0s every 100
//! iterations so the reactor makes progress), it yields to the scheduler every
//! [`YIELD_EVERY`] iterations, so a large resolution does not starve the
//! connections. It does not use `spawn_blocking`, which this crate avoids as
//! blocking tasks outlive the runtime's shutdown.

use std::{
    cmp::Reverse,
    collections::{BinaryHeap, HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{bail, Context};
use log::{debug, warn};
use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{
    lookup,
    store::{StateDifference, StateResStore},
    ConflictCache, EventMap, StateKey, StateMap, StateResEvent,
};
use crate::event_auth::{
    self, auth_types_for_event, check_state_dependent_auth_rules_with_map, is_creator,
    power_levels::{py_int, sub_mapping},
    AuthFailure, AuthMap, CREATE_KEY, CREATOR_POWER_LEVEL, POWER_LEVELS_KEY,
};
use crate::events::constants::event_type::{M_ROOM_JOIN_RULES, M_ROOM_MEMBER};
use crate::room_versions::{RoomVersion, StateResolutionVersions};

/// The result of a resolution.
#[derive(Debug, Default)]
pub struct Resolution {
    /// The keys all the state sets agree on.
    pub unconflicted: StateMap,
    /// The resolution of the conflicted keys, layered over the base state
    /// (the unconflicted keys the auth checks used, for v2). The unconflicted
    /// state takes precedence over this.
    pub resolved: Arc<StateMap>,
    pub stats: ResolveStats,
}

impl Resolution {
    /// The resolved state: the unconflicted state over the resolved conflicts.
    pub fn merged(&self) -> StateMap {
        let mut merged = (*self.resolved).clone();
        merged.extend(
            self.unconflicted
                .iter()
                .map(|(k, v)| (k.clone(), Arc::clone(v))),
        );
        merged
    }
}

/// Where the time went in a resolution. The times are wall time in each stage
/// of the core, including time yielded to other tasks.
#[derive(Debug, Default, Clone, Copy)]
pub struct ResolveStats {
    /// The resolution of the conflicted set came from the cache.
    pub cache_hit: bool,
    /// The number of events in the full conflicted set.
    pub conflicted_events: usize,
    /// Time in the reverse topological power sort.
    pub sort_time: Duration,
    /// Time in the iterative auth checks.
    pub auth_time: Duration,
    /// Time in the mainline sort.
    pub mainline_time: Duration,
    /// Time computing the auth chain difference, caches included.
    pub auth_chain_time: Duration,
    /// Time loading the conflicted events and the prefetch, caches included.
    pub load_events_time: Duration,
}

/// Resolve `state_sets` with state resolution v2 or v2.1.
///
/// `event_map` holds events that are not yet persisted (e.g. those being
/// persisted now). Everything else is read from `store`.
///
/// This is [`separate`], [`prepare`] and [`PendingResolution::run`]. Callers
/// that hold a database connection for the store should call those separately
/// and release the connection in between, as `run` does no I/O.
pub async fn resolve(
    store: &dyn StateResStore,
    room_id: &str,
    room_version: &'static RoomVersion,
    state_sets: &[Arc<StateMap>],
    event_map: EventMap,
    conflict_cache: Option<&ConflictCache>,
) -> anyhow::Result<Resolution> {
    match prepare(
        store,
        room_id,
        room_version,
        state_sets,
        separate(state_sets),
        event_map,
        conflict_cache,
    )
    .await?
    {
        Prepared::Done(resolution) => Ok(resolution),
        Prepared::Pending(pending) => pending.run(conflict_cache).await,
    }
}

/// The outcome of [`prepare`].
pub enum Prepared {
    /// Resolved without running the algorithm: there were no conflicts, or
    /// the conflict cache had the answer.
    Done(Resolution),
    /// Everything is loaded; [`PendingResolution::run`] resolves it.
    Pending(Box<PendingResolution>),
}

/// A resolution with every event it can read loaded, ready to run.
pub struct PendingResolution {
    room_id: Box<str>,
    room_version: &'static RoomVersion,
    unconflicted: StateMap,
    full_conflicted_set: HashSet<Arc<str>>,
    base_state: StateMap,
    event_map: EventMap,
    cache_key: Option<[u8; 32]>,
    stats: ResolveStats,
}

impl PendingResolution {
    /// Run the algorithm over the loaded events, and cache the result.
    pub async fn run(self, conflict_cache: Option<&ConflictCache>) -> anyhow::Result<Resolution> {
        let PendingResolution {
            room_id,
            room_version,
            unconflicted,
            full_conflicted_set,
            base_state,
            event_map,
            cache_key,
            mut stats,
        } = self;

        let core = Core {
            room_id: &room_id,
            room_version,
            events: &event_map,
        };
        let resolved = core
            .resolve_conflicted_set(&full_conflicted_set, base_state, &mut stats)
            .await?;

        let resolved = Arc::new(resolved);
        if let (Some(cache), Some(key)) = (conflict_cache, cache_key) {
            drop(cache.insert(key, Arc::clone(&resolved)));
        }

        Ok(Resolution {
            unconflicted,
            resolved,
            stats,
        })
    }
}

/// Everything in a resolution that reads the database: compute the auth chain
/// difference, check the conflict cache and load the events the algorithm can
/// read (see [`prefetch`]). Takes the state already split by [`separate`], so
/// a caller can answer a conflict-free resolution without a store.
pub async fn prepare(
    store: &dyn StateResStore,
    room_id: &str,
    room_version: &'static RoomVersion,
    state_sets: &[Arc<StateMap>],
    (unconflicted, conflicted): (StateMap, Conflicted),
    mut event_map: EventMap,
    conflict_cache: Option<&ConflictCache>,
) -> anyhow::Result<Prepared> {
    let mut stats = ResolveStats::default();

    if conflicted.is_empty() {
        return Ok(Prepared::Done(Resolution {
            unconflicted,
            resolved: Arc::default(),
            stats,
        }));
    }

    debug!("{} conflicted state entries", conflicted.len());

    let conflicted_ids: HashSet<Arc<str>> = conflicted.values().flatten().cloned().collect();
    let is_v21 = room_version.state_res == StateResolutionVersions::V2_1;
    let start = Instant::now();
    let auth_diff = get_auth_chain_difference(
        room_id,
        state_sets,
        &event_map,
        store,
        is_v21.then_some(&conflicted_ids),
    )
    .await?;
    stats.auth_chain_time = start.elapsed();

    let mut full_conflicted_set: HashSet<Arc<str>> =
        conflicted_ids.iter().chain(&auth_diff).cloned().collect();

    let missing: Vec<Arc<str>> = full_conflicted_set
        .iter()
        .filter(|id| !event_map.contains_key(*id))
        .cloned()
        .collect();
    let start = Instant::now();
    event_map.extend(store.get_events(&missing).await?);
    stats.load_events_time = start.elapsed();

    // Everything in the event map should be in the right room.
    for event in event_map.values() {
        if event.room_id() != room_id {
            bail!(
                "Attempting to state-resolve for room {room_id} with event {} which is in {}",
                event.event_id(),
                event.room_id()
            );
        }
    }

    full_conflicted_set.retain(|id| event_map.contains_key(id));
    stats.conflicted_events = full_conflicted_set.len();

    // The base state. v2 starts from the unconflicted state, restricted to the
    // keys the resolution can read: the auth types of the conflicted events
    // and the power levels for the mainline sort. That keeps keys that cannot
    // affect the outcome out of the cache key. v2.1 starts from empty state.
    let mut base_state = StateMap::new();
    if !is_v21 {
        let mut base_state_keys: HashSet<(&str, &str)> = HashSet::from([POWER_LEVELS_KEY]);
        for event_id in &full_conflicted_set {
            base_state_keys.extend(
                auth_types_for_event(room_version, &event_map[event_id])
                    .map_err(anyhow::Error::from)?,
            );
        }
        for key in base_state_keys {
            if let Some((k, v)) = unconflicted.get_key_value(&key as &dyn super::KeyRef) {
                base_state.insert(k.clone(), Arc::clone(v));
            }
        }
    }

    let cache_key = conflict_cache
        .map(|_| conflict_cache_key(room_id, room_version, &full_conflicted_set, &base_state));
    if let (Some(cache), Some(key)) = (conflict_cache, &cache_key) {
        if let Some(resolved) = cache.get(key) {
            debug!(
                "Reusing the resolution of {} conflicted events",
                full_conflicted_set.len()
            );
            stats.cache_hit = true;
            return Ok(Prepared::Done(Resolution {
                unconflicted,
                resolved,
                stats,
            }));
        }
    }

    let start = Instant::now();
    prefetch(store, &mut event_map, &full_conflicted_set, &base_state).await?;
    stats.load_events_time += start.elapsed();

    Ok(Prepared::Pending(Box::new(PendingResolution {
        room_id: Box::from(room_id),
        room_version,
        unconflicted,
        full_conflicted_set,
        base_state,
        event_map,
        cache_key,
        stats,
    })))
}

/// The conflicted state: each conflicted key to the event IDs the state sets
/// have for it.
pub type Conflicted = HashMap<StateKey, HashSet<Arc<str>>>;

/// Split the state into unconflicted and conflicted state. Port of
/// `_seperate`.
///
/// Unlike the original algorithm, a key is conflicted if one of the state
/// sets doesn't have it. The conflicted state maps each key to the event IDs
/// the sets have for it.
pub fn separate(state_sets: &[Arc<StateMap>]) -> (StateMap, Conflicted) {
    let mut unconflicted = StateMap::new();
    let mut conflicted = Conflicted::new();

    let mut seen: HashSet<&StateKey> = HashSet::new();
    for key in state_sets.iter().flat_map(|set| set.keys()) {
        if !seen.insert(key) {
            continue;
        }
        let values: Vec<Option<&Arc<str>>> = state_sets.iter().map(|set| set.get(key)).collect();
        let first = values[0];
        if values.iter().all(|v| *v == first) {
            let value = first.expect("the key is in at least one set");
            unconflicted.insert(key.clone(), Arc::clone(value));
        } else {
            conflicted.insert(key.clone(), values.into_iter().flatten().cloned().collect());
        }
    }

    (unconflicted, conflicted)
}

/// The auth chain difference of the state sets, handling events that are not
/// persisted yet. Port of `_get_auth_chain_difference`.
///
/// For v2.1 (`conflicted_state` is set) this includes the conflicted
/// subgraph.
pub async fn get_auth_chain_difference(
    room_id: &str,
    state_sets: &[Arc<StateMap>],
    unpersisted_events: &EventMap,
    store: &dyn StateResStore,
    conflicted_state: Option<&HashSet<Arc<str>>>,
) -> anyhow::Result<HashSet<Arc<str>>> {
    let is_v21 = conflicted_state.is_some();
    let num_conflicted_state = conflicted_state.map(HashSet::len);

    // The store assumes all events passed to it (and their auth chains) are
    // persisted, so we handle the unpersisted events here, in three steps:
    //   1. Compute the unpersisted events in the auth difference.
    //   2. Replace unpersisted events in the state sets with their auth
    //      events, recursively, so the sets contain only persisted events, and
    //      ask the store for the auth difference of those.
    //   3. Add the results of 1 and 2 together.

    // For each unpersisted event, its auth chain up to (and including) the
    // first persisted events.
    let mut events_to_auth_chain: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
    // The forward links: an event to the events that list it as an auth
    // event. Needed for v2.1.
    let mut event_to_next_event: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
    for (event_id, event) in unpersisted_events {
        let mut chain: HashSet<Arc<str>> = HashSet::from([Arc::clone(event_id)]);

        // The Python re-walks shared ancestors; skipping them gives the same
        // sets without the exponential worst case.
        let mut visited: HashSet<&Arc<str>> = HashSet::from([event_id]);
        let mut to_search: Vec<(&Arc<str>, &StateResEvent)> = vec![(event_id, event)];
        while let Some((next_id, next_event)) = to_search.pop() {
            for auth_id in next_event.auth_event_ids() {
                chain.insert(Arc::clone(auth_id));
                event_to_next_event
                    .entry(Arc::clone(auth_id))
                    .or_default()
                    .insert(Arc::clone(next_id));
                if let Some((auth_key, auth_event)) = unpersisted_events.get_key_value(auth_id) {
                    if visited.insert(auth_key) {
                        to_search.push((auth_key, auth_event));
                    }
                }
            }
        }
        events_to_auth_chain.insert(Arc::clone(event_id), chain);
    }

    let mut additional_backwards_reachable: HashSet<Arc<str>> = HashSet::new();
    let mut unpersisted_conflicted_events: HashSet<Arc<str>> = HashSet::new();
    let auth_difference_unpersisted_part: HashSet<Arc<str>>;
    let state_sets_ids: Vec<HashSet<Arc<str>>>;

    if !unpersisted_events.is_empty() {
        // The state sets as sets of event IDs, with unpersisted events
        // replaced by the persisted events in their auth chains.
        let mut sets_ids = Vec::with_capacity(state_sets.len());
        // For each state set, the unpersisted events reachable from it.
        let mut unpersisted_set_ids: Vec<HashSet<Arc<str>>> = Vec::with_capacity(state_sets.len());

        for state_set in state_sets {
            let mut set_ids: HashSet<Arc<str>> = HashSet::new();
            let mut unpersisted_ids: HashSet<Arc<str>> = HashSet::new();

            for event_id in state_set.values() {
                match events_to_auth_chain.get(event_id) {
                    Some(event_chain) => {
                        for e in event_chain {
                            if unpersisted_events.contains_key(e) {
                                unpersisted_ids.insert(Arc::clone(e));
                            } else {
                                set_ids.insert(Arc::clone(e));
                            }
                        }
                    }
                    None => {
                        set_ids.insert(Arc::clone(event_id));
                    }
                }
            }
            sets_ids.push(set_ids);
            unpersisted_set_ids.push(unpersisted_ids);
        }

        for conflicted_event_id in conflicted_state.into_iter().flatten() {
            // Presence in this map means it is unpersisted.
            if let Some(event_chain) = events_to_auth_chain.get(conflicted_event_id) {
                unpersisted_conflicted_events.insert(Arc::clone(conflicted_event_id));
                // Tell the store we have some unpersisted conflicted events.
                additional_backwards_reachable.extend(
                    event_chain
                        .iter()
                        .filter(|e| !unpersisted_events.contains_key(*e))
                        .cloned(),
                );
            }
        }

        auth_difference_unpersisted_part =
            super::auth_chain::union_minus_intersection(&unpersisted_set_ids);
        state_sets_ids = sets_ids;
    } else {
        auth_difference_unpersisted_part = HashSet::new();
        state_sets_ids = state_sets
            .iter()
            .map(|set| set.values().cloned().collect())
            .collect();
    }

    // The conflicted state passed to the store must be a subset of the state
    // set IDs, so drop the unpersisted conflicted events we replaced above.
    let conflicted_state: Option<HashSet<Arc<str>>> = conflicted_state.map(|conflicted| {
        conflicted
            .difference(&unpersisted_conflicted_events)
            .cloned()
            .collect()
    });

    let StateDifference {
        mut auth_difference,
        mut conflicted_subgraph,
    } = store
        .get_auth_chain_difference(
            room_id,
            &state_sets_ids,
            conflicted_state.as_ref(),
            Some(&additional_backwards_reachable),
        )
        .await?;
    auth_difference.extend(auth_difference_unpersisted_part);

    // For v2.1, the conflicted subgraph may continue into unpersisted events.
    if is_v21 && !unpersisted_events.is_empty() {
        if let Some(subgraph) = conflicted_subgraph.as_mut() {
            // We always include the conflicted events themselves.
            subgraph.extend(conflicted_state.iter().flatten().cloned());

            // Where the subgraph reaches the boundary: events that some
            // unpersisted conflicted event can reach backwards, and some
            // persisted conflicted event can reach forwards.
            let subgraph_frontier: HashSet<Arc<str>> = subgraph
                .intersection(&additional_backwards_reachable)
                .cloned()
                .collect();

            // Walk forwards from the frontier, and from the unpersisted
            // conflicted events (for subgraphs entirely among unpersisted
            // events), into the unpersisted events.
            let mut forwards_conflicted_set: HashSet<Arc<str>> = HashSet::new();
            let mut search_queue: Vec<Arc<str>> = subgraph_frontier
                .union(&unpersisted_conflicted_events)
                .cloned()
                .collect();
            while let Some(frontier_event) = search_queue.pop() {
                if !forwards_conflicted_set.insert(Arc::clone(&frontier_event)) {
                    continue;
                }
                if let Some(next_event_ids) = event_to_next_event.get(&frontier_event) {
                    search_queue.extend(next_event_ids.iter().cloned());
                }
            }

            // Backwards from the unpersisted conflicted events is their auth
            // chain.
            let mut backwards_conflicted_set: HashSet<Arc<str>> = HashSet::new();
            for uce in &unpersisted_conflicted_events {
                if let Some(chain) = events_to_auth_chain.get(uce) {
                    backwards_conflicted_set.extend(chain.iter().cloned());
                }
            }

            subgraph.extend(
                backwards_conflicted_set
                    .intersection(&forwards_conflicted_set)
                    .cloned(),
            );
        }
    }

    match conflicted_subgraph {
        Some(subgraph) if !subgraph.is_empty() => {
            debug!(
                "v2.1 {room_id} num_conflicts={num_conflicted_state:?} conflicted_subgraph={} auth_difference={}",
                subgraph.len(),
                auth_difference.len()
            );
            // State res v2.1 includes the conflicted subgraph.
            auth_difference.extend(subgraph);
            Ok(auth_difference)
        }
        _ => Ok(auth_difference),
    }
}

/// Load every event [`resolve_conflicted_set`] can read into `event_map`.
///
/// 1. The full conflicted set and the base state.
/// 2. The auth events of those.
/// 3. For every power levels event, its auth events, repeated until no new
///    power levels events turn up. The mainline sort walks from a power
///    levels event to the power levels event in its auth events, and from
///    each other event to the power levels event in its auth events.
///
/// Events that are still missing do not exist; the algorithm treats them as
/// the Python's `allow_none=True` does.
pub async fn prefetch(
    store: &dyn StateResStore,
    event_map: &mut EventMap,
    full_conflicted_set: &HashSet<Arc<str>>,
    base_state: &StateMap,
) -> anyhow::Result<()> {
    // IDs we asked for and the store did not have.
    let mut absent: HashSet<Arc<str>> = HashSet::new();

    async fn load(
        store: &dyn StateResStore,
        event_map: &mut EventMap,
        absent: &mut HashSet<Arc<str>>,
        ids: impl IntoIterator<Item = Arc<str>>,
    ) -> anyhow::Result<()> {
        let wanted: Vec<Arc<str>> = ids
            .into_iter()
            .filter(|id| !event_map.contains_key(id) && !absent.contains(id))
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        if wanted.is_empty() {
            return Ok(());
        }
        let loaded = store.get_events(&wanted).await?;
        for id in wanted {
            if !loaded.contains_key(&id) {
                absent.insert(id);
            }
        }
        event_map.extend(loaded);
        Ok(())
    }

    // 1.
    let roots: Vec<Arc<str>> = full_conflicted_set
        .iter()
        .chain(base_state.values())
        .cloned()
        .collect();
    load(store, event_map, &mut absent, roots.iter().cloned()).await?;

    // 2.
    let auth_ids: Vec<Arc<str>> = roots
        .iter()
        .filter_map(|id| event_map.get(id))
        .flat_map(|event| event.auth_event_ids().iter().cloned())
        .collect();
    load(store, event_map, &mut absent, auth_ids).await?;

    // 3.
    let mut expanded: HashSet<Arc<str>> = HashSet::new();
    loop {
        let mut wanted: Vec<Arc<str>> = Vec::new();
        for (id, event) in event_map.iter() {
            if is_key(event, POWER_LEVELS_KEY) && expanded.insert(Arc::clone(id)) {
                wanted.extend(event.auth_event_ids().iter().cloned());
            }
        }
        wanted.retain(|id| !event_map.contains_key(id) && !absent.contains(id));
        if wanted.is_empty() {
            break;
        }
        load(store, event_map, &mut absent, wanted).await?;
    }

    Ok(())
}

fn is_key(event: &StateResEvent, key: (&str, &str)) -> bool {
    event.type_() == key.0 && event.state_key() == Some(key.1)
}

/// Port of `_is_power_event`.
pub fn is_power_event(event: &StateResEvent) -> anyhow::Result<bool> {
    if is_key(event, POWER_LEVELS_KEY)
        || is_key(event, (M_ROOM_JOIN_RULES, ""))
        || is_key(event, CREATE_KEY)
    {
        return Ok(true);
    }

    if event.type_() == M_ROOM_MEMBER {
        let membership = event_auth::membership_of(event).map_err(anyhow::Error::from)?;
        if matches!(membership.as_str(), Some("leave" | "ban")) {
            // Python compares `sender != state_key`, and a non-state event
            // raises `AttributeError` reading `state_key`.
            let Some(state_key) = event.state_key() else {
                bail!("member event {} has no state_key", event.event_id());
            };
            return Ok(event.sender() != state_key);
        }
    }

    Ok(false)
}

/// The key material for `_conflict_cache_key`, byte-for-byte what the Python
/// `json.dumps` produces, so the two implementations agree on keys.
fn conflict_cache_key(
    room_id: &str,
    room_version: &RoomVersion,
    full_conflicted_set: &HashSet<Arc<str>>,
    base_state: &StateMap,
) -> [u8; 32] {
    let mut conflicted: Vec<&str> = full_conflicted_set.iter().map(|id| &**id).collect();
    conflicted.sort_unstable();
    let mut base: Vec<&str> = base_state.values().map(|id| &**id).collect();
    base.sort_unstable();

    let mut material = String::new();
    material.push_str("{\"room_id\": ");
    push_py_json_str(&mut material, room_id);
    material.push_str(", \"room_version\": ");
    push_py_json_str(&mut material, room_version.identifier);
    material.push_str(", \"conflicted_set\": ");
    push_py_json_list(&mut material, &conflicted);
    material.push_str(", \"base_state\": ");
    push_py_json_list(&mut material, &base);
    material.push('}');

    Sha256::digest(material.as_bytes()).into()
}

fn push_py_json_list(out: &mut String, items: &[&str]) {
    out.push('[');
    for (i, item) in items.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        push_py_json_str(out, item);
    }
    out.push(']');
}

/// Encode a string as Python's `json.dumps` does with `ensure_ascii=True`.
fn push_py_json_str(out: &mut String, s: &str) {
    use std::fmt::Write;

    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    write!(out, "\\u{unit:04x}").expect("writing to a String");
                }
            }
        }
    }
    out.push('"');
}

/// How many iterations of a loop in the core run between yields to the
/// scheduler. Matches `_AWAIT_AFTER_ITERATIONS` in the Python.
pub const YIELD_EVERY: usize = 100;

/// Yields to the scheduler every [`YIELD_EVERY`] calls.
#[derive(Default)]
struct Yielder(usize);

impl Yielder {
    async fn tick(&mut self) {
        self.0 += 1;
        if self.0 % YIELD_EVERY == 0 {
            tokio::task::yield_now().await;
        }
    }
}

/// The part of the algorithm that runs over a fully loaded event map.
pub struct Core<'a> {
    pub room_id: &'a str,
    pub room_version: &'static RoomVersion,
    pub events: &'a EventMap,
}

impl<'a> Core<'a> {
    /// Port of `_get_event` with `allow_none=True`.
    fn get_opt(&self, event_id: &str) -> anyhow::Result<Option<&'a StateResEvent>> {
        let Some(event) = self.events.get(event_id) else {
            return Ok(None);
        };
        if event.room_id() != self.room_id {
            bail!(
                "In state res for room {}, event {event_id} is in {}",
                self.room_id,
                event.room_id()
            );
        }
        Ok(Some(event))
    }

    /// Port of `_get_event`.
    fn get(&self, event_id: &str) -> anyhow::Result<&'a StateResEvent> {
        match self.get_opt(event_id)? {
            Some(event) => Ok(event),
            None => bail!("Unknown event {event_id}"),
        }
    }

    /// Apply the conflicted events to the base state. Port of
    /// `_resolve_conflicted_set`.
    ///
    /// Returns the base state with the conflicted events that pass auth
    /// applied over it, recording the time each stage took in `stats`.
    pub async fn resolve_conflicted_set(
        &self,
        full_conflicted_set: &HashSet<Arc<str>>,
        base_state: StateMap,
        stats: &mut ResolveStats,
    ) -> anyhow::Result<StateMap> {
        // Sort the power events (kicks, bans, etc).
        let start = Instant::now();
        let mut yielder = Yielder::default();
        let mut power_events = Vec::new();
        for event_id in full_conflicted_set {
            yielder.tick().await;
            if is_power_event(self.get(event_id)?)? {
                power_events.push(Arc::clone(event_id));
            }
        }
        let sorted_power_events = self
            .reverse_topological_power_sort(&power_events, full_conflicted_set)
            .await?;
        stats.sort_time = start.elapsed();
        debug!("sorted {} power events", sorted_power_events.len());

        // Auth each one in turn.
        let start = Instant::now();
        let resolved_state = self
            .iterative_auth_checks(&sorted_power_events, base_state)
            .await?;
        stats.auth_time = start.elapsed();

        // Sort the remaining events by the mainline of the resolved power
        // levels.
        let start = Instant::now();
        let power_set: HashSet<&Arc<str>> = sorted_power_events.iter().collect();
        let leftover_events: Vec<Arc<str>> = full_conflicted_set
            .iter()
            .filter(|id| !power_set.contains(id))
            .cloned()
            .collect();
        debug!("sorting {} remaining events", leftover_events.len());
        let pl = lookup(&resolved_state, POWER_LEVELS_KEY).cloned();
        let leftover_events = self.mainline_sort(leftover_events, pl.as_deref()).await?;
        stats.mainline_time = start.elapsed();

        let start = Instant::now();
        let resolved_state = self
            .iterative_auth_checks(&leftover_events, resolved_state)
            .await?;
        stats.auth_time += start.elapsed();

        Ok(resolved_state)
    }

    /// Port of `_reverse_topological_power_sort`: the events and their auth
    /// chains within the full conflicted set, in reverse topological order,
    /// ties broken by sender power level, then `origin_server_ts`, then event
    /// ID.
    async fn reverse_topological_power_sort(
        &self,
        event_ids: &[Arc<str>],
        full_conflicted_set: &HashSet<Arc<str>>,
    ) -> anyhow::Result<Vec<Arc<str>>> {
        // Event ID to its auth events in the full conflicted set.
        let mut graph: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
        let mut yielder = Yielder::default();
        for event_id in event_ids {
            // Port of `_add_event_and_auth_chain_to_graph`.
            let mut state = vec![Arc::clone(event_id)];
            while let Some(eid) = state.pop() {
                yielder.tick().await;
                graph.entry(Arc::clone(&eid)).or_default();
                let event = self.get(&eid)?;
                for aid in event.auth_event_ids() {
                    if full_conflicted_set.contains(aid) {
                        if !graph.contains_key(aid) {
                            state.push(Arc::clone(aid));
                        }
                        graph
                            .get_mut(&eid)
                            .expect("inserted above")
                            .insert(Arc::clone(aid));
                    }
                }
            }
        }

        let mut event_to_pl: HashMap<Arc<str>, i64> = HashMap::with_capacity(graph.len());
        for event_id in graph.keys() {
            yielder.tick().await;
            let pl = self.get_power_level_for_sender(event_id)?;
            event_to_pl.insert(Arc::clone(event_id), pl);
        }

        let key = |event_id: &Arc<str>| -> anyhow::Result<(i64, i64, Arc<str>)> {
            let event = self.get(event_id)?;
            Ok((
                -event_to_pl[event_id],
                event.origin_server_ts(),
                Arc::clone(event_id),
            ))
        };

        lexicographical_topological_sort(graph, key)
    }

    /// The power level of the sender of the event according to its auth
    /// events. Port of `_get_power_level_for_sender`.
    pub fn get_power_level_for_sender(&self, event_id: &str) -> anyhow::Result<i64> {
        let event = self.get(event_id)?;

        let mut pl = None;
        let mut create = None;
        for aid in event.auth_event_ids() {
            if let Some(aev) = self.get_opt(aid)? {
                if is_key(aev, POWER_LEVELS_KEY) {
                    pl = Some(aev);
                }
                if is_key(aev, CREATE_KEY) {
                    create = Some(aev);
                }
            }
        }

        if event.type_() != CREATE_KEY.0 && create.is_none() {
            // We should always have a create event.
            bail!("event {event_id} has no create event in its auth events");
        }

        if let Some(create) = create {
            if create.room_version().msc4289_creator_power_enabled
                && is_creator(create, event.sender()).map_err(anyhow::Error::from)?
            {
                return Ok(CREATOR_POWER_LEVEL);
            }
        }

        let Some(pl) = pl else {
            // No power levels: check if they're the creator of the room.
            for aid in event.auth_event_ids() {
                let Some(aev) = self.get_opt(aid)? else {
                    continue;
                };
                if is_key(aev, CREATE_KEY) {
                    let is_creator = if event.room_version().implicit_room_creator {
                        aev.sender() == event.sender()
                    } else {
                        match aev.content().get_field("creator") {
                            Some(creator) => creator.as_str() == Some(event.sender()),
                            None => {
                                warn!(
                                    "_get_power_level_for_sender: event {event_id} has no PL in \
                                     auth_events and creator is missing from create event {}",
                                    aev.event_id()
                                );
                                false
                            }
                        }
                    };
                    if is_creator {
                        return Ok(100);
                    }
                    break;
                }
            }
            return Ok(0);
        };

        let content = pl.content();
        let users =
            sub_mapping(content.get_field("users"), "users").map_err(anyhow::Error::from)?;
        let level = users
            .and_then(|users| users.get(event.sender()))
            .filter(|v| !v.is_null());
        // `content.get("users_default", 0)`: a present null means 0.
        let level = match level {
            Some(level) => Some(level),
            None => match content.get_field("users_default") {
                None => return Ok(0),
                Some(Value::Null) => None,
                Some(level) => Some(level),
            },
        };
        match level {
            None => Ok(0),
            Some(level) => py_int(level).map_err(anyhow::Error::from),
        }
    }

    /// Apply auth checks to each event in order, updating the state as we
    /// go. Port of `_iterative_auth_checks`.
    async fn iterative_auth_checks(
        &self,
        event_ids: &[Arc<str>],
        base_state: StateMap,
    ) -> anyhow::Result<StateMap> {
        let mut resolved_state = base_state;

        let mut yielder = Yielder::default();
        for event_id in event_ids {
            yielder.tick().await;
            let event = self
                .events
                .get(event_id)
                .with_context(|| format!("Unknown event {event_id}"))?;

            let mut auth_events: AuthMap<'a> = HashMap::new();
            for aid in event.auth_event_ids() {
                match self.get_opt(aid)? {
                    None => warn!("auth_event id {aid} for event {event_id} is missing"),
                    Some(ev) => {
                        if ev.rejected_reason().is_none() {
                            let Some(state_key) = ev.state_key() else {
                                bail!("auth event {aid} is not a state event");
                            };
                            auth_events.insert((ev.type_(), state_key), ev);
                        }
                    }
                }
            }

            for key in
                auth_types_for_event(self.room_version, event).map_err(anyhow::Error::from)?
            {
                if let Some(ev_id) = lookup(&resolved_state, key) {
                    let ev = self.get(ev_id)?;
                    if ev.rejected_reason().is_none() {
                        // Keyed as the Python does: by the auth type, not
                        // the event's own (type, state_key).
                        auth_events.insert(key, ev);
                    }
                }
            }

            if event.rejected_reason().is_some() {
                // Do not admit previously rejected events into state.
                //
                // TODO: This isn't spec compliant. Events that were previously
                // rejected due to failing auth checks at their state, but pass
                // auth checks during state resolution should be accepted.
                // https://github.com/matrix-org/synapse/issues/13797
                continue;
            }

            match check_state_dependent_auth_rules_with_map(event, &auth_events) {
                Ok(()) => {
                    let Some(state_key) = event.state_key() else {
                        bail!("event {event_id} in the conflicted set is not a state event");
                    };
                    resolved_state.insert(
                        StateKey::new(event.type_(), state_key),
                        Arc::clone(event_id),
                    );
                }
                Err(failure @ AuthFailure::Rejected { .. }) => {
                    debug!("{event_id} failed auth in state res: {failure}");
                }
                Err(failure @ AuthFailure::Invalid { .. }) => {
                    return Err(anyhow::Error::from(failure)
                        .context(format!("auth checks for {event_id} in state res")));
                }
            }
        }

        Ok(resolved_state)
    }

    /// Port of `_mainline_sort`: sort the events by the mainline of the
    /// resolved power levels event.
    async fn mainline_sort(
        &self,
        mut event_ids: Vec<Arc<str>>,
        resolved_power_event_id: Option<&str>,
    ) -> anyhow::Result<Vec<Arc<str>>> {
        if event_ids.is_empty() {
            return Ok(event_ids);
        }

        let mut yielder = Yielder::default();
        let mut mainline: Vec<&'a str> = Vec::new();
        let mut seen: HashSet<&str> = HashSet::new();
        let mut pl = resolved_power_event_id.map(|id| self.get(id)).transpose()?;
        while let Some(pl_ev) = pl {
            yielder.tick().await;
            if !seen.insert(pl_ev.event_id()) {
                bail!("cycle in the power levels chain at {}", pl_ev.event_id());
            }
            mainline.push(pl_ev.event_id());
            pl = None;
            for aid in pl_ev.auth_event_ids() {
                if let Some(ev) = self.get_opt(aid)? {
                    if is_key(ev, POWER_LEVELS_KEY) {
                        pl = Some(ev);
                        break;
                    }
                }
            }
        }

        let mainline_map: HashMap<&str, i64> = mainline
            .iter()
            .rev()
            .enumerate()
            .map(|(i, ev_id)| (*ev_id, i as i64 + 1))
            .collect();

        let mut order_map: HashMap<Arc<str>, (i64, i64, Arc<str>)> =
            HashMap::with_capacity(event_ids.len());
        for ev_id in &event_ids {
            yielder.tick().await;
            let event = self.get(ev_id)?;
            let depth = self.get_mainline_depth_for_event(event, &mainline_map)?;
            order_map.insert(
                Arc::clone(ev_id),
                (depth, event.origin_server_ts(), Arc::clone(ev_id)),
            );
        }

        event_ids.sort_by(|a, b| order_map[a].cmp(&order_map[b]));
        Ok(event_ids)
    }

    /// Port of `_get_mainline_depth_for_event`: follow the power levels
    /// events in the auth events until one is on the mainline.
    fn get_mainline_depth_for_event(
        &self,
        event: &'a StateResEvent,
        mainline_map: &HashMap<&str, i64>,
    ) -> anyhow::Result<i64> {
        let mut tmp_event = Some(event);
        let mut seen: HashSet<&str> = HashSet::new();
        while let Some(current) = tmp_event {
            if let Some(depth) = mainline_map.get(current.event_id()) {
                return Ok(*depth);
            }
            if !seen.insert(current.event_id()) {
                bail!("cycle in the power levels chain at {}", current.event_id());
            }

            tmp_event = None;
            for aid in current.auth_event_ids() {
                if let Some(aev) = self.get_opt(aid)? {
                    if is_key(aev, POWER_LEVELS_KEY) {
                        tmp_event = Some(aev);
                        break;
                    }
                }
            }
        }

        // Didn't find a power level auth event, so we just return 0.
        Ok(0)
    }
}

/// Port of `lexicographical_topological_sort`: Kahn's algorithm over the
/// nodes with no outgoing edges, ties broken by `key`. If A references B, B
/// comes before A.
pub fn lexicographical_topological_sort<K: Ord>(
    graph: HashMap<Arc<str>, HashSet<Arc<str>>>,
    key: impl Fn(&Arc<str>) -> anyhow::Result<K>,
) -> anyhow::Result<Vec<Arc<str>>> {
    let mut outdegree: HashMap<Arc<str>, usize> = HashMap::with_capacity(graph.len());
    let mut reverse_graph: HashMap<Arc<str>, Vec<Arc<str>>> = HashMap::with_capacity(graph.len());
    let mut zero_outdegree = BinaryHeap::new();

    for (node, edges) in &graph {
        if edges.is_empty() {
            zero_outdegree.push(Reverse((key(node)?, Arc::clone(node))));
        }
        outdegree.insert(Arc::clone(node), edges.len());
        reverse_graph.entry(Arc::clone(node)).or_default();
        for edge in edges {
            reverse_graph
                .entry(Arc::clone(edge))
                .or_default()
                .push(Arc::clone(node));
        }
    }

    let mut sorted = Vec::with_capacity(graph.len());
    while let Some(Reverse((_, node))) = zero_outdegree.pop() {
        for parent in reverse_graph.get(&node).into_iter().flatten() {
            let out = outdegree
                .get_mut(parent)
                .expect("every parent is a node in the graph");
            *out -= 1;
            if *out == 0 {
                zero_outdegree.push(Reverse((key(parent)?, Arc::clone(parent))));
            }
        }
        sorted.push(node);
    }

    Ok(sorted)
}

#[cfg(test)]
mod tests;
