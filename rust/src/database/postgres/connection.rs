//! The Python-facing [`Connection`] / [`Cursor`] pair for the Postgres backend.
//!
//! These implement enough of the PEP-249 (DBAPI2) shape for Synapse's needs,
//! arranged so that the existing Python transaction driver
//! (`synapse.storage.database.new_transaction`) can drive them *unchanged*: it
//! opens a [`Cursor`] with [`Connection::cursor`], runs the interaction
//! function against it, and then commits or rolls back at the **connection**
//! level with [`Connection::commit`] / [`Connection::rollback`].
//!
//! The driver underneath is async but the Python API is sync, so every database
//! call is driven to completion on the shared tokio runtime via the `block_on`
//! helpers (see [`super::helpers::BlockingPostgresResult`]).
//!
//! ## Connection owns the transaction; the cursor is a thin view
//!
//! The single [`tokio_postgres::Client`] lives in the [`Connection`] for the
//! whole life of the connection. Transaction control (`BEGIN` / `COMMIT` /
//! `ROLLBACK`) is issued *on the connection*, because that is where Synapse's
//! driver issues it — `conn.commit()` runs while the cursor that produced the
//! rows is still open (the driver only closes the cursor afterwards).
//!
//! A [`Cursor`] is therefore cheap: it holds a clone of the owning
//! [`Connection`] (an `Arc`) plus its own result-set state
//! ([`CursorQueryState`]). It borrows the client only for the brief moment it
//! takes to *start* a query (`prepare` + `query_raw`); the resulting row stream
//! is self-contained (`'static`), so once a query has been issued the cursor
//! reads rows from its own state without touching the connection again. Many
//! cursors can share one connection this way, though in practice Synapse's
//! driver uses one at a time.
//!
//! ## Implicit transactions (matching psycopg2)
//!
//! psycopg2 is transactional by default: the first statement after `connect`
//! (or after a `commit`/`rollback`) implicitly opens a transaction.
//! `tokio_postgres`, by contrast, is autocommit by default. To behave like
//! psycopg2 we track whether a transaction is open (`in_txn`) and lazily issue a
//! `BEGIN` before the first statement of each transaction — unless the
//! connection has been put into autocommit mode (see [`Connection::set_autocommit`]).
//! `commit`/`rollback` end the transaction and clear the flag; with no
//! transaction open they are no-ops, just like psycopg2.
//!
//! ## Dropping the `Client` on error
//!
//! A *query* error (bad SQL, a constraint violation, an integer out of range,
//! …) leaves the connection open with its transaction in the aborted state,
//! exactly as psycopg2 does: the error propagates to Python and the driver is
//! expected to `rollback()`. We do **not** throw the connection away for these.
//!
//! The transaction-control statements are different. If `COMMIT` or `ROLLBACK`
//! itself fails we no longer know what state the server-side session is in, so
//! we drop the `Client` (closing the socket) rather than hand a possibly-broken
//! connection back for reuse. Likewise, [`Connection::close`] drops the client;
//! the server rolls back any transaction left open when the socket closes.

use std::sync::{Arc, Mutex, MutexGuard, TryLockError};

use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyInt, PyTuple},
};
use tokio::runtime::Handle;
use tokio_postgres::Client;

use crate::database::postgres::{
    cursor_state::CursorQueryState, helpers::BlockingPostgresResult, value::PgValue,
};

/// `try_lock` a mutex that is single-threaded by contract, mapping its two
/// failure modes to Python errors.
///
/// A poisoned mutex means a panic happened while it was held, so the guarded
/// state can't be trusted: `on_poison` resets it (closing a connection /
/// discarding a result set) before we error. `WouldBlock` means the object is
/// being used from two threads at once, which is a caller bug rather than
/// something to block on. `noun` names the object in both messages (e.g.
/// `"connection"`, `"cursor"`).
fn try_lock_or_reset<'a, T>(
    mutex: &'a Mutex<T>,
    noun: &str,
    on_poison: impl FnOnce(&mut T),
) -> PyResult<MutexGuard<'a, T>> {
    match mutex.try_lock() {
        Ok(guard) => Ok(guard),
        Err(TryLockError::Poisoned(poisoned)) => {
            on_poison(&mut poisoned.into_inner());
            Err(PyRuntimeError::new_err(format!("{noun} mutex poisoned")))
        }
        Err(TryLockError::WouldBlock) => Err(PyRuntimeError::new_err(format!(
            "{noun} is being used in another thread and cannot be used concurrently"
        ))),
    }
}

