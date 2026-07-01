//! Extension traits for driving [`tokio_postgres`] futures to completion from
//! the synchronous, GIL-holding Python methods.
//!
//! Both [`BlockingPostgres`] and [`BlockingPostgresStream`] release the GIL
//! (`py.detach`) while blocking on the shared tokio runtime, so other Python
//! threads can make progress (and so the runtime's own connection task can run)
//! while we wait. The [`Ungil`] bounds are what let us hand the future across
//! the `detach` boundary.
//!
//! The stream helper is generic over the underlying stream type rather than
//! hard-wired to [`tokio_postgres::RowStream`]. In production it is always used
//! with a `RowStream`, but keeping it generic lets the cursor state machine
//! (which uses these helpers) be unit-tested against an in-memory fake stream
//! with no live database — see [`BlockingPostgresStream`]'s tests.
//!
//! ## Why polling a `RowStream` off the runtime is safe
//!
//! [`BlockingPostgresStream::get_next_if_ready`] polls the stream once on the
//! calling thread *without* entering the runtime. This is sound specifically
//! because a [`tokio_postgres::RowStream`] poll only reads from an in-memory
//! channel that the connection task (running on the runtime) feeds — it never
//! touches the tokio reactor or a timer, so it can't panic with "no reactor
//! running" and a single `Pending` poll genuinely means "nothing buffered yet".
//! A different stream that needs a reactor on the polling thread would *not* be
//! safe to use here, even though the generic bounds would accept it.

use std::{future::Future, pin::Pin};

use futures::{stream::Fuse, FutureExt, StreamExt};
use pyo3::{marker::Ungil, PyResult, Python};

use crate::database::{postgres::errors::pg_err_to_py, runtime::runtime};

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
        self.block_on(py).map_err(|e| pg_err_to_py(&e))
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

/// Pull items from a [`Fuse`]d stream from synchronous Python code, blocking on
/// the shared runtime only when the next item isn't already buffered.
///
/// Implemented for any pinned, fused stream (`Pin<&mut Fuse<S>>`) whose items
/// can cross the GIL-release boundary. In production `S` is
/// [`tokio_postgres::RowStream`]; the generic bound is what lets the cursor
/// logic be tested against an in-memory fake.
///
/// The [`Fuse`] is *required* by the impl (the trait is implemented only for
/// `Pin<&mut Fuse<S>>`), not merely assumed. This matters because
/// [`Self::get_next_if_ready`] may poll the stream again after it has finished:
/// a bare `Stream` is free to panic if polled past completion, whereas a fused
/// stream simply keeps yielding `None`. So repeated `get_next_if_ready` /
/// `block_on_next` calls after exhaustion are safe by construction.
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
            // `Some(Some(item))` (ready) and `Some(None)` (exhausted) are both
            // answers we can return immediately — we just hand the inner
            // `Option<Item>` straight back.
            Some(row) => row,
            // `None` means "not ready yet": release the GIL and block until the
            // next item (or end of stream) arrives.
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

// Blanket impl over any pinned, fused stream. Requiring the helper bounds here
// (rather than only for `RowStream`) is what makes the cursor logic testable
// with a fake stream.
impl<S> BlockingPostgresStream for Pin<&mut Fuse<S>>
where
    Self: futures::Stream + Send + Ungil + Unpin,
    <Self as futures::Stream>::Item: Ungil + Send,
{
}

#[cfg(test)]
mod tests {
    //! These tests don't touch Postgres: the future/stream helpers are generic,
    //! so we exercise them with plain async blocks and an in-memory stream.

    use std::pin::pin;

    use futures::stream::{self, StreamExt};

    use super::*;

    #[test]
    fn block_on_runs_future_and_returns_output() {
        Python::initialize();
        Python::attach(|py| {
            assert_eq!(async { 1 + 2 }.block_on(py), 3);
        });
    }

    #[test]
    fn block_on_result_maps_ok_through() {
        Python::initialize();
        Python::attach(|py| {
            let ok = async { Ok::<i32, tokio_postgres::Error>(5) };
            assert_eq!(ok.block_on_result(py).unwrap(), 5);
            // The error path (mapping a `tokio_postgres::Error` to a `PyErr`)
            // can't be unit-tested here, as that error type can't be
            // constructed by hand; it's exercised by the integration tests.
        });
    }

    #[test]
    fn get_next_if_ready_returns_buffered_rows_then_signals_end() {
        Python::initialize();
        Python::attach(|py| {
            // `stream::iter` yields each item immediately, so every poll is
            // ready: we get the items, then a `Some(None)` end-of-stream once
            // it's drained, without ever needing to block.
            let stream = stream::iter(vec![Ok::<i32, ()>(1), Ok(2)]).fuse();
            let mut stream = pin!(stream);

            assert_eq!(stream.as_mut().get_next_if_ready(), Some(Some(Ok(1))));
            assert_eq!(stream.as_mut().get_next_if_ready(), Some(Some(Ok(2))));
            // Exhausted: the item is "ready" and is `None`.
            assert_eq!(stream.as_mut().get_next_if_ready(), Some(None));
            // A fused stream keeps reporting end-of-stream rather than panicking.
            assert_eq!(stream.as_mut().get_next_if_ready(), Some(None));

            // `block_on_next` takes the same already-ready value.
            let stream = stream::iter(vec![Ok::<i32, ()>(9)]).fuse();
            let mut stream = pin!(stream);
            assert_eq!(stream.as_mut().block_on_next(py), Some(Ok(9)));
            assert_eq!(stream.as_mut().block_on_next(py), None);
        });
    }

    #[test]
    fn block_on_next_blocks_when_first_poll_is_pending() {
        Python::initialize();
        Python::attach(|py| {
            // A stream whose first poll is `Pending` (it yields back to the
            // runtime before producing the value). `get_next_if_ready` /
            // `now_or_never` polls exactly once and so sees `Pending` and gives
            // up, forcing `block_on_next` down its blocking path.
            let stream = stream::once(async {
                tokio::task::yield_now().await;
                Ok::<i32, ()>(7)
            })
            .fuse();
            let mut stream = pin!(stream);

            assert_eq!(stream.as_mut().get_next_if_ready(), None);
            // `get_next_if_ready` above polled (and so advanced) the *same*
            // pinned stream; `block_on_next` re-polls that same stream via
            // `&mut self`, resuming the yielded future rather than restarting
            // it, so it still resolves to 7.
            assert_eq!(stream.as_mut().block_on_next(py), Some(Ok(7)));
            assert_eq!(stream.as_mut().block_on_next(py), None);

            // And, on a fresh stream, `block_on_next` handles the pending first
            // poll entirely on its own (no preceding `get_next_if_ready`),
            // proving it doesn't rely on being "primed" by an earlier call.
            let stream = stream::once(async {
                tokio::task::yield_now().await;
                Ok::<i32, ()>(8)
            })
            .fuse();
            let mut stream = pin!(stream);
            assert_eq!(stream.as_mut().block_on_next(py), Some(Ok(8)));
        });
    }
}
