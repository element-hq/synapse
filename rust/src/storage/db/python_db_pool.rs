/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 */

//! A database pool that calls into Python to re-use the same database pool used on the
//! Python side. This is desirable because we want to avoid having two separate database
//! pools (one for Rust, one for Python) to avoid database connection exhaustion
//! problems. This is a stepping stone until all of our database interactions are in
//! Rust.
//!
//! We have these main classes:
//!  - Database pool [`PythonDatabasePoolWrapper`] (implements [`DatabasePool`]) which
//!    allows you to start a...
//!  - transaction [`LoggingTransactionWrapper`] (implements [`Transaction`]) and query
//!    the database.

use std::sync::{Arc, Mutex};

use anyhow::Context;
use futures::FutureExt;
use once_cell::sync::OnceCell;
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError},
    intern,
    prelude::*,
    types::{PyBool, PyCFunction, PyFloat, PyInt, PyList, PyString},
};

use crate::deferred::run_python_awaitable;
use crate::storage::db::{
    DatabasePool, DbRow, DbValue, ErasedInteraction, ErasedResult, Transaction,
};

/// A reference to the `synapse.storage.engines` module.
static STORAGE_ENGINES_MODULE: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `synapse.storage.engines` module.
fn storage_engines_module(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(STORAGE_ENGINES_MODULE
        .get_or_try_init(|| py.import("synapse.storage.engines").map(Into::into))?
        .bind(py))
}

static SQLITE3_ENGINE_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();
static POSTGRES_ENGINE_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `Sqlite3Engine` class
fn sqlite3_engine_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(SQLITE3_ENGINE_CLASS
        .get_or_try_init(|| {
            storage_engines_module(py)?
                .getattr("Sqlite3Engine")
                .map(Into::into)
        })?
        .bind(py))
}

/// Access to the `PostgresEngine` class
fn postgres_engine_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(POSTGRES_ENGINE_CLASS
        .get_or_try_init(|| {
            storage_engines_module(py)?
                .getattr("PostgresEngine")
                .map(Into::into)
        })?
        .bind(py))
}

/// The database engines we support in the Python side of Synapse
#[derive(Copy, Clone, Debug)]
pub enum DatabaseEngine {
    Sqlite,
    Postgres,
}

impl DatabaseEngine {
    pub fn supports_using_any_list(&self) -> bool {
        match self {
            DatabaseEngine::Sqlite => false,
            DatabaseEngine::Postgres => true,
        }
    }
}

/// Wrapper for a `DatabasePool` from the Python side of Synapse.
pub struct PythonDatabasePoolWrapper {
    /// The underlying Python `DatabasePool`
    database_pool_py: Py<PyAny>,

    /// The Twisted reactor. We need this to marshal back onto the reactor thread
    /// (via `callFromThread`) when starting transactions, since Twisted's thread
    /// pool machinery must be driven from there.
    reactor: Py<PyAny>,
}

impl PythonDatabasePoolWrapper {
    /// Build a wrapper around the Python `DatabasePool` (e.g.
    /// `hs.get_datastores().main.db_pool`) and the Twisted `reactor`.
    pub fn new(database_pool_py: Py<PyAny>, reactor: Py<PyAny>) -> Self {
        Self {
            database_pool_py,
            reactor,
        }
    }
}

