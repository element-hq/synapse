//! Tracks the result of the cursor's most recent `execute`.
//!
//! [`tokio_postgres`] returns rows as a [`RowStream`] that is consumed lazily,
//! so a cursor only ever holds onto the *current* query's stream plus the
//! metadata (column names, rowcount) derived from it.
//!
//! The lifecycle is modelled as an explicit state machine ([`CursorQueryState`])
//! so that illegal field combinations are unrepresentable, and so that fetching
//! after the result set has been reported exhausted is a clean error rather
//! than undefined behaviour (re-polling a finished stream surfaces as a
//! spurious "connection closed"). The row stream is [`Fuse`]d, which lets an
//! exhausted-but-not-yet-reported stream sit safely in the `Active` state: a
//! fused stream keeps yielding `None` instead of re-polling its completed
//! inner stream.
//!
//! ## Generic over the stream
//!
//! The state machine is generic over the underlying stream type `S`, defaulting
//! to [`RowStream`] (the only stream used in production). The
//! [`CursorRowStream`] trait abstracts the three things the logic needs from a
//! stream beyond [`futures::Stream`] itself — the affected-row count, how to
//! turn a row into a Python tuple, and how to render a stream error as a
//! `PyErr`. This is what lets the state-machine logic (exhaustion handling,
//! state transitions, error recovery) be unit-tested against an in-memory fake
//! stream with no live Postgres server; see the tests at the bottom of the
//! module.

use std::{mem, pin::Pin};

use futures::{stream::Fuse, StreamExt, TryStreamExt};
use pyo3::{
    exceptions::PyRuntimeError,
    marker::Ungil,
    types::{PyInt, PyTuple},
    Bound, PyErr, PyResult, Python,
};
use tokio_postgres::RowStream;

use crate::database::postgres::{
    errors::pg_err_to_py,
    helpers::{BlockingPostgres, BlockingPostgresStream as _},
    value::pg_row_to_py,
};

/// The capabilities the cursor state machine needs from the underlying row
/// stream, beyond [`futures::Stream`] itself.
///
/// Implemented for [`tokio_postgres::RowStream`] in production; the unit tests
/// provide an in-memory implementation so the state-machine logic can be
/// exercised without a live database.
///
/// The bounds (`Send` + [`Ungil`] on the stream and its row/error types) are
/// what let the rows and errors cross the GIL-release boundary in the
/// `block_on*` helpers.
pub trait CursorRowStream:
    futures::Stream<Item = Result<Self::Row, Self::Error>> + Send + Ungil + Sized
{
    /// A single result row.
    type Row: Send + Ungil;
    /// The stream's error type.
    type Error: Send + Ungil;

    /// The command tag's affected-row count, valid only once the stream has
    /// been fully drained. Maps to [`RowStream::rows_affected`].
    fn rows_affected(&self) -> Option<u64>;

    /// Convert a yielded row into a Python tuple, one element per column.
    fn row_to_py<'py>(py: Python<'py>, row: &Self::Row) -> PyResult<Bound<'py, PyTuple>>;

    /// Build the Python error raised when the stream yields an error.
    fn stream_err(err: &Self::Error) -> PyErr;
}

impl CursorRowStream for RowStream {
    type Row = tokio_postgres::Row;
    type Error = tokio_postgres::Error;

    fn rows_affected(&self) -> Option<u64> {
        RowStream::rows_affected(self)
    }

    fn row_to_py<'py>(py: Python<'py>, row: &Self::Row) -> PyResult<Bound<'py, PyTuple>> {
        pg_row_to_py(py, row)
    }

    fn stream_err(err: &Self::Error) -> PyErr {
        // Route through the shared mapping so a server error surfacing while
        // the result stream is drained (e.g. a constraint violation on an
        // `INSERT`) becomes the right DBAPI2 exception with its `pgcode`, just
        // as it would if it surfaced at `execute` time.
        pg_err_to_py(err)
    }
}

/// A live row stream, fused so that polling it after completion returns `None`
/// rather than re-polling the finished inner stream.
type FusedStream<S> = Pin<Box<Fuse<S>>>;

