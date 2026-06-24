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

use std::future::Future;
use std::pin::pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use futures::future::BoxFuture;
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError},
    intern,
    prelude::*,
    types::{PyBool, PyCFunction, PyFloat, PyInt, PyList, PyString},
};

use crate::deferred::run_python_awaitable;
use crate::storage::db::{DatabasePool, DbValue, Row, Transaction};

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

impl DatabasePool for PythonDatabasePoolWrapper {
    async fn run_interaction<R, F>(&self, name: &'static str, func: F) -> anyhow::Result<R>
    where
        R: Send + 'static,
        F: for<'txn> Fn(&'txn mut dyn Transaction) -> BoxFuture<'txn, anyhow::Result<R>>
            + Send
            + Sync
            + 'static,
    {
        // `runInteraction` calls `func` with a `LoggingTransaction` on a DB
        // thread and expects a synchronous return value. Since we can't
        // round-trip an arbitrary Rust `R` back out through Python, the callback
        // stashes the result here and we pick it up once the deferred fires.
        //
        // Note the callback may run more than once (`runInteraction` retries on
        // serialization/deadlock errors), so we only trust this slot once the
        // deferred has fired, i.e. once the transaction has finally committed or
        // failed.
        let result_slot: Arc<Mutex<Option<anyhow::Result<R>>>> = Arc::new(Mutex::new(None));

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

                    // Since we expect people to only call `.await` on [`Transaction`]
                    // related methods (mentioned in the [`Transaction`] docstring) AND
                    // because there is no async work to suspend on in the Python
                    // [`Transaction`] synchronously, we can get away with polling once
                    // as it should immediately resolve to [`Poll::Ready`]. Getting
                    // [`Poll::Pending`] would be considered a programming error.
                    //
                    // Alternatively, we could just use `futures::executor::block_on`
                    // which is probably cleaner but a single-shot poll is more
                    // enforcing of the concept we want to represent.
                    match poll_once(func(&mut txn)) {
                        Poll::Ready(Ok(value)) => {
                            *callback_slot.lock().unwrap() = Some(Ok(value));
                            Ok(py.None())
                        }
                        Poll::Ready(Err(err)) => {
                            // Re-raise into Python so `runInteraction` rolls the
                            // transaction back (and can apply its retry logic for
                            // serialization/deadlock errors).
                            let py_err = anyhow_to_pyerr(&err);
                            *callback_slot.lock().unwrap() = Some(Err(err));
                            Err(py_err)
                        }
                        Poll::Pending => unreachable!(
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
        let outcome = run_python_awaitable(reactor, move |py| {
            database_pool_py
                .bind(py)
                .call_method1(intern!(py, "runInteraction"), (name, callback.bind(py)))
        })
        .await;

        // Prefer the result captured by the callback (it carries the Rust
        // context). If the slot is empty, `runInteraction` failed before ever
        // invoking the callback (e.g. it couldn't acquire a connection), so
        // surface the deferred's outcome instead.
        let captured = result_slot.lock().unwrap().take();
        match captured {
            Some(result) => result,
            None => match outcome {
                Ok(_) => Err(anyhow::anyhow!("run_interaction produced no result")),
                Err(py_err) => Err(anyhow::Error::from(py_err)),
            },
        }
    }
}

/// Poll a future exactly once.
///
/// Returns [`Poll::Ready`] if the future resolves immediately, or
/// [`Poll::Pending`] if it would need to suspend. We use this where a future is
/// expected to never genuinely suspend (e.g. Synapse's synchronous DB query
/// path) and want to enforce that, rather than driving it to completion with a
/// blocking executor like `futures::executor::block_on`.
fn poll_once<F: Future>(future: F) -> Poll<F::Output> {
    let mut future = pin!(future);
    let waker = futures::task::noop_waker();
    let mut cx = Context::from_waker(&waker);
    future.as_mut().poll(&mut cx)
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

            // Pull the rows back out, converting each cell from its Python type
            // into the engine-agnostic `DbValue` representation as we go.
            let rows_py = self
                .logging_transaction_py
                .bind(py)
                .call_method0(intern!(py, "fetchall"))?;

            let mut rows: Vec<Row> = Vec::new();
            for row_py in rows_py.try_iter()? {
                let row_py = row_py?;
                let mut row: Row = Vec::new();
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
