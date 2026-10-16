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

//! The in-memory bookkeeping of a multi-writer stream: which stream IDs this
//! instance has allocated and not yet finished, how far each writer has got,
//! and from those the current token.
//!
//! `MultiWriterIdGenerator` in `synapse/storage/util/id_generators.py` keeps
//! the parts that touch the database or the reactor: loading the start
//! positions, the sequence, the `stream_positions` table and notifying
//! replication. It hands everything else to a [`StreamIdTracker`], so that
//! Rust code can allocate and finish stream IDs without taking the GIL.
//!
//! The current token must never pass an ID that this instance has allocated
//! but not finished. Otherwise a reader could see a later event and skip an
//! earlier one that commits after it. Allocation has two halves, around the
//! `nextval`: [`StreamIdTracker::begin_fetch`] records the highest ID seen so
//! far as "in flight", since the IDs that come back will be above it, and
//! [`StreamIdTracker::end_fetch`] swaps that for the allocated IDs.
//!
//! Units: the tracker stores IDs as positive numbers, also for streams that
//! count down such as backfill. Methods that take or return positions of
//! the stream as other code sees them (`advance`, the `get_*` methods) use
//! signed IDs. The bookkeeping methods (`begin_fetch`, `end_fetch`,
//! `mark_finished`, `load`) take positive IDs, as the sequence returns them.

use std::{
    cmp::Reverse,
    collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap, HashSet},
    sync::{Mutex, MutexGuard},
};

use log::debug;
use pyo3::prelude::*;

/// The logger of the debug line for issue 19795.
const ISSUE_19795_LOG_TARGET: &str = "synapse.9533_debug";

/// See the module docs.
pub struct StreamIdTracker {
    stream_name: String,
    instance_name: String,
    /// `1` for a stream that counts up, `-1` for one that counts down.
    return_factor: i64,
    state: Mutex<State>,
}

#[derive(Debug)]
struct State {
    /// Each writer's position. Includes our own once we have written.
    current_positions: HashMap<String, i64>,

    /// IDs this instance has allocated and not yet finished. The current
    /// position must stay below the smallest.
    unfinished_ids: BTreeSet<i64>,

    /// For each allocation in progress, the highest ID seen when it started,
    /// counted per value, since two allocations can start at the same one.
    /// The IDs they will get are above it, so the current position must not
    /// pass it.
    in_flight_fetches: BTreeMap<i64, usize>,

    /// IDs this instance has finished that are above the current position,
    /// because a lower ID is still unfinished.
    finished_ids: HashSet<i64>,

    /// The position up to which every writer has persisted everything.
    persisted_upto_position: i64,

    /// Persisted positions above `persisted_upto_position`. When the lowest
    /// is one above it, the position moves up. Gaps in the sequence (from
    /// rolled back transactions) stop that until the writers' positions pass
    /// them.
    known_persisted_positions: BinaryHeap<Reverse<i64>>,

    /// The highest ID allocated by any writer that we know of.
    max_seen_allocated_stream_id: i64,

    /// Our own position. It can be above our entry in `current_positions`
    /// when we have nothing in flight, since our next write will be above
    /// every ID allocated so far.
    max_position_of_local_instance: i64,
}

