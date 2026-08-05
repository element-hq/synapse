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

//! Extension traits for driving [`tokio_postgres`] futures to completion from
//! synchronous, GIL-holding Python code.
//!
//! Both [`BlockingPostgres`] and [`BlockingPostgresStream`] release the GIL
//! (`py.detach`) while blocking on the shared tokio runtime, so other Python
//! threads can make progress (and so the runtime's own connection task can run)
//! while we wait. The [`Ungil`] bounds are what let us hand the future across
//! the `detach` boundary.
//!
//! The stream helper is generic over the underlying stream type rather than
//! hard-wired to [`tokio_postgres::RowStream`], so code built on it can be
//! unit-tested against an in-memory stream with no live database — see
//! [`BlockingPostgresStream`]'s tests.

use std::{future::Future, pin::Pin};

use futures::{stream::Fuse, FutureExt, StreamExt};
use pyo3::{marker::Ungil, PyResult, Python};
use tokio::runtime::Handle;

use crate::database::postgres::pg_err_to_py;

/// Block on a future on the shared runtime, releasing the GIL while we wait.
pub trait BlockingPostgres
where
    Self: Future + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    /// Drive `self` to completion on the shared runtime, returning its output.
    /// Releases the GIL for the duration so the wait doesn't block other Python
    /// threads.
    ///
    /// Blocks on the runtime the calling thread has *entered* — taken from the
    /// thread-local context via [`Handle::current`], not passed in. Every
    /// thread that drives these calls must have the server's shared runtime
    /// (see [`crate::tokio_runtime`]) entered first with
    /// [`Handle::enter`](tokio::runtime::Handle::enter); [`Handle::current`]
    /// panics otherwise. The blocking wait runs on the calling (Python) thread,
    /// never on a runtime worker, so it cannot starve the worker threads that
    /// complete the future.
    fn block_on(self, py: Python<'_>) -> Self::Output {
        py.detach(|| Handle::current().block_on(self))
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

/// Pull items from a [`Fuse`]d stream from synchronous Python code, blocking on
/// the shared runtime only when the next item isn't already buffered.
///
/// Implemented for any pinned, fused stream (`Pin<&mut Fuse<S>>`) whose items
/// can cross the GIL-release boundary, so it can be tested against an
/// in-memory stream as well as a [`tokio_postgres::RowStream`].
///
/// The [`Fuse`] bound matters because [`Self::get_next_if_ready`] may poll the
/// stream again after it has finished. A bare `Stream` is allowed to panic if
/// polled past completion; a fused stream keeps yielding `None`, so calls
/// after exhaustion are safe.
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

// Blanket impl over any pinned, fused stream, not just `RowStream`, so the
// tests can use an in-memory stream.
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
    use tokio::runtime::Runtime;

    use super::*;

    /// A throwaway runtime standing in for the shared one. The helpers only
    /// need *some* runtime entered on the current thread, so that
    /// [`Handle::current`] resolves. Each test calls `rt.enter()` and holds
    /// the guard for the duration.
    fn test_runtime() -> Runtime {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap()
    }

    #[test]
    fn block_on_runs_future_and_returns_output() {
        Python::initialize();
        let rt = test_runtime();
        let _guard = rt.enter();
        Python::attach(|py| {
            assert_eq!(async { 1 + 2 }.block_on(py), 3);
        });
    }

    #[test]
    fn block_on_result_maps_ok_through() {
        Python::initialize();
        let rt = test_runtime();
        let _guard = rt.enter();
        Python::attach(|py| {
            let ok = async { Ok::<i32, tokio_postgres::Error>(5) };
            assert_eq!(ok.block_on_result(py).unwrap(), 5);
            // The error path (mapping a `tokio_postgres::Error` to a `PyErr`)
            // isn't covered here, because that error type can't be constructed
            // by hand. Exercising it needs a live server.
        });
    }

    #[test]
    fn get_next_if_ready_returns_buffered_rows_then_signals_end() {
        Python::initialize();
        let rt = test_runtime();
        let _guard = rt.enter();
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
        let rt = test_runtime();
        let _guard = rt.enter();
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

            // And, on a fresh stream, `block_on_next` handles a pending first
            // poll on its own, with no preceding `get_next_if_ready`.
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
