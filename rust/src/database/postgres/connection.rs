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
//! ## Returning vs discarding the connection
//!
//! Every [`Connection`] wraps a [`PooledConnection`] checked out of the
//! [`super::pool`]. Dropping it normally *returns it to the pool* for reuse.
//! Where that reuse would be unsafe we instead **discard** it: the connection is
//! detached from the pool with [`Object::take`] and dropped, which both closes
//! the socket and shrinks the pool so the bad connection is never handed out
//! again.
//!
//! A *query* error (bad SQL, a constraint violation, an integer out of range,
//! …) leaves the connection open with its transaction in the aborted state,
//! exactly as psycopg2 does: the error propagates to Python and the driver is
//! expected to `rollback()`. We do **not** throw the connection away for these.
//!
//! Three situations do force a discard, because the session state is unknown or
//! unclean and must not reach the next caller:
//!  - a failed `COMMIT`/`ROLLBACK` — we no longer know the session state;
//!  - a poisoned mutex — a panic happened mid-operation;
//!  - the connection being dropped with a transaction still open — it can't be
//!    rolled back synchronously from `Drop`, so the server does it for us when
//!    the socket closes.
//!
//! [`Connection::close`], by contrast, returns a clean connection to the pool
//! (discarding it only if a transaction was left open).

use std::sync::{Arc, Mutex, MutexGuard, TryLockError, Weak};

use deadpool::managed::Object;
use futures::future::try_join_all;
use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyInt, PyList, PyTuple},
};
use tokio_postgres::Client;

use crate::database::postgres::{
    cursor_state::CursorQueryState, helpers::BlockingPostgresResult, pool::PooledConnection,
    value::PgValue,
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
/// Wraps a connection checked out of the pool for its whole life and is the
/// authority on transaction state. The `Arc<Mutex<...>>` lets cursors hold a
/// cheap clone (so they can reach the client to start a query) while keeping all
/// access to the client serialised.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Connection {
    inner: Arc<Mutex<ConnInner>>,
}

/// The mutable guts of a [`Connection`], behind its mutex.
struct ConnInner {
    /// The pooled connection. `None` once the connection has been closed,
    /// returned to the pool, or discarded after an error; any further use is an
    /// error.
    client: Option<PooledConnection>,
    /// Whether a transaction is currently open (a `BEGIN` has been issued and
    /// not yet matched by a `COMMIT`/`ROLLBACK`). Drives the lazy `BEGIN`.
    in_txn: bool,
    /// In autocommit mode we never issue an implicit `BEGIN`, so each statement
    /// runs in its own implicit transaction. Defaults to `false`, matching
    /// psycopg2's transactional default.
    autocommit: bool,
    /// The query states of every cursor opened on this connection, held weakly
    /// so a dropped cursor doesn't leak an entry (pruned on each sweep). Used
    /// by [`ConnInner::drop_cursor_streams`] to drop live row streams before a
    /// `COMMIT`/`ROLLBACK` is sent or the connection is given up.
    cursor_states: Vec<Weak<Mutex<CursorQueryState>>>,
}

impl ConnInner {
    /// Drop any live cursor row streams, pruning entries for dropped cursors.
    ///
    /// `tokio_postgres` delivers responses strictly in order: a large unread
    /// row stream sitting ahead of a later statement's response (a `COMMIT`, or
    /// the next checkout's first query if the connection went back to the pool)
    /// can block the connection task on the stream's backpressure, stalling
    /// that later statement forever. Dropping the stream closes its channel, so
    /// the connection task discards the remaining rows and moves on.
    ///
    /// This diverges from psycopg2, whose client-side buffering lets rows be
    /// fetched after `commit()`: here commit/rollback/close invalidates any
    /// unfetched results. Synapse's transaction driver never fetches after
    /// commit, so the difference is unobservable in practice.
    fn drop_cursor_streams(&mut self) {
        self.cursor_states.retain(|weak| {
            let Some(state) = weak.upgrade() else {
                // The cursor (and with it any stream) is gone; prune the entry.
                return false;
            };
            // `try_lock`, honouring the single-thread-per-connection contract:
            // it can only fail if a cursor is concurrently in use on another
            // thread (the contract is already broken), where skipping the reset
            // beats blocking or panicking.
            if let Ok(mut state) = state.try_lock() {
                state.new_query();
            }
            true
        });
    }

