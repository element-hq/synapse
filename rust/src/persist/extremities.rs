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

//! The room's forward extremities after a chunk of events: ports of
//! `_calculate_new_extremities`, `_calculate_new_state_dag_extremities` and
//! `_prune_extremities` from `synapse/storage/controllers/persist_events.py`.
//!
//! The reads go through [`ExtremityReads`], which [`PersistReader`]
//! implements, so that the unit tests can run over an in-memory DAG. The
//! metrics go to a [`PersistCounts`] the caller owns. Shadow mode drops it,
//! as the Python path has already counted the same chunk.

use std::{
    collections::{HashMap, HashSet},
    fmt,
    sync::Arc,
};

use anyhow::{anyhow, bail};
use async_trait::async_trait;
use log::{error, info};
use serde_json::Value;

use super::{context::PersistContext, metrics::PersistCounts, reader::PersistReader};
use crate::{events::Event, state::StateMap, storage::events::RedactBehaviour};

/// `EventTypes.Dummy`.
const DUMMY: &str = "org.matrix.dummy_event";
/// `EventTypes.Create`.
const CREATE: &str = "m.room.create";

/// What the pre-transaction computation reads from an event being
/// persisted and its context.
#[derive(Clone, Debug, Default)]
pub struct NewEvent {
    pub event_id: Arc<str>,
    pub event_type: Arc<str>,
    /// The state key, if it is a state event. In an MSC4242 room, that is
    /// what puts it in the state DAG.
    pub state_key: Option<Arc<str>>,
    pub sender: Arc<str>,
    pub depth: i64,
    pub prev_event_ids: Vec<Arc<str>>,
    /// The MSC4242 `prev_state_events`, if the event's format has them.
    pub prev_state_events: Option<Vec<Arc<str>>>,
    /// `content.membership`, if it is a string.
    pub membership: Option<Arc<str>>,
    /// For a create event, `content.room_version` as
    /// `_get_new_state_after_events` reads it: `"1"` if it is missing, and
    /// `None` if it is falsy in Python's terms, which sends the reader to
    /// the database. A value that is not a string fails to parse as a
    /// room version, as Python's `KNOWN_ROOM_VERSIONS` lookup fails for it.
    pub create_room_version: Option<Arc<str>>,
    pub outlier: bool,
    pub soft_failed: bool,
    pub rejected: bool,
    /// The context's state group, the state after the event. `None` for an
    /// outlier.
    pub state_group: Option<i64>,
    /// The context's `state_group_deltas`.
    pub state_group_deltas: Arc<HashMap<(i64, i64), StateMap>>,
}

impl NewEvent {
    pub fn new(event: &Event, context: &PersistContext) -> anyhow::Result<Self> {
        let metadata = event.internal_metadata();
        let content = event.content();
        let membership = match content.get_field("membership") {
            Some(Value::String(membership)) => Some(Arc::from(membership.as_str())),
            _ => None,
        };
        let create_room_version = if event.type_() == CREATE && event.state_key() == Some("") {
            match content.get_field("room_version") {
                None => Some(Arc::from("1")),
                Some(Value::String(version)) if version.is_empty() => None,
                Some(Value::String(version)) => Some(Arc::from(version.as_str())),
                Some(value) if is_falsy(value) => None,
                // Prefixed so that a number such as `10` is not read as
                // the room version `"10"`.
                Some(value) => Some(Arc::from(format!("not a string: {value}"))),
            }
        } else {
            None
        };
        Ok(NewEvent {
            event_id: Arc::from(event.event_id()),
            event_type: Arc::from(event.type_()),
            state_key: event.state_key().map(Arc::from),
            sender: Arc::from(event.sender()),
            depth: event.depth(),
            prev_event_ids: event.prev_event_ids().into_iter().map(Arc::from).collect(),
            prev_state_events: event
                .prev_state_event_ids()
                .map(|ids| ids.iter().map(|id| Arc::from(id.as_str())).collect()),
            membership,
            create_room_version,
            outlier: metadata.is_outlier()?,
            soft_failed: metadata.soft_failed()?,
            rejected: context.is_rejected(),
            state_group: context.state_group,
            state_group_deltas: Arc::clone(&context.state_group_deltas),
        })
    }