#[async_trait::async_trait]
impl DatabasePool for PythonDatabasePoolWrapper {
    async fn run_interaction_erased(
        &self,
        name: &'static str,
        func: ErasedInteraction,
    ) -> ErasedResult {
        // `runInteraction` calls `func` with a `LoggingTransaction` on a DB thread and
        // expects a synchronous return value. Since we can't round-trip an arbitrary
        // Rust result back out through Python (remember, `func` returns an
        // `ErasedResult`, not a `PyAny`), the callback stashes the result here and we
        // pick it up once the deferred fires.
        //
        // Note the callback may run more than once (`runInteraction` retries on
        // serialization/deadlock errors), so we only trust this slot once the
        // deferred has fired, i.e. once the transaction has finally committed or
        // failed.
        let result_slot: Arc<Mutex<Option<ErasedResult>>> = Arc::new(Mutex::new(None));

        // Build the callback that Python's `runInteraction` invokes on a DB
        // thread with a `LoggingTransaction`, plus owned handles we can move onto
        // the reactor thread. We drive `func` to completion in the callback; the
        // Python query path is synchronous under the hood, so it's safe to block
        // this dedicated DB thread until the future resolves.
        let callback_slot = Arc::clone(&result_slot);
        let (callback, database_pool_py, reactor) = Python::attach(|py| -> PyResult<_> {
                let callback = PyCFunction::new_closure(
                    py,
                    None,
                    None,
                    move |args, _kwargs| -> PyResult<Py<PyAny>> {
                        let py = args.py();
                        let txn_py = args.get_item(0)?;
                        let mut txn = txn_py.extract::<LoggingTransactionWrapper>()?;

                        // Since we expect people to only call `.await` on
                        // [`Transaction`] related methods (mentioned in the
                        // [`Transaction`] docstring) AND because there is no actual
                        // async work to suspend on in the Python [`Transaction`]
                        // (resolves synchonously), we can get away with polling once as
                        // it should immediately resolve to [`Poll::Ready`]. Getting
                        // [`Poll::Pending`] would be considered a programming error.
                        //
                        // Alternatively, we could just use `futures::executor::block_on`
                        // which is probably cleaner but a single-shot poll is more
                        // enforcing of the concept we want to represent.
                        match func(&mut txn).now_or_never() {
                            Some(Ok(value)) => {
                                let mut callback_slot =  callback_slot
                                    .lock()
                                    .map_err(|err| anyhow::anyhow!("Failed to acquire lock on `callback_slot`: {:#}", err))?;
                                *callback_slot = Some(Ok(value));
                                Ok(py.None())
                            }
                            Some(Err(err)) => {
                                // Re-raise into Python so `runInteraction` rolls the
                                // transaction back (and can apply its retry logic for
                                // serialization/deadlock errors).
                                let py_err = anyhow_to_pyerr(&err);
                                let mut callback_slot =  callback_slot
                                    .lock()
                                    .map_err(|err| anyhow::anyhow!("Failed to acquire lock on `callback_slot`: {:#}", err))?;
                                *callback_slot = Some(Err(err));
                                Err(py_err)
                            }
                            None => unreachable!(
                                "The `run_interaction` transaction callback future returned `Poll::Pending`, \
                                but we expect Synapse Python database work to resolve synchronously. \
                                This is a Synapse programming error: genuine async work is \
                                not supported here.",
                            ),
                        }
                    },
                )?
                .unbind();

                Ok((
                    callback,
                    self.database_pool_py.clone_ref(py),
                    self.reactor.clone_ref(py),
                ))
            })
            .map_err(anyhow::Error::from)?;

        // Use `runInteraction` directly
        let run_interaction_outcome = run_python_awaitable(reactor, move |py| {
            database_pool_py
                .bind(py)
                .call_method1(intern!(py, "runInteraction"), (name, callback.bind(py)))
        })
        .await;

        // Return the result we captured based on if `runInteraction` was successful
        let captured_result = result_slot
            .lock()
            .map_err(|err| anyhow::anyhow!("Failed to acquire lock on `result_slot`: {:#}", err))?
            .take();
        match run_interaction_outcome {
            // Only return the `captured_result` if `runInteraction` succeeded. We don't
            // want to accidentally return a successful result when the transaction
            // actually failed to commit.
            Ok(_) => match captured_result {
                Some(result) => result,
                // This is unexpected as we either expect `runInteraction` to have
                // completed successfully and run the provided `callback` which runs the
                // `func` and we capture a result or it fails.
                None => Err(anyhow::anyhow!(
                    "Expected to capture result after running `runInteraction` and seeing it succeed (but saw nothing). \
                    This is a Synapse programming error."
                )),
            },
            Err(py_err) => Err(anyhow::Error::from(py_err)).with_context(|| format!("run_interaction(name={}) failed", name)),
        }
    }
}

/// Convert an [`anyhow::Error`] into a [`PyErr`] to re-raise into Python.
///
/// If the error wraps an original Python exception (e.g. a database error
/// surfaced through [`Transaction::query`]), we re-raise *that* exception so
/// Synapse's transaction machinery can apply its retry logic
/// (serialization/deadlock detection) on the real error.
fn anyhow_to_pyerr(err: &anyhow::Error) -> PyErr {
    if let Some(py_err) = err.downcast_ref::<PyErr>() {
        return Python::attach(|py| py_err.clone_ref(py));
    }
    PyRuntimeError::new_err(format!("{err:#}"))
}

