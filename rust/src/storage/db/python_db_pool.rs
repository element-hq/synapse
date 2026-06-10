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

//! We have these main classes:
//!  - Database pool [`PythonDatabasePoolWrapper`] which allows you to start a...
//!  - transaction [`LoggingTransactionWrapper`] and query the database

use std::sync::{Arc, Mutex};

use pyo3::{exceptions::PyRuntimeError, intern, prelude::*, types::PyCFunction, types::PyList};
use tokio::sync::oneshot;

use crate::storage::db::{AnyResult, DatabasePool, InteractionFn, Row, Transaction};

/// The database engines we support in the Python side of Synapse
#[derive(Copy, Clone, Debug)]
pub enum DatabaseEngine {
    Sqlite,
    Postgres,
}

impl DatabaseEngine {
    //[inline]
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
    async fn run_interaction_erased(&self, name: &'static str, func: InteractionFn) -> AnyResult {
        // We bridge the Python-side `runInteraction` (a coroutine, run on the
        // Twisted reactor + thread pool) back into our `async` Rust world using a
        // oneshot channel that resolves when the resulting deferred fires.
        let (tx, rx) = oneshot::channel::<AnyResult>();

        // `runInteraction` calls `func` with a `LoggingTransaction` on a DB
        // thread and expects a synchronous return value. Since we can't
        // round-trip an arbitrary Rust `R` back out through Python, the callback
        // stashes the result here and we pick it up once the deferred fires.
        let result_slot: Arc<Mutex<Option<AnyResult>>> = Arc::new(Mutex::new(None));

        Python::attach(|py| -> PyResult<()> {
            // (1) The callback that Python's `runInteraction` invokes on a DB
            // thread with a `LoggingTransaction`. We drive `func` to completion
            // here. The Python query path is synchronous under the hood, so it's
            // safe to block this dedicated DB thread until the future resolves.
            let callback_slot = Arc::clone(&result_slot);
            let callback = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    let py = args.py();
                    let txn_py = args.get_item(0)?;
                    let mut txn = txn_py.extract::<LoggingTransactionWrapper>()?;

                    let result = futures::executor::block_on(func(&mut txn));

                    match result {
                        Ok(value) => {
                            *callback_slot.lock().unwrap() = Some(Ok(value));
                            Ok(py.None())
                        }
                        Err(err) => {
                            // Re-raise into Python so `runInteraction` rolls the
                            // transaction back (and can apply its retry logic for
                            // serialization/deadlock errors).
                            let py_err = anyhow_to_pyerr(&err);
                            *callback_slot.lock().unwrap() = Some(Err(err));
                            Err(py_err)
                        }
                    }
                },
            )?
            .unbind();

            // The oneshot sender, shared between the success and error callbacks
            // (only one of which ever fires).
            let sender = Arc::new(Mutex::new(Some(tx)));

            // (2a) Fired when the transaction succeeds: hand the stashed result
            // back to the awaiting task.
            let success_slot = Arc::clone(&result_slot);
            let success_sender = Arc::clone(&sender);
            let on_success = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    let result = success_slot.lock().unwrap().take().unwrap_or_else(|| {
                        Err(anyhow::anyhow!("run_interaction produced no result"))
                    });
                    if let Some(tx) = success_sender.lock().unwrap().take() {
                        let _ = tx.send(result);
                    }
                    Ok(args.py().None())
                },
            )?
            .unbind();

            // (2b) Fired when the transaction fails. Prefer the original error
            // captured in the callback (it carries the Rust context); otherwise
            // fall back to the Twisted `Failure` text.
            let error_slot = Arc::clone(&result_slot);
            let error_sender = Arc::clone(&sender);
            let on_error = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    let result = error_slot.lock().unwrap().take().unwrap_or_else(|| {
                        let description = args
                            .get_item(0)
                            .and_then(|failure| failure.str())
                            .map(|s| s.to_string_lossy().into_owned())
                            .unwrap_or_else(|_| "<unknown failure>".to_owned());
                        Err(anyhow::anyhow!("run_interaction failed: {description}"))
                    });
                    if let Some(tx) = error_sender.lock().unwrap().take() {
                        let _ = tx.send(result);
                    }
                    Ok(args.py().None())
                },
            )?
            .unbind();

            // (3) Kick off `runInteraction` on the reactor thread. It's a
            // coroutine, so we wrap it with `ensureDeferred` and attach our
            // callbacks.
            let database_pool_py = self.database_pool_py.clone_ref(py);
            let starter = PyCFunction::new_closure(
                py,
                None,
                None,
                move |args, _kwargs| -> PyResult<Py<PyAny>> {
                    let py = args.py();
                    let coro = database_pool_py.bind(py).call_method1(
                        intern!(py, "runInteraction"),
                        (name, callback.bind(py)),
                    )?;
                    let deferred = py
                        .import("twisted.internet.defer")?
                        .call_method1(intern!(py, "ensureDeferred"), (coro,))?;
                    deferred.call_method1(
                        intern!(py, "addCallbacks"),
                        (on_success.bind(py), on_error.bind(py)),
                    )?;
                    Ok(py.None())
                },
            )?;

            self.reactor
                .bind(py)
                .call_method1(intern!(py, "callFromThread"), (starter,))?;

            Ok(())
        })
        .map_err(anyhow::Error::from)?;

        rx.await
            .map_err(|_| anyhow::anyhow!("run_interaction channel closed before completing"))?
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

