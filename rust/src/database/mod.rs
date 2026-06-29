//! DBAPI2-shaped Connection / Cursor types implemented in Rust.
//!
//! Currently this provides a single Postgres backend ([`tokio_postgres`]); see
//! the [`postgres`] submodule.

pub mod postgres;
pub mod runtime;

use pyo3::prelude::*;
use pyo3::types::PyModule;

/// Register the `database` submodule (and its per-backend children) on the
/// top-level `synapse_rust` module.
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
