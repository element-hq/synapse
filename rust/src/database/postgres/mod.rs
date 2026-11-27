//! [`tokio_postgres`]-backed Postgres backend for the Rust `database` module.
//!
//! The driver is async. [`helpers`] drives its futures to completion from
//! synchronous Python code on the shared tokio runtime, and [`value`] converts
//! between Python objects and Postgres' binary wire format. [`connection`]
//! holds the Python-facing `Connection` and `Cursor`.

use log::warn;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

use crate::database::postgres::helpers::BlockingPostgresResult;
use crate::runtime::RustRuntime;

mod connection;
mod cursor_state;
mod helpers;
mod value;

/// Register the `postgres` submodule under the parent `database` module.
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

/// Map a [`tokio_postgres`] error into a Python `RuntimeError`.
fn pg_err_to_py(e: tokio_postgres::Error) -> PyErr {
    PyRuntimeError::new_err(format!("postgres error: {e}"))
}

/// Open a new Postgres connection from a libpq-style DSN, blocking until it is
/// established. The connection task, and every later call on the connection,
/// runs on `runtime`.
#[pyfunction]
fn connect(py: Python<'_>, runtime: &RustRuntime, dsn: &str) -> PyResult<connection::Connection> {
    let handle = runtime.tokio_handle()?;

    let config = dsn
        .parse::<tokio_postgres::Config>()
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to parse DSN: {e}")))?;

    // Unlike libpq, tokio-postgres has no default host, and its own error for
    // a missing one only says "invalid configuration".
    if config.get_hosts().is_empty() && config.get_hostaddrs().is_empty() {
        return Err(PyRuntimeError::new_err(
            "No database host is configured. Set `host` in the `database.args` \
             section of the config, for example `/var/run/postgresql` for a local \
             Unix socket, or `localhost`.",
        ));
    }

    // TODO: TLS. With `NoTls`, `sslmode=prefer` (the default) connects in
    // plaintext and `sslmode=require` fails to connect. tokio-postgres doesn't
    // accept `verify-ca` or `verify-full`, so those fail to parse.
    let (client, connection) = config
        .connect(tokio_postgres::NoTls)
        .block_on_pg_result(py, &handle)?;

    handle.spawn(async move {
        if let Err(e) = connection.await {
            warn!("postgres connection error: {e}");
        }
    });

    Ok(connection::Connection::new(client, handle))
}
