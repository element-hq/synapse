//! [`tokio_postgres`]-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via a
//! shared multi-thread tokio runtime (see `super::runtime`).

use anyhow::Error;
use log::warn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

use crate::database::postgres::helpers::BlockingPostgresResult;
use crate::database::runtime::runtime;

mod connection;
mod cursor_state;
mod errors;
mod helpers;
mod libpq;
pub mod pool;
mod value;

/// Register the `postgres` submodule (the `Connection` / `Cursor` classes, the
/// DBAPI2 exception hierarchy and the `connect` factory) under the parent
/// `database` module.
pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "postgres")?;

    child.add_class::<connection::Connection>()?;
    child.add_class::<connection::Cursor>()?;
    child.add_function(wrap_pyfunction!(connect, &child)?)?;
    errors::register_exceptions(py, &child)?;

    m.add_submodule(&child)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust.database import postgres` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database.postgres", child)?;

    Ok(())
}

/// Open a new Postgres connection from a libpq-style DSN.
///
/// Blocks until the connection is established, then spawns the long-lived
/// connection task (which drives the socket) onto the shared runtime and
/// hands back a `Connection` wrapping the client.
#[pyfunction]
fn connect<'py>(py: Python<'py>, dsn: &str) -> PyResult<Bound<'py, connection::Connection>> {
    let config = fixup_default_host(dsn)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to prepare DSN: {e}")))?;

    // TLS is not yet supported: unlike libpq (whose default is
    // `sslmode=prefer`), we never negotiate TLS regardless of the DSN's
    // sslmode. Supporting it is left to a follow-up.
    let (client, connection) = config.connect(tokio_postgres::NoTls).block_on_result(py)?;

    // Spawn the connection task on the runtime.
    runtime().spawn(async move {
        if let Err(e) = connection.await {
            warn!("postgres connection error: {e}");
        }
    });

    let conn = connection::Connection::new(client);

    Bound::new(py, conn)
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
