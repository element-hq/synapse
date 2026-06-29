//! Tracks the result of the cursor's most recent `execute`.
//!
//! [`tokio_postgres`] returns rows as a [`RowStream`] that is consumed lazily, so a
//! cursor only ever holds onto the *current* query's stream plus the metadata
//! (column names, rowcount) derived from it. Starting a new query replaces all
//! of this state.

use std::pin::Pin;

use futures::{StreamExt, TryStreamExt};
use pyo3::{
    exceptions::PyRuntimeError,
    types::{PyInt, PyTuple},
    Bound, PyResult, Python,
};
use tokio_postgres::{Column, RowStream};

use crate::database::postgres::{
    helpers::{BlockingPostgres, BlockingPostgresResult, BlockingPostgresStream as _},
    value::pg_row_to_py,
};

/// The state carried over from the cursor's most recent query.
#[derive(Default)]
pub struct CursorQueryState {
    /// Live row stream for SELECT-style queries. `None` once the stream is
    /// exhausted, or before the first execute.
    stream: Option<Pin<Box<RowStream>>>,
    /// Column names for the current result set, set by `on_query_start` (a
    /// DML statement just gets an empty list). Reset on the next query.
    ///
    /// TODO: currently write-only; kept to back a future PEP-249
    /// `Cursor.description` accessor.
    description: Option<Vec<String>>,
    /// PEP-249 `rowcount`: the number of rows affected, taken from the
    /// command tag. `None` until the stream has been drained — we stream rows
    /// lazily, so it isn't known before then — and populated by
    /// `fetch_one`/`fetch_all`/`rowcount` once the stream completes.
    rowcount: Option<u64>,
}

impl CursorQueryState {
    /// A fresh state with no query yet run.
    pub fn new() -> Self {
        Self::default()
    }

    /// Reset all state, discarding any previous result set. Called at the
    /// start of every `execute`.
    pub fn new_query(&mut self) {
        self.stream = None;
        self.description = None;
        self.rowcount = None;
    }

    /// Record the stream and column metadata for a newly-started query.
    pub fn on_query_start(&mut self, stream: RowStream, columns: &[Column]) {
        self.stream = Some(Box::pin(stream));
        self.description = Some(columns.iter().map(|c| c.name().to_string()).collect());
        self.rowcount = None;
    }

    /// Pull the next row from the stream, or `None` once it's exhausted.
    ///
    /// On exhaustion the rowcount is captured and the stream dropped; on a
    /// stream error the state is cleared and the error surfaced to Python.
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };

        let next = stream.as_mut().next().block_on(py);

        self.parse_stream_row(py, next)
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
    /// Will only return an empty batch if the stream is exhausted.
    pub fn fetch_next_batch<'py>(
        &mut self,
        py: Python<'py>,
        capacity: usize,
    ) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        // If there's no live stream, either the previous result set has already
        // been fully drained — in which case we return an empty batch, per the
        // contract — or nothing has been executed yet, which is an error. We
        // tell the two apart by `description`, which is set for the duration of
        // a query and only cleared by the next `execute`.
        if self.stream.is_none() {
            return if self.description.is_some() {
                Ok(Vec::new())
            } else {
                Err(PyRuntimeError::new_err("no active query"))
            };
        }

        let mut stream = self.get_stream_mut()?;

        // Wait for at least one row.
        let first_row = stream.block_on_next(py);

        let first_pg_row = self.parse_stream_row(py, first_row)?;
        let Some(first_pg_row) = first_pg_row else {
            // The stream is exhausted, so we return an empty batch.
            return Ok(Vec::new());
        };

        let mut buffer = Vec::with_capacity(capacity);
        buffer.push(first_pg_row);

        loop {
            // `parse_stream_row` requires a mutable reference to self, and so
            // we can't hold onto a mutable reference to the `stream` across the
            // call (as it `stream` is a mutable reference of `self`). So we
            // have to get a new mutable reference to the stream after each row
            // is parsed. Hopefully the compiler can mostly optimise this away.
            stream = self.get_stream_mut()?;

            let Some(next) = stream.get_next_if_ready() else {
                // The stream isn't ready yet, so we return what we have (which
                // we know is non-empty because we pushed the first row above).
                break;
            };

            let row = self.parse_stream_row(py, next)?;
            match row {
                Some(pg_row) => buffer.push(pg_row),
                // End of stream, so we return what we have.
                None => break,
            }
        }

        Ok(buffer)
    }

    /// Collect every remaining row into a `Vec`, draining the stream.
    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyTuple>>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };

        let rows = stream.try_collect::<Vec<_>>().block_on_result(py)?;
        let rows = rows
            .into_iter()
            .map(|row| pg_row_to_py(py, &row))
            .collect::<PyResult<Vec<_>>>()?;

        self.rowcount = stream.rows_affected();
        self.stream = None;

        Ok(rows)
    }

    /// Return the affected-row count, draining the stream first if needed.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        // `stream.rows_affected()` is only valid after the stream is
        // drained, so we need to drain it here. This is OK as in Python the
        // rowcount should only be accessed for queries that DO NOT return
        // rows, e.g. INSERT, UPDATE, DELETE.
        if let Some(stream) = self.stream.as_mut() {
            drain_stream(stream.as_mut()).block_on_result(py)?;
            self.rowcount = stream.rows_affected();
        }

        let Some(rowcount) = self.rowcount else {
            // If we don't have a rowcount yet, PEP-249 says we should return
            // -1
            return Ok(PyInt::new(py, -1));
        };

        Ok(PyInt::new(py, rowcount))
    }

    /// Parse a row from the stream, handling errors and end-of-stream.
    ///
    /// On a stream error the state is cleared and the error surfaced to Python.
    /// On end-of-stream the rowcount is captured and the stream dropped.
    fn parse_stream_row<'py>(
        &'_ mut self,
        py: Python<'py>,
        row: Option<Result<tokio_postgres::Row, tokio_postgres::Error>>,
    ) -> PyResult<Option<Bound<'py, PyTuple>>> {
        match row {
            Some(Ok(row)) => {
                let pg_row = pg_row_to_py(py, &row)?;
                Ok(Some(pg_row))
            }
            Some(Err(err)) => {
                self.stream = None;
                Err(PyRuntimeError::new_err(format!(
                    "error fetching row from postgres: {err}"
                )))
            }
            None => {
                // The stream is exhausted: capture the rowcount and drop the
                // stream so we never poll a completed stream again (doing so
                // surfaces as a spurious "connection closed" error).
                self.rowcount = self.get_stream_mut()?.rows_affected();
                self.stream = None;
                Ok(None)
            }
        }
    }

    /// Get a mutable reference to the row stream, or an error if there is no
    /// active query.
    fn get_stream_mut(&mut self) -> PyResult<Pin<&mut RowStream>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };
        Ok(stream.as_mut())
    }
}

/// Consume and discard every row of a stream, propagating any error. Used to
/// reach the trailing command-complete message that carries the rowcount.
async fn drain_stream(mut stream: Pin<&mut RowStream>) -> Result<(), tokio_postgres::Error> {
    while let Some(row) = stream.next().await {
        row?;
    }
    Ok(())
}
