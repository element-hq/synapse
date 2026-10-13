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
};

use anyhow::bail;
use log::error;

use super::store::{NoChainCoverIndex, PostgresStateResStore, StateDifference};

/// Matches `MAX_CHAIN_LENGTH` in `event_federation.py`.
const MAX_CHAIN_LENGTH: i64 = (1 << 53) - 1;

/// Links out of each chain: origin chain ID to a list of (origin sequence
/// number, target chain ID, target sequence number).
pub type ChainLinks = HashMap<i64, Vec<(i64, i64, i64)>>;

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
    positions: HashMap<Arc<str>, (i64, i64)>,
    /// Chain ID to sequence number to event ID.
    chain_to_event: HashMap<i64, HashMap<i64, Arc<str>>>,
    /// Every chain seen.
    seen_chains: HashSet<i64>,
}

impl ChainInfo {
    async fn fetch<'a>(
        &mut self,
        store: &PostgresStateResStore,
        events: impl IntoIterator<Item = &'a Arc<str>>,
    ) -> anyhow::Result<()> {
        let events: Vec<&str> = events.into_iter().map(|id| &**id).collect();
        for batch in events.chunks(EVENT_BATCH_SIZE) {
            let batch = batch.to_vec();
            for row in store.query(CHAIN_INFO_SQL, &[&batch]).await? {
                let event_id: Arc<str> = Arc::from(row.try_get::<_, &str>(0)?);
                let chain_id: i64 = row.try_get(1)?;
                let sequence_number: i64 = row.try_get(2)?;
                self.positions
                    .insert(Arc::clone(&event_id), (chain_id, sequence_number));
                self.seen_chains.insert(chain_id);
                self.chain_to_event
                    .entry(chain_id)
                    .or_default()
                    .insert(sequence_number, event_id);
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
        self.chain_to_event.get(&chain_id)?.get(&seq_no)
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

    let mut info = ChainInfo::default();
    info.fetch(store, &initial_events).await?;

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

    // State res v2.1 bookkeeping for the conflicted subgraph.
    //
    // The chain position of each conflicted event.
    let mut conflicted_chain_positions: Vec<(i64, i64)> = Vec::new();
    // The chain position of each additional backwards reachable event.
    let mut additional_backwards_reachable_positions: Vec<(i64, i64)> = Vec::new();
    // Chain ID to the max sequence number backwards reachable from a
    // conflicted event, and to the min sequence number forwards reachable.
    // The conflicted subgraph is their intersection.
    let mut conflicted_backwards_reachable: HashMap<i64, i64> = HashMap::new();
    let mut conflicted_forwards_reachable: HashMap<i64, i64> = HashMap::new();

    if let Some(conflicted_set) = conflicted_set {
        for event_id in conflicted_set {
            conflicted_chain_positions.push(info.position(event_id)?);
        }
        for event_id in additional_backwards_reachable.into_iter().flatten() {
            additional_backwards_reachable_positions.push(info.position(event_id)?);
        }
    }

    // For each state set, the max sequence number reachable on each chain.
    let mut set_to_chain: Vec<HashMap<i64, i64>> = Vec::with_capacity(state_sets.len());
    for state_set in &state_sets {
        let mut chains: HashMap<i64, i64> = HashMap::new();
        for state_id in state_set {
            let (chain_id, seq_no) = info.position(state_id)?;
            let max = chains.entry(chain_id).or_insert(0);
            *max = (*max).max(seq_no);
        }
        set_to_chain.push(chains);
    }

    // Look up the links for the chains we have, adding chains reachable from
    // any event.
    let mut chains_to_fetch: HashSet<i64> = info.seen_chains.clone();
    while !chains_to_fetch.is_empty() {
        let links = fetch_chain_links_batch(store, &mut chains_to_fetch).await?;

        // `links` holds the backwards reachable events from a batch of chains
        // all the way to the root of the graph.
        for chains in &mut set_to_chain {
            // As in the Python, a chain reached by an earlier walk in this
            // loop is walked too, from the position that walk reached.
            for &chain_id in links.keys() {
                let Some(&seq_no) = chains.get(&chain_id) else {
                    continue;
                };
                materialize(chain_id, seq_no, &links, chains, true);
            }
            info.seen_chains.extend(chains.keys().copied());
        }

        if is_state_res_v21 {
            // Forwards reachability from the conflicted events.
            accumulate_forwards_reachable_events(
                &mut conflicted_forwards_reachable,
                &links,
                &conflicted_chain_positions,
            );

            // Backwards reachability from the conflicted events and the
            // additional backwards reachable events. The position itself
            // covers everything earlier on its own chain.
            for &(chain_id, seq_no) in conflicted_chain_positions
                .iter()
                .chain(&additional_backwards_reachable_positions)
            {
                if !links.contains_key(&chain_id) {
                    // Not on a path to the root.
                    continue;
                }
                let max = conflicted_backwards_reachable.entry(chain_id).or_insert(0);
                *max = (*max).max(seq_no);
                materialize(
                    chain_id,
                    seq_no,
                    &links,
                    &mut conflicted_backwards_reachable,
                    true,
                );
            }
        }
    }

    // For each chain, the events between the minimum sequence number
    // reachable from *all* state sets and the maximum reachable from *any*
    // are in the auth chain difference.
    let mut auth_diff_chain_to_gap: HashMap<i64, (i64, i64)> = HashMap::new();
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
    let mut conflicted_subgraph_chain_to_gap: HashMap<i64, (i64, i64)> = HashMap::new();
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

/// One batch of `_get_chain_links`: the links out of up to 1000 chains from
/// `chains_to_fetch`, and recursively out of every chain they reach. Removes
/// the fetched chains, and every chain the links come out of, from
/// `chains_to_fetch`.
///
/// The per-batch structure matters: callers walk each batch's links on their
/// own, as the Python generator's consumer does.
async fn fetch_chain_links_batch(
    store: &PostgresStateResStore,
    chains_to_fetch: &mut HashSet<i64>,
) -> anyhow::Result<ChainLinks> {
    let batch: Vec<i64> = chains_to_fetch
        .iter()
        .copied()
        .take(CHAIN_BATCH_SIZE)
        .collect();
    for chain_id in &batch {
        chains_to_fetch.remove(chain_id);
    }

    let mut links: ChainLinks = HashMap::new();
    for row in store.query(CHAIN_LINKS_SQL, &[&batch]).await? {
        let origin_chain_id: i64 = row.try_get(0)?;
        let origin_sequence_number: i64 = row.try_get(1)?;
        let target_chain_id: i64 = row.try_get(2)?;
        let target_sequence_number: i64 = row.try_get(3)?;
        links.entry(origin_chain_id).or_default().push((
            origin_sequence_number,
            target_chain_id,
            target_sequence_number,
        ));
    }

    for chain_id in links.keys() {
        chains_to_fetch.remove(chain_id);
    }
    Ok(links)
}

/// Port of `_fetch_event_ids_from_chains_txn`: the events in each chain with
/// `min < sequence_number <= max`.
async fn fetch_event_ids_from_chains(
    store: &PostgresStateResStore,
    chains: &HashMap<i64, (i64, i64)>,
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
    materialized: &mut HashMap<i64, i64>,
    backwards: bool,
) {
    let mut stack = vec![(origin_chain_id, origin_sequence_number)];

    while let Some((c, s)) = stack.pop() {
        let Some(chain_links) = links.get(&c) else {
            continue;
        };
        for &(sequence_number, target_chain_id, target_sequence_number) in chain_links {
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
    let mut new_links: ChainLinks = HashMap::new();
    for (&origin_chain_id, chain_links) in links {
        for &(origin_seq_num, target_chain_id, target_seq_num) in chain_links {
            new_links.entry(target_chain_id).or_default().push((
                target_seq_num,
                origin_chain_id,
                origin_seq_num,
            ));
        }
    }
    new_links
}

/// Port of `accumulate_forwards_reachable_events`: add the forwards
/// reachability from each conflicted position encoded in one batch of
/// backwards links.
///
/// A batch of links only encodes part of the forwards reachability, so this
/// accumulates across batches: a later batch can extend what an earlier
/// conflicted event reaches.
pub fn accumulate_forwards_reachable_events(
    conflicted_forwards_reachable: &mut HashMap<i64, i64>,
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
        let mut links: ChainLinks = HashMap::new();
        links.insert(1, vec![(3, 2, 2)]);
        links.insert(2, vec![(2, 3, 7)]);

        let mut reached = HashMap::new();
        materialize(1, 4, &links, &mut reached, true);
        assert_eq!(reached, HashMap::from([(2, 2), (3, 7)]));

        // From below the link, nothing is reachable.
        let mut reached = HashMap::new();
        materialize(1, 2, &links, &mut reached, true);
        assert!(reached.is_empty());

        // Forwards from chain 3 seq 5 reaches chain 2 seq 2 and chain 1 seq 3.
        let mut reached = HashMap::new();
        accumulate_forwards_reachable_events(&mut reached, &links, &[(3, 5)]);
        assert_eq!(reached, HashMap::from([(3, 5), (2, 2), (1, 3)]));
    }

    #[test]
    fn union_minus_intersection_of_sets() {
        let set = |ids: &[&str]| ids.iter().map(|id| Arc::from(*id)).collect::<HashSet<_>>();
        let result = union_minus_intersection(&[set(&["a", "b"]), set(&["b", "c"])]);
        assert_eq!(result, set(&["a", "c"]));
    }
}