    /// Give up the connection cleanly.
    ///
    /// The connection is returned to the pool for reuse — **unless** a
    /// transaction is still open, in which case it can't be rolled back from
    /// here, so we discard it (detach and drop) rather than hand a
    /// mid-transaction connection to the next caller.
    ///
    /// Any live cursor streams are dropped first, so a returned connection's
    /// task isn't left blocked delivering rows nobody will read (which would
    /// stall the next checkout's first statement — see
    /// [`ConnInner::drop_cursor_streams`]).
    fn release(&mut self) {
        self.drop_cursor_streams();
        if let Some(conn) = self.client.take() {
            if self.in_txn {
                let _ = Object::take(conn); // detach + drop: not returned to the pool
            }
            // else: `conn` dropped here → returned to the pool for reuse.
        }
        self.in_txn = false;
    }

    /// Discard the connection: the session state is unknown or unclean, so it
    /// must never be reused. It is detached from the pool (shrinking it).
    fn discard(&mut self) {
        self.drop_cursor_streams();
        if let Some(conn) = self.client.take() {
            let _ = Object::take(conn);
        }
        self.in_txn = false;
    }
}

impl Drop for ConnInner {
    fn drop(&mut self) {
        // Return the connection to the pool (or discard it) when the last
        // reference — the `Connection` and all its cursors — goes away.
        self.release();
    }
}

impl Connection {
    /// Wrap a connection checked out of the pool in a `Connection`.
    ///
    /// The connection is returned to the pool when this `Connection` (and every
    /// cursor cloned from it) is dropped, unless it is discarded first (see the
    /// module docs).
    pub fn new(conn: PooledConnection) -> Self {
        Self {
            inner: Arc::new(Mutex::new(ConnInner {
                client: Some(conn),
                in_txn: false,
                autocommit: false,
                cursor_states: Vec::new(),
            })),
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
            // On poison we no longer know the session state, so discard the
            // connection (never returning it to the pool).
            inner.discard();
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
        f: impl FnOnce(&Client) -> PyResult<R>,
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
                client.execute("BEGIN", &[]).block_on_result(py)?;
            }
            guard.in_txn = true;
        }

        let client = client_ref(&guard)?;
        f(client)
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

        // Drop any live cursor streams first: our COMMIT/ROLLBACK response is
        // delivered *after* any still-unread rows, so a stalled stream would
        // block it forever (see `drop_cursor_streams`).
        guard.drop_cursor_streams();

        let result = {
            let client = client_ref(&guard)?;
            client.execute(stmt, &[]).block_on_result(py)
        };

