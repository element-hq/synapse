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

//! The auth chain difference using the chain cover index. Port of
//! `_get_auth_chain_difference_using_cover_index_txn` and its helpers in
//! `synapse/storage/databases/main/event_federation.py`. See
//! `docs/auth_chain_difference_algorithm.md` for the algorithm.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::bail;
use log::{debug, error};
use rustc_hash::{FxHashMap, FxHashSet};

use super::lru::{CachedChainLinks, ChainLink, ChainLinkCache, ChainPosition};
use super::store::{NoChainCoverIndex, PostgresStateResStore, StateDifference};

/// Matches `MAX_CHAIN_LENGTH` in `event_federation.py`.
const MAX_CHAIN_LENGTH: i64 = (1 << 53) - 1;

/// Links out of each chain: origin chain ID to its [`ChainLink`]s. Only
/// chains with at least one link have an entry.
pub type ChainLinks = FxHashMap<i64, Arc<[ChainLink]>>;

/// Chains per recursive link query, as in `_get_chain_links`.
const CHAIN_BATCH_SIZE: usize = 1000;
/// Events per chain info query, as in `fetch_chain_info`.
const EVENT_BATCH_SIZE: usize = 1000;

const CHAIN_LINKS_SQL: &str = "
    WITH RECURSIVE links(chain_id) AS (
        SELECT
            DISTINCT origin_chain_id
        FROM event_auth_chain_links WHERE origin_chain_id = ANY($1)
        UNION
        SELECT
            target_chain_id
        FROM event_auth_chain_links
        INNER JOIN links ON (chain_id = origin_chain_id)
    )
    SELECT
        origin_chain_id, origin_sequence_number,
        target_chain_id, target_sequence_number
    FROM links
    INNER JOIN event_auth_chain_links ON (chain_id = origin_chain_id)
";

const CHAIN_INFO_SQL: &str = "
    SELECT event_id, chain_id, sequence_number
    FROM event_auth_chains
    WHERE event_id = ANY($1)
";

const FETCH_FROM_CHAINS_SQL: &str = "
    SELECT event_id
    FROM event_auth_chains AS c,
        unnest($1::bigint[], $2::bigint[], $3::bigint[])
            AS l(chain_id, min_seq, max_seq)
    WHERE
        c.chain_id = l.chain_id
        AND min_seq < sequence_number AND sequence_number <= max_seq
";

const FIXUP_SQL: &str = "
    SELECT tc.event_id, ea.auth_id, eac.chain_id IS NOT NULL
    FROM event_auth_chain_to_calculate AS tc
    LEFT JOIN event_auth AS ea USING (event_id)
    LEFT JOIN event_auth_chains AS eac ON (ea.auth_id = eac.event_id)
    WHERE tc.room_id = $1
";

/// The chain positions of events, and the reverse map.
#[derive(Default)]
struct ChainInfo {
    /// Event ID to (chain ID, sequence number).
    positions: FxHashMap<Arc<str>, ChainPosition>,
    /// (chain ID, sequence number) to event ID.
    position_to_event: FxHashMap<ChainPosition, Arc<str>>,
    /// Every chain seen.
    seen_chains: FxHashSet<i64>,
}

impl ChainInfo {
    fn add(&mut self, event_id: Arc<str>, position: ChainPosition) {
        self.positions.insert(Arc::clone(&event_id), position);
        self.seen_chains.insert(position.0);
        self.position_to_event.insert(position, event_id);
    }

    /// Look up the positions of `events`, from the store's chain position
    /// cache where it has them and the database otherwise. Events without a
    /// position are left out.
    async fn fetch<'a>(
        &mut self,
        store: &PostgresStateResStore,
        events: impl IntoIterator<Item = &'a Arc<str>>,
    ) -> anyhow::Result<()> {
        let cache = store.caches().map(|caches| &caches.chain_positions);
        let mut missing: Vec<&str> = Vec::new();
        let mut hits = 0;
        for event_id in events {
            match cache.and_then(|cache| cache.get(event_id)) {
                Some(position) => {
                    self.add(Arc::clone(event_id), position);
                    hits += 1;
                }
                None => missing.push(event_id),
            }
        }
        store.record_cached(hits, 0);

        for batch in missing.chunks(EVENT_BATCH_SIZE) {
            let batch = batch.to_vec();
            for row in store.query(CHAIN_INFO_SQL, &[&batch]).await? {
                let event_id: Arc<str> = Arc::from(row.try_get::<_, &str>(0)?);
                let chain_id: i64 = row.try_get(1)?;
                let sequence_number: i64 = row.try_get(2)?;
                let position = (chain_id, sequence_number);
                // A position never changes once assigned, so it can be
                // cached before the transaction commits.
                if let Some(cache) = cache {
                    cache.insert(Arc::clone(&event_id), position, 1);
                }
                self.add(event_id, position);
            }
        }
        Ok(())
    }

    fn position(&self, event_id: &str) -> anyhow::Result<(i64, i64)> {
        match self.positions.get(event_id) {
            Some(position) => Ok(*position),
            None => bail!("No chain cover position for {event_id}"),
        }
    }

    fn event_at(&self, chain_id: i64, seq_no: i64) -> Option<&Arc<str>> {
        self.position_to_event.get(&(chain_id, seq_no))
    }
}