/// A single Postgres connection exposed to Python.
///
/// Owns the [`tokio_postgres::Client`] for its whole life and is the authority
/// on transaction state. The `Arc<Mutex<...>>` lets cursors hold a cheap clone
/// (so they can reach the client to start a query) while keeping all access to
/// the client serialised.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Connection {
    inner: Arc<Mutex<ConnInner>>,
    /// Handle to the extension's shared tokio runtime, taken from the reactor at
    /// `connect` time (see [`super::connect`]). Every blocking DB call on this
    /// connection — and on cursors opened from it — drives its future on this
    /// handle, so nothing needs to re-consult the reactor after `connect`.
    handle: Handle,
}

/// The mutable guts of a [`Connection`], behind its mutex.
struct ConnInner {
    /// The driver client. `None` once the connection has been closed (or thrown
    /// away after a transaction-control error); any further use is an error.
    client: Option<Client>,
    /// Whether a transaction is currently open (a `BEGIN` has been issued and
    /// not yet matched by a `COMMIT`/`ROLLBACK`). Drives the lazy `BEGIN`.
    in_txn: bool,
    /// In autocommit mode we never issue an implicit `BEGIN`, so each statement
    /// runs in its own implicit transaction. Defaults to `false`, matching
    /// psycopg2's transactional default.
    autocommit: bool,
}

impl Connection {
    /// Wrap a freshly-established `Client` in a `Connection`, carrying the
    /// shared-runtime `handle` all its blocking calls will drive futures on.
    pub fn new(client: Client, handle: Handle) -> Self {
        Self {
            inner: Arc::new(Mutex::new(ConnInner {
                client: Some(client),
                in_txn: false,
                autocommit: false,
            })),
            handle,
        }
    }

    /// Lock the inner state.
    ///
    /// Uses `try_lock` rather than `lock`: a connection is used from a single
    /// thread at a time by contract (Synapse hands one connection to one
    /// worker thread), so contention means it's being used from two threads at
    /// once, which we surface as an error instead of blocking. A poisoned mutex
    /// (a panic happened mid-operation) closes the connection — we no longer
    /// know the session state — and errors.
    fn lock(&self) -> PyResult<MutexGuard<'_, ConnInner>> {
        try_lock_or_reset(&self.inner, "connection", |inner| {
            // On poison we no longer know the session state, so close the
            // connection: drop the client and clear the transaction flag.
            inner.client = None;
            inner.in_txn = false;
        })
    }

    /// Borrow the client just long enough to run `f` (typically starting a
    /// query), opening an implicit transaction first if one isn't already open.
    ///
    /// The borrow ends as soon as `f` returns; `f` is expected to hand back an
    /// owned, self-contained value (e.g. a `'static` row stream) rather than
    /// anything tied to the client. Errors if the connection is closed.
    fn with_client<R>(
        &self,
        py: Python<'_>,
        f: impl FnOnce(&Client, &Handle) -> PyResult<R>,
    ) -> PyResult<R> {
        let mut guard = self.lock()?;

        // Lazily open a transaction so statements are transactional by default,
        // matching psycopg2. We set `in_txn` *after* a successful `BEGIN` but
        // before running `f`, so that if `f` (the user's statement) fails the
        // open-but-aborted transaction is still tracked and `rollback()` knows
        // to clean it up.
        if !guard.autocommit && !guard.in_txn {
            {
                let client = client_ref(&guard)?;
                client
                    .execute("BEGIN", &[])
                    .block_on_result(py, &self.handle)?;
            }
            guard.in_txn = true;
        }

        let client = client_ref(&guard)?;
        f(client, &self.handle)
    }

    /// Issue a transaction-control statement (`COMMIT`/`ROLLBACK`) if a
    /// transaction is open; a no-op otherwise.
    ///
    /// On success the transaction flag is cleared. On failure the client is
    /// dropped (closing the socket): after a failed commit/rollback the session
    /// state is unknown, so the connection is thrown away rather than reused.
    fn end_txn(&self, py: Python<'_>, stmt: &'static str) -> PyResult<()> {
        let mut guard = self.lock()?;

        if !guard.in_txn {
            return Ok(());
        }

        // If the client is already gone the server has rolled the transaction
        // back for us; just clear the flag.
        if guard.client.is_none() {
            guard.in_txn = false;
            return Ok(());
        }

        let result = {
            let client = client_ref(&guard)?;
            client.execute(stmt, &[]).block_on_result(py, &self.handle)
        };

        match result {
            Ok(_) => {
                guard.in_txn = false;
                Ok(())
            }
            Err(err) => {
                // Unknown session state: drop the connection rather than reuse it.
                guard.client = None;
                guard.in_txn = false;
                Err(err)
            }
        }
    }
}

