//! [`tokio_postgres`]-backed Postgres backend for the Rust `database` module.
//!
//! The driver is async. [`helpers`] drives its futures to completion from
//! synchronous Python code on the shared tokio runtime, and [`value`] converts
//! between Python objects and Postgres' binary wire format.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

// `pub` so the items in these submodules count as public API even though
// nothing consumes them yet, which keeps clippy's `dead_code` lint quiet.
// Tighten to private once the connection/cursor code uses them.
pub mod helpers;
pub mod value;

/// Register the (currently empty) `postgres` submodule under the parent
/// `database` module.
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

/// Map a [`tokio_postgres`] error into a Python `RuntimeError`.
fn pg_err_to_py(e: tokio_postgres::Error) -> PyErr {
    PyRuntimeError::new_err(format!("postgres error: {e}"))
}
