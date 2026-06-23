//! DBAPI2-shaped Connection / Cursor types implemented in Rust.
//!
//! The Python-facing surface is a single `Connection` / `Cursor` pair (see
//! `unified`); two backends — Postgres (`tokio-postgres`) and SQLite
//! (`rusqlite`) — sit behind that single shape and are picked via the
//! `connect_postgres` / `connect_sqlite` factories.

pub mod helpers;
pub mod postgres;
pub mod runtime;

use pyo3::prelude::*;
use pyo3::types::PyModule;

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "database")?;

    postgres::register_module(py, &child)?;

    m.add_submodule(&child)?;

    // Mirror the convention used by other rust submodules so
    // `from synapse.synapse_rust import database` works.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database", child)?;

    Ok(())
}