/// Port of `_get_auth_chain_difference_using_cover_index_txn`.
pub async fn get_auth_chain_difference_using_cover_index(
    store: &PostgresStateResStore,
    room_id: &str,
    state_sets: &[HashSet<Arc<str>>],
    conflicted_set: Option<&HashSet<Arc<str>>>,
    additional_backwards_reachable: Option<&HashSet<Arc<str>>>,
) -> anyhow::Result<StateDifference> {
    let is_state_res_v21 = conflicted_set.is_some();

    // `_fixup_auth_chain_difference_sets` rewrites the state sets.
    let mut state_sets: Vec<HashSet<Arc<str>>> = state_sets.to_vec();

    let initial_events: HashSet<Arc<str>> = state_sets.iter().flatten().cloned().collect();

    if let Some(conflicted_set) = conflicted_set {
        if !conflicted_set.is_subset(&initial_events) {
            bail!("conflicted set is not a subset of the state sets");
        }
        if let Some(additional) = additional_backwards_reachable {
            if !additional.is_subset(&initial_events) {
                bail!("additional backwards reachable events are not a subset of the state sets");
            }
        }
    }

    let t0 = Instant::now();
    let mut info = ChainInfo::default();
    info.fetch(store, &initial_events).await?;
    let t_positions = t0.elapsed();

    let events_missing_chain_info: HashSet<Arc<str>> = initial_events
        .iter()
        .filter(|id| !info.positions.contains_key(*id))
        .cloned()
        .collect();

    // The result set to return, i.e. the auth chain difference.
    let mut auth_difference_result: HashSet<Arc<str>> = HashSet::new();

    if !events_missing_chain_info.is_empty() {
        // We have events we haven't calculated the chain index for, which
        // should only happen for older rooms where the server doesn't have
        // all the auth events.
        auth_difference_result = fixup_auth_chain_difference_sets(
            store,
            room_id,
            &mut state_sets,
            &events_missing_chain_info,
            &info.positions.keys().cloned().collect(),
        )
        .await?;

        // Fetch the chain info of any events added to the state sets.
        let new_events_to_fetch: HashSet<Arc<str>> = state_sets
            .iter()
            .flatten()
            .filter(|id| !initial_events.contains(*id))
            .cloned()
            .collect();
        info.fetch(store, &new_events_to_fetch).await?;
    }

    // State res v2.1: the chain position of each conflicted event, and of
    // each additional backwards reachable event. Empty for v2, which
    // `walk_links` takes as "no conflicted subgraph".
    let mut conflicted_chain_positions: Vec<ChainPosition> = Vec::new();
    let mut additional_backwards_reachable_positions: Vec<ChainPosition> = Vec::new();

    if let Some(conflicted_set) = conflicted_set {
        for event_id in conflicted_set {
            conflicted_chain_positions.push(info.position(event_id)?);
        }
        for event_id in additional_backwards_reachable.into_iter().flatten() {
            additional_backwards_reachable_positions.push(info.position(event_id)?);
        }
    }

    // For each state set, the max sequence number reachable on each chain
    // before following any links.
    let mut initial_set_to_chain: Vec<FxHashMap<i64, i64>> = Vec::with_capacity(state_sets.len());
    for state_set in &state_sets {
        let mut chains: FxHashMap<i64, i64> = FxHashMap::default();
        for state_id in state_set {
            let (chain_id, seq_no) = info.position(state_id)?;
            raise(&mut chains, chain_id, seq_no);
        }
        initial_set_to_chain.push(chains);
    }

    // Gather the links the walks need and walk them. `needed` is, per
    // chain, the highest sequence number a walk starts from or reaches, which
    // is how far the gathered links for that chain must be complete to. A
    // walk that reaches further than what was gathered makes another round;
    // see `gather_links` for why one round is the norm.
    let t1 = Instant::now();
    let mut t_gather = Duration::ZERO;
    let mut t_walk = Duration::ZERO;
    let mut gathered = GatheredLinks::default();
    let mut needed: FxHashMap<i64, i64> = FxHashMap::default();
    for chains in &initial_set_to_chain {
        for (&chain_id, &seq_no) in chains {
            raise(&mut needed, chain_id, seq_no);
        }
    }
    let walk = loop {
        let g = Instant::now();
        gather_links(store, &mut gathered, &needed).await?;
        t_gather += g.elapsed();
        let g = Instant::now();
        let walk = walk_links(
            &gathered.links,
            &initial_set_to_chain,
            &conflicted_chain_positions,
            &additional_backwards_reachable_positions,
        );
        t_walk += g.elapsed();

        let mut complete = true;
        for chains in walk
            .set_to_chain
            .iter()
            .chain(std::iter::once(&walk.conflicted_backwards_reachable))
        {
            for (&chain_id, &seq_no) in chains {
                raise(&mut needed, chain_id, seq_no);
                if gathered.complete_to(chain_id) < seq_no {
                    complete = false;
                }
            }
        }
        if complete {
            break walk;
        }
    };
    let Walk {
        set_to_chain,
        conflicted_backwards_reachable,
        conflicted_forwards_reachable,
    } = walk;
    for chains in &set_to_chain {
        info.seen_chains.extend(chains.keys().copied());
    }
    let t_links = t1.elapsed();
    let t2 = Instant::now();

    // For each chain, the events between the minimum sequence number
    // reachable from *all* state sets and the maximum reachable from *any*
    // are in the auth chain difference.
    let mut auth_diff_chain_to_gap: FxHashMap<i64, (i64, i64)> = FxHashMap::default();
    for &chain_id in &info.seen_chains {
        let reachable = set_to_chain
            .iter()
            .map(|chains| chains.get(&chain_id).copied().unwrap_or(0));
        let min_seq_no = reachable.clone().min().unwrap_or(0);
        let max_seq_no = reachable.max().unwrap_or(0);

        // Use what we already know of the gap, falling back to the database
        // if any of it is missing.
        for seq_no in min_seq_no + 1..=max_seq_no {
            match info.event_at(chain_id, seq_no) {
                Some(event_id) => {
                    auth_difference_result.insert(Arc::clone(event_id));
                }
                None => {
                    auth_diff_chain_to_gap.insert(chain_id, (min_seq_no, max_seq_no));
                    break;
                }
            }
        }
    }

    // The conflicted subgraph: per chain, the overlap between the forwards
    // and backwards reachable ranges.
    let mut conflicted_subgraph_result: HashSet<Arc<str>> = HashSet::new();
    let mut conflicted_subgraph_chain_to_gap: FxHashMap<i64, (i64, i64)> = FxHashMap::default();
    if is_state_res_v21 {
        for (&chain_id, &backwards_seq_num) in &conflicted_backwards_reachable {
            let Some(&forwards_seq_num) = conflicted_forwards_reachable.get(&chain_id) else {
                continue;
            };
            for seq_no in forwards_seq_num..=backwards_seq_num {
                match info.event_at(chain_id, seq_no) {
                    Some(event_id) => {
                        conflicted_subgraph_result.insert(Arc::clone(event_id));
                    }
                    None => {
                        // The fetch is exclusive of its lower bound.
                        conflicted_subgraph_chain_to_gap
                            .insert(chain_id, (forwards_seq_num - 1, backwards_seq_num));
                        break;
                    }
                }
            }
        }
    }

    debug!(
        "Chain cover difference: positions {t_positions:?}, links {t_links:?} (gathering {t_gather:?}, walking {t_walk:?}), gaps {:?}",
        t2.elapsed()
    );
    if !auth_diff_chain_to_gap.is_empty() {
        auth_difference_result
            .extend(fetch_event_ids_from_chains(store, &auth_diff_chain_to_gap).await?);
    }
    if !conflicted_subgraph_chain_to_gap.is_empty() {
        conflicted_subgraph_result
            .extend(fetch_event_ids_from_chains(store, &conflicted_subgraph_chain_to_gap).await?);
    }

    Ok(StateDifference {
        auth_difference: auth_difference_result,
        conflicted_subgraph: Some(conflicted_subgraph_result),
    })
}