    pub fn is_state(&self) -> bool {
        self.state_key.is_some()
    }

    /// The prev state events, which an event in the state DAG must have.
    fn prev_state(&self) -> anyhow::Result<&[Arc<str>]> {
        self.prev_state_events
            .as_deref()
            .ok_or_else(|| anyhow!("{} has no prev_state_events", self.event_id))
    }
}

/// Whether Python would read a JSON value as false.
fn is_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

/// What `_prune_extremities` reads from an extremity it might drop, and
/// from the prev events of a local dummy event.
#[derive(Clone, Debug, Default)]
pub struct DagEvent {
    pub sender: Arc<str>,
    pub event_type: Arc<str>,
    pub prev_event_ids: Vec<Arc<str>>,
    pub origin_server_ts: i64,
    pub depth: i64,
}

impl From<&Event> for DagEvent {
    fn from(event: &Event) -> Self {
        DagEvent {
            sender: Arc::from(event.sender()),
            event_type: Arc::from(event.type_()),
            prev_event_ids: event.prev_event_ids().into_iter().map(Arc::from).collect(),
            origin_server_ts: event.origin_server_ts(),
            depth: event.depth(),
        }
    }
}

/// `_calculate_new_state_dag_extremities` found prev state events that are
/// neither in the chunk nor in the database. Python raises a 500
/// `SynapseError` for this, and so does the facade.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MissingPrevStateEvents {
    pub room_id: String,
    pub count: usize,
}

impl fmt::Display for MissingPrevStateEvents {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "missing {} prev_state_events in room {}",
            self.count, self.room_id
        )
    }
}

impl std::error::Error for MissingPrevStateEvents {}

/// The reads the extremity calculations make.
#[async_trait]
pub trait ExtremityReads: Sync {
    /// See [`PersistReader::events_which_are_prevs`].
    async fn events_which_are_prevs(&self, event_ids: &[Arc<str>])
        -> anyhow::Result<Vec<Arc<str>>>;

    /// See [`PersistReader::prevs_before_rejected`].
    async fn prevs_before_rejected(
        &self,
        event_ids: &[Arc<str>],
        include_soft_failed: bool,
    ) -> anyhow::Result<HashSet<Arc<str>>>;

    /// See [`PersistReader::have_seen_events`].
    async fn have_seen_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<HashSet<Arc<str>>>;

    /// The events found among `event_ids`, rejected ones included, as
    /// `get_events(allow_rejected=True, redact_behaviour=as_is)` returns
    /// them.
    async fn dag_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<DagEvent>>;
}

#[async_trait]
impl ExtremityReads for PersistReader<'_> {
    async fn events_which_are_prevs(
        &self,
        event_ids: &[Arc<str>],
    ) -> anyhow::Result<Vec<Arc<str>>> {
        PersistReader::events_which_are_prevs(self, event_ids).await
    }

    async fn prevs_before_rejected(
        &self,
        event_ids: &[Arc<str>],
        include_soft_failed: bool,
    ) -> anyhow::Result<HashSet<Arc<str>>> {
        PersistReader::prevs_before_rejected(self, event_ids, include_soft_failed).await
    }

    async fn have_seen_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<HashSet<Arc<str>>> {
        PersistReader::have_seen_events(self, event_ids).await
    }

    async fn dag_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<DagEvent>> {
        let events = self
            .events(event_ids, RedactBehaviour::AsIs, true, false)
            .await?;
        Ok(events.iter().map(DagEvent::from).collect())
    }
}

