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

//! The Python-facing [`Connection`] / [`Cursor`] pair for the Postgres backend.
//!
//! These cover the parts of PEP-249 (DBAPI2) that
//! `DatabasePool.new_transaction` uses. It opens a cursor, runs the interaction
//! function on it, then commits or rolls back on the connection. Every call
//! blocks on the shared tokio runtime (see [`super::helpers`]).
//!
//! ## Transactions
//!
//! Like psycopg2, a connection is transactional by default: the first statement
//! issues a `BEGIN`, unless the connection is in autocommit mode.
//!
//! The connection tracks when a statement, or reading its result set, fails
//! inside a transaction. It then raises on every further `execute` until
//! `rollback` (or `commit`) ends the transaction.
//!
//! If `COMMIT` or `ROLLBACK` fails with a server error, such as a serialization
//! failure, Postgres has ended the transaction and the connection stays usable.
//! After any other failure the session state is unknown, so the connection
//! drops its client.
//!
//! ## One cursor at a time
//!
//! We only support having one active cursor per connection at a time, and only
//! one should be used per transaction.
//!
//! When [`Cursor::execute`] is called, it will drop any existing results for
//! any previous statement on the connection (whether or not on the same
//! cursor). If Postgres reports any errors while dropping the previous result
//! set, those errors are raised immediately (so that they are not lost). This
//! does mean that errors from previous statements can surface during the
//! execution of a new statement. (Generally this is not an issue as Synapse
//! will read the result set before executing a new statement).

use std::sync::{Arc, Mutex, MutexGuard, TryLockError};

use futures::StreamExt;
use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyInt, PyTuple},
};
use tokio::runtime::Handle;
use tokio_postgres::{error::Severity, Client};

use crate::database::postgres::{
    cursor_state::CursorQueryState,
    helpers::{BlockingPostgres, BlockingPostgresResult},
    pg_err_to_py,
    value::PgValue,
};

/// A single Postgres connection exposed to Python. Cursors hold a clone.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Connection {
    inner: Arc<Mutex<ConnInner>>,
    /// The runtime that drives every call on this connection and its cursors.
    handle: Handle,
}

/// The mutable state of a [`Connection`], behind its mutex.
struct ConnInner {
    /// `None` once the connection has been closed, either by `close` or after
    /// a failure that leaves the session state unknown.
    client: Option<Client>,
    /// Whether a `BEGIN` has been issued and not yet matched by a `COMMIT` or
    /// `ROLLBACK`.
    in_txn: bool,
    /// Whether a statement, or reading its result set, has failed since the
    /// last `BEGIN`. While a transaction is open, `execute` then raises until
    /// the transaction is closed
    aborted: bool,
    /// If false, runs statements outside of transactions.
    autocommit: bool,
    /// The result set of the most recent statement.
    state: CursorQueryState,
    /// The id of the cursor whose statement produced `state`, if any.
    owner: Option<u64>,
    /// The id the next cursor gets.
    next_cursor_id: u64,
}

impl ConnInner {
    fn new(client: Option<Client>) -> Self {
        Self {
            client,
            in_txn: false,
            aborted: false,
            autocommit: false,
            state: CursorQueryState::default(),
            owner: None,
            next_cursor_id: 0,
        }
    }

    /// The live client, or an error if the connection has been closed.
    fn client(&self) -> PyResult<&Client> {
        self.client
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("connection already closed"))
    }

    /// Drop the client, closing the socket, and the result set with it.
    fn close(&mut self) {
        self.client = None;
        self.in_txn = false;
        self.state = CursorQueryState::default();
        self.owner = None;
    }

    /// Run `f` on the result set, setting `aborted` if `f` fails while a row
    /// stream is active.
    ///
    /// `cursor_state` drops an active stream on any error. Either the
    /// statement failed on the server, or its remaining rows went unread along
    /// with any error among them. In both cases the transaction isn't safe to
    /// commit.
    fn on_state<R>(&mut self, f: impl FnOnce(&mut CursorQueryState) -> PyResult<R>) -> PyResult<R> {
        let was_active = matches!(self.state, CursorQueryState::Active { .. });
        let result = f(&mut self.state);
        if result.is_err() && was_active {
            self.aborted = true;
        }
        result
    }

    /// Convert an error from preparing or starting a statement, setting
    /// `aborted` if the server reported it. A client-side error, such as a
    /// parameter that fails to encode, happens before anything is sent, so it
    /// leaves the transaction intact.
    fn statement_err(&mut self, err: tokio_postgres::Error) -> PyErr {
        if err.as_db_error().is_some() {
            self.aborted = true;
        }
        pg_err_to_py(err)
    }
}

impl Connection {
    /// Wrap a client whose connection task runs on `handle`'s runtime.
    pub fn new(client: Client, handle: Handle) -> Self {
        Self {
            inner: Arc::new(Mutex::new(ConnInner::new(Some(client)))),
            handle,
        }
    }