/// Set `map[chain_id]` to at least `seq_no`.
fn raise(map: &mut FxHashMap<i64, i64>, chain_id: i64, seq_no: i64) {
    let max = map.entry(chain_id).or_insert(0);
    *max = (*max).max(seq_no);
}

/// The chain links gathered for one computation: the links out of each
/// chain, and how far along the chain they are complete (see
/// [`CachedChainLinks`]).
#[derive(Default)]
struct GatheredLinks {
    links: ChainLinks,
    complete_to: FxHashMap<i64, i64>,
}

impl GatheredLinks {
    /// The sequence number the links of `chain_id` are complete to, or 0 if
    /// none were gathered.
    fn complete_to(&self, chain_id: i64) -> i64 {
        self.complete_to.get(&chain_id).copied().unwrap_or(0)
    }

    fn add(&mut self, chain_id: i64, entry: CachedChainLinks) {
        if entry.complete_to < self.complete_to(chain_id) {
            return;
        }
        self.complete_to.insert(chain_id, entry.complete_to);
        if entry.links.is_empty() {
            self.links.remove(&chain_id);
        } else {
            self.links.insert(chain_id, entry.links);
        }
    }
}

/// Gather links into `gathered` until, for every chain in `needed`, its links
/// are complete to the sequence number given, following the links a
/// backwards walk from that position would follow so the chains it reaches
/// are gathered too.
///
/// Chains the store's chain link cache has far enough are taken from it.
/// The rest come from `_get_chain_links`' recursive query, which returns
/// the links out of the chains asked for and out of every chain reachable
/// from them, and so covers whatever a walk from them reaches. Either way a
/// walk after one call reaches nothing that is not gathered, unless the
/// cache held a chain whose links were read before the position the walk
/// reaches it at: then the caller sees the gap and calls again.
async fn gather_links(
    store: &PostgresStateResStore,
    gathered: &mut GatheredLinks,
    needed: &FxHashMap<i64, i64>,
) -> anyhow::Result<()> {
    let cache = store.caches().map(|caches| &caches.chain_links);
    let (to_fetch, hits) = gather_links_from_cache(cache, gathered, needed);
    store.record_cached(0, hits);

    loop {
        // A batch's query also answers for chains it reaches, so skip those
        // in later batches.
        let batch: Vec<(i64, i64)> = to_fetch
            .iter()
            .map(|(&c, &s)| (c, s))
            .filter(|&(chain_id, seq_no)| gathered.complete_to(chain_id) < seq_no)
            .take(CHAIN_BATCH_SIZE)
            .collect();
        if batch.is_empty() {
            break;
        }

        let chain_ids: Vec<i64> = batch.iter().map(|&(chain_id, _)| chain_id).collect();
        let mut links: FxHashMap<i64, Vec<ChainLink>> = FxHashMap::default();
        // Every chain asked for gets an entry, with no links if it has none,
        // complete to the position asked for. A chain the query reaches is
        // complete to the highest position a link points at or comes from,
        // which the event at that position being persisted guarantees.
        let mut complete_to: FxHashMap<i64, i64> = batch.into_iter().collect();
        for row in store.query(CHAIN_LINKS_SQL, &[&chain_ids]).await? {
            let origin_chain_id: i64 = row.try_get(0)?;
            let origin_sequence_number: i64 = row.try_get(1)?;
            let target_chain_id: i64 = row.try_get(2)?;
            let target_sequence_number: i64 = row.try_get(3)?;
            links.entry(origin_chain_id).or_default().push((
                origin_sequence_number,
                target_chain_id,
                target_sequence_number,
            ));
            raise(&mut complete_to, origin_chain_id, origin_sequence_number);
            raise(&mut complete_to, target_chain_id, target_sequence_number);
        }

        for (chain_id, seq_no) in complete_to {
            let entry = CachedChainLinks {
                complete_to: seq_no,
                links: links.remove(&chain_id).unwrap_or_default().into(),
            };
            if let Some(cache) = cache {
                cache.insert(chain_id, entry.clone(), entry.links.len());
            }
            gathered.add(chain_id, entry);
        }
    }
    Ok(())
}

