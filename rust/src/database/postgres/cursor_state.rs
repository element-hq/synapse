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

use std::{mem, pin::Pin};

use futures::{stream::Fuse, StreamExt, TryStreamExt};
use pyo3::{
    exceptions::PyRuntimeError,
    types::{PyInt, PyTuple},
    Bound, PyErr, PyResult, Python,
};
use tokio_postgres::{Column, RowStream};

use crate::database::postgres::{
    helpers::{BlockingPostgres, BlockingPostgresResult, BlockingPostgresStream as _},
    value::pg_row_to_py,
};

/// A live row stream, fused so that polling it after completion returns `None`
/// rather than re-polling the finished inner stream.
type FusedRowStream = Pin<Box<Fuse<RowStream>>>;

/// The lifecycle of the cursor's most recent query.
#[derive(Default)]
pub enum CursorQueryState {
    /// No query has been executed yet, or the previous result set was reset by
    /// the next `execute`. Fetching is an error.
    #[default]
    Idle,
    /// A query is in flight; rows can be fetched. Exhaustion has not yet been
    /// reported to the caller.
    Active {
        /// Live row stream for the current query.
        stream: FusedRowStream,
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
        /// Column names for the result set, retained for `description`.
        description: Vec<String>,
        /// PEP-249 `rowcount` from the command tag, if it was captured.
        rowcount: Option<u64>,
    },
}

impl CursorQueryState {
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
    pub fn on_query_start(&mut self, stream: RowStream, columns: &[Column]) {
        *self = Self::Active {
            stream: Box::pin(stream.fuse()),
            description: columns.iter().map(|c| c.name().to_string()).collect(),
        };
    }

    /// Pull the next row from the stream, or `None` once it's exhausted.
    ///
    /// Returning `None` reports exhaustion and moves the cursor to `Closed`, so
    /// a subsequent fetch is an error. On a stream error the cursor is reset to
    /// `Idle` and the error surfaced to Python.
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let Self::Active { stream, description } = self else {
            return Err(self.fetch_after_end_err());
        };

        match stream.as_mut().next().block_on(py) {
            Some(Ok(row)) => Ok(Some(pg_row_to_py(py, &row)?)),
            Some(Err(err)) => {
                *self = Self::Idle;
                Err(stream_err(&err))
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
    /// leaves the report to the next call.
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
                if let Self::Active { stream, description } = self {
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
    /// subsequent fetch is an error.
    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Self::Active { stream, description } = self else {
            return Err(self.fetch_after_end_err());
        };

        let rows = stream.as_mut().try_collect::<Vec<_>>().block_on_result(py)?;
        let rows = rows
            .into_iter()
            .map(|row| pg_row_to_py(py, &row))
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
        if let Self::Active { stream, description } = self {
            drain_stream(stream.as_mut()).block_on_result(py)?;
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
            // No rowcount known yet (no query run, or a SELECT not yet drained):
            // PEP-249 says to return -1.
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

/// Pull the first row (blocking until it arrives) plus any rows that are
/// already buffered, without blocking again.
///
/// Returns `Ok(None)` if the stream is already exhausted (there was no first
/// row). End-of-stream reached while draining the ready rows is *not* reported
/// here — we return the rows we have and leave the empty-batch report to a
/// later call; the fused stream makes re-polling safe.
fn pull_ready_batch<'py>(
    stream: &mut FusedRowStream,
    py: Python<'py>,
    capacity: usize,
) -> PyResult<Option<Vec<Bound<'py, PyTuple>>>> {
    // Wait for at least one row.
    let first = match stream.as_mut().block_on_next(py) {
        Some(Ok(row)) => pg_row_to_py(py, &row)?,
        Some(Err(err)) => return Err(stream_err(&err)),
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
            Some(Some(Ok(row))) => buffer.push(pg_row_to_py(py, &row)?),
            // The stream errored.
            Some(Some(Err(err))) => return Err(stream_err(&err)),
            // End of stream: stop, leaving the empty-batch report to the next
            // call.
            Some(None) => break,
        }
    }

    Ok(Some(buffer))
}

/// The command tag's affected-row count, valid only once the (fused) stream has
/// been fully drained.
fn rows_affected(stream: &FusedRowStream) -> Option<u64> {
    // The first `get_ref` unwraps the `Pin`, the second the `Fuse`.
    stream.as_ref().get_ref().get_ref().rows_affected()
}

/// Build the Python error for a stream-level Postgres failure.
fn stream_err(err: &tokio_postgres::Error) -> PyErr {
    PyRuntimeError::new_err(format!("error fetching row from postgres: {err}"))
}

/// Consume and discard every row of a stream, propagating any error. Used to
/// reach the trailing command-complete message that carries the rowcount.
async fn drain_stream(
    mut stream: Pin<&mut Fuse<RowStream>>,
) -> Result<(), tokio_postgres::Error> {
    while let Some(row) = stream.next().await {
        row?;
    }
    Ok(())
}