/// Given a Python `LoggingTransaction`, figures out the database engine that backs it
fn detect_engine(txn_py: &Bound<'_, PyAny>) -> PyResult<DatabaseEngine> {
    let py = txn_py.py();
    let database_engine = txn_py.getattr(intern!(py, "database_engine"))?;

    // Compare against the actual engine classes imported from Python (the PyO3
    // equivalent of an `isinstance` check).
    if database_engine.is_instance(postgres_engine_class(py)?)? {
        Ok(DatabaseEngine::Postgres)
    } else if database_engine.is_instance(sqlite3_engine_class(py)?)? {
        Ok(DatabaseEngine::Sqlite)
    } else {
        Err(PyTypeError::new_err(format!(
            "Unknown database engine {}. This is a Synapse programming error.",
            database_engine.get_type().name()?
        )))
    }
}

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
pub struct LoggingTransactionWrapper {
    /// The underlying `LoggingTransaction`
    ///
    /// We purposely avoid `Bound<'py, PyAny>` so it can be stored and moved freely
    /// across threads (as required by `Transaction` trait).
    logging_transaction_py: Py<PyAny>,

    /// Disambiguate which underlying database engine we're working with
    ///
    /// Some features are only available on Postgres vs SQLite and the queries need to
    /// be differentiated (for compatibility or performance reasons).
    pub database_engine: DatabaseEngine,
}

impl<'a, 'py> FromPyObject<'a, 'py> for LoggingTransactionWrapper {
    type Error = PyErr;

    /// Extract from a Python `LoggingTransaction` passed as an argument.
    fn extract(logging_transaction_py: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        let database_engine = detect_engine(&logging_transaction_py.to_owned())?;
        Ok(Self {
            logging_transaction_py: logging_transaction_py.to_owned().unbind(),
            database_engine,
        })
    }
}

impl LoggingTransactionWrapper {
    /// Calls the Python `LoggingTransaction.execute` function.
    fn execute<'py>(
        &mut self,
        py: Python<'py>,
        sql: &str,
        args: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let execute_fn = self
            .logging_transaction_py
            .bind(py)
            .getattr(intern!(py, "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }
}

#[async_trait::async_trait]
impl Transaction for LoggingTransactionWrapper {
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<DbRow>, anyhow::Error> {
        Python::attach(|py| -> PyResult<Vec<DbRow>> {
            // Convert the Rust `&[&str]` of SQL parameters into a Python sequence
            // so it can be passed through to the Python-side `execute`.
            //
            // We don't need to do anything to the SQL as the `?`-style arg placeholders
            // already align with what `LoggingTransaction` expects.
            let args = PyList::new(py, args)?;
            self.execute(py, sql, args.as_any())?;

            // Pull the rows back out, converting each cell from its Python type
            // into the engine-agnostic `DbValue` representation as we go.
            let rows_py = self
                .logging_transaction_py
                .bind(py)
                .call_method0(intern!(py, "fetchall"))?;

            let mut rows: Vec<DbRow> = Vec::new();
            for row_py in rows_py.try_iter()? {
                let row_py = row_py?;
                let mut row: DbRow = Vec::new();
                for cell in row_py.try_iter()? {
                    row.push(py_cell_to_value(&cell?)?);
                }
                rows.push(row);
            }

            Ok(rows)
        })
        .map_err(anyhow::Error::from)
    }
}

/// Convert a single cell from a Python row into a backend-agnostic [`DbValue`] by
/// inspecting its Python type (the pyo3 equivalent of `isinstance` checks).
fn py_cell_to_value(cell: &Bound<'_, PyAny>) -> PyResult<DbValue> {
    // `None` maps to SQL `NULL`.
    if cell.is_none() {
        return Ok(DbValue::Null);
    }

    // A `bool` *is* an `int` in SQLite, so ensure we try `bool` first.
    if let Ok(b) = cell.cast::<PyBool>() {
        Ok(DbValue::Bool(b.extract()?))
    } else if let Ok(i) = cell.cast::<PyInt>() {
        Ok(DbValue::Int(i.extract()?))
    } else if let Ok(f) = cell.cast::<PyFloat>() {
        Ok(DbValue::Float(f.extract()?))
    } else if let Ok(s) = cell.cast::<PyString>() {
        Ok(DbValue::Text(s.to_string()))
    } else {
        Err(PyTypeError::new_err(format!(
            "unsupported column type {} returned from the database",
            cell.get_type().name()?
        )))
    }
}