/// The cached part of [`gather_links`]: gather what `cache` has, following
/// the links a backwards walk from each needed position would. Returns the
/// chains and positions still to fetch, and the number of chains gathered.
fn gather_links_from_cache(
    cache: Option<&ChainLinkCache>,
    gathered: &mut GatheredLinks,
    needed: &FxHashMap<i64, i64>,
) -> (FxHashMap<i64, i64>, u64) {
    let mut to_fetch: FxHashMap<i64, i64> = FxHashMap::default();
    let mut hits = 0;
    let mut worklist: Vec<(i64, i64)> = needed.iter().map(|(&c, &s)| (c, s)).collect();
    while let Some((chain_id, seq_no)) = worklist.pop() {
        if gathered.complete_to(chain_id) >= seq_no {
            continue;
        }
        let cached = cache
            .and_then(|cache| cache.get(&chain_id))
            .filter(|cached| cached.complete_to >= seq_no);
        match cached {
            Some(cached) => {
                for &(origin_seq_no, target_chain_id, target_seq_no) in cached.links.iter() {
                    if origin_seq_no <= seq_no {
                        worklist.push((target_chain_id, target_seq_no));
                    }
                }
                gathered.add(chain_id, cached);
                hits += 1;
            }
            None => raise(&mut to_fetch, chain_id, seq_no),
        }
    }
    (to_fetch, hits)
}

