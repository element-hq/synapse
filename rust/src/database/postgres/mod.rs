//! [`tokio_postgres`]-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via a
//! shared multi-thread tokio runtime (see `super::runtime`).

use anyhow::Error;
use pyo3::prelude::*;
use pyo3::types::PyModule;

mod connection;
mod cursor_state;
mod errors;
mod helpers;
mod libpq;
pub mod pool;
pub(crate) mod query;
mod value;

/// Register the `postgres` submodule (the `ConnectionPool`, `Connection` and
/// `Cursor` classes and the DBAPI2 exception hierarchy) under the parent
/// `database` module.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "postgres")?;

    child.add_class::<pool::PyConnectionPool>()?;
    child.add_class::<connection::Connection>()?;
    child.add_class::<connection::Cursor>()?;
    errors::register_exceptions(py, &child)?;

    m.add_submodule(&child)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust.database import postgres` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database.postgres", child)?;

    Ok(())
}

/// Fix up a DSN to ensure it has a host, using libpq's default host if
/// necessary.
///
/// [`tokio_postgres`] has a different default host than libpq, which is what
/// Synapse previously used (and is what e.g. `psql` uses). libpq's default host
/// is configurable, so when the DSN omits a host we ask libpq what its default
/// would be and use that instead (see [`libpq::default_host`]).
pub(crate) fn fixup_default_host(dsn: &str) -> Result<tokio_postgres::Config, Error> {
    let mut config = dsn.parse::<tokio_postgres::Config>()?;

    // `tokio_postgres` parses only the DSN string (it does not consult `PGHOST`
    // or the compiled-in default), so an empty host list means the DSN really
    // omitted the host. A DSN that gives a `hostaddr` instead of a `host` is
    // still connectable as-is, so leave it alone too — injecting a default host
    // there would just confuse TLS/SNI.
    if !config.get_hosts().is_empty() || !config.get_hostaddrs().is_empty() {
        return Ok(config);
    }

    // Resolve libpq's default host without connecting (see `libpq::default_host`).
    let host = libpq::default_host()?;
    config.host(&host);

    Ok(config)
}