/// The lifecycle of the cursor's most recent query.
pub enum CursorQueryState<S = RowStream> {
    /// No query has been executed yet, or the previous result set was reset by
    /// the next `execute`. Fetching is an error.
    Idle,
    /// A query is in flight; rows can be fetched. Exhaustion has not yet been
    /// reported to the caller.
    Active {
        /// Live row stream for the current query.
        stream: FusedStream<S>,
        /// Column names for the result set (empty for a DML statement).
        /// Exposed to Python via [`CursorQueryState::description`].
        description: Vec<String>,
    },
    /// The result set has been fully consumed and exhaustion reported — a fetch
    /// returned `None`/`[]`, or `fetch_all`/`rowcount` drained it. Any further
    /// `fetch_*` is a programming error; `rowcount` still returns the count.
    Closed {
        /// Column names for the result set, carried over from `Active` so they
        /// survive once the rows are gone (PEP-249 keeps `description`
        /// available after the rows have been fetched). Exposed to Python via
        /// [`CursorQueryState::description`].
        description: Vec<String>,
        /// PEP-249 `rowcount` from the command tag, if it was captured.
        rowcount: Option<u64>,
    },
}

// Implemented by hand rather than derived: a derived `Default` would add an
// `S: Default` bound (which `RowStream` doesn't satisfy), even though the
// default state (`Idle`) doesn't depend on `S` at all. The hand-written impl is
// therefore *not* equivalent to the derive clippy suggests, so we silence it.
#[allow(clippy::derivable_impls)]
impl<S> Default for CursorQueryState<S> {
    fn default() -> Self {
        Self::Idle
    }
}

impl<S: CursorRowStream> CursorQueryState<S> {
    /// A fresh state with no query yet run.
    pub fn new() -> Self {
        Self::Idle
    }

    /// Reset to `Idle`, discarding any previous result set. Called at the
    /// start of every `execute`.
    pub fn new_query(&mut self) {
        *self = Self::Idle;
    }

    /// Record the stream and column metadata for a newly-started query.
    ///
    /// `description` is the list of column names (empty for a DML statement);
    /// taking it as a plain `Vec<String>` rather than the driver's column
    /// metadata keeps this layer decoupled from `tokio_postgres::Column` and so
    /// testable.
    pub fn on_query_start(&mut self, stream: S, description: Vec<String>) {
        *self = Self::Active {
            stream: Box::pin(stream.fuse()),
            description,
        };
    }

    /// Pull the next row from the stream, or `None` once it's exhausted.
    ///
    /// Returning `None` reports exhaustion and moves the cursor to `Closed`, so
    /// a subsequent fetch is an error. On a stream error the cursor is reset to
    /// `Idle` and the error surfaced to Python.
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let Self::Active {
            stream,
            description,
        } = self
        else {
            return Err(self.fetch_after_end_err());
        };

