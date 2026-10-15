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

//! Accounting for every time federation code takes the GIL.
//!
//! The GIL is what the reactor thread needs to run, so a tokio thread that
//! holds it stalls the reactor as surely as work on the reactor thread does.
//! The federation code aims to take it only at the edges: converting a
//! request in and a response out. So every `Python::attach` in this module
//! tree goes through [`with_gil`], which counts the calls and the time held
//! per call site. Tests pin the counts, so that a new crossing fails a test,
//! and the counts are exported as metrics.

use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};

use pyo3::prelude::*;

/// Calls and total hold time, per call site.
static STATS: Mutex<Option<HashMap<&'static str, SiteStats>>> = Mutex::new(None);

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct SiteStats {
    pub calls: u64,
    pub held: Duration,
}

/// Run `f` with the GIL held, and account for it under `site`.
///
/// The time counted is the time `f` runs for, which is the time other
/// threads cannot run Python. The wait to acquire the GIL is not counted.
pub fn with_gil<R>(site: &'static str, f: impl for<'py> FnOnce(Python<'py>) -> R) -> R {
    Python::attach(|py| {
        let start = Instant::now();
        let result = f(py);
        record(site, start.elapsed());
        result
    })
}

/// Account for `f` under `site`, for code that already holds the GIL, such as
/// a closure dispatched to the reactor.
pub fn account<R>(site: &'static str, py: Python<'_>, f: impl FnOnce(Python<'_>) -> R) -> R {
    let start = Instant::now();
    let result = f(py);
    record(site, start.elapsed());
    result
}

fn record(site: &'static str, held: Duration) {
    let mut stats = STATS.lock().expect("poisoned");
    let entry = stats
        .get_or_insert_with(HashMap::new)
        .entry(site)
        .or_default();
    entry.calls += 1;
    entry.held += held;
}

/// A snapshot of the counts.
pub fn snapshot() -> HashMap<&'static str, SiteStats> {
    STATS.lock().expect("poisoned").clone().unwrap_or_default()
}

/// The counts, as `{site: (calls, seconds held)}`. For the metrics collector
/// and tests.
#[pyfunction]
pub fn federation_gil_stats() -> HashMap<&'static str, (u64, f64)> {
    snapshot()
        .into_iter()
        .map(|(site, stats)| (site, (stats.calls, stats.held.as_secs_f64())))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_calls_per_site() {
        Python::initialize();

        let before = snapshot().get("test_site").copied().unwrap_or_default();
        let answer = with_gil("test_site", |_py| 42);
        with_gil("test_site", |_py| ());
        let after = snapshot()["test_site"];

        assert_eq!(answer, 42);
        assert_eq!(after.calls, before.calls + 2);
        assert!(after.held >= before.held);
    }
}