/// Borrow the live client out of a locked inner state, or error if the
/// connection has been closed.
fn client_ref(guard: &ConnInner) -> PyResult<&Client> {
    guard
        .client
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("connection already closed"))
}

#[pymethods]
impl Connection {
    /// Open a new cursor over this connection.
    ///
    /// Cheap: no I/O and no `BEGIN` happens here (the transaction is opened
    /// lazily on the first `execute`). The returned cursor shares this
    /// connection's client.
    fn cursor(&self) -> Cursor {
        Cursor::new(self.clone())
    }

    /// Commit the current transaction, if one is open. A no-op otherwise.
    fn commit(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, "COMMIT")
    }

    /// Roll back the current transaction, if one is open. A no-op otherwise.
    fn rollback(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, "ROLLBACK")
    }

    /// Close the connection, dropping the underlying client.
    ///
    /// Dropping the client closes the socket; the server rolls back any
    /// transaction that was still open. Idempotent: closing an
    /// already-closed connection is fine.
    fn close(&self) -> PyResult<()> {
        let mut guard = self.lock()?;
        guard.client = None;
        guard.in_txn = false;
        Ok(())
    }

    /// Switch autocommit mode on or off.
    ///
    /// In autocommit mode no implicit `BEGIN` is issued, so each statement runs
    /// in its own transaction. Mirrors psycopg2's `set_session(autocommit=...)`,
    /// including its rule that the mode can't be changed while a transaction is
    /// in progress.
    fn set_autocommit(&self, autocommit: bool) -> PyResult<()> {
        let mut guard = self.lock()?;
        if guard.in_txn {
            return Err(PyRuntimeError::new_err(
                "cannot change autocommit mode while a transaction is in progress",
            ));
        }
        guard.autocommit = autocommit;
        Ok(())
    }

    /// Context-manager entry: returns the connection itself.
    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Context-manager exit: commit if the block completed normally, roll back
    /// if it raised. Like psycopg2, this does *not* close the connection, and
    /// it does not suppress the exception (returns `False`).
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Bound<'_, PyAny>>,
        _exc_value: Option<Bound<'_, PyAny>>,
        _traceback: Option<Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        if exc_type.is_some() {
            self.rollback(py)?;
        } else {
            self.commit(py)?;
        }
        Ok(false)
    }
}

/// A PEP-249-style cursor over a connection's current transaction.
///
/// Cheap to create: it holds a clone of the owning [`Connection`] plus its own
/// result-set state ([`CursorQueryState`]). Transaction control lives on the
/// connection, not here. All interior state is behind a mutex so the cursor can
/// be `frozen` (shared via `Arc`) yet still mutated by its methods.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Cursor {
    /// The owning connection, used to reach the client when starting a query.
    connection: Connection,
    /// State of the most recent `execute` (live row stream, rowcount, etc.).
    state: Arc<Mutex<CursorQueryState>>,
}

impl Cursor {
    /// Build a cursor over `connection`.
    fn new(connection: Connection) -> Self {
        Self {
            connection,
            state: Arc::new(Mutex::new(CursorQueryState::new())),
        }
    }

    /// Lock the cursor's query state.
    ///
    /// Like [`Connection::lock`], uses `try_lock`: a cursor is single-threaded
    /// by contract, so contention means concurrent use, which we surface as an
    /// error. On a poisoned mutex we drop the result set (resetting to `Idle`)
    /// and error.
    fn lock_state(&self) -> PyResult<MutexGuard<'_, CursorQueryState>> {
        // On poison we drop the (untrusted) result set, resetting to `Idle`.
        try_lock_or_reset(&self.state, "cursor", |state| {
            *state = CursorQueryState::new()
        })
    }
}

