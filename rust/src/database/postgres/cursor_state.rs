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
//! so a cursor only ever holds the current query's stream plus the column
//! names and rowcount derived from it.
//!
//! [`CursorQueryState`] models the lifecycle as an explicit state machine.
//! Field combinations that make no sense cannot be represented. A fetch after
//! the result set was reported exhausted raises a specific error. Polling a
//! finished `RowStream` again would instead fail with a misleading
//! "connection closed".
//!
//! The stream must be a [`FusedStream`]. A fused stream keeps yielding [`None`]
//! after its end instead of polling its finished inner stream. So a stream that
//! has ended can stay [`CursorQueryState::Active`] until its end is reported.
//!
//! ## Generic over the stream
//!
//! The state machine is generic over the stream type `S`. It defaults to
//! [`PgRowStream`], the only stream used in production. The [`CursorRowStream`]
//! trait covers what the logic needs from a stream beyond [`futures::Stream`]:
//! the affected-row count, how to turn a row into a Python tuple, and how to
//! turn a stream error into a [`PyErr`]. That lets the unit tests use an
//! in-memory stream instead of a Postgres server.

use std::{mem, pin::Pin};

use futures::{
    stream::{Fuse, FusedStream},
    FutureExt, StreamExt, TryStreamExt,
};
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
/// Implemented for [`PgRowStream`] in production. The unit tests implement it
/// for an in-memory stream.
///
/// The stream must be fused, so that polling it after its end keeps returning
/// [`None`]. It must be [`Unpin`], so that the cursor can poll it where it is
/// stored. The [`Send`] and [`Ungil`] bounds on the stream and its row and
/// error types let them cross the GIL release in the `block_on*` helpers.
pub trait CursorRowStream:
    FusedStream<Item = Result<Self::Row, Self::Error>> + Unpin + Send + Ungil + Sized
{
    /// A single result row.
    type Row: Send + Ungil;
    /// The stream's error type.
    type Error: Send + Ungil;

    /// The number of rows the query affected. Postgres sends it after the last
    /// row, so it is only valid once the stream has been fully drained. Maps to
    /// [`RowStream::rows_affected`] in production.
    fn rows_affected(&self) -> Option<u64>;

    /// Convert a yielded row into a Python tuple, one element per column.
    fn row_to_py<'py>(py: Python<'py>, row: &Self::Row) -> PyResult<Bound<'py, PyTuple>>;

    /// Build the Python error raised when the stream yields an error.
    fn stream_err(err: Self::Error) -> PyErr;
}

/// The row stream production code stores. A [`RowStream`] is neither [`Unpin`]
/// nor fused, so it is boxed and fused once when the query starts.
pub type PgRowStream = Pin<Box<Fuse<RowStream>>>;

impl CursorRowStream for PgRowStream {
    type Row = tokio_postgres::Row;
    type Error = tokio_postgres::Error;

    fn rows_affected(&self) -> Option<u64> {
        // The first `get_ref` unwraps the `Pin`, the second the `Fuse`.
        self.as_ref().get_ref().get_ref().rows_affected()
    }

    fn row_to_py<'py>(py: Python<'py>, row: &Self::Row) -> PyResult<Bound<'py, PyTuple>> {
        pg_row_to_py(py, row)
    }

    fn stream_err(err: Self::Error) -> PyErr {
        pg_err_to_py(err)
    }
}

/// The lifecycle of the cursor's most recent query.
#[derive(Default)]
pub enum CursorQueryState<S = PgRowStream> {
    /// No `execute` has happened yet, or an error occurred during the previous
    /// `execute` or while fetching its rows. Fetching is an error.
    #[default]
    Idle,
    /// A query has been executed and rows can be fetched. The end of the
    /// result set has not yet been reported to the caller.
    Active {
        /// Live row stream for the current query.
        stream: S,
        /// The runtime the blocking `fetch_*` calls drive the stream on. It
        /// comes from [`crate::runtime::RustRuntime`].
        handle: Handle,
        /// Column names of the result set, empty for a statement that returns
        /// no rows.
        ///
        /// TODO: not read yet. Kept for a PEP-249 `Cursor.description`
        /// accessor.
        description: Vec<String>,
    },
    /// The result set has been fully consumed and its end reported. A fetch
    /// returned [`None`] or an empty batch, or [`CursorQueryState::fetch_all`]
    /// or [`CursorQueryState::rowcount`] drained the stream. A further
    /// `fetch_*` is an error. `rowcount` still returns the count.
    Closed {
        /// Column names of the result set, carried over from
        /// [`CursorQueryState::Active`]. See the TODO there.
        description: Vec<String>,
        /// The affected-row count for PEP-249's `rowcount`, if Postgres
        /// reported one.
        rowcount: Option<u64>,
    },
}

