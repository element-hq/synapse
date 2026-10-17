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

//! What persisting counts for the Python metrics.
//!
//! Rust code adds to [`PersistMetrics`] without the GIL. A
//! `pre_collect_hooks` hook takes the counts when the metrics are scraped
//! and adds them to the Python counters and histograms that the Python path
//! updates directly, so a dashboard sees one series whichever path ran.

use std::{
    collections::{BTreeMap, HashMap},
    sync::Mutex,
};

use pyo3::{prelude::*, types::PyDict};

/// The counts since the last [`PersistMetrics::take`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PersistCounts {
    /// `synapse_storage_events_persisted_events`.
    pub persisted_events: u64,
    /// `synapse_storage_events_persisted_events_sep`, by `(type, origin_type)`.
    pub persisted_events_by_type: HashMap<(String, &'static str), u64>,
    /// `synapse_storage_events_state_delta`.
    pub state_delta: u64,
    /// `synapse_storage_events_state_delta_single_event`.
    pub state_delta_single_event: u64,
    /// `synapse_storage_events_state_delta_reuse_delta`.
    pub state_delta_reuse_delta: u64,
    /// `synapse_storage_events_state_resolutions_during_persistence`.
    pub state_resolutions: u64,
    /// `synapse_storage_events_potential_times_prune_extremities`.
    pub potential_times_prune_extremities: u64,
    /// `synapse_storage_events_times_pruned_extremities`.
    pub times_pruned_extremities: u64,
    /// Observations of `synapse_storage_events_forward_extremities_persisted`,
    /// as value to count. The values are small, so this stays small however
    /// long it goes unscraped.
    pub forward_extremities: BTreeMap<u64, u64>,
    /// As above, for `..._stale_forward_extremities_persisted`.
    pub stale_forward_extremities: BTreeMap<u64, u64>,
    /// As above, for `synapse_storage_msc4242_state_dag_forward_extremities_persisted`.
    pub state_dag_forward_extremities: BTreeMap<u64, u64>,
    /// The newest value for the `synapse_event_persisted_position` gauge, if
    /// it moved since the last take. The Python path sets the same gauge, so
    /// an old value set again on every scrape could move it backwards.
    pub persisted_position: Option<i64>,
}

/// See the module docs.
#[derive(Default)]
pub struct PersistMetrics {
    counts: Mutex<PersistCounts>,
}

impl PersistMetrics {
    /// Update the counts.
    pub fn record(&self, f: impl FnOnce(&mut PersistCounts)) {
        f(&mut self.counts.lock().unwrap_or_else(|e| e.into_inner()));
    }

    /// The counts since the last call, which start again from zero.
    pub fn take(&self) -> PersistCounts {
        std::mem::take(&mut *self.counts.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

impl PersistCounts {
    /// Add `other` to these counts. Its persisted position, if it has one,
    /// is the newer.
    pub fn add(&mut self, other: PersistCounts) {
        self.persisted_events += other.persisted_events;
        for (key, count) in other.persisted_events_by_type {
            *self.persisted_events_by_type.entry(key).or_default() += count;
        }
        self.state_delta += other.state_delta;
        self.state_delta_single_event += other.state_delta_single_event;
        self.state_delta_reuse_delta += other.state_delta_reuse_delta;
        self.state_resolutions += other.state_resolutions;
        self.potential_times_prune_extremities += other.potential_times_prune_extremities;
        self.times_pruned_extremities += other.times_pruned_extremities;
        for (mine, theirs) in [
            (&mut self.forward_extremities, other.forward_extremities),
            (
                &mut self.stale_forward_extremities,
                other.stale_forward_extremities,
            ),
            (
                &mut self.state_dag_forward_extremities,
                other.state_dag_forward_extremities,
            ),
        ] {
            for (value, count) in theirs {
                *mine.entry(value).or_default() += count;
            }
        }
        if other.persisted_position.is_some() {
            self.persisted_position = other.persisted_position;
        }
    }

    /// The counts as the dict `flush_rust_persist_metrics` reads.
    pub fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item("persisted_events", self.persisted_events)?;
        let by_type = PyDict::new(py);
        for ((event_type, origin_type), count) in &self.persisted_events_by_type {
            by_type.set_item((event_type, *origin_type), count)?;
        }
        dict.set_item("persisted_events_by_type", by_type)?;
        dict.set_item("state_delta", self.state_delta)?;
        dict.set_item("state_delta_single_event", self.state_delta_single_event)?;
        dict.set_item("state_delta_reuse_delta", self.state_delta_reuse_delta)?;
        dict.set_item("state_resolutions", self.state_resolutions)?;
        dict.set_item(
            "potential_times_prune_extremities",
            self.potential_times_prune_extremities,
        )?;
        dict.set_item("times_pruned_extremities", self.times_pruned_extremities)?;
        dict.set_item("forward_extremities", &self.forward_extremities)?;
        dict.set_item("stale_forward_extremities", &self.stale_forward_extremities)?;
        dict.set_item(
            "state_dag_forward_extremities",
            &self.state_dag_forward_extremities,
        )?;
        dict.set_item("persisted_position", self.persisted_position)?;
        Ok(dict)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_sums_counts_and_keeps_the_newer_position() {
        let mut total = PersistCounts {
            state_delta: 1,
            persisted_position: Some(5),
            ..Default::default()
        };
        total.forward_extremities.insert(1, 1);
        let mut more = PersistCounts {
            state_delta: 2,
            times_pruned_extremities: 1,
            ..Default::default()
        };
        more.forward_extremities.insert(1, 2);
        more.forward_extremities.insert(3, 1);

        total.add(more.clone());
        assert_eq!(total.state_delta, 3);
        assert_eq!(total.times_pruned_extremities, 1);
        assert_eq!(total.forward_extremities, BTreeMap::from([(1, 3), (3, 1)]));
        // `more` had no position, so the old one stays.
        assert_eq!(total.persisted_position, Some(5));

        more.persisted_position = Some(7);
        total.add(more);
        assert_eq!(total.persisted_position, Some(7));
    }

    #[test]
    fn take_starts_again() {
        let metrics = PersistMetrics::default();
        metrics.record(|c| {
            c.persisted_events += 3;
            *c.forward_extremities.entry(1).or_default() += 2;
            c.persisted_position = Some(10);
        });

        let taken = metrics.take();
        assert_eq!(taken.persisted_events, 3);
        assert_eq!(taken.forward_extremities[&1], 2);
        assert_eq!(taken.persisted_position, Some(10));

        let again = metrics.take();
        assert_eq!(again.persisted_events, 0);
        assert!(again.forward_extremities.is_empty());
        assert_eq!(again.persisted_position, None);
    }
}