/// What walking the links from the state sets' positions reaches.
struct Walk {
    /// Per state set, chain ID to the max sequence number reachable.
    set_to_chain: Vec<FxHashMap<i64, i64>>,
    /// Chain ID to the max sequence number backwards reachable from a
    /// conflicted or additional backwards reachable event. State res v2.1
    /// only.
    conflicted_backwards_reachable: FxHashMap<i64, i64>,
    /// Chain ID to the min sequence number forwards reachable from a
    /// conflicted event. State res v2.1 only.
    conflicted_forwards_reachable: FxHashMap<i64, i64>,
}

/// Walk `links` from every position in `initial_set_to_chain`, and for state
/// res v2.1 backwards and forwards from the conflicted positions.
///
/// The Python walks each batch of `_get_chain_links` as it arrives. A batch
/// holds the links out of every chain reachable from the chains it was asked
/// for, so a walk never crosses a batch boundary, and walking the union of
/// the batches once comes to the same thing.
fn walk_links(
    links: &ChainLinks,
    initial_set_to_chain: &[FxHashMap<i64, i64>],
    conflicted_chain_positions: &[ChainPosition],
    additional_backwards_reachable_positions: &[ChainPosition],
) -> Walk {
    let mut set_to_chain = initial_set_to_chain.to_vec();
    for chains in &mut set_to_chain {
        let starts: Vec<i64> = chains.keys().copied().collect();
        for chain_id in starts {
            let seq_no = chains[&chain_id];
            materialize(chain_id, seq_no, links, chains, true);
        }
    }

    // Backwards reachability from the conflicted events and the additional
    // backwards reachable events: the position itself covers everything
    // earlier on its own chain. Forwards reachability from the conflicted
    // events. The conflicted subgraph is their intersection.
    let mut conflicted_backwards_reachable: FxHashMap<i64, i64> = FxHashMap::default();
    let mut conflicted_forwards_reachable: FxHashMap<i64, i64> = FxHashMap::default();
    accumulate_forwards_reachable_events(
        &mut conflicted_forwards_reachable,
        links,
        conflicted_chain_positions,
    );
    for &(chain_id, seq_no) in conflicted_chain_positions
        .iter()
        .chain(additional_backwards_reachable_positions)
    {
        if !links.contains_key(&chain_id) {
            // Not on a path to the root.
            continue;
        }
        raise(&mut conflicted_backwards_reachable, chain_id, seq_no);
        materialize(
            chain_id,
            seq_no,
            links,
            &mut conflicted_backwards_reachable,
            true,
        );
    }

    Walk {
        set_to_chain,
        conflicted_backwards_reachable,
        conflicted_forwards_reachable,
    }
}

/// Port of `_fetch_event_ids_from_chains_txn`: the events in each chain with
/// `min < sequence_number <= max`.
async fn fetch_event_ids_from_chains(
    store: &PostgresStateResStore,
    chains: &FxHashMap<i64, (i64, i64)>,
) -> anyhow::Result<HashSet<Arc<str>>> {
    let mut chain_ids = Vec::with_capacity(chains.len());
    let mut min_seqs = Vec::with_capacity(chains.len());
    let mut max_seqs = Vec::with_capacity(chains.len());
    for (&chain_id, &(min_no, max_no)) in chains {
        chain_ids.push(chain_id);
        min_seqs.push(min_no);
        max_seqs.push(max_no);
    }

    let rows = store
        .query(FETCH_FROM_CHAINS_SQL, &[&chain_ids, &min_seqs, &max_seqs])
        .await?;
    rows.iter()
        .map(|row| Ok(Arc::from(row.try_get::<_, &str>(0)?)))
        .collect()
}