impl<S: CursorRowStream> CursorQueryState<S> {
    /// Reset to [`Self::Idle`], discarding any previous result set. Called at
    /// the start of every `execute`.
    pub fn reset(&mut self) {
        *self = Self::Idle;
    }

    /// Record the stream and column names of a newly started query.
    ///
    /// `description` holds the column names as plain strings, empty for a
    /// statement that returns no rows. Plain strings keep this module free of
    /// the driver's column type, so the tests need no driver. `handle` is the
    /// runtime the blocking `fetch_*` calls drive the stream on.
    pub fn on_query_start(&mut self, stream: S, description: Vec<String>, handle: Handle) {
        *self = Self::Active {
            stream,
            handle,
            description,
        };
    }

    /// Pull the next row from the stream, or [`None`] once it's exhausted.
    ///
    /// Returning `None` reports exhaustion and moves the cursor to
    /// [`Self::Closed`], so a subsequent fetch is an error. If reading or
    /// converting the row fails, the cursor is reset to [`Self::Idle`] and the
    /// error surfaced to Python.
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        let row = match Pin::new(stream).block_on_next(py, handle) {
            Some(row) => row
                .map_err(S::stream_err)
                .and_then(|row| S::row_to_py(py, &row)),
            None => {
                self.close_drained();
                return Ok(None);
            }
        };
        self.reset_on_err(row).map(Some)
    }

    /// Fetch the next batch of rows.
    ///
    /// Blocks until the first row is available, then adds any further rows that
    /// are already buffered without blocking again. `capacity` sizes the
    /// returned buffer and does not cap the batch. This lets Python iterate a
    /// large result set without a call per row ([`Self::fetch_one`]) and
    /// without waiting for the whole result set ([`Self::fetch_all`]).
    ///
    /// An empty batch reports exhaustion and moves the cursor to
    /// [`Self::Closed`], so a subsequent fetch is an error. A batch that
    /// reaches the end of the stream still returns the rows it has, so the
    /// empty batch always comes from a later call. If reading or converting a
    /// row fails, the cursor is reset to [`Self::Idle`] and the error surfaced
    /// to Python.
    pub fn fetch_next_batch<'py>(
        &mut self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        let batch = pull_ready_batch(stream, py, capacity, handle);
        if matches!(&batch, Ok(rows) if rows.is_empty()) {
            self.close_drained();
        }
        self.reset_on_err(batch)
    }

    /// Collect every remaining row into a [`Vec`], draining the stream.
    ///
    /// Draining reports exhaustion and moves the cursor to [`Self::Closed`], so
    /// a subsequent fetch is an error. If reading or converting a row fails,
    /// the cursor is reset to [`Self::Idle`] and the error surfaced to Python.
    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active { stream, handle, .. } = self else {
            return Err(self.fetch_after_end_err());
        };

        let rows = stream
            .try_collect::<Vec<_>>()
            .block_on(py, handle)
            .map_err(S::stream_err)
            .and_then(|rows| rows.iter().map(|row| S::row_to_py(py, row)).collect());
        if rows.is_ok() {
            self.close_drained();
        }
        self.reset_on_err(rows)
    }

    /// Return the affected-row count, draining the stream first if needed.
    ///
    /// Unlike the `fetch_*` methods, this also works on a [`Self::Closed`]
    /// cursor. It returns the count captured when the stream ended.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        // Postgres only sends the affected-row count after the last row, so
        // the stream must be drained first. Draining discards any rows not yet
        // fetched. psycopg2 keeps every row on the client, so there they stay
        // fetchable after reading `rowcount`. Here a caller must fetch the rows
        // it wants first.
        if let Self::Active { stream, handle, .. } = self {
            let drained = drain_stream(stream)
                .block_on(py, handle)
                .map_err(S::stream_err);
            self.reset_on_err(drained)?;
            self.close_drained();
        }

        match self {
            Self::Closed {
                rowcount: Some(rowcount),
                ..
            } => Ok(PyInt::new(py, *rowcount)),
            // The cursor is `Idle`, or Postgres reported no count.
            // PEP-249 uses -1 for an unknown rowcount.
            _ => Ok(PyInt::new(py, -1)),
        }
    }

    /// The error a `fetch_*` method raises when there is no [`Self::Active`]
    /// result set. It tells "no query has run" apart from "the result set is
    /// already exhausted". It leaves the state unchanged.
    fn fetch_after_end_err(&self) -> PyErr {
        match self {
            Self::Closed { .. } => {
                PyRuntimeError::new_err("cannot fetch: the result set is already exhausted")
            }
            _ => PyRuntimeError::new_err("no active query"),
        }
    }

    /// Reset the cursor to [`Self::Idle`] if `result` is an error, so a failed
    /// fetch never leaves a partly read result set behind.
    fn reset_on_err<T>(&mut self, result: PyResult<T>) -> PyResult<T> {
        if result.is_err() {
            *self = Self::Idle;
        }
        result
    }

    /// Move a [`Self::Active`] cursor to [`Self::Closed`], capturing the
    /// affected-row count. The count only arrives after the last row, so
    /// callers drain the stream, or observe its end, first.
    ///
    /// A no-op in any other state.
    fn close_drained(&mut self) {
        if let Self::Active {
            stream,
            description,
            ..
        } = self
        {
            let rowcount = stream.rows_affected();
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
/// Returns an empty batch only if the stream was already exhausted. If the
/// stream ends after the first row, this returns the rows collected so far. The
/// next call then returns the empty batch, because the fused stream keeps
/// yielding [`None`].
///
/// A stream error part way through discards the rows collected so far.
fn pull_ready_batch<'py, S: CursorRowStream>(
    stream: &mut S,
    py: Python<'py>,
    capacity: usize,
    handle: &Handle,
) -> PyResult<Vec<Bound<'py, PyTuple>>> {
    let first = match Pin::new(&mut *stream).block_on_next(py, handle) {
        Some(row) => row.map_err(S::stream_err)?,
        None => return Ok(Vec::new()),
    };

    let mut buffer = Vec::with_capacity(capacity);
    buffer.push(S::row_to_py(py, &first)?);

    // `now_or_never` returns `None` when the next row is not buffered yet, and
    // `Some(None)` at the end of the stream. Either way the batch is done.
    while let Some(Some(row)) = stream.next().now_or_never() {
        buffer.push(S::row_to_py(py, &row.map_err(S::stream_err)?)?);
    }

    Ok(buffer)
}

