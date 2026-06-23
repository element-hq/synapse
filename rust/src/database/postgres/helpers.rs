use std::future::Future;

use pyo3::{marker::Ungil, PyResult, Python};

use crate::database::{postgres::pg_err_to_py, runtime::runtime};

pub trait BlockingPostgres
where
    Self: Future + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    fn block_on(self, py: Python<'_>) -> Self::Output {
        py.detach(|| runtime().block_on(self))
    }
}

pub trait BlockingPostgresResult<T>
where
    Self: Future<Output = Result<T, tokio_postgres::Error>> + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    fn block_on_result(self, py: Python<'_>) -> PyResult<T> {
        self.block_on(py).map_err(pg_err_to_py)
    }
}

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