/// Port of `_fixup_auth_chain_difference_sets`: handles events that have no
/// chain cover index yet.
///
/// Rewrites `state_sets` to contain only indexed events, and returns the auth
/// difference of the unindexed events.
async fn fixup_auth_chain_difference_sets(
    store: &PostgresStateResStore,
    room_id: &str,
    state_sets: &mut [HashSet<Arc<str>>],
    events_missing_chain_info: &HashSet<Arc<str>>,
    events_that_have_chain_index: &HashSet<Arc<str>>,
) -> anyhow::Result<HashSet<Arc<str>>> {
    // This works like the handling of unpersisted events in
    // `synapse.state.v2._get_auth_chain_difference`. Every event without a
    // chain cover index is in `event_auth_chain_to_calculate`, so we pull
    // those out with their auth events, which gives us enough to build the
    // auth chain of each one up to events that do have the index.
    let mut event_to_auth_ids: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
    let mut events_that_have_chain_index = events_that_have_chain_index.clone();
    for row in store.query(FIXUP_SQL, &[&room_id]).await? {
        let event_id: Arc<str> = Arc::from(row.try_get::<_, &str>(0)?);
        let auth_id: Option<&str> = row.try_get(1)?;
        let auth_id_has_chain: Option<bool> = row.try_get(2)?;
        let auth_ids = event_to_auth_ids.entry(event_id).or_default();
        if let Some(auth_id) = auth_id {
            let auth_id: Arc<str> = Arc::from(auth_id);
            if auth_id_has_chain == Some(true) {
                events_that_have_chain_index.insert(Arc::clone(&auth_id));
            }
            auth_ids.insert(auth_id);
        }
    }

    let unexpected: Vec<&Arc<str>> = events_missing_chain_info
        .iter()
        .filter(|id| !event_to_auth_ids.contains_key(*id))
        .collect();
    if !unexpected.is_empty() {
        error!(
            "Unexpectedly found that events don't have chain IDs in room {room_id}: {unexpected:?}"
        );
        return Err(NoChainCoverIndex(room_id.to_owned()).into());
    }

    // The partial auth chain of each unindexed event in a state set.
    let mut event_id_to_partial_auth_chain: HashMap<Arc<str>, HashSet<Arc<str>>> = HashMap::new();
    for (event_id, auth_ids) in &event_to_auth_ids {
        if !state_sets.iter().any(|set| set.contains(event_id)) {
            continue;
        }

        let mut processing: Vec<Arc<str>> = auth_ids.iter().cloned().collect();
        let mut to_add: HashSet<Arc<str>> = HashSet::new();
        while let Some(auth_id) = processing.pop() {
            if !to_add.insert(Arc::clone(&auth_id)) {
                continue;
            }
            if let Some(sub_auth_ids) = event_to_auth_ids.get(&auth_id) {
                processing.extend(
                    sub_auth_ids
                        .iter()
                        .filter(|id| !to_add.contains(*id))
                        .cloned(),
                );
            }
        }

        event_id_to_partial_auth_chain.insert(Arc::clone(event_id), to_add);
    }

    // Update the state sets to only include indexed events, and collect the
    // auth chains of the unindexed ones.
    let mut unindexed_state_sets: Vec<HashSet<Arc<str>>> = Vec::with_capacity(state_sets.len());
    for state_set in state_sets.iter_mut() {
        let mut unindexed_state_set = HashSet::new();
        for (event_id, auth_chain) in &event_id_to_partial_auth_chain {
            if !state_set.contains(event_id) {
                continue;
            }

            unindexed_state_set.insert(Arc::clone(event_id));

            state_set.remove(event_id);
            for auth_id in auth_chain {
                state_set.remove(auth_id);
            }
            for auth_id in auth_chain {
                if events_that_have_chain_index.contains(auth_id) {
                    state_set.insert(Arc::clone(auth_id));
                } else {
                    unindexed_state_set.insert(Arc::clone(auth_id));
                }
            }
        }
        unindexed_state_sets.push(unindexed_state_set);
    }

    // The auth difference of the unindexed events.
    Ok(union_minus_intersection(&unindexed_state_sets))
}

