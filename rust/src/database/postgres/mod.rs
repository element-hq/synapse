//! `tokio-postgres`-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via a
//! shared multi-thread tokio runtime (see `super::runtime`). The async helper
//! methods are kept `pub` so that future Rust callers can drive them
//! directly without going through the PyO3 wrappers.

use anyhow::Error;
use log::warn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

use crate::database::postgres::helpers::BlockingPostgresResult;
use crate::database::runtime::runtime;

mod connection;
mod cursor_state;
mod helpers;
mod value;

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child = PyModule::new(py, "postgres")?;

    child.add_class::<connection::Connection>()?;
    child.add_class::<connection::Cursor>()?;
    child.add_function(wrap_pyfunction!(connect, &child)?)?;

    m.add_submodule(&child)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust.database import postgres` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.database.postgres", child)?;

    Ok(())
}

fn pg_err_to_py(e: tokio_postgres::Error) -> PyErr {
    PyRuntimeError::new_err(format!("postgres error: {e}"))
}

#[pyfunction]
fn connect<'py>(py: Python<'py>, dsn: &str) -> PyResult<Bound<'py, connection::Connection>> {
    let config = fixup_default_host(dsn)
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to parse DSN: {e}")))?;

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
/// [`tokio-postgres`] has a different default host than [`libpq`], which is
/// what Synapse previously used (and is what e.g. `psql` uses). The default
/// host in `libpq` is configurable, and so we need to pull it out and runtime.
fn fixup_default_host(dsn: &str) -> Result<tokio_postgres::Config, Error> {
    let mut config = dsn.parse::<tokio_postgres::Config>()?;

    if !config.get_hosts().is_empty() {
        return Ok(config);
    }

    // Resolve libpq's default host without connecting: PQconnectStart parses
    // the (empty) conninfo and applies libpq's defaults/env, but opens no
    // socket because we never poll it. Closed on Drop.
    let pq_conn = libpq::Connection::start("")?;
    let host = pq_conn.host()?;

    config.host(host);

    Ok(config)
}
