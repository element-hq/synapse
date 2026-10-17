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

//! The chain cover index of a chunk's state events: a port of
//! `calculate_chain_cover_index_for_events` and the class methods it calls
//! in `synapse/storage/databases/main/events.py`, with `_LinkMap` and
//! `sorted_topologically`. See `docs/auth_chain_difference_algorithm.md`.
//!
//! As in Python, the step runs in a write transaction of its own, before
//! the main one and not retried with it. It reads, allocates chain IDs from
//! the `event_auth_chain_id` sequence, and puts off the events whose auth
//! events it lacks by writing them to `event_auth_chain_to_calculate`. It
//! does not write the index itself. The main transaction does that.
//!
//! The reads and writes go through [`ChainCoverStore`], so that the unit
//! tests can fake them. The result matches Python's except in two orders
//! that Python leaves to chance:
//!
//! - New chains get their IDs in the order the events are allocated
//!   (topological, ties by event ID). Python zips a set of placeholder
//!   objects with the IDs, so which new chain gets which ID varies.
//! - An event's links are in the order of its auth events. Python builds
//!   them from a set.
//!
//! The redundant-link reduction checks every ordered pair of auth events,
//! as Python does, so that the index matches Python's link for link.

use std::{
    cmp::Reverse,
    collections::{BinaryHeap, HashMap, HashSet},
    sync::Arc,
};

use anyhow::{anyhow, ensure};
use async_trait::async_trait;
use futures::FutureExt;

use super::reader::{self, AuthEventChain, ChainLink, ChainToCalculate};
use crate::{
    database::postgres::{
        pool::ConnectionPool,
        write::{run_rolled_back_interaction, run_write_interaction, WriteTxn},
    },
    events::Event,
};

/// The sequence `event_chain_id_gen` allocates from.
const CHAIN_ID_SEQUENCE: &str = "event_auth_chain_id";

/// A chain position: `(chain ID, sequence number)`.
type Position = (i64, i64);

/// `NewEventChainLinks`: the chain position allocated to a new event, and
/// the links to add from it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewEventChainLinks {
    pub chain_id: i64,
    pub sequence_number: i64,
    /// The `(chain ID, sequence number)` targets of the links.
    pub links: Vec<Position>,
}

/// What the chain cover step reads from a state event being persisted.
#[derive(Debug, Clone)]
pub struct ChainCoverEvent {
    pub event_id: Arc<str>,
    pub event_type: Arc<str>,
    pub state_key: Arc<str>,
    /// The auth event IDs, or why the event could not give them. Python
    /// reads them only for events not yet in the index, so the step fails
    /// only for those.
    pub auth_event_ids: Result<Vec<Arc<str>>, String>,
}

impl ChainCoverEvent {
    /// The view of a state event, or `None` for an event that is not one.
    pub fn new(event: &Event) -> Option<Self> {
        let state_key = event.state_key()?;
        Some(ChainCoverEvent {
            event_id: Arc::from(event.event_id()),
            event_type: Arc::from(event.type_()),
            state_key: Arc::from(state_key),
            auth_event_ids: event
                .auth_event_ids()
                .map(|ids| ids.into_iter().map(Arc::from).collect())
                .map_err(|e| e.to_string()),
        })
    }
}

/// What the chain cover step reads and writes, inside its transaction.
#[async_trait]
pub trait ChainCoverStore: Send + Sync {
    /// See [`reader::has_auth_chain_index`].
    async fn has_auth_chain_index(&self, room_id: &str) -> anyhow::Result<bool>;

    /// See [`reader::events_with_chain_ids`].
    async fn events_with_chain_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashSet<Arc<str>>>;

    /// See [`reader::chains_to_calculate`].
    async fn chains_to_calculate(&self, room_id: &str) -> anyhow::Result<Vec<ChainToCalculate>>;

    /// See [`reader::auth_event_ids`].
    async fn auth_event_ids(&self, event_id: &str) -> anyhow::Result<Vec<Arc<str>>>;

    /// See [`reader::auth_event_chains`].
    async fn auth_event_chains(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<Vec<AuthEventChain>>;

    /// See [`reader::chain_links`].
    async fn chain_links(&self, chains: Vec<i64>) -> anyhow::Result<Vec<ChainLink>>;

    /// See [`reader::max_sequence_numbers`].
    async fn max_sequence_numbers(&self, chain_ids: &[i64]) -> anyhow::Result<HashMap<i64, i64>>;

    /// Record that the event's chain must wait for its auth events, in
    /// `event_auth_chain_to_calculate`.
    async fn put_off(
        &self,
        room_id: &str,
        event_id: &str,
        event_type: &str,
        state_key: &str,
    ) -> anyhow::Result<()>;

    /// Allocate `n` new chain IDs.
    async fn next_chain_ids(&self, n: usize) -> anyhow::Result<Vec<i64>>;
}

#[async_trait]
impl ChainCoverStore for WriteTxn {
    async fn has_auth_chain_index(&self, room_id: &str) -> anyhow::Result<bool> {
        reader::has_auth_chain_index(self, room_id).await
    }

