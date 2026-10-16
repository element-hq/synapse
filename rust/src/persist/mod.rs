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

//! Persisting events, behind `experimental_features.rust_event_persistence`.
//!
//! This is being built up from the inside out (see
//! `renders/event-persistence-rust-plan.md`). So far:
//!
//! - [`context`]: what persisting needs from an `EventContext`.
//! - [`effects`]: the Python cache updates a Rust transaction asks for once
//!   it commits.
//! - [`extremities`]: the room's forward extremities after a chunk.
//! - [`metrics`]: counts for the Python metrics, flushed when scraped.
//! - [`python`]: the [`python::Persister`] and test helpers.
//! - [`reader`]: what persisting reads before its write transaction.
//! - [`state_delta`]: the room's new current state after a chunk.
//!
//! Every GIL hold in this tree goes through [`crate::gil::with_gil`] with a
//! site named `persist_*`, so that tests can pin the count per site.

use pyo3::prelude::*;

pub mod context;
pub mod effects;
pub mod extremities;
pub mod metrics;
pub mod python;
pub mod reader;
pub mod state_delta;

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "persist")?;
    child_module.add_class::<python::Persister>()?;
    child_module.add_class::<effects::PersistEffects>()?;
    child_module.add_class::<reader::PyPersistReaderForTest>()?;
    child_module.add_function(wrap_pyfunction!(
        crate::gil::persist_gil_stats,
        &child_module
    )?)?;
    child_module.add_function(wrap_pyfunction!(
        python::persistence_json_for_test,
        &child_module
    )?)?;
    child_module.add_function(wrap_pyfunction!(
        python::persist_context_for_test,
        &child_module
    )?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import persist` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.persist", child_module)?;

    Ok(())
}
