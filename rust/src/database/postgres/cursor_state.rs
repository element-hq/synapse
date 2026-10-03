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

//! Tracks the result of the cursor's most recent `execute`.
//!
//! [`tokio_postgres`] returns rows as a [`RowStream`] that is consumed lazily,
//! so a cursor only ever holds the current query's stream plus the metadata
//! (column names, rowcount) derived from it.
//!
//! The lifecycle is an explicit state machine ([`CursorQueryState`]), so
//! impossible field combinations cannot be represented and a fetch after the
//! result set was reported exhausted is a specific error. Polling a finished
//! `RowStream` again would instead fail with a misleading "connection closed".
//! The stream is [`Fuse`]d, so a stream that has ended but whose end has not
//! yet been reported can stay in the `Active` state: a fused stream keeps
//! yielding `None` instead of polling its completed inner stream.
//!
//! ## Generic over the stream
//!
//! The state machine is generic over the stream type `S`, defaulting to
//! [`RowStream`], the only stream used in production. The [`CursorRowStream`]
//! trait covers what the logic needs from a stream beyond [`futures::Stream`]:
//! the affected-row count, how to turn a row into a Python tuple, and how to
//! turn a stream error into a `PyErr`. That lets the state transitions,
//! exhaustion handling and error recovery be unit-tested against an in-memory
//! stream with no Postgres server. See the tests at the bottom of the module.

use std::{mem, pin::Pin};

use futures::{stream::Fuse, StreamExt, TryStreamExt};
use pyo3::{
    exceptions::PyRuntimeError,
    marker::Ungil,
    types::{PyInt, PyTuple},
    Bound, PyErr, PyResult, Python,
};
use tokio::runtime::Handle;
use tokio_postgres::RowStream;

use crate::database::postgres::{
    helpers::{BlockingPostgres, BlockingPostgresStream as _},
    pg_err_to_py,
    value::pg_row_to_py,
};

/// What the cursor state machine needs from the row stream beyond
/// [`futures::Stream`] itself.
///
/// Implemented for [`tokio_postgres::RowStream`] in production. The unit tests
/// implement it for an in-memory stream.
///
/// The `Send` and [`Ungil`] bounds on the stream and its row and error types
/// let them cross the GIL release in the `block_on*` helpers.
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
    fn stream_err(err: Self::Error) -> PyErr;
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

    fn stream_err(err: Self::Error) -> PyErr {
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
    /// A query has been executed and rows can be fetched. The end of the
    /// result set has not yet been reported to the caller.
    Active {
        /// Live row stream for the current query.
        stream: FusedStream<S>,
        /// The runtime the blocking `fetch_*` calls drive the stream on. This
        /// is the extension's shared tokio runtime, see
        /// [`crate::tokio_runtime`].
        handle: Handle,
        /// Column names of the result set, empty for a statement that returns
        /// no rows.
        ///
        /// TODO: not read yet. Kept for a PEP-249 `Cursor.description`
        /// accessor.
        description: Vec<String>,
    },
    /// The result set has been fully consumed and its end reported: a fetch
    /// returned `None` or an empty batch, or `fetch_all`/`rowcount` drained
    /// it. A further `fetch_*` is an error. `rowcount` still returns the count.
    Closed {
        /// Column names of the result set, carried over from `Active`. See the
        /// TODO there.
        description: Vec<String>,
        /// PEP-249 `rowcount` from the command tag, if it was captured.
        rowcount: Option<u64>,
    },
}

