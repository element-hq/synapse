//! Tracks the result of the cursor's most recent `execute`.
//!
//! [`tokio_postgres`] returns rows as a [`RowStream`] that is consumed lazily, so a
//! cursor only ever holds onto the *current* query's stream plus the metadata
//! (column names, rowcount) derived from it. Starting a new query replaces all
//! of this state.

use std::pin::Pin;

use futures::{StreamExt, TryStreamExt};
use pyo3::{exceptions::PyRuntimeError, PyResult, Python};
use tokio_postgres::{Column, RowStream};

use crate::database::postgres::helpers::{BlockingPostgres, BlockingPostgresResult};

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
    pub fn fetch_one<'py>(&mut self, py: Python<'py>) -> PyResult<Option<tokio_postgres::Row>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };

        let next = stream.as_mut().next().block_on(py);

        match next {
            Some(Ok(row)) => Ok(Some(row)),
            Some(Err(err)) => {
                self.stream = None;
                self.description = None;
                Err(PyRuntimeError::new_err(format!(
                    "error fetching row from postgres: {err}"
                )))
            }
            None => {
                self.rowcount = stream.rows_affected();
                self.stream = None;
                Ok(None)
            }
        }
    }

    /// Collect every remaining row into a `Vec`, draining the stream.
    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<tokio_postgres::Row>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };

        let rows = stream.try_collect::<Vec<_>>().block_on_result(py)?;

        self.rowcount = stream.rows_affected();
        self.stream = None;

        Ok(rows)
    }

    /// Return the affected-row count, draining the stream first if needed.
    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<Option<u64>> {
        // `stream.rows_affected()` is only valid after the stream is
        // drained, so we need to drain it here. This is OK as in Python the
        // rowcount should only be accessed for queries that DO NOT return
        // rows, e.g. INSERT, UPDATE, DELETE.
        if let Some(stream) = self.stream.as_mut() {
            drain_stream(stream.as_mut()).block_on_result(py)?;
            self.rowcount = stream.rows_affected();
        }

        Ok(self.rowcount)
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