impl StreamIdTracker {
    /// A tracker before [`StreamIdTracker::load`]. Like a stream with no
    /// writes: ID 1 counts as persisted, so the first ID is 2.
    pub fn new(stream_name: String, instance_name: String, positive: bool) -> Self {
        StreamIdTracker {
            stream_name,
            instance_name,
            return_factor: if positive { 1 } else { -1 },
            state: Mutex::new(State {
                current_positions: HashMap::new(),
                unfinished_ids: BTreeSet::new(),
                in_flight_fetches: BTreeMap::new(),
                finished_ids: HashSet::new(),
                persisted_upto_position: 1,
                known_persisted_positions: BinaryHeap::new(),
                max_seen_allocated_stream_id: 1,
                max_position_of_local_instance: 1,
            }),
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        // The state is a set of integers that every method leaves
        // consistent before it can panic, so a poisoned lock is still usable.
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Set the start state, from what the generator read from the database.
    ///
    /// - `current_positions`: each writer's position.
    /// - `persisted_upto_position`: the lowest of those, or if there are
    ///   none, the highest ID in the stream's tables.
    /// - `persisted_rows`: the `(instance, stream ID)` rows above that
    ///   position, sorted by stream ID. Our own rows also move our position.
    ///   Old rows can have no instance.
    /// - `writers`: the configured writers. Each gets a position. With none,
    ///   any instance may write, so we start ourselves at the persisted
    ///   position.
    ///
    /// All IDs are positive (see the module docs).
    pub fn load(
        &self,
        current_positions: HashMap<String, i64>,
        persisted_upto_position: i64,
        persisted_rows: &[(Option<String>, i64)],
        writers: &[String],
    ) {
        let mut state = self.lock();
        state.current_positions = current_positions;
        state.persisted_upto_position = persisted_upto_position;

        for (instance, stream_id) in persisted_rows {
            self.add_persisted_position(&mut state, *stream_id, false);
            if instance.as_deref() == Some(self.instance_name.as_str()) {
                state
                    .current_positions
                    .insert(self.instance_name.clone(), *stream_id);
            }
        }

        let persisted_upto_position = state.persisted_upto_position;
        for writer in writers {
            state
                .current_positions
                .entry(writer.clone())
                .or_insert(persisted_upto_position);
        }

        // `stream_positions` may lag the tables, so the persisted position
        // can be the higher one.
        let max_seen = state
            .current_positions
            .values()
            .copied()
            .max()
            .unwrap_or(1)
            .max(state.persisted_upto_position);
        state.max_seen_allocated_stream_id = max_seen;
        state.max_position_of_local_instance = max_seen;

        if writers.is_empty() {
            state
                .current_positions
                .insert(self.instance_name.clone(), persisted_upto_position);
        }
    }

    /// Start allocating IDs. Until [`StreamIdTracker::end_fetch`] the
    /// current position stays at or below the returned value, the highest
    /// ID seen so far, since the new IDs will be above it.
    pub fn begin_fetch(&self) -> i64 {
        let mut state = self.lock();
        let current_max = state.max_seen_allocated_stream_id;
        *state.in_flight_fetches.entry(current_max).or_insert(0) += 1;
        current_max
    }

    /// Finish allocating IDs: `token` is what
    /// [`StreamIdTracker::begin_fetch`] returned and `allocated` the IDs, or
    /// `None` if the allocation failed. The IDs are unfinished until
    /// [`StreamIdTracker::mark_finished`].
    pub fn end_fetch(&self, token: i64, allocated: Option<&[i64]>) {
        let mut state = self.lock();
        if let Some(allocated) = allocated {
            state.unfinished_ids.extend(allocated.iter().copied());
            if let Some(&max_unfinished) = state.unfinished_ids.last() {
                state.max_seen_allocated_stream_id =
                    state.max_seen_allocated_stream_id.max(max_unfinished);
            }
        }
        match state.in_flight_fetches.get_mut(&token) {
            Some(count) if *count > 1 => *count -= 1,
            Some(_) => {
                state.in_flight_fetches.remove(&token);
            }
            None => panic!("end_fetch({token}) without a matching begin_fetch"),
        }
    }

    /// The transactions that wrote these IDs have finished, committed or
    /// not. Moves the current position up if it can.
    pub fn mark_finished(&self, ids: &[i64]) {
        let log_issue_19795 = self.wants_issue_19795_log();
        let mut state = self.lock();
        for id in ids {
            state.unfinished_ids.remove(id);
        }
        state.finished_ids.extend(ids.iter().copied());

        let new_cur = if let Some(min_unfinished) = state.min_unfinished() {
            // The new position is the largest finished ID below the smallest
            // unfinished one. The rest stay finished.
            let mut new_cur = None;
            state.finished_ids.retain(|&s| {
                if s < min_unfinished {
                    new_cur = new_cur.max(Some(s));
                    false
                } else {
                    true
                }
            });
            new_cur
        } else {
            // Nothing is unfinished, so the largest finished ID is the new
            // position.
            let new_cur = state.finished_ids.iter().copied().max();
            state.finished_ids.clear();
            new_cur
        };

        // Python skipped a new position of 0 as falsy. IDs start at 2, so
        // that only matters for the type.
        if let Some(new_cur) = new_cur.filter(|&c| c != 0) {
            let curr = state
                .current_positions
                .get(&self.instance_name)
                .copied()
                .unwrap_or(0);
            state
                .current_positions
                .insert(self.instance_name.clone(), curr.max(new_cur));
            state.max_position_of_local_instance =
                state.max_position_of_local_instance.max(curr.max(new_cur));
        }

        let mut log_lines = Vec::new();
        for &id in ids {
            if let Some(line) = self.add_persisted_position(&mut state, id, log_issue_19795) {
                log_lines.push(line);
            }
        }
        drop(state);
        // Logging can take the GIL, so it must not happen under the lock: a
        // Python thread holding the GIL may be waiting for it.
        for line in log_lines {
            debug!(target: ISSUE_19795_LOG_TARGET, "{line}");
        }
    }

    /// Another writer has persisted up to `new_id` (signed).
    pub fn advance(&self, instance_name: &str, new_id: i64) {
        let log_issue_19795 = self.wants_issue_19795_log();
        let new_id = new_id * self.return_factor;
        let mut state = self.lock();
        let position = state
            .current_positions
            .entry(instance_name.to_owned())
            .or_insert(0);
        *position = (*position).max(new_id);
        state.max_seen_allocated_stream_id = state.max_seen_allocated_stream_id.max(new_id);
        let line = self.add_persisted_position(&mut state, new_id, log_issue_19795);
        drop(state);
        if let Some(line) = line {
            debug!(target: ISSUE_19795_LOG_TARGET, "{line}");
        }
    }

    /// The highest position such that everything up to it has been
    /// persisted by every writer. Signed.
    pub fn get_persisted_upto_position(&self) -> i64 {
        self.return_factor * self.lock().persisted_upto_position
    }

    /// The position of `instance_name`, signed. For a writer we have not
    /// heard from yet, the persisted position: assuming it is new saves a
    /// table scan when it announces itself.
    pub fn get_current_token_for_writer(&self, instance_name: &str) -> i64 {
        let state = self.lock();
        if self.instance_name == instance_name {
            return self.return_factor * state.max_position_of_local_instance;
        }
        let pos = state
            .current_positions
            .get(instance_name)
            .copied()
            .unwrap_or(state.persisted_upto_position)
            .max(state.persisted_upto_position);
        self.return_factor * pos
    }

    /// Our last finished write, or the persisted position if we have none.
    /// Signed.
    pub fn get_minimal_local_current_token(&self) -> i64 {
        let state = self.lock();
        self.return_factor
            * state
                .current_positions
                .get(&self.instance_name)
                .copied()
                .unwrap_or(state.persisted_upto_position)
    }

    /// Each writer's position, signed.
    pub fn get_positions(&self) -> HashMap<String, i64> {
        self.lock()
            .current_positions
            .iter()
            .map(|(name, &pos)| (name.clone(), self.return_factor * pos))
            .collect()
    }

    /// The persisted position as a positive number, also for streams that
    /// count down. For the `synapse_storage_stream_current_position` gauge.
    pub fn persisted_upto_position_abs(&self) -> i64 {
        self.lock().persisted_upto_position
    }

    fn wants_issue_19795_log(&self) -> bool {
        self.stream_name == "to_device"
            && log::log_enabled!(target: ISSUE_19795_LOG_TARGET, log::Level::Debug)
    }

    /// Record that `new_id` (positive) has been persisted, and move the
    /// persisted position up as far as that allows. Returns the issue 19795
    /// debug line if `log_issue_19795` and it applies.
    fn add_persisted_position(
        &self,
        state: &mut State,
        new_id: i64,
        log_issue_19795: bool,
    ) -> Option<String> {
        state.known_persisted_positions.push(Reverse(new_id));

        // Every position below the lowest writer's is persisted. While we
        // have nothing in flight our own position does not count: our next
        // write will be above every other writer's position.
        let our_current_position = state
            .current_positions
            .get(&self.instance_name)
            .copied()
            .unwrap_or(0);
        let mut min_curr = state
            .current_positions
            .iter()
            .filter(|(name, _)| **name != self.instance_name)
            .map(|(_, &token)| token)
            .min()
            .unwrap_or(our_current_position);
        let in_flight = !state.unfinished_ids.is_empty() || !state.in_flight_fetches.is_empty();
        if our_current_position != 0 && in_flight {
            min_curr = min_curr.min(our_current_position);
        }

        state.persisted_upto_position = state.persisted_upto_position.max(min_curr);
        state.max_position_of_local_instance = state
            .max_position_of_local_instance
            .max(state.persisted_upto_position);

        if !in_flight {
            // Nothing in flight, so our next write will be above everything
            // allocated so far.
            state.max_position_of_local_instance = state
                .max_position_of_local_instance
                .max(state.max_seen_allocated_stream_id);
        }

        // Drop known positions at or below the persisted position, and move
        // it up through any run without gaps.
        while let Some(&Reverse(lowest)) = state.known_persisted_positions.peek() {
            if lowest <= state.persisted_upto_position {
                state.known_persisted_positions.pop();
            } else if lowest == state.persisted_upto_position + 1 {
                state.known_persisted_positions.pop();
                state.persisted_upto_position += 1;
            } else {
                break;
            }
        }

        // Debugging for https://github.com/element-hq/synapse/issues/19795,
        // on the to-device stream of a writer.
        if log_issue_19795 && our_current_position > 0 {
            let mut known: Vec<i64> = state
                .known_persisted_positions
                .iter()
                .map(|Reverse(p)| *p)
                .collect();
            known.sort_unstable();
            Some(format!(
                "stream_id={new_id} now persisted for stream={}; _current_positions={:?} \
                 _unfinished_ids={:?}, _known_persisted_positions={known:?} \
                 _persisted_upto_position={} min_curr={min_curr}",
                self.stream_name,
                state.current_positions,
                state.unfinished_ids,
                state.persisted_upto_position,
            ))
        } else {
            None
        }
    }
}

impl State {
    /// The smallest ID the current position must stay below, if any.
    fn min_unfinished(&self) -> Option<i64> {
        // An in-flight fetch records the highest safe ID, so the smallest
        // unsafe one is one above it.
        let in_flight = self.in_flight_fetches.keys().next().map(|&max| max + 1);
        let unfinished = self.unfinished_ids.first().copied();
        match (unfinished, in_flight) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
    }
}

/// The Python face of a [`StreamIdTracker`]. Shared with Rust code as an
/// `Arc` through [`PyStreamIdTracker::tracker`].
#[pyclass(frozen, name = "StreamIdTracker")]
pub struct PyStreamIdTracker {
    tracker: std::sync::Arc<StreamIdTracker>,
}

impl PyStreamIdTracker {
    pub fn tracker(&self) -> std::sync::Arc<StreamIdTracker> {
        std::sync::Arc::clone(&self.tracker)
    }
}

#[pymethods]
impl PyStreamIdTracker {
    #[new]
    #[pyo3(signature = (stream_name, instance_name, positive))]
    fn py_new(stream_name: String, instance_name: String, positive: bool) -> Self {
        PyStreamIdTracker {
            tracker: std::sync::Arc::new(StreamIdTracker::new(
                stream_name,
                instance_name,
                positive,
            )),
        }
    }

    fn load(
        &self,
        current_positions: HashMap<String, i64>,
        persisted_upto_position: i64,
        persisted_rows: Vec<(Option<String>, i64)>,
        writers: Vec<String>,
    ) {
        self.tracker.load(
            current_positions,
            persisted_upto_position,
            &persisted_rows,
            &writers,
        )
    }

    fn begin_fetch(&self) -> i64 {
        self.tracker.begin_fetch()
    }

    #[pyo3(signature = (token, allocated))]
    fn end_fetch(&self, token: i64, allocated: Option<Vec<i64>>) {
        self.tracker.end_fetch(token, allocated.as_deref())
    }

    fn mark_finished(&self, ids: Vec<i64>) {
        self.tracker.mark_finished(&ids)
    }

    fn advance(&self, instance_name: &str, new_id: i64) {
        self.tracker.advance(instance_name, new_id)
    }

    fn get_persisted_upto_position(&self) -> i64 {
        self.tracker.get_persisted_upto_position()
    }

    fn get_current_token_for_writer(&self, instance_name: &str) -> i64 {
        self.tracker.get_current_token_for_writer(instance_name)
    }

    fn get_minimal_local_current_token(&self) -> i64 {
        self.tracker.get_minimal_local_current_token()
    }

    fn get_positions(&self) -> HashMap<String, i64> {
        self.tracker.get_positions()
    }

    fn persisted_upto_position_abs(&self) -> i64 {
        self.tracker.persisted_upto_position_abs()
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "stream_ids")?;
    child_module.add_class::<PyStreamIdTracker>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import stream_ids` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.stream_ids", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tracker(writers: &[&str]) -> StreamIdTracker {
        let tracker = StreamIdTracker::new("events".into(), "first".into(), true);
        let writers: Vec<String> = writers.iter().map(|w| w.to_string()).collect();
        let positions = writers.iter().map(|w| (w.clone(), 7)).collect();
        tracker.load(positions, 7, &[], &writers);
        tracker
    }

    fn allocate(tracker: &StreamIdTracker, ids: &[i64]) {
        let token = tracker.begin_fetch();
        tracker.end_fetch(token, Some(ids));
    }

    #[test]
    fn finishing_out_of_order_waits_for_the_gap() {
        let tracker = tracker(&["first"]);
        allocate(&tracker, &[8]);
        allocate(&tracker, &[9]);

        tracker.mark_finished(&[9]);
        assert_eq!(tracker.get_persisted_upto_position(), 7);
        assert_eq!(tracker.get_current_token_for_writer("first"), 7);

        tracker.mark_finished(&[8]);
        assert_eq!(tracker.get_persisted_upto_position(), 9);
        assert_eq!(tracker.get_current_token_for_writer("first"), 9);
    }

    #[test]
    fn in_flight_fetches_count_each_start() {
        // Two fetches that start at the same maximum: the position must stay
        // pinned until both have ended.
        let tracker = tracker(&["first"]);
        let a = tracker.begin_fetch();
        let b = tracker.begin_fetch();
        assert_eq!(a, b);

        tracker.end_fetch(a, Some(&[8]));
        tracker.mark_finished(&[8]);
        // Our own position waits for `b`. (The persisted position moves to
        // 8, as 8 is the next ID after 7 and IDs are never reused.)
        assert_eq!(tracker.get_minimal_local_current_token(), 7);
        assert_eq!(tracker.get_persisted_upto_position(), 8);

        tracker.end_fetch(b, Some(&[9]));
        tracker.mark_finished(&[9]);
        assert_eq!(tracker.get_persisted_upto_position(), 9);
    }

    #[test]
    fn failed_fetch_releases_the_position() {
        let tracker = tracker(&["first"]);
        let token = tracker.begin_fetch();
        tracker.end_fetch(token, None);
        allocate(&tracker, &[8]);
        tracker.mark_finished(&[8]);
        assert_eq!(tracker.get_persisted_upto_position(), 8);
    }

    #[test]
    fn unfinished_ids_hold_back_the_persisted_position() {
        let tracker = StreamIdTracker::new("events".into(), "first".into(), true);
        let writers = vec!["first".to_string(), "second".to_string()];
        tracker.load(
            [("first".to_string(), 3), ("second".to_string(), 5)].into(),
            3,
            &[],
            &writers,
        );
        assert_eq!(tracker.get_persisted_upto_position(), 3);

        // With nothing in flight, our own position does not hold the
        // persisted position back.
        tracker.advance("second", 6);
        assert_eq!(tracker.get_persisted_upto_position(), 6);

        tracker.advance("second", 8);
        allocate(&tracker, &[9]);
        tracker.advance("second", 11);
        // Our unfinished 9 counts our own position (still 3), so the
        // persisted position stays where it was.
        assert_eq!(tracker.get_persisted_upto_position(), 8);
        tracker.mark_finished(&[9]);
        assert_eq!(tracker.get_persisted_upto_position(), 11);
    }

    #[test]
    fn negative_streams_return_negative_ids() {
        let tracker = StreamIdTracker::new("backfill".into(), "first".into(), false);
        let writers = vec!["first".to_string()];
        tracker.load([("first".to_string(), 3)].into(), 3, &[], &writers);
        allocate(&tracker, &[4]);
        tracker.mark_finished(&[4]);
        assert_eq!(tracker.get_persisted_upto_position(), -4);
        assert_eq!(tracker.get_positions()["first"], -4);
        assert_eq!(tracker.persisted_upto_position_abs(), 4);

        tracker.advance("other", -6);
        assert_eq!(tracker.get_current_token_for_writer("other"), -6);
    }

    #[test]
    fn load_moves_our_position_to_our_later_rows() {
        let tracker = StreamIdTracker::new("events".into(), "first".into(), true);
        let writers = vec!["first".to_string(), "second".to_string()];
        tracker.load(
            [("first".to_string(), 3), ("second".to_string(), 5)].into(),
            3,
            &[
                (Some("first".to_string()), 4),
                (None, 5),
                (Some("first".to_string()), 6),
            ],
            &writers,
        );
        assert_eq!(tracker.get_positions()["first"], 6);
        assert_eq!(tracker.get_persisted_upto_position(), 6);
        assert_eq!(tracker.get_current_token_for_writer("first"), 6);
    }
}