/// The events in some but not all of the sets.
pub fn union_minus_intersection(sets: &[HashSet<Arc<str>>]) -> HashSet<Arc<str>> {
    let union: HashSet<&Arc<str>> = sets.iter().flatten().collect();
    union
        .into_iter()
        .filter(|id| !sets.iter().all(|set| set.contains(*id)))
        .cloned()
        .collect()
}

/// Port of `_materialize`: update `materialized` (chain ID to the max, or for
/// a forwards walk the min, reachable sequence number) with the chains
/// reachable from the given position through `links`.
pub fn materialize(
    origin_chain_id: i64,
    origin_sequence_number: i64,
    links: &ChainLinks,
    materialized: &mut FxHashMap<i64, i64>,
    backwards: bool,
) {
    let mut stack = vec![(origin_chain_id, origin_sequence_number)];

    while let Some((c, s)) = stack.pop() {
        let Some(chain_links) = links.get(&c) else {
            continue;
        };
        for &(sequence_number, target_chain_id, target_sequence_number) in chain_links.iter() {
            if backwards {
                // Ignore any links that are higher up the chain.
                if sequence_number > s {
                    continue;
                }
                // Skip chains we have already reached this far down.
                if materialized.get(&target_chain_id).copied().unwrap_or(0) < target_sequence_number
                {
                    stack.push((target_chain_id, target_sequence_number));
                    materialized.insert(target_chain_id, target_sequence_number);
                }
            } else {
                // Ignore any links that are lower down the chain.
                if sequence_number < s {
                    continue;
                }
                if materialized
                    .get(&target_chain_id)
                    .copied()
                    .unwrap_or(MAX_CHAIN_LENGTH)
                    > target_sequence_number
                {
                    stack.push((target_chain_id, target_sequence_number));
                    materialized.insert(target_chain_id, target_sequence_number);
                }
            }
        }
    }
}

/// Port of `_generate_forward_links`: reverse the backwards links.
fn generate_forward_links(links: &ChainLinks) -> ChainLinks {
    let mut new_links: FxHashMap<i64, Vec<ChainLink>> = FxHashMap::default();
    for (&origin_chain_id, chain_links) in links {
        for &(origin_seq_num, target_chain_id, target_seq_num) in chain_links.iter() {
            new_links.entry(target_chain_id).or_default().push((
                target_seq_num,
                origin_chain_id,
                origin_seq_num,
            ));
        }
    }
    new_links
        .into_iter()
        .map(|(chain_id, links)| (chain_id, links.into()))
        .collect()
}

