//! The Python-facing [`Connection`] / [`Cursor`] pair for the Postgres backend.
//!
//! These implement enough of the PEP-249 (DBAPI2) shape for Synapse's needs: a
//! [`Connection`] owns a single [`tokio_postgres::Client`], and each
//! `run_interaction` hands the interaction function a [`Cursor`] wrapped in an
//! explicit transaction (`BEGIN` ... `COMMIT`/`ROLLBACK`).
//!
//! The driver is async but the Python API is sync, so every database call is
//! driven to completion on the shared tokio runtime via the `block_on` helpers
//! (see [`super::helpers`]).
//!
//! ## Ownership of the `Client`
//!
//! There is exactly one `Client` per connection, and it *moves* between the
//! `Connection` and the `Cursor` rather than being shared. It lives in the
//! `Connection` between interactions, is *taken out* for the duration of a
//! `Cursor`, and is *put back* when the cursor finishes. An `Option` on each
//! side (the `Connection`'s client slot, the `Cursor`'s state) tracks where it
//! currently is.
//!
//! This passing-by-move is deliberate: it means the `Client` can only ever be
//! in one place at a time, so we structurally cannot
//!
//!   * use the `Connection` while a transaction is in flight (its slot is empty
//!     until the cursor hands the client back), nor
//!   * use the `Cursor` once the transaction has finished (its slot is empty
//!     once it has handed the client back).
//!
//! Both of those become a clean "already closed" error rather than a panic or,
//! worse, two overlapping transactions on one socket.
//!
//! ## Dropping the `Client` on error
//!
//! If any of the transaction-control statements (`BEGIN`, `COMMIT`, `ROLLBACK`)
//! — or anything else that leaves the connection in an unknown state — errors,
//! we intentionally drop the `Client` instead of returning it to the
//! `Connection`. Dropping it closes the underlying socket.
//!
//! This matters because the `Connection` is intended to live in a connection
//! pool in future: on an unexpected error we don't actually know what state the
//! server-side session is in, so it is safer to throw the connection away and
//! let the pool open a fresh one than to hand a possibly-broken connection back
//! to be reused.

use std::sync::{Arc, Mutex, MutexGuard, TryLockError};

use log::warn;
use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyDict, PyInt, PyTuple},
};
use tokio_postgres::Client;

use crate::database::postgres::{
    cursor_state::CursorQueryState, helpers::BlockingPostgresResult, value::PgValue,
};

/// A single Postgres connection exposed to Python.
///
/// The underlying [`Client`] is held in an `Option` so that it can be moved out
/// while a cursor is in use (and so that a dropped/closed connection is a
/// recoverable error). The `Arc<Mutex<...>>` lets the cursor hold a clone of
/// the [`Connection`] and hand the client back when it finishes.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Connection {
    // Private: all access goes through `take_client`/`put_client` to preserve
    // the "client lives in exactly one place" invariant (see module docs).
    client: Arc<Mutex<Option<Client>>>,
}

impl Connection {
    /// Wrap a freshly-established `Client` in a `Connection`.
    pub fn new(client: Client) -> Self {
        Self {
            client: Arc::new(Mutex::new(Some(client))),
        }
    }

    /// Lock the client slot, mapping a poisoned mutex to a Python error rather
    /// than panicking.
    fn py_lock(&self) -> PyResult<MutexGuard<'_, Option<Client>>> {
        self.client
            .lock()
            .map_err(|_| PyRuntimeError::new_err("connection mutex poisoned"))
    }

    /// Move the [`Client`] out of the connection, leaving the slot empty.
    ///
    /// Errors if the client has already been taken (i.e. the connection is in
    /// use by a cursor, or has been closed).
    fn take_client(&self) -> PyResult<Client> {
        let client = self.py_lock()?.take();
        let Some(client) = client else {
            return Err(PyRuntimeError::new_err("connection already closed"));
        };
        Ok(client)
    }

    /// Return a [`Client`] to the connection's slot.
    ///
    /// Errors if the slot is already occupied, which would indicate a logic
    /// bug (two clients for one connection).
    fn put_client(&self, client: Client) -> PyResult<()> {
        let mut existing_client = self.py_lock()?;
        if existing_client.is_some() {
            return Err(PyRuntimeError::new_err("connection already has a client"));
        }
        existing_client.replace(client);
        Ok(())
    }

    /// Open a new cursor, starting a transaction with `BEGIN`.
    ///
    /// Returns both the Python-visible [`Cursor`] and a [`CursorGuard`] whose
    /// [`Drop`] impl rolls back and returns the client if the cursor is never
    /// explicitly finished (e.g. the interaction panics before `finish`).
    fn cursor<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, Cursor>, CursorGuard)> {
        // Take the client out for the duration of the transaction; the
        // connection's slot stays empty until the cursor hands it back, so the
        // connection can't be used concurrently.
        let client = self.take_client()?;

        // If `BEGIN` fails we never built the cursor, so `client` is dropped
        // here and the connection closed. That's intentional: see the
        // "Dropping the `Client` on error" note at the top of this module.
        client.execute("BEGIN", &[]).block_on_result(py)?;

        let cursor = Cursor::new(self.clone(), client);
        let bound_cursor = Bound::new(py, cursor.clone())?;
        Ok((bound_cursor, CursorGuard { cursor }))
    }
}