    async fn events_with_chain_ids(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        reader::events_with_chain_ids(self, event_ids).await
    }

    async fn chains_to_calculate(&self, room_id: &str) -> anyhow::Result<Vec<ChainToCalculate>> {
        reader::chains_to_calculate(self, &[room_id]).await
    }

    async fn auth_event_ids(&self, event_id: &str) -> anyhow::Result<Vec<Arc<str>>> {
        reader::auth_event_ids(self, event_id).await
    }

    async fn auth_event_chains(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<Vec<AuthEventChain>> {
        reader::auth_event_chains(self, event_ids).await
    }

    async fn chain_links(&self, chains: Vec<i64>) -> anyhow::Result<Vec<ChainLink>> {
        reader::chain_links(self, chains).await
    }

    async fn max_sequence_numbers(&self, chain_ids: &[i64]) -> anyhow::Result<HashMap<i64, i64>> {
        reader::max_sequence_numbers(self, chain_ids).await
    }

    async fn put_off(
        &self,
        room_id: &str,
        event_id: &str,
        event_type: &str,
        state_key: &str,
    ) -> anyhow::Result<()> {
        self.execute(
            "INSERT INTO event_auth_chain_to_calculate (event_id, room_id, type, state_key)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (event_id) DO UPDATE SET
                room_id = EXCLUDED.room_id,
                type = EXCLUDED.type,
                state_key = EXCLUDED.state_key",
            &[&event_id, &room_id, &event_type, &state_key],
        )
        .await?;
        Ok(())
    }

    async fn next_chain_ids(&self, n: usize) -> anyhow::Result<Vec<i64>> {
        if n == 0 {
            return Ok(Vec::new());
        }
        let n = i64::try_from(n)?;
        let rows = self
            .query(
                "SELECT nextval($1::text) FROM generate_series(1, $2::bigint)",
                &[&CHAIN_ID_SEQUENCE, &n],
            )
            .await?;
        rows.iter()
            .map(|row| Ok(row.try_get::<_, i64>(0)?))
            .collect()
    }
}

/// `calculate_chain_cover_index_for_events`: the chain positions and links
/// of the state events, all of `room_id`, in a write transaction on `pool`.
/// Events already in the index, and events whose auth events are missing,
/// are left out. The result can also hold events that are not in `events`:
/// those put off by an earlier chunk, and auth events persisted out of
/// band, once their auth events are known.
///
/// A `dry_run` rolls the transaction back, so that nothing it puts off is
/// written. Its chain IDs are still used up, as the sequence is not
/// transactional.
pub async fn calculate_chain_cover_index_for_events(
    pool: &ConnectionPool,
    room_id: &str,
    events: &[ChainCoverEvent],
    dry_run: bool,
) -> anyhow::Result<HashMap<Arc<str>, NewEventChainLinks>> {
    // Ensure there are no duplicates.
    let mut seen = HashSet::new();
    let state_events: Vec<&ChainCoverEvent> = events
        .iter()
        .filter(|event| seen.insert(Arc::clone(&event.event_id)))
        .collect();
    if state_events.is_empty() {
        return Ok(HashMap::new());
    }
    let state_events = &state_events;
    let name = "_calculate_chain_cover_index_for_events";
    // The closure is written out twice: bound to a variable, it would lose
    // the lifetimes the interactions need.
    Ok(if dry_run {
        run_rolled_back_interaction(pool, name, |txn, _| {
            async move {
                Ok(calculate_chain_cover_index_for_events_txn(txn, room_id, state_events).await?)
            }
            .boxed()
        })
        .await?
    } else {
        run_write_interaction(pool, name, |txn, _| {
            async move {
                Ok(calculate_chain_cover_index_for_events_txn(txn, room_id, state_events).await?)
            }
            .boxed()
        })
        .await?
    })
}

/// `calculate_chain_cover_index_for_events_txn`, over deduplicated state
/// events.
pub async fn calculate_chain_cover_index_for_events_txn(
    store: &dyn ChainCoverStore,
    room_id: &str,
    state_events: &[&ChainCoverEvent],
) -> anyhow::Result<HashMap<Arc<str>, NewEventChainLinks>> {
    // We ignore legacy rooms that we aren't filling the chain cover index
    // for.
    if !store.has_auth_chain_index(room_id).await? {
        return Ok(HashMap::new());
    }

    // Filter out events that we've already calculated.
    let ids: Vec<Arc<str>> = state_events
        .iter()
        .map(|event| Arc::clone(&event.event_id))
        .collect();
    let already_persisted = store.events_with_chain_ids(&ids).await?;
    let state_events: Vec<&ChainCoverEvent> = state_events
        .iter()
        .copied()
        .filter(|event| !already_persisted.contains(&event.event_id))
        .collect();
    if state_events.is_empty() {
        return Ok(HashMap::new());
    }

    let mut graph = AuthGraph::default();
    for event in &state_events {
        let auth_events = event
            .auth_event_ids
            .clone()
            .map_err(|e| anyhow!("auth events of {}: {e}", event.event_id))?;
        graph.types.insert(
            Arc::clone(&event.event_id),
            (Arc::clone(&event.event_type), Arc::clone(&event.state_key)),
        );
        graph
            .auth_events
            .insert(Arc::clone(&event.event_id), auth_events);
    }
    let persisting = ids_of(&state_events);
    calculate_chain_cover_index(store, room_id, &persisting, graph).await
}

/// The type, state key and auth events of the events the step looks at.
#[derive(Default)]
struct AuthGraph {
    /// `event_to_types`.
    types: HashMap<Arc<str>, (Arc<str>, Arc<str>)>,
    /// `event_to_auth_chain`: each event's auth event IDs.
    auth_events: HashMap<Arc<str>, Vec<Arc<str>>>,
}

fn ids_of(events: &[&ChainCoverEvent]) -> HashSet<Arc<str>> {
    events
        .iter()
        .map(|event| Arc::clone(&event.event_id))
        .collect()
}

/// `_calculate_chain_cover_index` for the events of `persisting`, all in
/// `room_id`.
async fn calculate_chain_cover_index(
    store: &dyn ChainCoverStore,
    room_id: &str,
    persisting: &HashSet<Arc<str>>,
    mut graph: AuthGraph,
) -> anyhow::Result<HashMap<Arc<str>, NewEventChainLinks>> {
    // Map from event ID to chain ID/sequence number.
    let mut chain_map: HashMap<Arc<str>, Position> = HashMap::new();

    // Set of event IDs to calculate chain ID/seq numbers for.
    let mut to_calculate = persisting.clone();

    // Any events of the room that still need handling. These should be out
    // of band memberships, whose auth chains we didn't have when we first
    // persisted them.
    for row in store.chains_to_calculate(room_id).await? {
        let auth_events = store.auth_event_ids(&row.event_id).await?;
        to_calculate.insert(Arc::clone(&row.event_id));
        graph
            .types
            .insert(Arc::clone(&row.event_id), (row.event_type, row.state_key));
        graph.auth_events.insert(row.event_id, auth_events);
    }

    // The chain positions of the auth events not being calculated here. An
    // auth event without one was persisted out of band, so it joins the
    // events to calculate, and we loop to fetch its own auth events.
    let mut missing: HashSet<Arc<str>> = graph
        .auth_events
        .values()
        .flatten()
        .filter(|auth_id| !to_calculate.contains(*auth_id))
        .cloned()
        .collect();
    while !missing.is_empty() {
        let ids: Vec<Arc<str>> = missing.drain().collect();
        for row in store.auth_event_chains(&ids).await? {
            graph
                .types
                .insert(Arc::clone(&row.event_id), (row.event_type, row.state_key));
            match row.chain {
                Some(position) => {
                    chain_map.insert(row.event_id, position);
                }
                None => {
                    to_calculate.insert(Arc::clone(&row.event_id));
                    let auth_events = store.auth_event_ids(&row.event_id).await?;
                    missing.extend(
                        auth_events
                            .iter()
                            .filter(|auth_id| !graph.types.contains_key(*auth_id))
                            .cloned(),
                    );
                    graph.auth_events.insert(row.event_id, auth_events);
                }
            }
        }
    }

    // Drop the events with an auth event that has no chain and is not being
    // calculated. That should only be out of band memberships. An event
    // being persisted is put off until its auth events arrive. In
    // topological order, so that dropping an event drops those it auths.
    for event_id in sorted_topologically(graph.auth_events.keys(), &graph.auth_events) {
        let has_missing_auth = graph.auth_events[&event_id]
            .iter()
            .any(|auth_id| !chain_map.contains_key(auth_id) && !to_calculate.contains(auth_id));
        if !has_missing_auth {
            continue;
        }
        to_calculate.remove(&event_id);
        if persisting.contains(&event_id) {
            let (event_type, state_key) = graph
                .types
                .get(&event_id)
                .ok_or_else(|| anyhow!("no type for {event_id}"))?;
            store
                .put_off(room_id, &event_id, event_type, state_key)
                .await?;
        }
    }

    if to_calculate.is_empty() {
        return Ok(HashMap::new());
    }

    let new_positions = allocate_chain_ids(store, &graph, &to_calculate, &chain_map).await?;
    chain_map.extend(new_positions.iter().map(|(id, pos)| (Arc::clone(id), *pos)));

    let mut to_return: HashMap<Arc<str>, NewEventChainLinks> = new_positions
        .into_iter()
        .map(|(event_id, (chain_id, sequence_number))| {
            (
                event_id,
                NewEventChainLinks {
                    chain_id,
                    sequence_number,
                    links: Vec::new(),
                },
            )
        })
        .collect();

    // The existing links of every chain we have seen, so that new links
    // reachable through them are left out.
    let mut chain_links = LinkMap::default();
    let chains: HashSet<i64> = chain_map.values().map(|(chain_id, _)| *chain_id).collect();
    for link in store.chain_links(chains.into_iter().collect()).await? {
        chain_links.add_link(
            (link.origin_chain_id, link.origin_sequence_number),
            (link.target_chain_id, link.target_sequence_number),
            false,
        )?;
    }

    let position_of = |event_id: &Arc<str>| {
        chain_map
            .get(event_id)
            .copied()
            .ok_or_else(|| anyhow!("no chain position for {event_id}"))
    };

    // In topological order, so that an event's links are added before the
    // links of the events it auths.
    for event_id in sorted_topologically(&to_calculate, &graph.auth_events) {
        let position = position_of(&event_id)?;
        let auth_events = graph
            .auth_events
            .get(&event_id)
            .map(Vec::as_slice)
            .unwrap_or_default();

        // Leave out the auth events on the event's own chain, and those
        // reachable from another of its auth events. Every ordered pair of
        // auth events (by position in the list, as `itertools.permutations`
        // takes them) is checked.
        let mut reduction: Vec<&Arc<str>> = Vec::new();
        for auth_id in auth_events {
            if position_of(auth_id)?.0 != position.0 && !reduction.contains(&auth_id) {
                reduction.push(auth_id);
            }
        }
        for (i, start) in auth_events.iter().enumerate() {
            for (j, end) in auth_events.iter().enumerate() {
                if i != j && chain_links.exists_path_from(position_of(start)?, position_of(end)?) {
                    reduction.retain(|auth_id| *auth_id != end);
                }
            }
        }

        let new_links = to_return
            .get_mut(&event_id)
            .ok_or_else(|| anyhow!("no chain allocated for {event_id}"))?;
        for auth_id in reduction {
            let target = position_of(auth_id)?;
            new_links.links.push(target);
            chain_links.add_link(position, target, true)?;
        }
    }

    Ok(to_return)
}

/// A chain ID during allocation: an existing chain, or the nth new one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum ChainId {
    Existing(i64),
    New(usize),
}

/// `_allocate_chain_ids`: chain positions for the events of `to_calculate`,
/// allocated but not persisted. An event extends the chain of its first
/// auth event with the same type and state key, if that chain has no
/// event after it yet. Otherwise it starts a new chain.
async fn allocate_chain_ids(
    store: &dyn ChainCoverStore,
    graph: &AuthGraph,
    to_calculate: &HashSet<Arc<str>>,
    chain_map: &HashMap<Arc<str>, Position>,
) -> anyhow::Result<HashMap<Arc<str>, Position>> {
    // Each event and the auth event whose chain it would extend, auth
    // events first. This turns the auth graph into a tree.
    let mut existing_chains: HashSet<i64> = HashSet::new();
    let mut tree: Vec<(Arc<str>, Option<&Arc<str>>)> = Vec::new();
    for event_id in sorted_topologically(to_calculate, &graph.auth_events) {
        let event_type = graph.types.get(&event_id);
        let parent = graph
            .auth_events
            .get(&event_id)
            .into_iter()
            .flatten()
            .find(|auth_id| graph.types.get(*auth_id) == event_type);
        if let Some((chain_id, _)) = parent.and_then(|auth_id| chain_map.get(auth_id)) {
            existing_chains.insert(*chain_id);
        }
        tree.push((event_id, parent));
    }

    let existing_chains: Vec<i64> = existing_chains.into_iter().collect();
    let mut max_sequence_numbers: HashMap<ChainId, i64> = store
        .max_sequence_numbers(&existing_chains)
        .await?
        .into_iter()
        .map(|(chain_id, max)| (ChainId::Existing(chain_id), max))
        .collect();

    // Allocate along the tree, with placeholders for new chains, so that the
    // new chain IDs come from the sequence in one statement.
    let mut new_chains = 0;
    let mut new_positions: Vec<(Arc<str>, (ChainId, i64))> = Vec::with_capacity(tree.len());
    let mut allocated: HashMap<Arc<str>, (ChainId, i64)> = HashMap::new();
    for (event_id, parent) in tree {
        let mut position = None;
        if let Some(auth_id) = parent {
            let (chain_id, sequence_number) = match allocated.get(auth_id) {
                Some(position) => *position,
                None => {
                    let (chain_id, sequence_number) = chain_map
                        .get(auth_id)
                        .ok_or_else(|| anyhow!("no chain position for {auth_id}"))?;
                    (ChainId::Existing(*chain_id), *sequence_number)
                }
            };
            // Extend the chain, unless a later event already has.
            let max = max_sequence_numbers
                .get(&chain_id)
                .ok_or_else(|| anyhow!("no events on chain {chain_id:?}"))?;
            if *max < sequence_number + 1 {
                position = Some((chain_id, sequence_number + 1));
            }
        }
        let position = position.unwrap_or_else(|| {
            new_chains += 1;
            (ChainId::New(new_chains - 1), 1)
        });
        max_sequence_numbers.insert(position.0, position.1);
        allocated.insert(Arc::clone(&event_id), position);
        new_positions.push((event_id, position));
    }

    let new_chain_ids = store.next_chain_ids(new_chains).await?;
    ensure!(
        new_chain_ids.len() == new_chains,
        "asked for {new_chains} chain IDs and got {}",
        new_chain_ids.len()
    );
    Ok(new_positions
        .into_iter()
        .map(|(event_id, (chain_id, sequence_number))| {
            let chain_id = match chain_id {
                ChainId::Existing(chain_id) => chain_id,
                ChainId::New(n) => new_chain_ids[n],
            };
            (event_id, (chain_id, sequence_number))
        })
        .collect())
}

/// `_LinkMap`: the links between chains, without the `additions` that the
/// chain cover step does not use.
#[derive(Default)]
struct LinkMap {
    /// Source chain ID -> target chain ID -> source sequence number ->
    /// target sequence number.
    maps: HashMap<i64, HashMap<i64, HashMap<i64, i64>>>,
}

impl LinkMap {
    /// Add a link between two chains. A `new` link is dropped if an existing
    /// link between the same chains crosses it, and the result says whether
    /// it was kept. New links should be added in topological order.
    fn add_link(&mut self, src: Position, target: Position, new: bool) -> anyhow::Result<bool> {
        let (src_chain, src_seq) = src;
        let (target_chain, target_seq) = target;
        ensure!(
            src_chain != target_chain,
            "a chain link from chain {src_chain} to itself"
        );
        let current_links = self
            .maps
            .entry(src_chain)
            .or_default()
            .entry(target_chain)
            .or_default();

        // A link that starts at or after an existing link and ends at or
        // before it is redundant: anything it reaches, the existing link
        // reaches.
        if new
            && current_links.iter().any(|(&current_src, &current_target)| {
                current_src <= src_seq && target_seq <= current_target
            })
        {
            return Ok(false);
        }

        current_links.insert(src_seq, target_seq);
        Ok(true)
    }