        match result {
            Ok(_) => {
                guard.in_txn = false;
                Ok(())
            }
            Err(err) => {
                // Unknown session state: discard the connection rather than
                // reuse it (or return it to the pool).
                guard.discard();
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
        .as_deref()
        .ok_or_else(|| PyRuntimeError::new_err("connection already closed"))
}

#[pymethods]
impl Connection {
    /// Open a new cursor over this connection.
    ///
    /// Cheap: no I/O and no `BEGIN` happens here (the transaction is opened
    /// lazily on the first `execute`). The returned cursor shares this
    /// connection's client; its query state is registered (weakly) with the
    /// connection so live row streams can be dropped at transaction end (see
    /// `ConnInner::drop_cursor_streams`).
    fn cursor(&self) -> PyResult<Cursor> {
        let cursor = Cursor::new(self.clone());
        self.lock()?
            .cursor_states
            .push(Arc::downgrade(&cursor.state));
        Ok(cursor)
    }

    /// Commit the current transaction, if one is open. A no-op otherwise.
    fn commit(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, "COMMIT")
    }

    /// Roll back the current transaction, if one is open. A no-op otherwise.
    fn rollback(&self, py: Python<'_>) -> PyResult<()> {
        self.end_txn(py, "ROLLBACK")
    }

    /// Close the connection, releasing the underlying client.
    ///
    /// A standalone client's socket is closed; a pooled connection is returned
    /// to the pool for reuse (or discarded if a transaction was left open, in
    /// which case the server rolls it back). Idempotent: closing an
    /// already-closed connection is fine.
    fn close(&self) -> PyResult<()> {
        self.lock()?.release();
        Ok(())
    }

    /// Discard the connection instead of returning it to the pool: the
    /// underlying socket is closed and the pool shrinks (growing back on
    /// demand). For forced recycling — Synapse's per-connection transaction
    /// limit calls `reconnect()` expecting a *fresh server session*, which
    /// `close()` can't deliver (it hands the same live session back to the
    /// pool, where the next checkout just picks it up again). Idempotent,
    /// like `close`.
    fn discard(&self) -> PyResult<()> {
        self.lock()?.discard();
        Ok(())
    }

    /// Whether this connection has been closed (or discarded).
    ///
    /// Mirrors psycopg2's `connection.closed`: it is true once the connection
    /// has been closed/returned/discarded, or if the underlying socket has been
    /// torn down. The database engine's `is_connection_closed` reads this to
    /// decide whether a pooled connection needs replacing.
    fn is_closed(&self) -> PyResult<bool> {
        Ok(match self.lock()?.client.as_ref() {
            None => true,
            Some(conn) => conn.is_closed(),
        })
    }

    /// Whether a transaction is currently open on this connection.
    ///
    /// Mirrors the engine's `in_transaction` check (psycopg2 reads
    /// `conn.status`): the transaction driver asserts a pooled connection is
    /// not left mid-transaction before handing it out again.
    fn in_transaction(&self) -> PyResult<bool> {
        Ok(self.lock()?.in_txn)
    }

    /// The current autocommit mode.
    ///
    /// Mirrors psycopg2's readable `connection.autocommit`; `prepare_database`
    /// reads it to decide whether it needs to open a transaction explicitly.
    #[getter]
    fn autocommit(&self) -> PyResult<bool> {
        Ok(self.lock()?.autocommit)
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
        let (stream, description) = self.connection.with_client(py, |client| {
            let statement = client.prepare(query).block_on_result(py)?;

            let stream = client
                .query_raw(&statement, params.unwrap_or_default())
                .block_on_result(py)?;

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
            state.finish_no_rows(py)?;
        }

        Ok(())
    }

    /// Execute `query` once for each parameter set in `params_seq`.
    ///
    /// This is DBAPI2's `executemany`, used by Synapse for batched writes. The
    /// statement is `prepare`d once and then run for each parameter set; it
    /// produces no fetchable rows. Like a single `execute`, the whole batch runs
    /// inside the connection's (lazily opened) transaction, so a failure
    /// part-way through aborts it and leaves the caller to roll back.
    ///
    /// The per-parameter-set executions are *pipelined*: their futures are
    /// driven concurrently, so `tokio_postgres` streams the whole batch onto the
    /// connection without waiting for a round-trip between each — one round-trip
    /// for the batch rather than one per statement. Results are matched to
    /// requests in order, and on the first error the rest are abandoned (the
    /// transaction is aborted anyway).
    ///
    /// After it returns `rowcount` reports the total number of rows affected
    /// across all executions (as psycopg2 does), `description` is `None`, and
    /// any `fetch_*` is an error. An empty `params_seq` runs nothing at all —
    /// no statement is sent and no transaction is opened — and leaves
    /// `rowcount` at the PEP-249 "unknown" sentinel (`-1`).
    #[pyo3(signature = (query, params_seq))]
    fn executemany(
        &self,
        py: Python<'_>,
        query: &str,
        params_seq: Vec<Vec<PgValue>>,
    ) -> PyResult<()> {
        // Drop any previous result set before starting the new statement.
        self.lock_state()?.new_query();

        // An empty batch is a no-op (matching psycopg2): don't send anything or
        // open a transaction, and leave `rowcount` reporting "unknown".
        if params_seq.is_empty() {
            self.lock_state()?.on_command_complete(None);
            return Ok(());
        }

        let total = self.connection.with_client(py, |client| {
            // Prepare once, then build a future per parameter set. Driving them
            // concurrently is what makes `tokio_postgres` pipeline them onto the
            // connection; blocking on the joined future runs the whole batch.
            let statement = client.prepare(query).block_on_result(py)?;

            let counts = try_join_all(
                params_seq
                    .into_iter()
                    .map(|params| client.execute_raw(&statement, params)),
            )
            .block_on_result(py)?;

            Ok(counts.into_iter().sum::<u64>())
        })?;

        // Retain the summed affected-row count for `rowcount`, as psycopg2 does.
        self.lock_state()?.on_command_complete(Some(total));

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

        self.connection.with_client(py, |client| {
            client.batch_execute(script).block_on_result(py)
        })
    }

    /// Return the next row of the current result set, or `None` if exhausted.
    fn fetch_one<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.lock_state()?.fetch_one(py)
    }

    /// Drain and return all remaining rows of the current result set.
    fn fetch_all<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        self.lock_state()?.fetch_all(py)
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
        self.lock_state()?.fetch_next_batch(py, capacity)
    }