        // `fetch_next_batch` blocks for its first row too, but then uses the
        // non-blocking `get_next_if_ready` to scoop up already-buffered rows
        // without releasing the GIL again. A single fetch has nothing to scoop,
        // so we just block directly rather than bothering with that fast path.
        match stream.as_mut().block_on_next(py) {
            Some(Ok(row)) => Ok(Some(S::row_to_py(py, &row)?)),
            Some(Err(err)) => {
                *self = Self::Idle;
                Err(S::stream_err(&err))
            }
            None => {
                let rowcount = rows_affected(stream);
                *self = Self::Closed {
                    description: mem::take(description),
                    rowcount,
                };
                Ok(None)
            }
        }
    }

    /// Fetch the next batch of rows.
    ///
    /// This method will block on the first row if it's not immediately
    /// available, but will return any additional rows that are also ready
    /// without blocking.
    ///
    /// This is a convenience for Python code that wants to avoid the overhead
    /// of calling `fetch_one` repeatedly, but still wants to avoid blocking on
    /// the entire result set.
    ///
    /// An empty batch reports exhaustion and moves the cursor to `Closed`, so a
    /// subsequent fetch is an error. Note that the call that returns the final
    /// rows and the call that reports the empty batch may be distinct: a batch
    /// that runs into the end of the stream still returns the rows it has and
    /// leaves the report to the next call. On a stream error the cursor is
    /// reset to `Idle` and the error surfaced to Python.
    pub fn fetch_next_batch<'py>(
        &mut self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active { stream, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        match pull_ready_batch(stream, py, capacity) {
            // We have rows to return now; stay `Active`. If the end of the
            // stream was reached while draining, the fused stream will report
            // it as an empty batch on the next call.
            Ok(Some(buffer)) => Ok(buffer),
            // The stream was already exhausted: report it and close. `self` is
            // still `Active`, so re-borrow to move its fields into `Closed`.
            Ok(None) => {
                if let Self::Active {
                    stream,
                    description,
                } = self
                {
                    let rowcount = rows_affected(stream);
                    *self = Self::Closed {
                        description: mem::take(description),
                        rowcount,
                    };
                }
                Ok(Vec::new())
            }
            Err(err) => {
                *self = Self::Idle;
                Err(err)
            }
        }
    }

    /// Collect every remaining row into a `Vec`, draining the stream.
    ///
    /// Draining reports exhaustion and moves the cursor to `Closed`, so a
    /// subsequent fetch is an error. On a stream error the cursor is reset to
    /// `Idle` and the error surfaced to Python.
    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active {
            stream,
            description,
        } = self
        else {
            return Err(self.fetch_after_end_err());
        };

        let rows = match stream.as_mut().try_collect::<Vec<_>>().block_on(py) {
            Ok(rows) => rows,
            Err(err) => {
                *self = Self::Idle;
                return Err(S::stream_err(&err));
            }
        };
        let rows = rows
            .into_iter()
            .map(|row| S::row_to_py(py, &row))
            .collect::<PyResult<Vec<_>>>()?;

        let rowcount = rows_affected(stream);
        *self = Self::Closed {
            description: mem::take(description),
            rowcount,
        };

        Ok(rows)
    }

    /// Drive a non-row-returning statement's stream to completion, surfacing
    /// any execution error and capturing the affected-row count. A no-op unless
    /// the cursor is `Active`.
    ///
    /// `execute` calls this for statements with no result columns
    /// (INSERT/UPDATE/DELETE/DDL without RETURNING). Their `query_raw` stream is
    /// never fetched, but a `query_raw` stream only reports the server's
    /// response — including an error such as a constraint violation, and the
    /// affected-row count — once it is polled. Draining here makes such an error
    /// surface at `execute` time, as psycopg2 does, instead of being silently
    /// lost when the result is never fetched (most visibly under autocommit,
    /// where there is no later `commit` to surface it).
    pub fn finish_no_rows(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Self::Active {
            stream,
            description,
        } = self
        {
            if let Err(err) = drain_stream(stream.as_mut()).block_on(py) {
                *self = Self::Idle;
                return Err(S::stream_err(&err));
            }
            let rowcount = rows_affected(stream);
            *self = Self::Closed {
                description: mem::take(description),
                rowcount,
            };
        }
        Ok(())
    }

    /// Return the affected-row count, draining the stream first if needed.
    ///
    /// Unlike the `fetch_*` methods this is always valid: reading the rowcount
    /// of an already-exhausted (`Closed`) cursor returns the captured count
    /// rather than erroring, per PEP-249.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        // `rows_affected()` is only valid after the stream is drained, so drain
        // it here. This is OK as in Python the rowcount should only be accessed
        // for queries that DO NOT return rows, e.g. INSERT, UPDATE, DELETE.
        self.finish_no_rows(py)?;

        match self {
            Self::Closed {
                rowcount: Some(rowcount),
                ..
            } => Ok(PyInt::new(py, *rowcount)),
            // No rowcount known: either we're `Idle` (no query run, or reset by
            // an earlier error), or `Closed` with a command tag that carried no
            // count. The stream is always drained by the block above before we
            // get here, so "not yet drained" isn't a case. PEP-249 says -1.
            _ => Ok(PyInt::new(py, -1)),
        }
    }

    /// The column names of the current result set, or `None` when there is no
    /// row-returning result set to describe.
    ///
    /// This backs the PEP-249 `Cursor.description`. It is `None` in the `Idle`
    /// state (no query has run yet, or an error reset the cursor) and also for
    /// a statement that returns no columns — e.g. an `INSERT`/`UPDATE`/`DELETE`
    /// without `RETURNING` — matching psycopg2, which reports `description` as
    /// `None` for such statements. For a row-returning statement the names stay
    /// available after the rows have been fetched (the `Closed` state), as
    /// PEP-249 requires.
    ///
    /// Only the column *names* are tracked (see `on_query_start`); it is the
    /// caller's job to shape them into PEP-249's 7-tuples.
    pub fn description(&self) -> Option<&[String]> {
        let columns = match self {
            Self::Active { description, .. } | Self::Closed { description, .. } => description,
            Self::Idle => return None,
        };

        // A statement that returns no columns (DML) has an empty column list;
        // PEP-249 / psycopg2 report `description` as `None` in that case.
        if columns.is_empty() {
            None
        } else {
            Some(columns)
        }
    }

    /// Record a completed command that produced no fetchable rows, retaining
    /// its affected-row count for `rowcount`.
    ///
    /// Used by `executemany`, which runs a statement repeatedly for its side
    /// effects: there is no result set to fetch (`description` is `None` and
    /// any `fetch_*` errors as exhausted), but the (summed) rowcount is kept.
    pub fn on_command_complete(&mut self, rowcount: Option<u64>) {
        *self = Self::Closed {
            description: Vec::new(),
            rowcount,
        };
    }

    /// The error to raise when a `fetch_*` method finds no rows available,
    /// distinguishing "no query was ever run" from "the result set has already
    /// been exhausted". Only meaningful for the non-`Active` states.
    fn fetch_after_end_err(&self) -> PyErr {
        match self {
            Self::Closed { .. } => {
                PyRuntimeError::new_err("cannot fetch: the result set is already exhausted")
            }
            _ => PyRuntimeError::new_err("no active query"),
        }
    }
}

