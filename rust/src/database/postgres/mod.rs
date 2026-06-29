//! [`tokio_postgres`]-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via the
//! homeserver's tokio runtime (see [`crate::runtime::RustRuntime`]).
//! [`connect`] takes the runtime's handle and stores it on the
//! [`connection::Connection`] it returns; the blocking helpers (see
//! [`helpers::BlockingPostgres`]) then drive every database call on that
//! runtime, whatever thread the Python caller is on.

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

/// Register the `postgres` submodule (the `Connection` / `Cursor` classes and
/// the `connect` factory) under the parent `database` module.
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

/// Open a new Postgres connection from a libpq-style DSN.
///
/// Blocks until the connection is established, then spawns the long-lived
/// connection task (which drives the socket) onto the shared runtime and
/// hands back a `Connection` wrapping the client.
///
/// `runtime` is the homeserver's [`RustRuntime`]; its tokio runtime is started
/// on first use, so this works during schema setup and in tests. The runtime's
/// handle is stored on the returned [`Connection`](connection::Connection),
/// which uses it to drive every subsequent call, whatever thread that call
/// comes from.
#[pyfunction]
fn connect<'py>(
    py: Python<'py>,
    runtime: &RustRuntime,
    dsn: &str,
) -> PyResult<Bound<'py, connection::Connection>> {
    let handle = runtime.tokio_handle()?;

    let config = dsn
        .parse::<tokio_postgres::Config>()
        .map_err(|e| PyRuntimeError::new_err(format!("Failed to parse DSN: {e}")))?;

    // TLS is not yet supported: unlike libpq (whose default is
    // `sslmode=prefer`), we never negotiate TLS regardless of the DSN's
    // sslmode. Supporting it is left to a follow-up.
    let (client, connection) = config
        .connect(tokio_postgres::NoTls)
        .block_on_pg_result(py, &handle)?;

    // Spawn the connection task on the shared runtime.
    handle.spawn(async move {
        if let Err(e) = connection.await {
            warn!("postgres connection error: {e}");
        }
    });

    let conn = connection::Connection::new(client, handle);

    Bound::new(py, conn)
}
