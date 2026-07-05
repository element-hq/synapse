//! [`tokio_postgres`]-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via a
//! shared multi-thread tokio runtime (see `super::runtime`).

use anyhow::{Context, Error};
use pyo3::prelude::*;
use pyo3::types::PyModule;

mod connection;
mod cursor_state;
mod errors;
mod helpers;
mod libpq;
pub mod pool;
pub(crate) mod query;
mod tls;
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

    // Port: libpq falls back to `PGPORT` (tokio_postgres itself defaults to
    // 5432 when none is set, matching libpq's compiled-in default). libpq
    // also accepts a comma-separated list, one port per host.
    if config.get_ports().is_empty() {
        if let Some(port) = env_nonempty("PGPORT") {
            for part in port.split(',') {
                let parsed = part.trim().parse::<u16>().with_context(|| {
                    format!("invalid port {part:?} in the PGPORT environment variable")
                })?;
                config.port(parsed);
            }
        }
    }

    // User: libpq falls back to `PGUSER`, then the OS user.
    if config.get_user().is_none() {
        if let Some(user) = env_nonempty("PGUSER").or_else(|| env_nonempty("USER")) {
            config.user(&user);
        }
    }

    // Password: libpq falls back to `PGPASSWORD`.
    if config.get_password().is_none() {
        if let Some(password) = env_nonempty("PGPASSWORD") {
            config.password(&password);
        }
    }

    // Database: libpq falls back to `PGDATABASE` (then the user name, which
    // the *server* also does when the startup packet names no database, so
    // that final fallback needs no help here).
    if config.get_dbname().is_none() {
        if let Some(dbname) = env_nonempty("PGDATABASE") {
            config.dbname(&dbname);
        }
    }

    Ok(config)
}

/// Read an environment variable, treating a set-but-empty value as unset —
/// libpq's behaviour, and what a blanked-out `PGPORT=` line in a compose file
/// or systemd unit means.
fn env_nonempty(var: &str) -> Option<String> {
    std::env::var(var).ok().filter(|value| !value.is_empty())
}