/// Pull the first row (blocking until it arrives) plus any rows that are
/// already buffered, without blocking again.
///
/// Returns `Ok(None)` if the stream is already exhausted (there was no first
/// row). End-of-stream reached while draining the ready rows is *not* reported
/// here — we return the rows we have and leave the empty-batch report to a
/// later call; the fused stream makes re-polling safe.
///
/// Expects the stream of an `Active` cursor; `fetch_next_batch` is its only
/// caller and only invokes it in that state. A mid-drain stream error discards
/// the partially-built buffer and propagates, leaving the caller to reset.
fn pull_ready_batch<'py, S: CursorRowStream>(
    stream: &mut FusedStream<S>,
    py: Python<'py>,
    capacity: usize,
) -> PyResult<Option<Vec<Bound<'py, PyTuple>>>> {
    // Wait for at least one row.
    let first = match stream.as_mut().block_on_next(py) {
        Some(Ok(row)) => S::row_to_py(py, &row)?,
        Some(Err(err)) => return Err(S::stream_err(&err)),
        None => return Ok(None),
    };

    let mut buffer = Vec::with_capacity(capacity);
    buffer.push(first);

    loop {
        match stream.as_mut().get_next_if_ready() {
            // Not ready yet: return what we have (non-empty — we pushed the
            // first row above).
            None => break,
            // A row is already available.
            Some(Some(Ok(row))) => buffer.push(S::row_to_py(py, &row)?),
            // The stream errored.
            Some(Some(Err(err))) => return Err(S::stream_err(&err)),
            // End of stream: stop, leaving the empty-batch report to the next
            // call.
            Some(None) => break,
        }
    }

    Ok(Some(buffer))
}

/// The command tag's affected-row count, valid only once the (fused) stream has
/// been fully drained.
fn rows_affected<S: CursorRowStream>(stream: &FusedStream<S>) -> Option<u64> {
    // The first `get_ref` unwraps the `Pin`, the second the `Fuse`.
    stream.as_ref().get_ref().get_ref().rows_affected()
}