    /// Return the PEP-249 `rowcount` for the last statement.
    ///
    /// This is the number of rows affected by a DML statement; for queries
    /// where it isn't (yet) known it follows PEP-249 and returns `-1`.
    fn rowcount<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        self.lock_state()?.rowcount(py)
    }

    /// Return the PEP-249 `description` for the current result set, or `None`.
    ///
    /// This is a list with one entry per column, each a 7-tuple
    /// `(name, type_code, display_size, internal_size, precision, scale,
    /// null_ok)` as PEP-249 specifies. Only the column name is populated; the
    /// remaining six fields are always `None` — that is all Synapse needs, as
    /// it only ever reads `column[0]`.
    ///
    /// It is `None` when there is no row-returning result set to describe:
    /// before any query, after an error reset the cursor, or for a statement
    /// that returns no rows (e.g. a bare `INSERT`), matching psycopg2.
    fn description<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyList>>> {
        let state = self.lock_state()?;
        let Some(columns) = state.description() else {
            return Ok(None);
        };

        let rows = columns
            .iter()
            .map(|name| {
                // PEP-249's 7-tuple; only `name` carries a meaningful value.
                PyTuple::new(
                    py,
                    [
                        name.into_pyobject(py)?.into_any(),
                        py.None().into_bound(py),
                        py.None().into_bound(py),
                        py.None().into_bound(py),
                        py.None().into_bound(py),
                        py.None().into_bound(py),
                        py.None().into_bound(py),
                    ],
                )
            })
            .collect::<PyResult<Vec<_>>>()?;

        Ok(Some(PyList::new(py, rows)?))
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

#[cfg(test)]
mod tests {
    //! These exercise the pool-backed `Connection` against a live Postgres, so
    //! they only run when `SYNAPSE_TEST_POSTGRES_DSN` is set (e.g. to
    //! `host=postgres user=postgres password=postgres dbname=postgres`);
    //! otherwise they no-op. They assert *which* connections end up back in the
    //! pool — the transaction/value logic itself is covered by the Python test
    //! suite that drives these classes end to end.

    use super::*;
    use crate::database::postgres::pool::create_pool;
    use crate::database::runtime::runtime;

    fn test_dsn() -> Option<String> {
        std::env::var("SYNAPSE_TEST_POSTGRES_DSN").ok()
    }

    /// A clean connection (its transaction committed) is returned to the pool
    /// when the `Connection` is dropped.
    #[test]
    fn pooled_connection_returns_to_pool_after_commit() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 1).unwrap();
        let obj = runtime().block_on(async { pool.get().await.unwrap() });
        assert_eq!(pool.status().size, 1);

        Python::initialize();
        Python::attach(move |py| {
            let conn = Connection::new(obj);
            let cursor = conn.cursor().unwrap();
            cursor.execute(py, "SELECT 1", None).unwrap();
            conn.commit(py).unwrap();
            // Dropping both references releases the pooled connection.
            drop(cursor);
            drop(conn);
        });

        // The (clean) connection went back to the pool rather than being torn
        // down, so it's available for the next caller.
        assert_eq!(pool.status().size, 1);
        assert_eq!(pool.status().available, 1);
    }

    /// A connection dropped with a transaction still open can't be rolled back
    /// from `Drop`, so it is discarded (detached from the pool) rather than
    /// handed to the next caller mid-transaction.
    #[test]
    fn pooled_connection_discarded_when_dropped_mid_transaction() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 1).unwrap();
        let obj = runtime().block_on(async { pool.get().await.unwrap() });
        assert_eq!(pool.status().size, 1);

        Python::initialize();
        Python::attach(move |py| {
            let conn = Connection::new(obj);
            let cursor = conn.cursor().unwrap();
            // Opens a transaction lazily (BEGIN) but never commits/rolls back.
            cursor.execute(py, "SELECT 1", None).unwrap();
            drop(cursor);
            drop(conn);
        });

        // Detached: the pool shrank rather than accepting a mid-transaction
        // connection back.
        assert_eq!(pool.status().size, 0);
        assert_eq!(pool.status().available, 0);
    }

    /// `is_closed` and `in_transaction` track the connection's lifecycle, as
    /// the database engine's `is_connection_closed` / `in_transaction` need.
    #[test]
    fn is_closed_and_in_transaction_reflect_state() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 1).unwrap();
        let obj = runtime().block_on(async { pool.get().await.unwrap() });

        Python::initialize();
        Python::attach(move |py| {
            let conn = Connection::new(obj);
            // Freshly checked out: open and idle.
            assert!(!conn.is_closed().unwrap());
            assert!(!conn.in_transaction().unwrap());

            // The first statement lazily opens a transaction.
            let cursor = conn.cursor().unwrap();
            cursor.execute(py, "SELECT 1", None).unwrap();
            assert!(conn.in_transaction().unwrap());

            // Committing ends it; the connection is still open.
            conn.commit(py).unwrap();
            assert!(!conn.in_transaction().unwrap());
            assert!(!conn.is_closed().unwrap());

            // Closing releases it: further use sees it as closed.
            conn.close().unwrap();
            assert!(conn.is_closed().unwrap());

            drop(cursor);
        });
    }

    /// A plain query error does *not* poison the connection: after the caller
    /// rolls back, the (now-clean) connection returns to the pool.
    #[test]
    fn pooled_connection_returns_to_pool_after_query_error_and_rollback() {
        let Some(dsn) = test_dsn() else {
            eprintln!("skipping: set SYNAPSE_TEST_POSTGRES_DSN to run");
            return;
        };

        let pool = create_pool(&dsn, 1).unwrap();
        let obj = runtime().block_on(async { pool.get().await.unwrap() });

        Python::initialize();
        Python::attach(move |py| {
            let conn = Connection::new(obj);
            let cursor = conn.cursor().unwrap();
            // A bad statement aborts the transaction but leaves the connection
            // usable, exactly as psycopg2 does.
            cursor
                .execute(py, "SELECT * FROM does_not_exist", None)
                .unwrap_err();
            // The driver's job on failure: roll back, which clears the txn.
            conn.rollback(py).unwrap();
            drop(cursor);
            drop(conn);
        });

        assert_eq!(pool.status().size, 1);
        assert_eq!(pool.status().available, 1);
    }
}