// Not derived: `#[derive(Default)]` would add an `S: Default` bound, which
// `RowStream` doesn't satisfy, even though `Idle` holds no `S`. The derive
// clippy suggests is therefore not equivalent, hence the allow.
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

    /// Record the stream and column names of a newly started query.
    ///
    /// `description` is the list of column names, empty for a statement that
    /// returns no rows. It is a plain `Vec<String>` rather than
    /// `tokio_postgres::Column` metadata so this module does not depend on the
    /// driver's types and can be tested without them. `handle` is the runtime
    /// the blocking `fetch_*` calls drive the stream on.
    pub fn on_query_start(&mut self, stream: S, description: Vec<String>, handle: Handle) {
        *self = Self::Active {
            stream: Box::pin(stream.fuse()),
            handle,
            description,
        };
    }

    /// Pull the next row from the stream, or `None` once it's exhausted.
    ///
    /// Returning `None` reports exhaustion and moves the cursor to `Closed`, so
    /// a subsequent fetch is an error. On a stream error the cursor is reset to
    /// `Idle` and the error surfaced to Python.
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        match stream.as_mut().block_on_next(py, handle) {
            Some(Ok(row)) => Ok(Some(S::row_to_py(py, &row)?)),
            Some(Err(err)) => {
                *self = Self::Idle;
                Err(S::stream_err(err))
            }
            None => {
                self.close_drained();
                Ok(None)
            }
        }
    }

    /// Fetch the next batch of rows.
    ///
    /// Blocks until the first row is available, then adds any further rows
    /// that are already buffered without blocking again. `capacity` sizes the
    /// returned buffer and does not cap the batch. This lets Python iterate a
    /// large result set without a call per row (`fetch_one`) and without
    /// waiting for the whole result set (`fetch_all`).
    ///
    /// An empty batch reports exhaustion and moves the cursor to `Closed`, so a
    /// subsequent fetch is an error. A batch that reaches the end of the stream
    /// still returns the rows it has, so the empty batch always comes from a
    /// later call. On a stream error the cursor is reset to `Idle` and the
    /// error surfaced to Python.
    pub fn fetch_next_batch<'py>(
        &mut self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        match pull_ready_batch(stream, py, capacity, handle) {
            // Rows to return, so stay `Active`. If the batch reached the end
            // of the stream, the fused stream reports that as an empty batch
            // on the next call.
            Ok(Some(buffer)) => Ok(buffer),
            // No first row: the stream was already exhausted.
            Ok(None) => {
                self.close_drained();
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
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        let rows = match stream.as_mut().try_collect::<Vec<_>>().block_on(py, handle) {
            Ok(rows) => rows,
            Err(err) => {
                *self = Self::Idle;
                return Err(S::stream_err(err));
            }
        };
        let rows = rows
            .into_iter()
            .map(|row| S::row_to_py(py, &row))
            .collect::<PyResult<Vec<_>>>()?;

        self.close_drained();
        Ok(rows)
    }

    /// Return the affected-row count, draining the stream first if needed.
    ///
    /// Unlike the `fetch_*` methods this is always valid: reading the rowcount
    /// of an already-exhausted (`Closed`) cursor returns the captured count
    /// rather than erroring, per PEP-249.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        // The command tag, and so the count, only arrives once the stream is
        // drained. Draining discards any unfetched rows, which is fine: callers
        // only read `rowcount` after INSERT, UPDATE or DELETE.
        if let Self::Active { stream, handle, .. } = self {
            if let Err(err) = drain_stream(stream.as_mut()).block_on(py, handle) {
                *self = Self::Idle;
                return Err(S::stream_err(err));
            }
            self.close_drained();
        }

        match self {
            Self::Closed {
                rowcount: Some(rowcount),
                ..
            } => Ok(PyInt::new(py, *rowcount)),
            // `Idle` (no query, or reset by an earlier error), or a command
            // tag without a count. PEP-249 says -1 for an unknown rowcount.
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

    /// Move an `Active` cursor to `Closed`, capturing the command tag's
    /// affected-row count. The count only arrives once the stream is drained,
    /// so callers drain the stream, or observe its end, first.
    ///
    /// A no-op in any other state.
    fn close_drained(&mut self) {
        if let Self::Active {
            stream,
            description,
            ..
        } = self
        {
            // The first `get_ref` unwraps the `Pin`, the second the `Fuse`.
            let rowcount = stream.as_ref().get_ref().get_ref().rows_affected();
            *self = Self::Closed {
                description: mem::take(description),
                rowcount,
            };
        }
    }
}

/// Block for the first row, then add any rows that are already buffered
/// without blocking again.
///
/// Returns `Ok(None)` if there is no first row because the stream is already
/// exhausted. Reaching the end of the stream after the first row is not
/// reported here. The rows collected so far are returned, and the next call
/// finds the fused stream still yielding `None`.
///
/// A stream error part way through discards the rows collected so far and is
/// returned. The caller resets the state.
fn pull_ready_batch<'py, S: CursorRowStream>(
    stream: &mut FusedStream<S>,
    py: Python<'py>,
    capacity: usize,
    handle: &Handle,
) -> PyResult<Option<Vec<Bound<'py, PyTuple>>>> {
    // Wait for at least one row.
    let first = match stream.as_mut().block_on_next(py, handle) {
        Some(Ok(row)) => S::row_to_py(py, &row)?,
        Some(Err(err)) => return Err(S::stream_err(err)),
        None => return Ok(None),
    };

    let mut buffer = Vec::with_capacity(capacity);
    buffer.push(first);

    loop {
        match stream.as_mut().get_next_if_ready() {
            // Not ready yet.
            None => break,
            Some(Some(Ok(row))) => buffer.push(S::row_to_py(py, &row)?),
            Some(Some(Err(err))) => return Err(S::stream_err(err)),
            // End of stream. The next call reports it as an empty batch.
            Some(None) => break,
        }
    }

    Ok(Some(buffer))
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
    //! These tests drive the state machine with in-memory streams instead of
    //! a live [`RowStream`], so they need no Postgres server. Rows are integer
    //! tuples, enough to check that values pass through `row_to_py`.
    //!
    //! [`FakeStream`] is always ready. [`SteppedStream`] can also return
    //! `Pending`, for the partial-batch and blocking paths of
    //! `fetch_next_batch`. The `block_on*` helpers themselves are tested in
    //! `helpers`.

    use std::{
        collections::VecDeque,
        task::{Context, Poll},
    };

    use pyo3::prelude::*;
    use tokio::runtime::Runtime;

    use super::*;

    /// A handle to a process-wide test runtime, standing in for the shared
    /// runtime production code passes to `on_query_start`.
    fn runtime_handle() -> Handle {
        static RUNTIME: std::sync::OnceLock<Runtime> = std::sync::OnceLock::new();
        RUNTIME
            .get_or_init(|| {
                tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(1)
                    .enable_all()
                    .build()
                    .unwrap()
            })
            .handle()
            .clone()
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
            // Every item is ready immediately, then `None`.
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

        fn stream_err(err: FakeError) -> PyErr {
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
            runtime_handle(),
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
                runtime_handle(),
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
                runtime_handle(),
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
                runtime_handle(),
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

    // ------------------------------------------------------------------
    // fetch_next_batch
    // ------------------------------------------------------------------

    #[test]
    fn fetch_next_batch_returns_ready_rows_then_defers_the_empty_report() {
        Python::initialize();
        Python::attach(|py| {
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
            // `capacity` only sizes the buffer. All ready rows are returned
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
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Err(FakeError("splat"))]),
                    rows_affected: Some(0),
                },
                vec!["col".to_string()],
                runtime_handle(),
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
            // First row is fine, but the stream errors while the batch is still
            // being drained (both items are immediately ready here).
            let mut state = CursorQueryState::<FakeStream>::new();
            state.on_query_start(
                FakeStream {
                    items: VecDeque::from(vec![Ok(FakeRow(vec![1])), Err(FakeError("midway"))]),
                    rows_affected: Some(0),
                },
                vec!["col".to_string()],
                runtime_handle(),
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

    /// A stream that can report "not ready yet" part way through, which the
    /// always-ready [`FakeStream`] cannot. Used to check that
    /// `fetch_next_batch` blocks for the first row and then returns only the
    /// rows that are immediately ready.
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

        fn stream_err(err: FakeError) -> PyErr {
            PyRuntimeError::new_err(format!("stepped stream error: {}", err.0))
        }
    }

    #[test]
    fn fetch_next_batch_returns_partial_batch_when_next_is_not_ready() {
        Python::initialize();
        Python::attach(|py| {
            // Row 1 is ready, row 2 is not ready yet (one Pending poll), and
            // row 3 is ready behind it.
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
                runtime_handle(),
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
                runtime_handle(),
            );

            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 2);
            assert_tuple(&batch[0], &[1]);
            assert_tuple(&batch[1], &[2]);
        });
    }
}