fn detect_engine(txn_py: &Bound<'_, PyAny>) -> PyResult<DatabaseEngine> {
    let name = txn_py
        .getattr("database_engine")
        .expect("`LoggingTransaction` must have `database_engine` attr")
        .get_type()
        .name()
        .expect("`database_engine` type must have a name")
        .to_str()
        .expect("`database_engine` type name must be valid UTF-8")
        .to_owned();

    Ok(match name.as_str() {
        "PostgresEngine" => DatabaseEngine::Postgres,
        "Sqlite3Engine" => DatabaseEngine::Sqlite,
        other => unimplemented!(
            "Unknown database engine {other:?}. This is a Synapse programming error."
        ),
    })
}

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
pub struct LoggingTransactionWrapper {
    /// The underlying `LoggingTransaction`
    ///
    /// We purposely avoid `Bound<'py, PyAny>` so it can be stored and moved freely
    /// across threads (like to extract it from the `new_transaction(...)` callback).
    /// You will need to acquire your own `py` and bind it using
    /// `logging_transaction_py.bind(py)` to do anything useful.
    logging_transaction_py: Py<PyAny>,

    /// Disambiguate which underlying database engine we're working with
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
    async fn query(&mut self, sql: &str, args: &[&str]) -> Result<Vec<Row>, anyhow::Error> {
        Python::attach(|py| -> PyResult<Vec<Row>> {
            // Convert the Rust `&[&str]` of SQL parameters into a Python sequence
            // so it can be passed through to the Python-side `execute`. Note that
            // `LoggingTransaction.execute` converts `?` placeholders into the
            // appropriate param style for the underlying engine, so we pass
            // `?`-style SQL.
            let args = PyList::new(py, args)?;
            self.execute(py, sql, args.as_any())?;

            // Pull the rows back out. Each cell is converted to its textual
            // representation so we have a single engine-agnostic `Row` type;
            // callers parse the strings into richer types as needed.
            let rows_py = self
                .logging_transaction_py
                .bind(py)
                .call_method0(intern!(py, "fetchall"))?;

            let mut rows: Vec<Row> = Vec::new();
            for row_py in rows_py.try_iter()? {
                let row_py = row_py?;
                let mut row: Row = Vec::new();
                for cell in row_py.try_iter()? {
                    let cell = cell?;
                    row.push(cell.str()?.to_string_lossy().into_owned());
                }
                rows.push(row);
            }

            Ok(rows)
        })
        .map_err(anyhow::Error::from)
    }
}