/// A PEP-249-style cursor over an in-progress transaction.
///
/// Holds the [`Client`] for the lifetime of the interaction and tracks the
/// state of the most recent query (see [`CursorQueryState`]). All interior
/// state is behind a mutex so that the cursor can be `frozen` (shared via
/// `Arc`) yet still mutated by its methods.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Cursor {
    /// The cursor's state while the transaction is open; `None` once the
    /// cursor has been finished/closed (the whole state is taken out at once,
    /// which also drops the client and any in-flight result set). Its presence
    /// doubles as the "transaction still open" flag: a cursor only ever holds
    /// state between `BEGIN` and `finish`.
    inner: Arc<Mutex<Option<CursorInner>>>,
}

/// The mutable guts of a [`Cursor`], held while its transaction is open.
struct CursorInner {
    /// The client driving the transaction.
    client: Client,
    /// The owning connection, so the client can be handed back on finish.
    connection: Connection,
    /// State of the most recent `execute` (live row stream, rowcount, etc.).
    query_state: CursorQueryState,
}

/// RAII guard that guarantees a cursor's transaction is cleaned up.
///
/// If the cursor is finished normally (`finish`) the client is already gone by
/// the time this drops, so the guard is a no-op. If instead the interaction
/// bailed out before `finish` ran, the guard rolls back the open transaction
/// and returns the client to the connection.
struct CursorGuard {
    cursor: Cursor,
}

impl Cursor {
    /// Build a cursor that owns `client` on behalf of `connection`.
    fn new(connection: Connection, client: Client) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Some(CursorInner {
                client,
                connection,
                query_state: CursorQueryState::new(),
            }))),
        }
    }

    /// Lock the cursor's inner state.
    ///
    /// Uses `try_lock` rather than `lock`: a cursor is single-threaded by
    /// contract, so contention means it's being used from two threads at once,
    /// which we surface as an error instead of blocking.
    fn py_lock(&self) -> PyResult<MutexGuard<'_, Option<CursorInner>>> {
        match self.inner.try_lock() {
            Ok(guard) => Ok(guard),
            Err(TryLockError::Poisoned(p)) => {
                // If the mutex is poisoned let's immediately drop the cursor
                // state (and thus the client) to close the connection, as we
                // don't know what state it is in. Otherwise we'd have to wait
                // for the `Cursor` to be dropped on the python side, which can
                // take a while.
                //
                // Note that we'll definitely hit this code on a panic, as
                // `py_lock` is called when `CursorGuard` drops.
                *p.into_inner() = None;

                Err(PyRuntimeError::new_err("cursor mutex poisoned"))
            }
            Err(TryLockError::WouldBlock) => Err(PyRuntimeError::new_err(
                "cursor is being used in another thread and cannot be used concurrently",
            )),
        }
    }

    /// Run `f` against the live cursor state, or error if the cursor has
    /// already been finished/closed (its state taken out, leaving `None`).
    fn with_inner<R>(&self, f: impl FnOnce(&mut CursorInner) -> PyResult<R>) -> PyResult<R> {
        let mut guard = self.py_lock()?;
        let inner = guard
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("cursor already closed"))?;
        f(inner)
    }

    /// Run `f` against the live query state, or error if the cursor has
    /// already been finished/closed (its state taken out, leaving `None`).
    pub fn with_cursor_state<R>(
        &self,
        f: impl FnOnce(&mut CursorQueryState) -> PyResult<R>,
    ) -> PyResult<R> {
        self.with_inner(|inner| f(&mut inner.query_state))
    }

    /// Finish the transaction — `COMMIT` if `commit` is set, otherwise
    /// `ROLLBACK` — and hand the client back to the connection.
    ///
    /// Taking the cursor's state out marks it as finished, so a second call
    /// (from Python, or from the [`CursorGuard`] once the explicit `finish` has
    /// already run) finds nothing to do and returns `Ok(())`. If the statement
    /// itself fails the client is dropped rather than returned, closing the
    /// connection (see the module docs).
    fn finish(&self, commit: bool) -> PyResult<()> {
        // Take the whole cursor state out under one lock, closing the cursor
        // (and dropping any in-flight result set with it). If it's already
        // gone there's nothing to do, so we don't even need the GIL.
        let Some(CursorInner {
            client, connection, ..
        }) = self.py_lock()?.take()
        else {
            return Ok(());
        };

        // Attach the GIL (cheap if the caller already holds it) only now that
        // we have a client; `block_on_result` detaches it again for the wait.
        // No cursor lock is held here.
        Python::attach(|py| {
            let stmt = if commit { "COMMIT" } else { "ROLLBACK" };
            client.execute(stmt, &[]).block_on_result(py)?;

            // Only on success do we hand the client back to the connection for
            // the next interaction (or, in future, back to the pool).
            connection.put_client(client)
        })
    }
}