/// `_calculate_new_extremities`: the forward extremities after persisting
/// `events` into a room whose extremities were `latest_event_ids`.
pub async fn calculate_new_extremities(
    reads: &dyn ExtremityReads,
    events: &[NewEvent],
    latest_event_ids: &HashSet<Arc<str>>,
    counts: &mut PersistCounts,
) -> anyhow::Result<HashSet<Arc<str>>> {
    // Only the new events that are not outliers, rejected or soft failed can
    // become extremities.
    let new_events: Vec<&NewEvent> = events
        .iter()
        .filter(|event| !event.outlier && !event.rejected && !event.soft_failed)
        .collect();
    let new_prevs: Vec<Arc<str>> = new_events
        .iter()
        .flat_map(|event| event.prev_event_ids.iter().cloned())
        .collect();

    let mut result = latest_event_ids.clone();
    result.extend(new_events.iter().map(|event| Arc::clone(&event.event_id)));
    for prev in &new_prevs {
        result.remove(prev);
    }

    // Drop the events that are already prev events of persisted events.
    let candidates: Vec<Arc<str>> = result.iter().cloned().collect();
    for prev in reads.events_which_are_prevs(&candidates).await? {
        result.remove(&prev);
    }

    // Drop the extremities behind soft failed or rejected prev events of
    // the new events. Otherwise they would dangle.
    for prev in reads.prevs_before_rejected(&new_prevs, true).await? {
        result.remove(&prev);
    }

    // Only chunks that change the extremities count, which leaves out
    // backfill and outliers.
    if result != *latest_event_ids {
        *counts
            .forward_extremities
            .entry(result.len() as u64)
            .or_default() += 1;
        let stale = latest_event_ids.intersection(&result).count();
        *counts
            .stale_forward_extremities
            .entry(stale as u64)
            .or_default() += 1;
    }

    Ok(result)
}

/// `_calculate_new_state_dag_extremities`: the MSC4242 state DAG forward
/// extremities after persisting `events` into a room whose state DAG
/// extremities were `existing`.
///
/// Fails with [`MissingPrevStateEvents`] if a prev state event is neither
/// in the chunk nor persisted. Also fails if no extremity is left, where
/// Python's `assert` raises an `AssertionError` instead.
pub async fn calculate_new_state_dag_extremities(
    reads: &dyn ExtremityReads,
    room_id: &str,
    existing: &HashSet<Arc<str>>,
    events: &[NewEvent],
    counts: &mut PersistCounts,
) -> anyhow::Result<HashSet<Arc<str>>> {
    let state_events: Vec<&NewEvent> = events.iter().filter(|event| event.is_state()).collect();
    if state_events.is_empty() {
        return Ok(existing.clone());
    }

    // Unlike the room DAG, this ignores outliers, keeps soft failed events
    // and does not look for persisted children: the state DAG is persisted
    // in causal order, so no persisted event can point at a new one.
    let accepted: Vec<&NewEvent> = state_events
        .iter()
        .copied()
        .filter(|event| !event.rejected)
        .collect();

    // Every prev state event must be known. Rejected events count as known,
    // as other events may point at them.
    let mut missing: HashSet<Arc<str>> = HashSet::new();
    for event in &state_events {
        missing.extend(event.prev_state()?.iter().cloned());
    }
    for event in &state_events {
        missing.remove(&event.event_id);
    }
    let to_check: Vec<Arc<str>> = missing.iter().cloned().collect();
    for seen in reads.have_seen_events(&to_check).await? {
        missing.remove(&seen);
    }
    if !missing.is_empty() {
        let mut missing: Vec<&str> = missing.iter().map(|id| &**id).collect();
        missing.sort_unstable();
        error!(
            "_calculate_new_state_dag_extremities: missing the following prev_state_events in room {room_id} : {missing:?}"
        );
        let handling: Vec<&str> = state_events.iter().map(|e| &*e.event_id).collect();
        error!("_calculate_new_state_dag_extremities: was handling {handling:?}");
        return Err(MissingPrevStateEvents {
            room_id: room_id.to_owned(),
            count: missing.len(),
        }
        .into());
    }

    let mut new_prevs: Vec<Arc<str>> = Vec::new();
    for event in &accepted {
        new_prevs.extend(event.prev_state()?.iter().cloned());
    }

    let mut result = existing.clone();
    result.extend(accepted.iter().map(|event| Arc::clone(&event.event_id)));
    for prev in &new_prevs {
        result.remove(prev);
    }

    // Drop the extremities behind rejected prev state events of the new
    // events, so that they do not dangle.
    for prev in reads.prevs_before_rejected(&new_prevs, false).await? {
        result.remove(&prev);
    }

    if result != *existing {
        *counts
            .state_dag_forward_extremities
            .entry(result.len() as u64)
            .or_default() += 1;
    }

    if result.is_empty() {
        bail!("No state dag forward extremities left in room {room_id}!");
    }
    Ok(result)
}

