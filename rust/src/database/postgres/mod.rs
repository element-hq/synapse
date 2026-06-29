//! [`tokio_postgres`]-backed Postgres backend for the Rust `database` module.
//!
//! This module will grow the Python-facing `Connection` / `Cursor` classes and
//! the `connect` factory; for now it hosts the value-mapping layer ([`value`])
//! that converts between Python objects and Postgres' binary wire format.
//!
//! The driver itself is async; the eventual `Connection` / `Cursor` types will
//! drive it from sync Python methods via a shared multi-thread tokio runtime.

use pyo3::prelude::*;
use pyo3::types::PyModule;

// `pub` (rather than private) so the value-mapping types are reachable from the
// crate root as public API while nothing inside the crate consumes them yet.
// This is what stops clippy's `dead_code` lint from firing on them before the
// cursor/connection code (added in later changes) wires them up; the visibility
// is tightened back to private once that happens.
pub mod value;

/// Register the `postgres` submodule under the parent `database` module.
///
/// The `Connection` / `Cursor` classes and the `connect` factory are added in
/// later changes; for now this just creates the (otherwise empty) submodule so
/// the module tree — and the `value` mapping layer hanging off it — exists.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "postgres")?;

    m.add_submodule(&child)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust.database import postgres` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database.postgres", child)?;

    Ok(())
}