#[pymethods]
impl Cursor {
    /// Execute `query`, optionally with positional `params` bound to `$1`,
    /// `$2`, ... placeholders.
    ///
    /// Any previous result set is discarded. After this returns, rows (if any)
    /// can be read with `fetch_one`/`fetch_all`.
    #[pyo3(signature = (query, params = None))]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        query: &str,
        params: Option<Vec<PgValue>>,
    ) -> PyResult<()> {
        self.with_inner(|inner| inner.execute(py, query, params))
    }

    /// Return the next row of the current result set, or `None` if exhausted.
    fn fetch_one<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.with_cursor_state(|state| state.fetch_one(py))
    }

    /// Drain and return all remaining rows of the current result set.
    fn fetch_all<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.with_cursor_state(|state| state.fetch_all(py))
    }

    /// Return the PEP-249 `rowcount` for the last statement.
    ///
    /// This is the number of rows affected by a DML statement; for queries
    /// where it isn't (yet) known it follows PEP-249 and returns `-1`.
    fn rowcount<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        self.with_cursor_state(|state| state.rowcount(py))
    }
}

impl CursorInner {
    /// Prepare and run `query`, stashing the resulting row stream in
    /// `query_state` for later fetching.
    fn execute<'py>(
        &mut self,
        py: Python<'py>,
        query: &str,
        params: Option<Vec<PgValue>>,
    ) -> PyResult<()> {
        // Drop any previous result set before starting the new query.
        self.query_state.new_query();

        let statement = &self.client.prepare(query).block_on_result(py)?;

        let row_stream = self
            .client
            .query_raw(statement, params.unwrap_or_default())
            .block_on_result(py)?;

        self.query_state
            .on_query_start(row_stream, statement.columns());

        Ok(())
    }
}

impl Drop for CursorGuard {
    fn drop(&mut self) {
        // Roll back any transaction the interaction left open. On the happy
        // path `finish` already ran, so this is a no-op (no client left to
        // take); only when the interaction bailed out before finishing does
        // this actually ROLLBACK. `finish` attaches the GIL itself and handles
        // returning or dropping the client, so all we add is logging — `Drop`
        // can't propagate the error.
        if let Err(err) = self.cursor.finish(false) {
            warn!("failed to roll back abandoned transaction on drop: {err}");
        }
    }
}

#[pymethods]
impl Connection {
    /// Run `func` inside a transaction, passing it a fresh cursor.
    ///
    /// The cursor is prepended to `args` (so the callback is invoked as
    /// `func(cursor, *args, **kwargs)`), mirroring Synapse's existing
    /// `run_interaction` API. The transaction is committed if the callback
    /// returns normally and rolled back if it raises, and the callback's
    /// return value is propagated back to Python.
    #[pyo3(signature = (func, *args, **kwargs))]
    fn run_interaction<'py>(
        &self,
        py: Python<'py>,
        func: Bound<'py, PyAny>,
        args: Bound<'py, PyTuple>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (cursor, _guard) = self.cursor(py)?;

        // Build a new argument list with the cursor prepended, then call the
        // provided function with that new argument list.
        let args = args.to_list();
        args.insert(0, &cursor)?;

        let result = func.call(args.to_tuple(), kwargs);

        // Commit on success, rollback on failure. (If `finish` itself errors,
        // that error takes precedence over the callback's result.)
        cursor.get().finish(result.is_ok())?;

        result
    }
}
