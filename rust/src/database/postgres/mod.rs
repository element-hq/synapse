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
/// necessary, and fill in libpq's environment defaults for the fields
/// [`tokio_postgres`] doesn't read from the environment.
///
/// [`tokio_postgres`] parses only the DSN string: unlike libpq (which is what
/// Synapse previously used, and what e.g. `psql` uses) it does not consult
/// `PGHOST` / `PGUSER` / `PGPASSWORD` or the compiled-in defaults. So when the
/// DSN omits one of these we fill it the way libpq would, so a config that
/// relied on those environment variables (as Synapse's test setup does)
/// connects the same way it did under psycopg2.
pub(crate) fn fixup_config_defaults(dsn: &str) -> Result<tokio_postgres::Config, Error> {
    let mut config = dsn.parse::<tokio_postgres::Config>()?;

    // Host. A DSN that gives a `hostaddr` instead of a `host` is still
    // connectable as-is, so leave it alone too — injecting a default host there
    // would just confuse TLS/SNI. Otherwise resolve libpq's default host
    // without connecting (see `libpq::default_host`).
    if config.get_hosts().is_empty() && config.get_hostaddrs().is_empty() {
        config.host(&libpq::default_host()?);
    }

    // User: libpq falls back to `PGUSER`, then the OS user.
    if config.get_user().is_none() {
        if let Some(user) = std::env::var("PGUSER")
            .ok()
            .or_else(|| std::env::var("USER").ok())
        {
            config.user(&user);
        }
    }

    // Password: libpq falls back to `PGPASSWORD`.
    if config.get_password().is_none() {
        if let Ok(password) = std::env::var("PGPASSWORD") {
            config.password(&password);
        }
    }

    Ok(config)
}