/// Consume and discard every row of a stream, propagating any error. Used to
/// reach the trailing command-complete message that carries the rowcount.
async fn drain_stream<S: CursorRowStream>(mut stream: Pin<&mut Fuse<S>>) -> Result<(), S::Error> {
    while let Some(row) = stream.next().await {
        row?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    //! The state machine is driven here by an in-memory [`FakeStream`] instead
    //! of a live [`RowStream`], so these tests cover the exhaustion handling,
    //! the state transitions, and the error recovery without any Postgres
    //! server. Rows are simple integer tuples; that's enough to assert the
    //! plumbing carries values through `row_to_py`.
    //!
    //! Note the one thing they deliberately *don't* cover: [`FakeStream`]'s
    //! `poll_next` is always `Poll::Ready`, so the GIL-release / blocking path
    //! in the `block_on*` helpers is never exercised here (it has its own tests
    //! in `helpers`). These tests are purely about the state-machine logic.

    use std::{
        collections::VecDeque,
        task::{Context, Poll},
    };

    use pyo3::prelude::*;
    use tokio::runtime::{EnterGuard, Runtime};

    use super::*;

    /// Enter a process-wide runtime on the current thread, returning the guard
    /// to hold for the test's duration.
    ///
    /// The state machine's blocking calls take the runtime from the thread's
    /// context via [`Handle::current`](tokio::runtime::Handle::current), so each
    /// test thread must have a runtime entered — just as production threads do
    /// (see [`super::super::helpers::BlockingPostgres`]). [`FakeStream`] is
    /// always `Poll::Ready`, so nothing here actually parks on the runtime; it
    /// only needs to exist for `Handle::current` to resolve.
    fn enter_runtime() -> EnterGuard<'static> {
        static RUNTIME: std::sync::OnceLock<Runtime> = std::sync::OnceLock::new();
        RUNTIME
            .get_or_init(|| {
                tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(1)
                    .enable_all()
                    .build()
                    .unwrap()
            })
            .enter()
    }

    /// A result row: a list of integer cells.
    #[derive(Clone, Debug, PartialEq)]
    struct FakeRow(Vec<i64>);

    /// A stream error carrying a message we can assert on.
    #[derive(Debug)]
    struct FakeError(&'static str);

    /// An in-memory stream of pre-canned items, plus the `rows_affected` value
    /// the command tag would report once it's drained.
    struct FakeStream {
        items: VecDeque<Result<FakeRow, FakeError>>,
        rows_affected: Option<u64>,
    }

    impl futures::Stream for FakeStream {
        type Item = Result<FakeRow, FakeError>;

        fn poll_next(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
            // Every item is immediately available; `None` once drained.
            Poll::Ready(self.items.pop_front())
        }
    }

    impl CursorRowStream for FakeStream {
        type Row = FakeRow;
        type Error = FakeError;

        fn rows_affected(&self) -> Option<u64> {
            self.rows_affected
        }

        fn row_to_py<'py>(py: Python<'py>, row: &FakeRow) -> PyResult<Bound<'py, PyTuple>> {
            PyTuple::new(py, &row.0)
        }

        fn stream_err(err: &FakeError) -> PyErr {
            PyRuntimeError::new_err(format!("fake stream error: {}", err.0))
        }
    }

    /// Build an `Active` state over the given rows (all `Ok`) with the given
    /// command-tag rowcount.
    fn active_with(
        rows: Vec<Vec<i64>>,
        rows_affected: Option<u64>,
    ) -> CursorQueryState<FakeStream> {
        let mut state = CursorQueryState::<FakeStream>::new();
        state.on_query_start(
            FakeStream {
                items: rows.into_iter().map(|r| Ok(FakeRow(r))).collect(),
                rows_affected,
            },
            vec!["col".to_string()],
        );
        state
    }

    /// Assert a `Bound<PyTuple>` decodes to the given integer cells.
    fn assert_tuple(tuple: &Bound<'_, PyTuple>, expected: &[i64]) {
        let got: Vec<i64> = tuple.extract().unwrap();
        assert_eq!(got, expected);
    }

    #[test]
    fn fetch_one_yields_rows_then_reports_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1], vec![2]], Some(0));

            assert_tuple(&state.fetch_one(py).unwrap().unwrap(), &[1]);
            assert_tuple(&state.fetch_one(py).unwrap().unwrap(), &[2]);
            // The end of the stream is reported as `None`, closing the cursor.
            assert!(state.fetch_one(py).unwrap().is_none());
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // A fetch after exhaustion is a clean, specific error.
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("already exhausted"), "{err}");
        });
    }

    #[test]
    fn fetch_one_on_idle_errors_with_no_active_query() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("no active query"), "{err}");
        });
    }

    #[test]
    fn finish_no_rows_drains_and_captures_rowcount() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // A non-row-returning statement: no rows to yield, a command-tag
            // rowcount, and an empty column list.
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::new(),
                    rows_affected: Some(3),
                },
                vec![],
            );

            state.finish_no_rows(py).unwrap();
            assert!(matches!(
                state,
                CursorQueryState::Closed {
                    rowcount: Some(3),
                    ..
                }
            ));
        });
    }

    #[test]
    fn finish_no_rows_surfaces_execution_error() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // The statement fails server-side (e.g. a constraint violation),
            // surfaced only when the stream is polled — which `finish_no_rows`
            // does, so the error is raised rather than silently swallowed.
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from([Err(FakeError("boom"))]),
                    rows_affected: None,
                },
                vec![],
            );

            let err = state.finish_no_rows(py).unwrap_err();
            assert!(err.to_string().contains("boom"), "{err}");
            // A stream error resets the cursor to `Idle`.
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    #[test]
    fn finish_no_rows_on_idle_is_a_noop() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            state.finish_no_rows(py).unwrap();
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    #[test]
    fn fetch_all_drains_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1, 2], vec![3, 4]], Some(0));

            let rows = state.fetch_all(py).unwrap();
            assert_eq!(rows.len(), 2);
            assert_tuple(&rows[0], &[1, 2]);
            assert_tuple(&rows[1], &[3, 4]);
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // Draining again is an error: the result set is gone.
            assert!(state
                .fetch_all(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    #[test]
    fn fetch_all_on_empty_stream_returns_empty_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![], Some(0));
            assert!(state.fetch_all(py).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    #[test]
    fn fetch_one_after_fetch_all_errors() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1]], Some(0));
            let _ = state.fetch_all(py).unwrap();
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    #[test]
    fn fetch_one_then_fetch_all_continues_partially_consumed_stream() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1], vec![2], vec![3]], Some(0));

            // Consume one row with `fetch_one`...
            assert_tuple(&state.fetch_one(py).unwrap().unwrap(), &[1]);
            // ...then `fetch_all` should pick up the *rest*, not restart.
            let rest = state.fetch_all(py).unwrap();
            assert_eq!(rest.len(), 2);
            assert_tuple(&rest[0], &[2]);
            assert_tuple(&rest[1], &[3]);
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    #[test]
    fn fetch_all_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // First row ok, then an error part-way through the collection.
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Ok(FakeRow(vec![1])), Err(FakeError("kaboom"))]),
                    rows_affected: Some(0),
                },
                vec!["col".to_string()],
            );

            let err = state.fetch_all(py).unwrap_err();
            assert!(err.to_string().contains("kaboom"), "{err}");
            // A mid-stream error resets to `Idle`, exactly like `fetch_one`.
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state
                .fetch_all(py)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    #[test]
    fn rowcount_after_drain_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Err(FakeError("drain failed"))]),
                    rows_affected: Some(7),
                },
                vec!["col".to_string()],
            );

            // Draining for the rowcount hits the error and surfaces it.
            let err = state.rowcount(py).unwrap_err();
            assert!(err.to_string().contains("drain failed"), "{err}");
            // The cursor is reset to `Idle`, so no stale count is retained: a
            // subsequent rowcount is the PEP-249 "unknown" sentinel, -1.
            assert!(matches!(state, CursorQueryState::Idle));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
        });
    }

    #[test]
    fn stream_error_resets_to_idle_and_surfaces() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // A stream whose second item is an error.
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Ok(FakeRow(vec![1])), Err(FakeError("boom"))]),
                    rows_affected: None,
                },
                vec!["col".to_string()],
            );

            assert_tuple(&state.fetch_one(py).unwrap().unwrap(), &[1]);
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("boom"), "{err}");

            // After an error the cursor is reset to `Idle`, so the next fetch
            // reports "no active query" rather than "exhausted".
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    #[test]
    fn rowcount_is_minus_one_before_any_query() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
        });
    }

    #[test]
    fn rowcount_drains_and_reports_command_tag() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // A DML-style result: no rows, but a command tag of 5 affected rows.
            let mut state = active_with(vec![], Some(5));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 5);
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // Reading it again on a `Closed` cursor returns the retained count,
            // rather than erroring like a `fetch_*` would.
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 5);
        });
    }

    #[test]
    fn rowcount_drains_remaining_rows_of_a_select() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // Even with rows pending, asking for the rowcount drains them and
            // then reports the command tag.
            let mut state = active_with(vec![vec![1], vec![2]], Some(2));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 2);
            // Stream is drained, so fetching is now an "exhausted" error.
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    #[test]
    fn description_is_none_before_any_query() {
        let state = CursorQueryState::<FakeStream>::new();
        assert!(state.description().is_none());
    }

    #[test]
    fn description_reports_columns_while_active_and_after_close() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // `active_with` builds an `Active` state whose columns are `["col"]`.
            let mut state = active_with(vec![vec![1]], Some(0));
            assert_eq!(state.description(), Some(["col".to_string()].as_slice()));

            // Draining the rows moves the cursor to `Closed`, but the column
            // names survive so `description` is still available (PEP-249).
            let _ = state.fetch_all(py).unwrap();
            assert!(matches!(state, CursorQueryState::Closed { .. }));
            assert_eq!(state.description(), Some(["col".to_string()].as_slice()));
        });
    }

    #[test]
    fn description_is_none_for_a_column_less_result() {
        // A DML statement yields no columns; `description` should be `None`
        // even while the (empty) result set is `Active`.
        let mut state = CursorQueryState::<FakeStream>::new();
        state.on_query_start(
            FakeStream {
                items: VecDeque::new(),
                rows_affected: Some(3),
            },
            vec![],
        );
        assert!(state.description().is_none());
    }

    #[test]
    fn description_is_none_after_an_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Err(FakeError("boom"))]),
                    rows_affected: None,
                },
                vec!["col".to_string()],
            );

            // The error resets the cursor to `Idle`, dropping the columns.
            let _ = state.fetch_one(py).unwrap_err();
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state.description().is_none());
        });
    }

    #[test]
    fn on_command_complete_retains_rowcount_without_a_result_set() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            // Simulate a completed `executemany` that affected 5 rows.
            state.on_command_complete(Some(5));

            assert!(matches!(state, CursorQueryState::Closed { .. }));
            // The rowcount is retained, but there is nothing to describe or
            // fetch.
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 5);
            assert!(state.description().is_none());
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    #[test]
    fn on_command_complete_with_no_count_reports_minus_one() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            // An empty `executemany` batch: nothing ran, so no count.
            state.on_command_complete(None);
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
            assert!(state.description().is_none());
        });
    }

    #[test]
    fn new_query_resets_an_active_cursor() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1]], Some(0));
            state.new_query();
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    // ------------------------------------------------------------------
    // fetch_next_batch
    // ------------------------------------------------------------------

    #[test]
    fn fetch_next_batch_returns_ready_rows_then_defers_the_empty_report() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // With an always-ready stream every row is immediately available,
            // so the first batch drains the whole result set...
            let mut state = active_with(vec![vec![1], vec![2], vec![3]], Some(0));
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 3);
            assert_tuple(&batch[0], &[1]);
            assert_tuple(&batch[2], &[3]);

            // ...but the cursor stays `Active`: hitting the end of the stream
            // while draining does *not* report exhaustion in the same call.
            assert!(matches!(state, CursorQueryState::Active { .. }));

            // The *next* call is the one that reports the empty batch and
            // moves to `Closed`.
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // And a further fetch is the "already exhausted" error.
            assert!(state
                .fetch_next_batch(py, 100)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    #[test]
    fn fetch_next_batch_empty_stream_reports_exhaustion_immediately() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![], Some(0));
            // No first row at all -> empty batch and straight to `Closed`.
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    #[test]
    fn fetch_next_batch_capacity_is_a_hint_not_a_limit() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // `capacity` only sizes the buffer; all ready rows are returned
            // even when there are more of them than the hint.
            let mut state = active_with(vec![vec![1], vec![2], vec![3]], Some(0));
            let batch = state.fetch_next_batch(py, 1).unwrap();
            assert_eq!(batch.len(), 3);
        });
    }

    #[test]
    fn fetch_next_batch_without_query_errors() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            assert!(state
                .fetch_next_batch(py, 100)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    #[test]
    fn fetch_next_batch_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Err(FakeError("splat"))]),
                    rows_affected: Some(0),
                },
                vec!["col".to_string()],
            );

            let err = state.fetch_next_batch(py, 100).unwrap_err();
            assert!(err.to_string().contains("splat"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    #[test]
    fn fetch_next_batch_error_after_first_row_discards_buffer_and_resets() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // First row is fine, but the stream errors while the batch is still
            // being drained (both items are immediately ready here).
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Ok(FakeRow(vec![1])), Err(FakeError("midway"))]),
                    rows_affected: Some(0),
                },
                vec!["col".to_string()],
            );

            // The error wins: the already-buffered first row is discarded
            // rather than returned, and the cursor resets to `Idle`.
            let err = state.fetch_next_batch(py, 100).unwrap_err();
            assert!(err.to_string().contains("midway"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    #[test]
    fn fetch_next_batch_interleaves_with_fetch_one() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            let mut state = active_with(vec![vec![1], vec![2], vec![3]], Some(0));
            // Take one row directly...
            assert_tuple(&state.fetch_one(py).unwrap().unwrap(), &[1]);
            // ...then the batch picks up the rest of the (still partially
            // consumed) stream.
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 2);
            assert_tuple(&batch[0], &[2]);
            assert_tuple(&batch[1], &[3]);
        });
    }

    /// A stream that can report "not ready yet" mid-way through, so we can
    /// exercise the *partial batch* behaviour the always-ready [`FakeStream`]
    /// can't reach: `fetch_next_batch` should block for the first row, then
    /// return only the rows that are immediately ready.
    struct SteppedStream {
        steps: VecDeque<Step>,
    }

    enum Step {
        /// A row that is ready right now.
        Ready(FakeRow),
        /// One poll that returns `Pending` (re-waking itself so a blocking poll
        /// makes progress), modelling a row that isn't buffered yet.
        Pending,
    }

    impl futures::Stream for SteppedStream {
        type Item = Result<FakeRow, FakeError>;

        fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
            match self.steps.pop_front() {
                Some(Step::Ready(row)) => Poll::Ready(Some(Ok(row))),
                Some(Step::Pending) => {
                    // Wake immediately so a blocking `block_on` re-polls and
                    // makes progress, while a single `now_or_never` poll still
                    // observes `Pending`.
                    cx.waker().wake_by_ref();
                    Poll::Pending
                }
                None => Poll::Ready(None),
            }
        }
    }

    impl CursorRowStream for SteppedStream {
        type Row = FakeRow;
        type Error = FakeError;

        fn rows_affected(&self) -> Option<u64> {
            Some(0)
        }

        fn row_to_py<'py>(py: Python<'py>, row: &FakeRow) -> PyResult<Bound<'py, PyTuple>> {
            PyTuple::new(py, &row.0)
        }

        fn stream_err(err: &FakeError) -> PyErr {
            PyRuntimeError::new_err(format!("stepped stream error: {}", err.0))
        }
    }

    #[test]
    fn fetch_next_batch_returns_partial_batch_when_next_is_not_ready() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // Row 1 is ready; row 2 is "not ready yet" (a Pending poll); row 3
            // is ready behind it.
            let mut state = CursorQueryState::<SteppedStream>::new();
            state.on_query_start(
                SteppedStream {
                    steps: VecDeque::from(vec![
                        Step::Ready(FakeRow(vec![1])),
                        Step::Pending,
                        Step::Ready(FakeRow(vec![3])),
                    ]),
                },
                vec!["col".to_string()],
            );

            // First batch: got row 1, then the stream wasn't ready, so the
            // batch is returned with just that one row. The cursor stays open.
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 1);
            assert_tuple(&batch[0], &[1]);
            assert!(matches!(state, CursorQueryState::Active { .. }));

            // Second batch: the next row is now available.
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 1);
            assert_tuple(&batch[0], &[3]);

            // Third call drains to the end and reports the empty batch.
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    #[test]
    fn fetch_next_batch_blocks_for_a_pending_first_row() {
        Python::initialize();
        Python::attach(|py| {
            let _guard = enter_runtime();
            // The very first poll is `Pending`, so `block_on_next` must take its
            // blocking path to get the first row (rather than the fast path).
            let mut state = CursorQueryState::<SteppedStream>::new();
            state.on_query_start(
                SteppedStream {
                    steps: VecDeque::from(vec![
                        Step::Pending,
                        Step::Ready(FakeRow(vec![1])),
                        Step::Ready(FakeRow(vec![2])),
                    ]),
                },
                vec!["col".to_string()],
            );

            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 2);
            assert_tuple(&batch[0], &[1]);
            assert_tuple(&batch[1], &[2]);
        });
    }
}