/// `_prune_extremities`: after state resolution, drop the extremities
/// whose state group is not `resolved_state_group`, if they all look stuck.
/// Either all of them go or none do, as state resolution over a subset
/// might come out differently.
///
/// An extremity looks stuck when it is not being persisted, is not a local
/// event (a local dummy event counts as local if it leads to a local event
/// through other local dummy events) and is either over a day old and more
/// than 100 events deep behind the chunk, or from a server that sent an
/// event in the chunk and more than 20 events deep behind it.
///
/// Fails if an extremity has no state group in `event_id_to_state_group`
/// (a `KeyError` in Python) or a sender is not a valid ID (a 400
/// `SynapseError` in Python, though event validation rules that out). As in
/// Python, the attempt is counted even then, so the caller should keep
/// `counts` on failure too.
#[allow(clippy::too_many_arguments)]
pub async fn prune_extremities(
    reads: &dyn ExtremityReads,
    room_id: &str,
    server_name: &str,
    new_latest_event_ids: HashSet<Arc<str>>,
    resolved_state_group: i64,
    event_id_to_state_group: &HashMap<Arc<str>, i64>,
    events: &[NewEvent],
    now_ms: i64,
    counts: &mut PersistCounts,
) -> anyhow::Result<HashSet<Arc<str>>> {
    counts.potential_times_prune_extremities += 1;

    let mut kept = HashSet::new();
    let mut dropped = Vec::new();
    for event_id in &new_latest_event_ids {
        let state_group = event_id_to_state_group
            .get(event_id)
            .ok_or_else(|| anyhow!("no state group for extremity {event_id}"))?;
        if *state_group == resolved_state_group {
            kept.insert(Arc::clone(event_id));
        } else {
            dropped.push(Arc::clone(event_id));
        }
    }

    let dropped_set: HashSet<&Arc<str>> = dropped.iter().collect();
    if events
        .iter()
        .any(|event| dropped_set.contains(&event.event_id))
    {
        return Ok(new_latest_event_ids);
    }

    let dropped_events = reads.dag_events(&dropped).await?;

    let mut new_senders = HashSet::new();
    for event in events {
        new_senders.insert(domain_from_id(&event.sender)?);
    }

    let one_day_ago = now_ms - 24 * 60 * 60 * 1000;
    let current_depth = events
        .iter()
        .map(|event| event.depth)
        .max()
        .ok_or_else(|| anyhow!("pruning extremities for an empty chunk"))?;

    for event in &dropped_events {
        // A local dummy event can go only if it does not lead to a local
        // event through other local dummy events: a new local event should
        // reference those.
        let mut to_check = vec![event.clone()];
        while !to_check.is_empty() {
            let mut next: HashSet<Arc<str>> = HashSet::new();
            for checking in &to_check {
                if is_mine_id(&checking.sender, server_name) {
                    if &*checking.event_type != DUMMY {
                        return Ok(new_latest_event_ids);
                    }
                    next.extend(checking.prev_event_ids.iter().cloned());
                }
            }
            if next.is_empty() {
                break;
            }
            let next: Vec<Arc<str>> = next.into_iter().collect();
            to_check = reads.dag_events(&next).await?;
        }

        if event.origin_server_ts < one_day_ago && event.depth < current_depth - 100 {
            continue;
        }

        // Extremities from a server in the chunk can go sooner, but not at
        // once, or every extremity of that server would go.
        if new_senders.contains(domain_from_id(&event.sender)?) && event.depth < current_depth - 20
        {
            continue;
        }

        return Ok(new_latest_event_ids);
    }

    counts.times_pruned_extremities += 1;
    info!(
        "Pruning forward extremities in room {room_id}: from {:?} -> {:?}",
        new_latest_event_ids, kept
    );
    Ok(kept)
}

