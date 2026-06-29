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

use crate::database::postgres::{helpers::BlockingPostgres, value::pg_row_to_py};

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
        PyRuntimeError::new_err(format!("error fetching row from postgres: {err}"))
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
        ///
        /// TODO: currently write-only; kept to back a future PEP-249
        /// `Cursor.description` accessor.
        description: Vec<String>,
    },
    /// The result set has been fully consumed and exhaustion reported — a fetch
    /// returned `None`/`[]`, or `fetch_all`/`rowcount` drained it. Any further
    /// `fetch_*` is a programming error; `rowcount` still returns the count.
    Closed {
        /// Column names for the result set, carried over from `Active` so they
        /// survive once the rows are gone.
        ///
        /// TODO: currently write-only; like `Active::description` it is kept to
        /// back a future PEP-249 `Cursor.description` accessor. `#[allow]`d
        /// until that reader lands rather than dropped, so the column metadata
        /// isn't silently lost at the `Active` -> `Closed` transition.
        #[allow(dead_code)]
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

        // Unlike `fetch_next_batch` (which uses `block_on_next` to grab any
        // already-buffered rows without releasing the GIL), a single fetch has
        // to wait for the one row either way, so we block directly rather than
        // bothering with the non-blocking fast path.
        match stream.as_mut().next().block_on(py) {
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

    /// Return the affected-row count, draining the stream first if needed.
    ///
    /// Unlike the `fetch_*` methods this is always valid: reading the rowcount
    /// of an already-exhausted (`Closed`) cursor returns the captured count
    /// rather than erroring, per PEP-249.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        // `rows_affected()` is only valid after the stream is drained, so we
        // drain it here. This is OK as in Python the rowcount should only be
        // accessed for queries that DO NOT return rows, e.g. INSERT, UPDATE,
        // DELETE.
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

    use super::*;

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
            let mut state = CursorQueryState::<FakeStream>::new();
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("no active query"), "{err}");
        });
    }

    #[test]
    fn fetch_all_drains_and_closes() {
        Python::initialize();
        Python::attach(|py| {
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
            let mut state = active_with(vec![], Some(0));
            assert!(state.fetch_all(py).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    #[test]
    fn fetch_one_after_fetch_all_errors() {
        Python::initialize();
        Python::attach(|py| {
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
            let mut state = CursorQueryState::<FakeStream>::new();
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
        });
    }

    #[test]
    fn rowcount_drains_and_reports_command_tag() {
        Python::initialize();
        Python::attach(|py| {
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
    fn new_query_resets_an_active_cursor() {
        Python::initialize();
        Python::attach(|py| {
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
}
