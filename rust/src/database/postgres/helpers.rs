//! Extension traits for driving [`tokio_postgres`] futures to completion from
//! the synchronous, GIL-holding Python methods.
//!
//! Both traits release the GIL (`py.detach`) while blocking on the shared
//! tokio runtime, so other Python threads can make progress (and so the
//! runtime's own connection task can run) while we wait. The [`Ungil`] bounds
//! are what let us hand the future across the `detach` boundary.

use std::{future::Future, pin::Pin};

use futures::{FutureExt, StreamExt};
use pyo3::{marker::Ungil, PyResult, Python};

use crate::database::{postgres::pg_err_to_py, runtime::runtime};

/// Block on a future on the shared runtime, releasing the GIL while we wait.
pub trait BlockingPostgres
where
    Self: Future + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    /// Drive `self` to completion, returning its output. Releases the GIL for
    /// the duration so the wait doesn't block other Python threads.
    fn block_on(self, py: Python<'_>) -> Self::Output {
        py.detach(|| runtime().block_on(self))
    }
}

/// Same as [`BlockingPostgres`], but for futures that yield a
/// [`tokio_postgres::Result`], mapping any error into a Python exception.
pub trait BlockingPostgresResult<T>
where
    Self: Future<Output = Result<T, tokio_postgres::Error>> + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    /// Block on `self` and convert a Postgres error into a `PyErr`.
    fn block_on_result(self, py: Python<'_>) -> PyResult<T> {
        self.block_on(py).map_err(pg_err_to_py)
    }
}

// Blanket impls: every suitable future automatically gets `block_on` /
// `block_on_result`, so callers can write `fut.block_on(py)` directly.
impl<F> BlockingPostgres for F
where
    F: Future + Sized + Send + Ungil,
    F::Output: Ungil + Send,
{
}
impl<F, T> BlockingPostgresResult<T> for F
where
    F: Future<Output = Result<T, tokio_postgres::Error>> + Sized + Send + Ungil,
    F::Output: Ungil + Send,
{
}

pub trait BlockingPostgresStream
where
    Self: futures::Stream + Sized + Send + Ungil + Unpin,
    Self::Item: Ungil + Send,
{
    /// Get the next item from the stream, blocking on the shared runtime if
    /// necessary.
    ///
    /// If the stream is not ready to yield an item, this will release the GIL
    /// and block until the next item is available.
    ///
    /// This method will return `None` if the stream is exhausted.
    fn block_on_next(&mut self, py: Python<'_>) -> Option<Self::Item> {
        match self.get_next_if_ready() {
            Some(row) => row,
            None => self.next().block_on(py),
        }
    }

    /// Get the next item from the stream if it's ready, without blocking.
    ///
    /// Returns `None` if the stream is not ready to yield an item. Returns
    /// `Some(None)` if the stream is exhausted.
    fn get_next_if_ready(&mut self) -> Option<Option<Self::Item>> {
        self.next().now_or_never()
    }
}

impl BlockingPostgresStream for Pin<&mut tokio_postgres::RowStream> {}