    /// Lock the connection's state.
    ///
    /// Synapse hands a connection to one thread at a time, so contention is a
    /// caller bug, and we error at once. After a panic poisoned the
    /// mutex, the session state is unknown, so we close the connection and
    /// clear the poison. Later calls then get "connection already closed".
    fn lock(&self) -> PyResult<MutexGuard<'_, ConnInner>> {
        match self.inner.try_lock() {
            Ok(guard) => Ok(guard),
            Err(TryLockError::Poisoned(poisoned)) => {
                poisoned.into_inner().close();
                self.inner.clear_poison();
                Err(PyRuntimeError::new_err("connection mutex poisoned"))
            }
            Err(TryLockError::WouldBlock) => Err(PyRuntimeError::new_err(
                "connection is being used in another thread and cannot be used concurrently",
            )),
        }
    }

    /// End the current transaction, if one is open.
    ///
    /// A `commit` of an aborted transaction, or one whose final drain fails,
    /// rolls back and raises instead.
    fn end_txn(&self, py: Python<'_>, commit: bool) -> PyResult<()> {
        let mut inner = self.lock()?;

        // Clear any current owning cursor as we invalidate the result set.
        inner.owner = None;

        // Always close the cursor, regardless of transaction state.
        let drained = inner.state.close(py);

        // If there is no active transaction, we can return early.
        if !inner.in_txn {
            // Report any errors from closing the cursor if we're committing.
            // This ensures the caller sees any pending errors rather than
            // thinking the queries succeeded.
            if commit {
                drained?;
            }
            return Ok(());
        }

        let (stmt, our_err) = match (commit, drained) {
            // Rollback if the caller requested it, the transaction was aborted,
            // or the cursor failed to close.
            (false, _) => ("ROLLBACK", None),
            (true, _) if inner.aborted => (
                "ROLLBACK",
                Some(PyRuntimeError::new_err("the transaction was aborted")),
            ),
            (true, Err(err)) => ("ROLLBACK", Some(err)),

            // Only commit if requested, the transaction was not aborted, and
            // the cursor closed successfully.
            (true, Ok(_)) => ("COMMIT", None),
        };

        // Use `batch_execute` to run the statement. It uses fewer round trips
        // than `execute`, and immediately returns any errors without requiring
        // reading the result stream.
        let result = inner
            .client()?
            .batch_execute(stmt)
            .block_on(py, &self.handle);

        match result {
            Ok(()) => {
                inner.in_txn = false;
                our_err.map_or(Ok(()), Err)
            }
            Err(err) => {
                // A server error, such as a serialization failure or a
                // deferred constraint violation, ends the transaction and
                // leaves the session idle. A fatal one closes the session.
                let session_ok = err
                    .as_db_error()
                    .is_some_and(|e| e.parsed_severity() == Some(Severity::Error));
                if session_ok {
                    inner.in_txn = false;
                } else {
                    // Close the connection as the connection is in an unknown
                    // state.
                    inner.close();
                }
                // Report the error that turned this into a `ROLLBACK`. A
                // failed drain, for example from a closed socket, is often why
                // the `ROLLBACK` failed too.
                Err(our_err.unwrap_or_else(|| pg_err_to_py(err)))
            }
        }
    }
}

#[pymethods]
impl Connection {
    /// Open a new cursor over this connection. No I/O happens here.
    fn cursor(&self) -> PyResult<Cursor> {
        let mut inner = self.lock()?;
        let id = inner.next_cursor_id;
        inner.next_cursor_id += 1;
        Ok(Cursor {
            connection: self.clone(),
            id,
        })
    }

    /// Commit the current transaction, if one is open. A no-op otherwise.
    fn commit(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, true)
    }

    /// Roll back the current transaction, if one is open. A no-op otherwise.
    fn rollback(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, false)
    }

    /// Close the connection. The server rolls back any open transaction.
    /// Closing an already closed connection is fine.
    fn close(&self) -> PyResult<()> {
        self.lock()?.close();
        Ok(())
    }

    /// Switch autocommit mode on or off, like psycopg2's
    /// `set_session(autocommit=...)`. Errors inside a transaction.
    fn set_autocommit(&self, autocommit: bool) -> PyResult<()> {
        let mut inner = self.lock()?;
        if inner.in_txn {
            return Err(PyRuntimeError::new_err(
                "cannot change autocommit mode while a transaction is in progress",
            ));
        }
        inner.autocommit = autocommit;
        Ok(())
    }

    /// Context-manager entry: returns the connection itself.
    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Context-manager exit: commit, or roll back if the block raised. Like
    /// psycopg2, this leaves the connection open and doesn't suppress the
    /// exception.
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Bound<'_, PyAny>>,
        _exc_value: Option<Bound<'_, PyAny>>,
        _traceback: Option<Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        self.end_txn(py, exc_type.is_none())?;
        Ok(false)
    }
}

/// A PEP-249-style cursor.
#[pyclass(frozen, skip_from_py_object)]
pub struct Cursor {
    connection: Connection,
    id: u64,
}

