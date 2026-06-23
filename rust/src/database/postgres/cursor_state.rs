use std::pin::Pin;

use futures::{StreamExt, TryStreamExt};
use pyo3::{exceptions::PyRuntimeError, PyResult, Python};
use tokio_postgres::{Column, RowStream};

use crate::database::postgres::helpers::{BlockingPostgres, BlockingPostgresResult};

#[derive(Default)]
pub struct CursorQueryState {
    /// Live row stream for SELECT-style queries. `None` once the stream is
    /// exhausted, or before the first execute.
    stream: Option<Pin<Box<RowStream>>>,
    /// Column metadata for the current result set. Set when a stream is
    /// started; cleared after DML.
    description: Option<Vec<String>>,
    /// PEP-249 `rowcount`. For DML this is the affected count. For
    /// SELECTs we stream and the total isn't known until the stream is
    /// drained, so we leave this as `None` throughout.
    rowcount: Option<u64>,
}

impl CursorQueryState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn new_query(&mut self) {
        self.stream = None;
        self.description = None;
        self.rowcount = None;
    }

    pub fn on_query_start(&mut self, stream: RowStream, columns: &[Column]) {
        self.stream = Some(Box::pin(stream));
        self.description = Some(columns.iter().map(|c| c.name().to_string()).collect());
        self.rowcount = None;
    }

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

    pub fn fetch_all<'py>(&mut self, py: Python<'py>) -> PyResult<Vec<tokio_postgres::Row>> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(PyRuntimeError::new_err("no active query"));
        };

        let rows = stream.try_collect::<Vec<_>>().block_on_result(py)?;

        self.rowcount = stream.rows_affected();
        self.stream = None;

        Ok(rows)
    }

    pub fn rowcount<'py>(&mut self, py: Python<'py>) -> PyResult<u64> {
        // `stream.rows_affected()` is only valid after the stream is
        // drained, so we need to drain it here. This is OK as in Python the
        // rowcount should only be accessed for queries that DO NOT return
        // rows, e.g. INSERT, UPDATE, DELETE.
        if let Some(stream) = self.stream.as_mut() {
            drain_stream(stream.as_mut()).block_on_result(py)?;
            self.rowcount = stream.rows_affected();
        }

        if let Some(rowcount) = self.rowcount {
            Ok(rowcount)
        } else {
            Err(PyRuntimeError::new_err("no active query").into())
        }
    }
}

async fn drain_stream(mut stream: Pin<&mut RowStream>) -> Result<(), tokio_postgres::Error> {
    while let Some(row) = stream.next().await {
        row?;
    }
    Ok(())
}
