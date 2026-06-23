//! `tokio-postgres`-backed `Connection` / `Cursor` types exposed to Python.
//!
//! The driver itself is async; we drive it from sync Python methods via a
//! shared multi-thread tokio runtime (see `super::runtime`). The async helper
//! methods are kept `pub` so that future Rust callers can drive them
//! directly without going through the PyO3 wrappers.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

mod connection;
mod cursor_state;
mod helpers;
mod value;

fn pg_err_to_py(e: tokio_postgres::Error) -> PyErr {
    PyRuntimeError::new_err(format!("postgres error: {e}"))
}