/// Port of `accumulate_forwards_reachable_events`: add the forwards
/// reachability from each conflicted position encoded in one batch of
/// backwards links.
///
/// A batch of links only encodes part of the forwards reachability, so this
/// accumulates across batches: a later batch can extend what an earlier
/// conflicted event reaches.
pub fn accumulate_forwards_reachable_events(
    conflicted_forwards_reachable: &mut FxHashMap<i64, i64>,
    back_links: &ChainLinks,
    conflicted_chain_positions: &[(i64, i64)],
) {
    let fwd_links = generate_forward_links(back_links);

    for &(chain_id, seq_no) in conflicted_chain_positions {
        // The conflicted event itself makes later events on its chain
        // reachable.
        let min = conflicted_forwards_reachable
            .entry(chain_id)
            .or_insert(MAX_CHAIN_LENGTH);
        *min = (*min).min(seq_no);
        materialize(
            chain_id,
            seq_no,
            &fwd_links,
            conflicted_forwards_reachable,
            false,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn materialize_backwards_and_forwards() {
        // Chain 1: seq 1..5. At seq 3 it links to chain 2 seq 2, and chain 2
        // seq 2 links to chain 3 seq 7.
        let mut links: ChainLinks = FxHashMap::default();
        links.insert(1, vec![(3, 2, 2)].into());
        links.insert(2, vec![(2, 3, 7)].into());

        let mut reached = FxHashMap::default();
        materialize(1, 4, &links, &mut reached, true);
        assert_eq!(reached, FxHashMap::from_iter([(2, 2), (3, 7)]));

        // From below the link, nothing is reachable.
        let mut reached = FxHashMap::default();
        materialize(1, 2, &links, &mut reached, true);
        assert!(reached.is_empty());

        // Forwards from chain 3 seq 5 reaches chain 2 seq 2 and chain 1 seq 3.
        let mut reached = FxHashMap::default();
        accumulate_forwards_reachable_events(&mut reached, &links, &[(3, 5)]);
        assert_eq!(reached, FxHashMap::from_iter([(3, 5), (2, 2), (1, 3)]));
    }

    fn cached(complete_to: i64, links: &[ChainLink]) -> CachedChainLinks {
        CachedChainLinks {
            complete_to,
            links: links.into(),
        }
    }

    #[test]
    fn gathers_from_the_cache_as_far_as_a_walk_goes() {
        // Chain 1 links to chain 2 at seq 3, chain 2 to chain 3 at seq 2.
        // Chain 3 is not cached.
        let cache = ChainLinkCache::new(100, std::time::Duration::from_secs(60));
        cache.insert(1, cached(5, &[(3, 2, 2)]), 1);
        cache.insert(2, cached(2, &[(2, 3, 7)]), 1);

        // From (1, 4) the walk crosses to chain 2 and then needs chain 3.
        let mut gathered = GatheredLinks::default();
        let (to_fetch, hits) =
            gather_links_from_cache(Some(&cache), &mut gathered, &FxHashMap::from_iter([(1, 4)]));
        assert_eq!(hits, 2);
        assert_eq!(to_fetch, FxHashMap::from_iter([(3, 7)]));
        assert_eq!(gathered.complete_to(1), 5);
        assert_eq!(gathered.complete_to(2), 2);
        assert_eq!(gathered.links.len(), 2);

        // From (1, 2) the link out at seq 3 is not followed.
        let mut gathered = GatheredLinks::default();
        let (to_fetch, hits) =
            gather_links_from_cache(Some(&cache), &mut gathered, &FxHashMap::from_iter([(1, 2)]));
        assert_eq!(hits, 1);
        assert!(to_fetch.is_empty());
        assert_eq!(gathered.links.len(), 1);

        // Beyond what the cached links are complete to, the chain is fetched.
        let mut gathered = GatheredLinks::default();
        let (to_fetch, hits) =
            gather_links_from_cache(Some(&cache), &mut gathered, &FxHashMap::from_iter([(1, 9)]));
        assert_eq!(hits, 0);
        assert_eq!(to_fetch, FxHashMap::from_iter([(1, 9)]));

        // Without a cache everything is fetched.
        let mut gathered = GatheredLinks::default();
        let (to_fetch, _) =
            gather_links_from_cache(None, &mut gathered, &FxHashMap::from_iter([(1, 1)]));
        assert_eq!(to_fetch, FxHashMap::from_iter([(1, 1)]));
    }

    #[test]
    fn gathered_links_keep_the_most_complete_entry() {
        let mut gathered = GatheredLinks::default();
        gathered.add(1, cached(5, &[(3, 2, 2)]));
        gathered.add(1, cached(4, &[]));
        assert_eq!(gathered.complete_to(1), 5);
        assert!(gathered.links.contains_key(&1));

        // A chain with no links has no entry, as in the Python's `links`.
        gathered.add(1, cached(6, &[]));
        assert_eq!(gathered.complete_to(1), 6);
        assert!(!gathered.links.contains_key(&1));
    }

    #[test]
    fn walk_from_state_sets_and_conflicted_positions() {
        let mut links: ChainLinks = FxHashMap::default();
        links.insert(1, vec![(3, 2, 2)].into());
        links.insert(2, vec![(2, 3, 7)].into());

        let walk = walk_links(
            &links,
            &[
                FxHashMap::from_iter([(1, 4)]),
                FxHashMap::from_iter([(1, 2)]),
            ],
            &[(2, 2)],
            &[(3, 1)],
        );
        assert_eq!(
            walk.set_to_chain[0],
            FxHashMap::from_iter([(1, 4), (2, 2), (3, 7)])
        );
        assert_eq!(walk.set_to_chain[1], FxHashMap::from_iter([(1, 2)]));
        // Chain 3 has no links out, so the additional position on it is
        // skipped, as the Python skips chains absent from `links`.
        assert_eq!(
            walk.conflicted_backwards_reachable,
            FxHashMap::from_iter([(2, 2), (3, 7)])
        );
        assert_eq!(
            walk.conflicted_forwards_reachable,
            FxHashMap::from_iter([(2, 2), (1, 3)])
        );
    }

    #[test]
    fn union_minus_intersection_of_sets() {
        let set = |ids: &[&str]| ids.iter().map(|id| Arc::from(*id)).collect::<HashSet<_>>();
        let result = union_minus_intersection(&[set(&["a", "b"]), set(&["b", "c"])]);
        assert_eq!(result, set(&["a", "c"]));
    }
}