/// Consume and discard every row of a stream, and return the first stream
/// error. Used to reach the affected-row count, which Postgres sends after the
/// last row.
async fn drain_stream<S: CursorRowStream>(stream: &mut S) -> Result<(), S::Error> {
    while let Some(row) = stream.next().await {
        row?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    //! These tests drive the state machine with an in-memory [`FakeStream`]
    //! instead of a live [`RowStream`], so they need no Postgres server. Rows
    //! are integer tuples, enough to check that values pass through
    //! [`CursorRowStream::row_to_py`]. A fake stream can also return
    //! [`Poll::Pending`], for the partial-batch and blocking paths of
    //! [`CursorQueryState::fetch_next_batch`]. The `block_on*` helpers
    //! themselves are tested in [`crate::database::postgres::helpers`].

    use std::{
        collections::VecDeque,
        task::{Context, Poll},
    };

    use pyo3::prelude::*;
    use tokio::runtime::Runtime;

    use super::*;

    /// A handle to a process-wide test runtime, standing in for the shared
    /// runtime production code passes to [`CursorQueryState::on_query_start`].
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

    /// A result row.
    #[derive(Clone, Debug, PartialEq)]
    enum FakeRow {
        /// Integer cells, one per column.
        Cells(&'static [i64]),
        /// A row holding a value with no Python conversion, so
        /// [`CursorRowStream::row_to_py`] fails on it.
        Unconvertible,
    }

    /// A stream error carrying a message we can assert on.
    #[derive(Debug)]
    struct FakeError(&'static str);

    /// One poll of a [`FakeStream`].
    #[derive(Clone, Copy)]
    enum Step {
        /// A row that is ready now.
        Row(&'static [i64]),
        /// A row that is ready now but fails to convert to Python.
        Unconvertible,
        /// A stream error that is ready now.
        Err(&'static str),
        /// One poll that returns [`Poll::Pending`]. It models a row that is not
        /// buffered yet. The stream wakes itself, so a blocking poll makes
        /// progress. A single [`FutureExt::now_or_never`] poll still sees
        /// `Pending`.
        Pending,
    }

    /// An in-memory stream that plays back its steps and then ends. Once
    /// drained, it reports `rows_affected` as the affected-row count. It is
    /// [`Unpin`] and fused itself, so the tests store it without boxing.
    struct FakeStream {
        steps: VecDeque<Step>,
        rows_affected: Option<u64>,
    }

    impl futures::Stream for FakeStream {
        type Item = Result<FakeRow, FakeError>;

        fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
            match self.steps.pop_front() {
                Some(Step::Row(cells)) => Poll::Ready(Some(Ok(FakeRow::Cells(cells)))),
                Some(Step::Unconvertible) => Poll::Ready(Some(Ok(FakeRow::Unconvertible))),
                Some(Step::Err(msg)) => Poll::Ready(Some(Err(FakeError(msg)))),
                Some(Step::Pending) => {
                    cx.waker().wake_by_ref();
                    Poll::Pending
                }
                None => Poll::Ready(None),
            }
        }
    }

    impl FusedStream for FakeStream {
        fn is_terminated(&self) -> bool {
            // With no steps left, every poll returns `None`.
            self.steps.is_empty()
        }
    }

    impl CursorRowStream for FakeStream {
        type Row = FakeRow;
        type Error = FakeError;

        fn rows_affected(&self) -> Option<u64> {
            self.rows_affected
        }

        fn row_to_py<'py>(py: Python<'py>, row: &FakeRow) -> PyResult<Bound<'py, PyTuple>> {
            match row {
                FakeRow::Cells(cells) => PyTuple::new(py, *cells),
                FakeRow::Unconvertible => Err(PyRuntimeError::new_err("unconvertible row")),
            }
        }

        fn stream_err(err: FakeError) -> PyErr {
            PyRuntimeError::new_err(format!("fake stream error: {}", err.0))
        }
    }

    /// Build a [`CursorQueryState::Active`] state that plays back `steps`, with
    /// the given affected-row count.
    fn active_with_steps(
        steps: &[Step],
        rows_affected: Option<u64>,
    ) -> CursorQueryState<FakeStream> {
        let mut state = CursorQueryState::default();
        state.on_query_start(
            FakeStream {
                steps: steps.iter().copied().collect(),
                rows_affected,
            },
            vec!["col".to_string()],
            runtime_handle(),
        );
        state
    }

    /// Build a [`CursorQueryState::Active`] state over the given rows, all
    /// ready, with the given affected-row count.
    fn active_with(
        rows: &[&'static [i64]],
        rows_affected: Option<u64>,
    ) -> CursorQueryState<FakeStream> {
        let steps: Vec<Step> = rows.iter().copied().map(Step::Row).collect();
        active_with_steps(&steps, rows_affected)
    }

    /// Assert a [`Bound<PyTuple>`] decodes to the given integer cells.
    fn assert_tuple(tuple: &Bound<'_, PyTuple>, expected: &[i64]) {
        let got: Vec<i64> = tuple.extract().unwrap();
        assert_eq!(got, expected);
    }

    /// Assert a [`CursorQueryState::fetch_one`] result is a row, not the end of
    /// the result set, and that it decodes to the given integer cells.
    fn assert_tuple_opt(row: Option<Bound<'_, PyTuple>>, expected: &[i64]) {
        let row = row.expect("expected a row, got the end of the result set");
        assert_tuple(&row, expected);
    }

    /// [`CursorQueryState::fetch_one`] returns each row in turn, then [`None`]
    /// at the end of the stream. That closes the cursor, and a further fetch
    /// raises the "already exhausted" error.
    #[test]
    fn fetch_one_yields_rows_then_reports_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1], &[2]], Some(0));

            assert_tuple_opt(state.fetch_one(py).unwrap(), &[1]);
            assert_tuple_opt(state.fetch_one(py).unwrap(), &[2]);
            // The end of the stream is reported as `None`, closing the cursor.
            assert!(state.fetch_one(py).unwrap().is_none());
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // A fetch after exhaustion raises the "already exhausted" error.
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("already exhausted"), "{err}");
        });
    }

    /// [`CursorQueryState::fetch_one`] on a cursor that has run no query raises
    /// the "no active query" error.
    #[test]
    fn fetch_one_on_idle_errors_with_no_active_query() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = CursorQueryState::<FakeStream>::default();
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("no active query"), "{err}");
        });
    }

    /// [`CursorQueryState::fetch_all`] returns every row and closes the cursor.
    /// Calling it again raises the "already exhausted" error.
    #[test]
    fn fetch_all_drains_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1, 2], &[3, 4]], Some(0));

            let rows = state.fetch_all(py).unwrap();
            assert_eq!(rows.len(), 2);
            assert_tuple(&rows[0], &[1, 2]);
            assert_tuple(&rows[1], &[3, 4]);
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // Draining again is an error, because the result set is gone.
            assert!(state
                .fetch_all(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    /// [`CursorQueryState::fetch_all`] on a result set with no rows returns an
    /// empty list and closes the cursor.
    #[test]
    fn fetch_all_on_empty_stream_returns_empty_and_closes() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[], Some(0));
            assert!(state.fetch_all(py).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    /// [`CursorQueryState::fetch_one`] after [`CursorQueryState::fetch_all`]
    /// raises the "already exhausted" error.
    #[test]
    fn fetch_one_after_fetch_all_errors() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1]], Some(0));
            let _ = state.fetch_all(py).unwrap();
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    /// [`CursorQueryState::fetch_all`] after [`CursorQueryState::fetch_one`]
    /// returns only the remaining rows.
    #[test]
    fn fetch_one_then_fetch_all_continues_partially_consumed_stream() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1], &[2], &[3]], Some(0));

            assert_tuple_opt(state.fetch_one(py).unwrap(), &[1]);
            let rest = state.fetch_all(py).unwrap();
            assert_eq!(rest.len(), 2);
            assert_tuple(&rest[0], &[2]);
            assert_tuple(&rest[1], &[3]);
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    /// A stream error part way through [`CursorQueryState::fetch_all`] is
    /// raised and resets the cursor to [`CursorQueryState::Idle`].
    #[test]
    fn fetch_all_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            // First row ok, then an error part-way through the collection.
            let mut state = active_with_steps(&[Step::Row(&[1]), Step::Err("kaboom")], Some(0));

            let err = state.fetch_all(py).unwrap_err();
            assert!(err.to_string().contains("kaboom"), "{err}");
            // A mid-stream error resets to `Idle`, like in `fetch_one`.
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state
                .fetch_all(py)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    /// A stream error while [`CursorQueryState::rowcount`] drains the stream is
    /// raised and resets the cursor to [`CursorQueryState::Idle`]. The cursor
    /// keeps no stale count.
    #[test]
    fn rowcount_after_drain_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with_steps(&[Step::Err("drain failed")], Some(7));

            // Draining for the rowcount hits the error and surfaces it.
            let err = state.rowcount(py).unwrap_err();
            assert!(err.to_string().contains("drain failed"), "{err}");
            // The cursor is reset to `Idle` and keeps no stale count. A later
            // `rowcount` returns -1, PEP-249's value for an unknown count.
            assert!(matches!(state, CursorQueryState::Idle));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
        });
    }

    /// A stream error in [`CursorQueryState::fetch_one`] is raised and resets
    /// the cursor to [`CursorQueryState::Idle`], so the next fetch raises "no
    /// active query".
    #[test]
    fn stream_error_resets_to_idle_and_surfaces() {
        Python::initialize();
        Python::attach(|py| {
            // A stream whose second item is an error.
            let mut state = active_with_steps(&[Step::Row(&[1]), Step::Err("boom")], None);

            assert_tuple_opt(state.fetch_one(py).unwrap(), &[1]);
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("boom"), "{err}");

            // After an error the cursor is `Idle`, so the next fetch reports
            // "no active query".
            assert!(matches!(state, CursorQueryState::Idle));
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    /// A row that fails to convert to Python resets the cursor to
    /// [`CursorQueryState::Idle`] in every fetch method, the same as a stream
    /// error.
    #[test]
    fn conversion_error_resets_to_idle_in_every_fetch() {
        Python::initialize();
        Python::attach(|py| {
            let rows = [Step::Row(&[1]), Step::Unconvertible, Step::Row(&[3])];

            let mut state = active_with_steps(&rows, Some(0));
            assert_tuple_opt(state.fetch_one(py).unwrap(), &[1]);
            let err = state.fetch_one(py).unwrap_err();
            assert!(err.to_string().contains("unconvertible"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));

            let mut state = active_with_steps(&rows, Some(0));
            let err = state.fetch_next_batch(py, 100).unwrap_err();
            assert!(err.to_string().contains("unconvertible"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));

            let mut state = active_with_steps(&rows, Some(0));
            let err = state.fetch_all(py).unwrap_err();
            assert!(err.to_string().contains("unconvertible"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    /// The "already exhausted" error leaves the cursor
    /// [`CursorQueryState::Closed`], so [`CursorQueryState::rowcount`] still
    /// returns the affected-row count.
    #[test]
    fn fetch_after_exhaustion_keeps_the_rowcount() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1]], Some(1));
            state.fetch_all(py).unwrap();
            state.fetch_one(py).unwrap_err();
            assert!(matches!(state, CursorQueryState::Closed { .. }));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 1);
        });
    }

    /// [`CursorQueryState::rowcount`] returns -1 before any query has run,
    /// PEP-249's value for an unknown count.
    #[test]
    fn rowcount_is_minus_one_before_any_query() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = CursorQueryState::<FakeStream>::default();
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), -1);
        });
    }

    /// [`CursorQueryState::rowcount`] drains the stream, closes the cursor and
    /// returns the affected-row count. Reading it again returns the same count.
    #[test]
    fn rowcount_drains_and_reports_affected_rows() {
        Python::initialize();
        Python::attach(|py| {
            // An UPDATE-like result with no rows and 5 affected rows.
            let mut state = active_with(&[], Some(5));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 5);
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // Reading it again on a `Closed` cursor returns the retained count.
            // A `fetch_*` would raise an error here.
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 5);
        });
    }

    /// [`CursorQueryState::rowcount`] discards the rows not yet fetched to
    /// reach the affected-row count. A fetch afterwards raises the "already
    /// exhausted" error.
    #[test]
    fn rowcount_drains_remaining_rows_of_a_select() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1], &[2]], Some(2));
            assert_eq!(state.rowcount(py).unwrap().extract::<i64>().unwrap(), 2);
            // The stream is drained, so a fetch now raises the "exhausted"
            // error.
            assert!(state
                .fetch_one(py)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    /// [`CursorQueryState::reset`] discards an [`CursorQueryState::Active`]
    /// result set, so a fetch afterwards raises "no active query".
    #[test]
    fn reset_discards_an_active_cursor() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1]], Some(0));
            state.reset();
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

    /// [`CursorQueryState::fetch_next_batch`] returns every ready row but
    /// leaves the cursor [`CursorQueryState::Active`] when it reaches the end
    /// of the stream. The next call returns the empty batch and closes the
    /// cursor.
    #[test]
    fn fetch_next_batch_returns_ready_rows_then_defers_the_empty_report() {
        Python::initialize();
        Python::attach(|py| {
            // Every row is ready, so the first batch takes the whole result
            // set.
            let mut state = active_with(&[&[1], &[2], &[3]], Some(0));
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 3);
            assert_tuple(&batch[0], &[1]);
            assert_tuple(&batch[2], &[3]);

            // The cursor stays `Active` even though the batch reached the end
            // of the stream.
            assert!(matches!(state, CursorQueryState::Active { .. }));

            // The next call returns the empty batch and closes the cursor.
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));

            // A further fetch raises the "already exhausted" error.
            assert!(state
                .fetch_next_batch(py, 100)
                .unwrap_err()
                .to_string()
                .contains("exhausted"));
        });
    }

    /// [`CursorQueryState::fetch_next_batch`] on a result set with no rows
    /// returns an empty batch and closes the cursor in the same call.
    #[test]
    fn fetch_next_batch_empty_stream_reports_exhaustion_immediately() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[], Some(0));
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    /// The `capacity` passed to [`CursorQueryState::fetch_next_batch`] only
    /// sizes the buffer. The batch holds every ready row, even when there are
    /// more than `capacity`.
    #[test]
    fn fetch_next_batch_capacity_is_a_hint_not_a_limit() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1], &[2], &[3]], Some(0));
            let batch = state.fetch_next_batch(py, 1).unwrap();
            assert_eq!(batch.len(), 3);
        });
    }

    /// [`CursorQueryState::fetch_next_batch`] on a cursor that has run no query
    /// raises the "no active query" error.
    #[test]
    fn fetch_next_batch_without_query_errors() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = CursorQueryState::<FakeStream>::default();
            assert!(state
                .fetch_next_batch(py, 100)
                .unwrap_err()
                .to_string()
                .contains("no active query"));
        });
    }

    /// A stream error on the first row of
    /// [`CursorQueryState::fetch_next_batch`] is raised and resets the cursor
    /// to [`CursorQueryState::Idle`].
    #[test]
    fn fetch_next_batch_error_resets_to_idle() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with_steps(&[Step::Err("splat")], Some(0));

            let err = state.fetch_next_batch(py, 100).unwrap_err();
            assert!(err.to_string().contains("splat"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    /// A stream error after the first row of a batch is raised. The rows
    /// collected so far are discarded, and the cursor resets to
    /// [`CursorQueryState::Idle`].
    #[test]
    fn fetch_next_batch_error_after_first_row_discards_buffer_and_resets() {
        Python::initialize();
        Python::attach(|py| {
            // The first row is fine, but the stream errors while the batch is
            // still being collected. Both items are ready at once.
            let mut state = active_with_steps(&[Step::Row(&[1]), Step::Err("midway")], Some(0));

            let err = state.fetch_next_batch(py, 100).unwrap_err();
            assert!(err.to_string().contains("midway"), "{err}");
            assert!(matches!(state, CursorQueryState::Idle));
        });
    }

    /// [`CursorQueryState::fetch_next_batch`] after
    /// [`CursorQueryState::fetch_one`] returns only the remaining rows.
    #[test]
    fn fetch_next_batch_interleaves_with_fetch_one() {
        Python::initialize();
        Python::attach(|py| {
            let mut state = active_with(&[&[1], &[2], &[3]], Some(0));
            assert_tuple_opt(state.fetch_one(py).unwrap(), &[1]);
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 2);
            assert_tuple(&batch[0], &[2]);
            assert_tuple(&batch[1], &[3]);
        });
    }

    /// [`CursorQueryState::fetch_next_batch`] stops at the first row that is
    /// not ready yet. The cursor stays [`CursorQueryState::Active`], and the
    /// next call returns the rows after it.
    #[test]
    fn fetch_next_batch_returns_partial_batch_when_next_is_not_ready() {
        Python::initialize();
        Python::attach(|py| {
            // Row 1 is ready. One poll then returns `Pending`, and row 3 is
            // ready after it.
            let mut state =
                active_with_steps(&[Step::Row(&[1]), Step::Pending, Step::Row(&[3])], Some(0));

            // The first batch stops at the `Pending` poll, so it holds only
            // row 1. The cursor stays `Active`.
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 1);
            assert_tuple(&batch[0], &[1]);
            assert!(matches!(state, CursorQueryState::Active { .. }));

            // The second batch gets row 3.
            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 1);
            assert_tuple(&batch[0], &[3]);

            // The third call reaches the end and returns the empty batch.
            assert!(state.fetch_next_batch(py, 100).unwrap().is_empty());
            assert!(matches!(state, CursorQueryState::Closed { .. }));
        });
    }

    /// [`CursorQueryState::fetch_next_batch`] waits for a first row that is not
    /// ready yet, then returns it with the rows ready behind it.
    #[test]
    fn fetch_next_batch_blocks_for_a_pending_first_row() {
        Python::initialize();
        Python::attach(|py| {
            // The first poll returns `Pending`, so `block_on_next` has to block
            // for the first row.
            let mut state =
                active_with_steps(&[Step::Pending, Step::Row(&[1]), Step::Row(&[2])], Some(0));

            let batch = state.fetch_next_batch(py, 100).unwrap();
            assert_eq!(batch.len(), 2);
            assert_tuple(&batch[0], &[1]);
            assert_tuple(&batch[1], &[2]);
        });
    }
}
