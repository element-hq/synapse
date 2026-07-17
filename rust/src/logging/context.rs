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

//! Native counterparts to `synapse.logging.context`.
//!
//! Currently just [`ContextResourceUsage`]; the goal is per-operation resource
//! accounting with no Python allocation on the switch path.
//!
//! TODO: the storage for the "current" logcontext, `LoggingContext` itself and
//! the sentinel follow — see `synapse.logging.context` for the Python
//! implementations being replaced.

use pyo3::prelude::*;

/// Tracks the resources used by a log context.
///
/// The public attribute surface, operators and `repr` are a compatibility
/// contract with the Python callers (Measure, request/background-process
/// metrics, the task scheduler, ...) — change both sides together. Native so the
/// switch machinery can do its rusage accounting without allocating a Python
/// object per operation.
// `skip_from_py_object`: pyo3 0.28 requires `Clone` pyclasses to explicitly
// opt in or out of a generated extract-by-clone `FromPyObject` (a bare
// `#[pyclass]` is a deprecation warning, and we build with `-D warnings`).
// Nothing extracts this type by value, so opt out.
#[pyclass(skip_from_py_object, get_all, set_all)]
#[derive(Clone, Default)]
pub struct ContextResourceUsage {
    /// System CPU time, in seconds.
    pub ru_stime: f64,
    /// User CPU time, in seconds.
    pub ru_utime: f64,
    /// Number of database transactions done.
    pub db_txn_count: i64,
    /// Time spent doing database transactions (excluding scheduling), in seconds.
    pub db_txn_duration_sec: f64,
    /// Time spent waiting for a database connection, in seconds.
    pub db_sched_duration_sec: f64,
    /// Number of events requested from the database.
    pub evt_db_fetch_count: i64,
}

impl ContextResourceUsage {
    fn add_assign(&mut self, other: &ContextResourceUsage) {
        self.ru_utime += other.ru_utime;
        self.ru_stime += other.ru_stime;
        self.db_txn_count += other.db_txn_count;
        self.db_txn_duration_sec += other.db_txn_duration_sec;
        self.db_sched_duration_sec += other.db_sched_duration_sec;
        self.evt_db_fetch_count += other.evt_db_fetch_count;
    }

    fn sub_assign(&mut self, other: &ContextResourceUsage) {
        self.ru_utime -= other.ru_utime;
        self.ru_stime -= other.ru_stime;
        self.db_txn_count -= other.db_txn_count;
        self.db_txn_duration_sec -= other.db_txn_duration_sec;
        self.db_sched_duration_sec -= other.db_sched_duration_sec;
        self.evt_db_fetch_count -= other.evt_db_fetch_count;
    }
}

#[pymethods]
impl ContextResourceUsage {
    /// `ContextResourceUsage(copy_from=None)` — if `copy_from` is given, copy its
    /// stats; otherwise start at zero.
    #[new]
    #[pyo3(signature = (copy_from=None))]
    fn new(copy_from: Option<&ContextResourceUsage>) -> Self {
        copy_from.cloned().unwrap_or_default()
    }

    /// Return a copy of this object.
    fn copy(&self) -> ContextResourceUsage {
        self.clone()
    }

    /// Reset all stats to zero.
    fn reset(&mut self) {
        *self = ContextResourceUsage::default();
    }

    fn __repr__(&self) -> String {
        // The single-quoted, `repr()`-style value formatting is the shape
        // this class logs in, and scrapers may match on it — keep it stable.
        // Rust's `{:?}` renders exponent-form floats as e.g. `1e-7` (Python
        // `repr` writes `1e-07`); the only consumer of the string form is
        // Measure's "Failed to save metrics!" warning, so we don't chase exact
        // parity with Python there.
        format!(
            "<ContextResourceUsage ru_stime='{:?}', ru_utime='{:?}', \
             db_txn_count='{}', db_txn_duration_sec='{:?}', \
             db_sched_duration_sec='{:?}', evt_db_fetch_count='{}'>",
            self.ru_stime,
            self.ru_utime,
            self.db_txn_count,
            self.db_txn_duration_sec,
            self.db_sched_duration_sec,
            self.evt_db_fetch_count,
        )
    }

    /// `self += other`; mutate in place. pyo3 returns `self` for the in-place slot.
    fn __iadd__(&mut self, other: &ContextResourceUsage) {
        self.add_assign(other);
    }

    /// `self -= other`; mutate in place. pyo3 returns `self` for the in-place slot.
    fn __isub__(&mut self, other: &ContextResourceUsage) {
        self.sub_assign(other);
    }

    /// `self + other`, returning a new object.
    fn __add__(&self, other: &ContextResourceUsage) -> ContextResourceUsage {
        let mut res = self.clone();
        res.add_assign(other);
        res
    }

    /// `self - other`, returning a new object.
    fn __sub__(&self, other: &ContextResourceUsage) -> ContextResourceUsage {
        let mut res = self.clone();
        res.sub_assign(other);
        res
    }
}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module: Bound<'_, PyModule> = PyModule::new(py, "logcontext")?;
    child_module.add_class::<ContextResourceUsage>()?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import logcontext` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.logcontext", child_module)?;

    Ok(())
}