    /// Whether there is a path from the source position to the target.
    fn exists_path_from(&self, src: Position, target: Position) -> bool {
        let (src_chain, src_seq) = src;
        let (target_chain, target_seq) = target;
        if src_chain == target_chain {
            return target_seq <= src_seq;
        }

        // Each chain's highest sequence number searched from so far.
        let mut visited: HashMap<i64, i64> = HashMap::new();
        let mut search = vec![src];
        while let Some((chain, seq)) = search.pop() {
            let highest = visited.entry(chain).or_insert(0);
            *highest = (*highest).max(seq);
            let Some(targets) = self.maps.get(&chain) else {
                continue;
            };
            for (&next_chain, links) in targets {
                for (&link_src, &link_target) in links {
                    // Don't revisit chains we've already seen, unless the
                    // target sequence number is higher than last time.
                    if link_target <= visited.get(&next_chain).copied().unwrap_or(0) {
                        continue;
                    }
                    if link_src <= seq {
                        if next_chain == target_chain {
                            if target_seq <= link_target {
                                return true;
                            }
                        } else {
                            search.push((next_chain, link_target));
                        }
                    }
                }
            }
        }
        false
    }
}

/// `sorted_topologically`: `nodes` in topological order over `graph`, each
/// node after the nodes it points to, by Kahn's algorithm. Among the nodes
/// that are ready, the lowest ID comes first, as Python's heap gives. Nodes
/// on a cycle are left out.
fn sorted_topologically<'a>(
    nodes: impl IntoIterator<Item = &'a Arc<str>>,
    graph: &HashMap<Arc<str>, Vec<Arc<str>>>,
) -> Vec<Arc<str>> {
    let mut degree: HashMap<&Arc<str>, usize> = nodes.into_iter().map(|node| (node, 0)).collect();
    let mut reverse: HashMap<&Arc<str>, HashSet<&Arc<str>>> = HashMap::new();
    for (node, edges) in graph {
        if !degree.contains_key(node) {
            continue;
        }
        let edges: HashSet<&Arc<str>> = edges.iter().collect();
        for edge in edges {
            if degree.contains_key(edge) {
                *degree.get_mut(node).expect("checked above") += 1;
            }
            reverse.entry(edge).or_default().insert(node);
        }
    }

    let mut ready: BinaryHeap<Reverse<&Arc<str>>> = degree
        .iter()
        .filter(|(_, degree)| **degree == 0)
        .map(|(node, _)| Reverse(*node))
        .collect();
    let mut sorted = Vec::with_capacity(degree.len());
    while let Some(Reverse(node)) = ready.pop() {
        for edge in reverse.get(node).into_iter().flatten() {
            if let Some(degree) = degree.get_mut(edge) {
                *degree -= 1;
                if *degree == 0 {
                    ready.push(Reverse(*edge));
                }
            }
        }
        sorted.push(Arc::clone(node));
    }
    sorted
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    /// A stored state event.
    struct Stored {
        event_type: &'static str,
        state_key: &'static str,
        auth_events: Vec<&'static str>,
        position: Option<Position>,
    }

    /// The chain cover tables in memory.
    #[derive(Default)]
    struct FakeStore {
        legacy: bool,
        events: HashMap<Arc<str>, Stored>,
        to_calculate: Mutex<Vec<(String, String, String, String)>>,
        links: Vec<ChainLink>,
        next_chain_id: Mutex<i64>,
    }

    impl FakeStore {
        fn new() -> Self {
            FakeStore {
                next_chain_id: Mutex::new(100),
                ..Default::default()
            }
        }

        fn add(
            &mut self,
            id: &str,
            event_type: &'static str,
            auth_events: &[&'static str],
            position: Option<Position>,
        ) {
            self.events.insert(
                Arc::from(id),
                Stored {
                    event_type,
                    state_key: "",
                    auth_events: auth_events.to_vec(),
                    position,
                },
            );
        }

        fn link(&mut self, from: Position, to: Position) {
            self.links.push(ChainLink {
                origin_chain_id: from.0,
                origin_sequence_number: from.1,
                target_chain_id: to.0,
                target_sequence_number: to.1,
            });
        }

        fn put_off_ids(&self) -> Vec<String> {
            let rows = self.to_calculate.lock().unwrap();
            rows.iter().map(|row| row.0.clone()).collect()
        }
    }

    #[async_trait]
    impl ChainCoverStore for FakeStore {
        async fn has_auth_chain_index(&self, _room_id: &str) -> anyhow::Result<bool> {
            Ok(!self.legacy)
        }

        async fn events_with_chain_ids(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(event_ids
                .iter()
                .filter(|id| self.events.get(*id).is_some_and(|e| e.position.is_some()))
                .cloned()
                .collect())
        }

        async fn chains_to_calculate(
            &self,
            _room_id: &str,
        ) -> anyhow::Result<Vec<ChainToCalculate>> {
            let rows = self.to_calculate.lock().unwrap();
            Ok(rows
                .iter()
                .map(|(event_id, _, event_type, state_key)| ChainToCalculate {
                    event_id: Arc::from(event_id.as_str()),
                    event_type: Arc::from(event_type.as_str()),
                    state_key: Arc::from(state_key.as_str()),
                })
                .collect())
        }

        async fn auth_event_ids(&self, event_id: &str) -> anyhow::Result<Vec<Arc<str>>> {
            Ok(self
                .events
                .get(event_id)
                .map(|e| e.auth_events.iter().map(|id| Arc::from(*id)).collect())
                .unwrap_or_default())
        }

        async fn auth_event_chains(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<Vec<AuthEventChain>> {
            Ok(event_ids
                .iter()
                .filter_map(|id| {
                    self.events.get(id).map(|e| AuthEventChain {
                        event_id: Arc::clone(id),
                        event_type: Arc::from(e.event_type),
                        state_key: Arc::from(e.state_key),
                        chain: e.position,
                    })
                })
                .collect())
        }

        async fn chain_links(&self, _chains: Vec<i64>) -> anyhow::Result<Vec<ChainLink>> {
            // May return links not reachable from the chains, as the real
            // one may.
            Ok(self.links.clone())
        }

        async fn max_sequence_numbers(
            &self,
            chain_ids: &[i64],
        ) -> anyhow::Result<HashMap<i64, i64>> {
            let mut max = HashMap::new();
            for (chain_id, seq) in self.events.values().filter_map(|e| e.position) {
                if chain_ids.contains(&chain_id) {
                    let entry = max.entry(chain_id).or_insert(seq);
                    *entry = (*entry).max(seq);
                }
            }
            Ok(max)
        }

        async fn put_off(
            &self,
            room_id: &str,
            event_id: &str,
            event_type: &str,
            state_key: &str,
        ) -> anyhow::Result<()> {
            let mut rows = self.to_calculate.lock().unwrap();
            rows.retain(|row| row.0 != event_id);
            rows.push((
                event_id.to_owned(),
                room_id.to_owned(),
                event_type.to_owned(),
                state_key.to_owned(),
            ));
            Ok(())
        }

        async fn next_chain_ids(&self, n: usize) -> anyhow::Result<Vec<i64>> {
            let mut next = self.next_chain_id.lock().unwrap();
            let ids = (*next..*next + n as i64).collect();
            *next += n as i64;
            Ok(ids)
        }
    }

    fn event(id: &str, event_type: &str, auth_events: &[&str]) -> ChainCoverEvent {
        ChainCoverEvent {
            event_id: Arc::from(id),
            event_type: Arc::from(event_type),
            state_key: Arc::from(""),
            auth_event_ids: Ok(auth_events.iter().map(|a| Arc::from(*a)).collect()),
        }
    }

    fn calculate(
        store: &FakeStore,
        events: &[ChainCoverEvent],
    ) -> HashMap<String, (i64, i64, Vec<Position>)> {
        let events: Vec<&ChainCoverEvent> = events.iter().collect();
        let result = futures::executor::block_on(calculate_chain_cover_index_for_events_txn(
            store, "!room", &events,
        ))
        .unwrap();
        result
            .into_iter()
            .map(|(id, links)| {
                (
                    id.to_string(),
                    (links.chain_id, links.sequence_number, links.links),
                )
            })
            .collect()
    }

    /// A room of a create event on chain 1 and a power levels event on
    /// chain 2, linked to it.
    fn room() -> FakeStore {
        let mut store = FakeStore::new();
        store.add("$create", "m.room.create", &[], Some((1, 1)));
        store.add("$pl", "m.room.power_levels", &["$create"], Some((2, 1)));
        store.link((2, 1), (1, 1));
        store
    }

    #[test]
    fn sorted_topologically_puts_auth_events_first() {
        let graph: HashMap<Arc<str>, Vec<Arc<str>>> = [
            ("c", vec!["b", "a"]),
            ("b", vec!["a", "a"]),
            ("a", vec!["outside"]),
            ("x", vec!["y"]),
            ("y", vec!["x"]),
        ]
        .into_iter()
        .map(|(node, edges)| (Arc::from(node), edges.into_iter().map(Arc::from).collect()))
        .collect();
        let nodes: Vec<Arc<str>> = ["c", "b", "a", "d", "x", "y"]
            .into_iter()
            .map(Arc::from)
            .collect();
        let sorted: Vec<String> = sorted_topologically(&nodes, &graph)
            .iter()
            .map(|n| n.to_string())
            .collect();
        // Ties go to the lowest ID, and the cycle is left out.
        assert_eq!(sorted, ["a", "b", "c", "d"]);
    }

    #[test]
    fn link_map_drops_crossing_links() {
        let mut links = LinkMap::default();
        assert!(links.add_link((1, 5), (2, 5), true).unwrap());
        // Starts after and ends before the existing link.
        assert!(!links.add_link((1, 6), (2, 4), true).unwrap());
        // Ends after it.
        assert!(links.add_link((1, 7), (2, 6), true).unwrap());
        assert!(links.add_link((1, 1), (1, 1), true).is_err());
    }

    #[test]
    fn exists_path_from_follows_links() {
        let mut links = LinkMap::default();
        links.add_link((1, 3), (2, 4), false).unwrap();
        links.add_link((2, 2), (3, 7), false).unwrap();
        // Along a chain.
        assert!(links.exists_path_from((1, 5), (1, 2)));
        assert!(!links.exists_path_from((1, 2), (1, 5)));
        // Through chain 2 to chain 3.
        assert!(links.exists_path_from((1, 3), (3, 7)));
        assert!(links.exists_path_from((1, 9), (3, 1)));
        // From before the first link.
        assert!(!links.exists_path_from((1, 2), (3, 1)));
        // Past the end of the link.
        assert!(!links.exists_path_from((1, 3), (3, 8)));
        assert!(!links.exists_path_from((3, 7), (1, 1)));
    }

    #[test]
    fn a_new_event_extends_its_auth_event_chain() {
        let store = room();
        let result = calculate(
            &store,
            &[event("$pl2", "m.room.power_levels", &["$create", "$pl"])],
        );
        // The link to the create event is reachable through the old power
        // levels, so it is left out.
        assert_eq!(result["$pl2"], (2, 2, vec![]));
    }

    #[test]
    fn a_taken_position_starts_a_new_chain() {
        let mut store = room();
        store.add(
            "$pl2",
            "m.room.power_levels",
            &["$create", "$pl"],
            Some((2, 2)),
        );
        let result = calculate(
            &store,
            &[event("$fork", "m.room.power_levels", &["$create", "$pl"])],
        );
        assert_eq!(result["$fork"], (100, 1, vec![(2, 1)]));
    }

    #[test]
    fn events_of_the_chunk_chain_on_each_other() {
        let store = room();
        let result = calculate(
            &store,
            &[
                event("$m2", "m.room.member", &["$create", "$pl", "$m1"]),
                event("$m1", "m.room.member", &["$create", "$pl"]),
                event("$name", "m.room.name", &["$create", "$pl", "$m2"]),
                // A second event after $m1 on the same chain.
                event("$m3", "m.room.member", &["$create", "$pl", "$m1"]),
            ],
        );
        assert_eq!(result["$m1"], (100, 1, vec![(2, 1)]));
        // $m2 is allocated before $m3 by ID, so it takes the place after
        // $m1.
        assert_eq!(result["$m2"], (100, 2, vec![]));
        assert_eq!(result["$m3"], (101, 1, vec![(100, 1)]));
        assert_eq!(result["$name"], (102, 1, vec![(100, 2)]));
    }

    #[test]
    fn legacy_rooms_and_known_events_are_skipped() {
        let mut store = room();
        store.legacy = true;
        assert!(calculate(&store, &[event("$x", "m.room.name", &["$create"])]).is_empty());

        let store = room();
        assert!(calculate(&store, &[event("$pl", "m.room.power_levels", &["$create"])]).is_empty());
    }

    #[test]
    fn an_event_with_a_missing_auth_event_is_put_off() {
        let store = room();
        let result = calculate(
            &store,
            &[
                event("$oob", "m.room.member", &["$create", "$unknown"]),
                event("$after", "m.room.member", &["$create", "$pl", "$oob"]),
                event("$fine", "m.room.name", &["$create", "$pl"]),
            ],
        );
        // $after is dropped with $oob, but only the events being persisted
        // are put off, and $after is one of them.
        assert_eq!(result.keys().collect::<Vec<_>>(), ["$fine"]);
        let mut put_off = store.put_off_ids();
        put_off.sort();
        assert_eq!(put_off, ["$after", "$oob"]);
    }

    #[test]
    fn an_auth_event_without_a_chain_is_calculated() {
        let mut store = room();
        // Persisted out of band, so without a chain, but its auth events
        // are now known.
        store.add("$oob", "m.room.member", &["$create", "$pl"], None);
        let result = calculate(
            &store,
            &[event("$next", "m.room.member", &["$create", "$pl", "$oob"])],
        );
        assert_eq!(result["$oob"], (100, 1, vec![(2, 1)]));
        assert_eq!(result["$next"], (100, 2, vec![]));
    }

    #[test]
    fn events_put_off_earlier_are_calculated() {
        let mut store = room();
        store.add("$oob", "m.room.member", &["$create", "$pl"], None);
        store
            .put_off("!room", "$oob", "m.room.member", "")
            .now_or_never()
            .unwrap()
            .unwrap();
        let result = calculate(
            &store,
            &[event("$name", "m.room.name", &["$create", "$pl"])],
        );
        // Allocated in order of event ID.
        assert_eq!(result["$name"], (100, 1, vec![(2, 1)]));
        assert_eq!(result["$oob"], (101, 1, vec![(2, 1)]));
    }

    #[test]
    fn auth_events_are_read_only_for_new_events() {
        let store = room();
        let broken = |id| ChainCoverEvent {
            auth_event_ids: Err("no auth events".to_owned()),
            ..event(id, "m.room.power_levels", &[])
        };
        // Already in the index, so its auth events are never read.
        assert!(calculate(&store, &[broken("$pl")]).is_empty());
        let events = [broken("$new")];
        let events: Vec<&ChainCoverEvent> = events.iter().collect();
        let result = futures::executor::block_on(calculate_chain_cover_index_for_events_txn(
            &store, "!room", &events,
        ));
        assert!(result.is_err());
    }

    #[test]
    fn a_duplicated_auth_event_gets_no_link() {
        // `itertools.permutations` pairs the two copies, and each reaches
        // the other, so Python drops both.
        let store = room();
        let result = calculate(
            &store,
            &[event("$name", "m.room.name", &["$create", "$pl", "$pl"])],
        );
        assert_eq!(result["$name"], (100, 1, vec![]));
    }
}