impl Cursor {
    /// Run `f` on this cursor's result set, with the connection locked.
    ///
    /// Errors if the current result set isn't this cursor's.
    fn with_state<R>(&self, f: impl FnOnce(&mut CursorQueryState) -> PyResult<R>) -> PyResult<R> {
        let mut inner = self.connection.lock()?;
        if inner.owner != Some(self.id) {
            // A closed connection has no owner, so report that first.
            inner.client()?;
            return Err(PyRuntimeError::new_err(
                "this cursor has no result set: it hasn't executed, its last execute \
                 failed, or another cursor's execute has replaced it",
            ));
        }
        inner.on_state(f)
    }
}

#[pymethods]
impl Cursor {
    /// Execute `query`, with optional positional `params` bound to `$1`, `$2`,
    /// ... placeholders.
    #[pyo3(signature = (query, params = None))]
    fn execute(&self, py: Python<'_>, query: &str, params: Option<Vec<PgValue>>) -> PyResult<()> {
        let handle = &self.connection.handle;
        let mut inner = self.connection.lock()?;

        // Clear any current owning cursor as we invalidate the result set.
        inner.owner = None;

        // Drain the previous result set. This may raise an exception if the
        // previous query failed and the caller dropped the result set without
        // reading it.
        inner.on_state(|state| state.close(py))?;

        // If the transaction is aborted, no further statements will
        // successfully execute until the transaction is rolled back.
        if inner.in_txn && inner.aborted {
            return Err(PyRuntimeError::new_err("the transaction was aborted"));
        }

        // Start a transaction if necessary.
        if !inner.autocommit && !inner.in_txn {
            inner
                .client()?
                .batch_execute("BEGIN")
                .block_on_pg_result(py, handle)?;
            inner.in_txn = true;
            inner.aborted = false;
        }

        // Prepare the statement for execution.
        let statement = inner
            .client()?
            .prepare(query)
            .block_on(py, handle)
            .map_err(|err| inner.statement_err(err))?;

        // Execute the statement with the provided parameters.
        let stream = inner
            .client()?
            .query_raw(&statement, params.unwrap_or_default())
            .block_on(py, handle)
            .map_err(|err| inner.statement_err(err))?;

        // The column names are kept for PEP-249's `Cursor.description`.
        let description = statement
            .columns()
            .iter()
            .map(|c| c.name().to_string())
            .collect::<Vec<_>>();
        let returns_rows = !description.is_empty();

        inner
            .state
            .on_query_start(Box::pin(stream.fuse()), description, handle.clone())?;

        // A statement with no result columns has no rows to fetch (e.g. an
        // INSERT). We close the state immediately so that any errors from the
        // statement are raised now.
        if !returns_rows {
            inner.on_state(|state| state.close(py))?;
        }

        // The result set is ready to be read by this cursor.
        inner.owner = Some(self.id);

        Ok(())
    }

    /// Return the next row of the current result set, or `None` if exhausted.
    fn fetch_one<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.with_state(|state| state.fetch_one(py))
    }

    /// Return all remaining rows of the current result set.
    fn fetch_all<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.with_state(|state| state.fetch_all(py))
    }

    /// Fetch the next batch of rows.
    ///
    /// Blocks until the first row is available, then adds any further rows that
    /// are already buffered without blocking again. `capacity` sizes the
    /// returned buffer and does not cap the batch. This lets Python iterate a
    /// large result set without a call per row ([`Self::fetch_one`]) and
    /// without waiting for the whole result set ([`Self::fetch_all`]).
    ///
    /// An empty batch indicates that the result set is exhausted.
    #[pyo3(signature = (capacity = 100))]
    fn fetch_next_batch<'py>(
        &self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.with_state(|state| state.fetch_next_batch(py, capacity))
    }

    /// Return the affected-row count.
    ///
    /// This closes an active cursor, discarding any remaining rows. This means
    /// that the caller cannot call the `fetch_*` methods afterwards, and must
    /// only call `rowcount` after fetching any needed rows.
    #[getter]
    fn rowcount<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        self.with_state(|state| state.rowcount(py))
    }

    /// Close the cursor, draining its result set and raising any error the
    /// statement hit. A cursor with no result set closes as a no-op.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let mut inner = self.connection.lock()?;
        if inner.owner == Some(self.id) {
            inner.on_state(|state| state.close(py))?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A poisoned connection is closed and errors once, then reports itself
    /// as closed.
    #[test]
    fn poisoned_connection_is_closed_and_cleared() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let conn = Connection {
            inner: Arc::new(Mutex::new(ConnInner::new(None))),
            handle: runtime.handle().clone(),
        };
        conn.inner.lock().unwrap().in_txn = true;
        let inner = conn.inner.clone();
        let _ = std::panic::catch_unwind(move || {
            let _guard = inner.lock().unwrap();
            panic!("poison the mutex");
        });
        assert!(conn.inner.is_poisoned());

        Python::initialize();
        Python::attach(|_py| {
            let err = conn.lock().err().expect("a poisoned lock errors");
            assert!(err.to_string().contains("mutex poisoned"), "{err}");

            let inner = conn.lock().expect("the poison is cleared");
            assert!(!inner.in_txn);
            let err = inner.client().unwrap_err();
            assert!(err.to_string().contains("already closed"), "{err}");
        });
    }
}