#[pymethods]
impl Cursor {
    /// Execute `query`, optionally with positional `params` bound to `$1`,
    /// `$2`, ... placeholders.
    ///
    /// Any previous result set is discarded. After this returns, rows (if any)
    /// can be read with `fetch_one`/`fetch_all`/`fetch_next_batch`.
    #[pyo3(signature = (query, params = None))]
    fn execute(&self, py: Python<'_>, query: &str, params: Option<Vec<PgValue>>) -> PyResult<()> {
        // Drop any previous result set before starting the new query.
        self.lock_state()?.new_query();

        // Borrow the connection's client only for as long as it takes to start
        // the query. `query_raw` returns a `'static` `RowStream`, so the borrow
        // ends here and the cursor owns the stream from now on.
        let (stream, description) = self.connection.with_client(py, |client, handle| {
            let statement = client.prepare(query).block_on_result(py, handle)?;

            let stream = client
                .query_raw(&statement, params.unwrap_or_default())
                .block_on_result(py, handle)?;

            // The column names back the (future) PEP-249 `Cursor.description`;
            // pull them out of the prepared statement here so `cursor_state`
            // stays decoupled from `tokio_postgres::Column`.
            let description = statement
                .columns()
                .iter()
                .map(|c| c.name().to_string())
                .collect::<Vec<_>>();

            Ok((stream, description))
        })?;

        // A statement with no result columns (INSERT/UPDATE/DELETE/DDL without
        // RETURNING) produces no rows for anyone to fetch, so nothing would
        // otherwise poll its stream — and a `query_raw` stream only reports the
        // server's response, including any error (e.g. a constraint violation)
        // and the affected-row count, once polled. Drive it to completion now so
        // such errors surface here at `execute` time (as psycopg2 does) rather
        // than being lost when the result is never fetched — most visibly under
        // autocommit, where there is no later `commit` to surface them.
        let returns_rows = !description.is_empty();
        let mut state = self.lock_state()?;
        state.on_query_start(stream, description);
        if !returns_rows {
            state.finish_no_rows(py, &self.connection.handle)?;
        }

        Ok(())
    }

    /// Execute a multi-statement SQL script (statements separated by `;`).
    ///
    /// Unlike [`Cursor::execute`], which `prepare`s a single statement, this
    /// runs the whole script on the simple-query protocol (`batch_execute`), so
    /// it may contain many `;`-separated statements — as Synapse's schema files
    /// do. It takes no parameters and produces no fetchable rows.
    ///
    /// This is a thin primitive: the script runs inside the connection's current
    /// transaction, opening one lazily like `execute` and leaving it open for
    /// the caller to commit. The higher-level engine `executescript` — which
    /// also substitutes the auto-increment placeholder — is layered on top.
    /// Note it does *not* commit any prior transaction first: unlike the
    /// psycopg2 engine (which still prefixes `COMMIT; BEGIN TRANSACTION;`,
    /// committing script-by-script), the Rust engine deliberately keeps a whole
    /// sequence of schema/delta scripts in one transaction so it is applied
    /// either completely or not at all — see
    /// `BaseDatabaseEngine.executescript`'s docstring for the contract.
    fn executescript(&self, py: Python<'_>, script: &str) -> PyResult<()> {
        // A script yields no fetchable rows, so drop any previous result set.
        self.lock_state()?.new_query();

        self.connection.with_client(py, |client, handle| {
            client.batch_execute(script).block_on_result(py, handle)
        })
    }

    /// Return the next row of the current result set, or `None` if exhausted.
    fn fetch_one<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.lock_state()?.fetch_one(py, &self.connection.handle)
    }

    /// Drain and return all remaining rows of the current result set.
    fn fetch_all<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.lock_state()?.fetch_all(py, &self.connection.handle)
    }

    /// Fetch the next batch of rows from the current result set.
    ///
    /// Blocks for the first row, then returns any further rows that are already
    /// available without blocking. Returns an empty list only once the result
    /// set is exhausted. `capacity` is a hint for the size of the returned
    /// buffer, not a limit on the number of rows returned.
    #[pyo3(signature = (capacity = 100))]
    fn fetch_next_batch<'py>(
        &self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.lock_state()?
            .fetch_next_batch(py, &self.connection.handle, capacity)
    }

    /// Return the PEP-249 `rowcount` for the last statement.
    ///
    /// This is the number of rows affected by a DML statement; for queries
    /// where it isn't (yet) known it follows PEP-249 and returns `-1`.
    fn rowcount<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        self.lock_state()?.rowcount(py, &self.connection.handle)
    }

    /// Close the cursor, discarding any in-flight result set.
    ///
    /// This does *not* touch the transaction — that's the connection's job.
    /// Idempotent.
    fn close(&self) -> PyResult<()> {
        *self.lock_state()? = CursorQueryState::new();
        Ok(())
    }
}