/// `get_domain_from_id`.
fn domain_from_id(id: &str) -> anyhow::Result<&str> {
    id.split_once(':')
        .map(|(_, domain)| domain)
        .ok_or_else(|| anyhow!("Invalid ID: {id:?}"))
}

/// `HomeServer.is_mine_id`: false for a malformed ID.
pub(super) fn is_mine_id(id: &str, server_name: &str) -> bool {
    id.split_once(':')
        .is_some_and(|(_, domain)| domain == server_name)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A persisted event in the in-memory DAG.
    #[derive(Clone, Default)]
    struct Stored {
        prevs: Vec<&'static str>,
        outlier: bool,
        rejected: bool,
        soft_failed: bool,
        sender: &'static str,
        event_type: &'static str,
        ts: i64,
        depth: i64,
    }

    /// The reads over an in-memory DAG, with the same semantics as the SQL.
    #[derive(Default)]
    struct FakeReads {
        events: HashMap<&'static str, Stored>,
    }

    impl FakeReads {
        fn add(&mut self, id: &'static str, stored: Stored) {
            self.events.insert(id, stored);
        }
    }

    #[async_trait]
    impl ExtremityReads for FakeReads {
        async fn events_which_are_prevs(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<Vec<Arc<str>>> {
            let mut out = Vec::new();
            for child in self.events.values() {
                if child.outlier || child.rejected || child.soft_failed {
                    continue;
                }
                for prev in &child.prevs {
                    if event_ids.iter().any(|id| &**id == *prev) {
                        out.push(Arc::from(*prev));
                    }
                }
            }
            Ok(out)
        }

        async fn prevs_before_rejected(
            &self,
            event_ids: &[Arc<str>],
            include_soft_failed: bool,
        ) -> anyhow::Result<HashSet<Arc<str>>> {
            let mut found: HashSet<Arc<str>> = HashSet::new();
            let mut to_check: Vec<Arc<str>> = event_ids.to_vec();
            while let Some(id) = to_check.pop() {
                let Some(event) = self.events.get(&*id) else {
                    continue;
                };
                // As in the SQL, the flags are those of the event being
                // walked, and what is found is its prev events.
                let bad = event.rejected || (include_soft_failed && event.soft_failed);
                if event.outlier || !bad {
                    continue;
                }
                for prev in &event.prevs {
                    if found.insert(Arc::from(*prev)) {
                        to_check.push(Arc::from(*prev));
                    }
                }
            }
            Ok(found)
        }

        async fn have_seen_events(
            &self,
            event_ids: &[Arc<str>],
        ) -> anyhow::Result<HashSet<Arc<str>>> {
            Ok(event_ids
                .iter()
                .filter(|id| self.events.contains_key(&***id))
                .cloned()
                .collect())
        }

        async fn dag_events(&self, event_ids: &[Arc<str>]) -> anyhow::Result<Vec<DagEvent>> {
            Ok(event_ids
                .iter()
                .filter_map(|id| self.events.get(&**id))
                .map(|stored| DagEvent {
                    sender: Arc::from(stored.sender),
                    event_type: Arc::from(stored.event_type),
                    prev_event_ids: stored.prevs.iter().map(|p| Arc::from(*p)).collect(),
                    origin_server_ts: stored.ts,
                    depth: stored.depth,
                })
                .collect())
        }
    }

    fn ids(ids: &[&str]) -> HashSet<Arc<str>> {
        ids.iter().map(|id| Arc::from(*id)).collect()
    }

    fn new_event(id: &str, prevs: &[&str]) -> NewEvent {
        NewEvent {
            event_id: Arc::from(id),
            sender: Arc::from("@a:remote"),
            depth: 1,
            prev_event_ids: prevs.iter().map(|p| Arc::from(*p)).collect(),
            ..Default::default()
        }
    }

    fn state_event(id: &str, prev_state: &[&str]) -> NewEvent {
        NewEvent {
            state_key: Some(Arc::from("")),
            prev_state_events: Some(prev_state.iter().map(|p| Arc::from(*p)).collect()),
            ..new_event(id, &[])
        }
    }

    fn block_on<F: std::future::Future>(f: F) -> F::Output {
        futures::executor::block_on(f)
    }

    #[test]
    fn new_event_replaces_its_prev() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_extremities(
            &reads,
            &[new_event("$b", &["$a"])],
            &ids(&["$a"]),
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$b"]));
        assert_eq!(counts.forward_extremities[&1], 1);
        assert_eq!(counts.stale_forward_extremities[&0], 1);
    }

    #[test]
    fn outliers_rejected_and_soft_failed_do_not_count() {
        let reads = FakeReads::default();
        let events = [
            NewEvent {
                outlier: true,
                ..new_event("$o", &["$a"])
            },
            NewEvent {
                rejected: true,
                ..new_event("$r", &["$a"])
            },
            NewEvent {
                soft_failed: true,
                ..new_event("$s", &["$a"])
            },
        ];
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_extremities(
            &reads,
            &events,
            &ids(&["$a"]),
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$a"]));
        // Nothing changed, so nothing is counted.
        assert!(counts.forward_extremities.is_empty());
        assert!(counts.stale_forward_extremities.is_empty());
    }

    #[test]
    fn events_with_persisted_children_are_dropped() {
        // $c was persisted (as by backfill) before its prev $b arrived.
        let mut reads = FakeReads::default();
        reads.add(
            "$c",
            Stored {
                prevs: vec!["$b"],
                ..Default::default()
            },
        );
        // A soft failed child does not count.
        reads.add(
            "$sf",
            Stored {
                prevs: vec!["$d"],
                soft_failed: true,
                ..Default::default()
            },
        );
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_extremities(
            &reads,
            &[new_event("$b", &["$a"]), new_event("$d", &["$a"])],
            &ids(&["$a", "$c"]),
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$c", "$d"]));
        assert_eq!(counts.stale_forward_extremities[&1], 1);
    }

    #[test]
    fn soft_failed_prevs_take_their_ancestors_with_them() {
        // F <- SF1 <- SF2 <- N: F stops being an extremity.
        let mut reads = FakeReads::default();
        reads.add("$f", Stored::default());
        reads.add(
            "$sf1",
            Stored {
                prevs: vec!["$f"],
                soft_failed: true,
                ..Default::default()
            },
        );
        reads.add(
            "$sf2",
            Stored {
                prevs: vec!["$sf1"],
                soft_failed: true,
                ..Default::default()
            },
        );
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_extremities(
            &reads,
            &[new_event("$n", &["$sf2"])],
            &ids(&["$f"]),
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$n"]));
    }

    #[test]
    fn state_dag_without_state_events_is_unchanged() {
        let reads = FakeReads::default();
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$a"]),
            &[new_event("$m", &["$a"])],
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$a"]));
        assert!(counts.state_dag_forward_extremities.is_empty());
    }

    #[test]
    fn state_dag_missing_prev_state_events_fail() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let mut counts = PersistCounts::default();
        let err = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$a"]),
            &[
                state_event("$b", &["$a", "$gone1"]),
                state_event("$c", &["$b", "$gone2"]),
            ],
            &mut counts,
        ))
        .unwrap_err();
        assert_eq!(
            err.downcast_ref::<MissingPrevStateEvents>(),
            Some(&MissingPrevStateEvents {
                room_id: "!r:x".to_owned(),
                count: 2,
            })
        );
    }

    #[test]
    fn state_dag_checks_the_prevs_of_rejected_events() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let events = [NewEvent {
            rejected: true,
            ..state_event("$r", &["$gone"])
        }];
        let mut counts = PersistCounts::default();
        let err = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$a"]),
            &events,
            &mut counts,
        ))
        .unwrap_err();
        assert_eq!(
            err.downcast_ref::<MissingPrevStateEvents>()
                .map(|e| e.count),
            Some(1)
        );
    }

    #[test]
    fn state_dag_unchanged_is_not_counted() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let events = [NewEvent {
            rejected: true,
            ..state_event("$r", &["$a"])
        }];
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$a"]),
            &events,
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$a"]));
        assert!(counts.state_dag_forward_extremities.is_empty());
    }

    #[test]
    fn state_dag_keeps_soft_failed_and_knows_rejected() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let events = [
            // Rejected, but $c may point at it.
            NewEvent {
                rejected: true,
                ..state_event("$r", &["$a"])
            },
            NewEvent {
                soft_failed: true,
                ..state_event("$c", &["$r"])
            },
            // Not in the state DAG at all.
            new_event("$m", &["$c"]),
        ];
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$a"]),
            &events,
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$a", "$c"]));
        assert_eq!(counts.state_dag_forward_extremities[&2], 1);
    }

    #[test]
    fn state_dag_drops_extremities_behind_rejected_prevs() {
        // F <- R (rejected, persisted) <- N.
        let mut reads = FakeReads::default();
        reads.add("$f", Stored::default());
        reads.add(
            "$r",
            Stored {
                prevs: vec!["$f"],
                rejected: true,
                ..Default::default()
            },
        );
        let mut counts = PersistCounts::default();
        let result = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &ids(&["$f"]),
            &[state_event("$n", &["$r"])],
            &mut counts,
        ))
        .unwrap();
        assert_eq!(result, ids(&["$n"]));
    }

    #[test]
    fn state_dag_with_no_extremities_left_fails() {
        let mut reads = FakeReads::default();
        reads.add("$a", Stored::default());
        let mut counts = PersistCounts::default();
        let events = [NewEvent {
            rejected: true,
            ..state_event("$r", &["$a"])
        }];
        let err = block_on(calculate_new_state_dag_extremities(
            &reads,
            "!r:x",
            &HashSet::new(),
            &events,
            &mut counts,
        ))
        .unwrap_err();
        assert!(err.to_string().contains("No state dag forward extremities"));
    }

    const NOW: i64 = 10 * 24 * 60 * 60 * 1000;
    const TWO_DAYS_AGO: i64 = NOW - 2 * 24 * 60 * 60 * 1000;

    /// Prune with extremities $keep (state group 1, the resolved one) and
    /// $drop (state group 2), persisting `events`.
    fn prune(reads: &FakeReads, events: &[NewEvent]) -> (HashSet<Arc<str>>, PersistCounts) {
        let groups: HashMap<Arc<str>, i64> =
            [(Arc::from("$keep"), 1), (Arc::from("$drop"), 2)].into();
        let mut counts = PersistCounts::default();
        let result = block_on(prune_extremities(
            reads,
            "!r:x",
            "local",
            ids(&["$keep", "$drop"]),
            1,
            &groups,
            events,
            NOW,
            &mut counts,
        ))
        .unwrap();
        (result, counts)
    }

    fn deep_event(depth: i64) -> NewEvent {
        NewEvent {
            depth,
            sender: Arc::from("@n:other"),
            ..new_event("$n", &["$keep"])
        }
    }

    fn stored(sender: &'static str, ts: i64, depth: i64) -> Stored {
        Stored {
            sender,
            event_type: "m.room.message",
            ts,
            depth,
            ..Default::default()
        }
    }

    #[test]
    fn prune_drops_old_deep_extremities() {
        let mut reads = FakeReads::default();
        reads.add("$drop", stored("@x:remote", TWO_DAYS_AGO, 10));
        let (result, counts) = prune(&reads, &[deep_event(111)]);
        assert_eq!(result, ids(&["$keep"]));
        assert_eq!(counts.potential_times_prune_extremities, 1);
        assert_eq!(counts.times_pruned_extremities, 1);
    }

    #[test]
    fn prune_keeps_extremities_that_are_too_new_or_shallow() {
        let mut reads = FakeReads::default();
        // Old, but exactly 100 deep: kept.
        reads.add("$drop", stored("@x:remote", TWO_DAYS_AGO, 10));
        let (result, counts) = prune(&reads, &[deep_event(110)]);
        assert_eq!(result, ids(&["$keep", "$drop"]));
        assert_eq!(counts.potential_times_prune_extremities, 1);
        assert_eq!(counts.times_pruned_extremities, 0);

        // Deep, but recent: kept.
        reads.add("$drop", stored("@x:remote", NOW, 10));
        let (result, _) = prune(&reads, &[deep_event(500)]);
        assert_eq!(result, ids(&["$keep", "$drop"]));
    }

    #[test]
    fn prune_drops_sooner_for_a_server_in_the_chunk() {
        let mut reads = FakeReads::default();
        reads.add("$drop", stored("@x:other", NOW, 10));
        let (result, _) = prune(&reads, &[deep_event(31)]);
        assert_eq!(result, ids(&["$keep"]));
        let (result, _) = prune(&reads, &[deep_event(30)]);
        assert_eq!(result, ids(&["$keep", "$drop"]));
    }

    #[test]
    fn prune_keeps_extremities_being_persisted() {
        let mut reads = FakeReads::default();
        reads.add("$drop", stored("@x:remote", TWO_DAYS_AGO, 10));
        let (result, _) = prune(
            &reads,
            &[
                deep_event(500),
                NewEvent {
                    depth: 10,
                    ..new_event("$drop", &[])
                },
            ],
        );
        assert_eq!(result, ids(&["$keep", "$drop"]));
    }

    #[test]
    fn prune_keeps_local_events() {
        let mut reads = FakeReads::default();
        reads.add("$drop", stored("@me:local", TWO_DAYS_AGO, 10));
        let (result, _) = prune(&reads, &[deep_event(500)]);
        assert_eq!(result, ids(&["$keep", "$drop"]));
    }

    #[test]
    fn prune_follows_local_dummy_events() {
        let dummy = |prevs: Vec<&'static str>| Stored {
            event_type: DUMMY,
            prevs,
            ..stored("@me:local", TWO_DAYS_AGO, 10)
        };

        // dummy -> dummy -> remote: can go.
        let mut reads = FakeReads::default();
        reads.add("$drop", dummy(vec!["$d2"]));
        reads.add("$d2", dummy(vec!["$remote"]));
        reads.add("$remote", stored("@x:remote", TWO_DAYS_AGO, 8));
        let (result, _) = prune(&reads, &[deep_event(500)]);
        assert_eq!(result, ids(&["$keep"]));

        // dummy -> dummy -> local message: stays.
        reads.add("$local", stored("@me:local", TWO_DAYS_AGO, 8));
        reads.add("$d2", dummy(vec!["$remote", "$local"]));
        let (result, _) = prune(&reads, &[deep_event(500)]);
        assert_eq!(result, ids(&["$keep", "$drop"]));
    }

    #[test]
    fn prune_needs_every_state_group() {
        let reads = FakeReads::default();
        let mut counts = PersistCounts::default();
        let err = block_on(prune_extremities(
            &reads,
            "!r:x",
            "local",
            ids(&["$a"]),
            1,
            &HashMap::new(),
            &[deep_event(1)],
            NOW,
            &mut counts,
        ))
        .unwrap_err();
        assert!(err.to_string().contains("no state group"));
    }

    #[test]
    fn ids_and_domains() {
        assert_eq!(domain_from_id("@a:b.c:8448").unwrap(), "b.c:8448");
        assert!(domain_from_id("nope").is_err());
        assert!(is_mine_id("@a:local", "local"));
        assert!(!is_mine_id("@a:other", "local"));
        assert!(!is_mine_id("nope", "local"));
    }
}
