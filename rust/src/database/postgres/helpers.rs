//! Extension traits for driving [`tokio_postgres`] futures to completion from
//! the synchronous, GIL-holding Python methods.
//!
//! Both traits release the GIL (`py.detach`) while blocking on the shared
//! tokio runtime, so other Python threads can make progress (and so the
//! runtime's own connection task can run) while we wait. The [`Ungil`] bounds
//! are what let us hand the future across the `detach` boundary.

use std::future::Future;

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
