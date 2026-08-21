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

use std::{
    future::Future,
    pin::{pin, Pin},
};

use futures::{stream::FusedStream, FutureExt, StreamExt};
use pyo3::{marker::Ungil, PyResult, Python};
use tokio::runtime::Handle;

use crate::database::postgres::pg_err_to_py;

/// Block on a future on a given runtime, releasing the GIL while we wait.
///
/// Note that this drives the future on *this* thread, not on the runtime's
/// worker threads. See [`tokio::runtime::Handle::block_on`] for details.
pub trait BlockingPostgres
where
    Self: Future + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    /// Drive `self` to completion on the given runtime, returning its output.
    /// Releases the GIL for the duration so the wait doesn't block other Python
    /// threads.
    ///
    /// `handle` is expected to be the server's shared runtime (see
    /// [`crate::tokio_runtime`]). It is passed in explicitly rather than taken
    /// from the thread-local context via [`Handle::current`], so callers work
    /// from any Python thread with no need to have *entered* the runtime first.
    /// The blocking wait runs on the calling (Python) thread, never on a
    /// runtime worker, so it cannot starve the worker threads that complete the
    /// future.
    fn block_on(self, py: Python<'_>, handle: &Handle) -> Self::Output {
        // Check if this future is already ready, and if so return immediately
        // without needlessly releasing and reacquiring the GIL.
        //
        // This is basically the same as `FutureExt::now_or_never`, except we a)
        // keep the pinned future to poll again below, and b) enter the runtime
        // in case the future needs it.
        let mut pin_self = pin!(self);
        {
            let _guard = handle.enter();
            let noop_waker = std::task::Waker::noop();
            let mut cx = std::task::Context::from_waker(noop_waker);
            match pin_self.poll_unpin(&mut cx) {
                std::task::Poll::Ready(val) => return val,
                std::task::Poll::Pending => (),
            }
        }

        // Note: this will drive the future to completion on *this* thread, not
        // on the runtime's worker threads. See `Handle::block_on` for details.
        py.detach(|| handle.block_on(pin_self))
    }
}

/// Same as [`BlockingPostgres`], but for futures that yield a
/// [`tokio_postgres::Result`], mapping any error into a Python exception.
pub trait BlockingPostgresResult<T>
where
    Self: Future<Output = Result<T, tokio_postgres::Error>> + Sized + Send + Ungil,
    Self::Output: Ungil + Send,
{
    /// Block on `self` on the given runtime and convert a Postgres error into
    /// a `PyErr`.
    fn block_on_pg_result(self, py: Python<'_>, handle: &Handle) -> PyResult<T> {
        self.block_on(py, handle).map_err(pg_err_to_py)
    }
}

// Blanket impls: every suitable future automatically gets `block_on` /
// `block_on_pg_result`, so callers can write `fut.block_on(py, handle)`
// directly.
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

/// Pull items from a [`FusedStream`] from synchronous Python code, blocking on
/// a given runtime only when the next item isn't already buffered.
///
/// Implemented for any pinned, fused stream (e.g. `Pin<&mut Fuse<S>>`) whose
/// items can cross the GIL-release boundary, so it can be tested against an
/// in-memory stream as well as a [`tokio_postgres::RowStream`].
///
/// The [`FusedStream`] bound matters because [`Self::get_next_if_ready`] may
/// poll the stream again after it has finished. A bare `Stream` is allowed to
/// panic if polled past completion; a fused stream keeps yielding `None`, so
/// calls after exhaustion are safe.
pub trait BlockingPostgresStream
where
    Self: FusedStream + Sized + Send + Ungil + Unpin,
    Self::Item: Ungil + Send,
{
    /// Get the next item from the stream, blocking on the given runtime if
    /// necessary.
    ///
    /// If the stream is not ready to yield an item, this will release the GIL
    /// and block until the next item is available.
    ///
    /// This method will return `None` if the stream is exhausted.
    fn block_on_next(&mut self, py: Python<'_>, handle: &Handle) -> Option<Self::Item> {
        if self.is_terminated() {
            return None;
        }

        self.next().block_on(py, handle)
    }
}

// Blanket impl over any pinned, fused stream, not just `RowStream`, so the
// tests can use an in-memory stream.
impl<S> BlockingPostgresStream for Pin<&mut S>
where
    Self: FusedStream + Send + Ungil + Unpin,
    <Self as futures::stream::Stream>::Item: Ungil + Send,
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

    /// A throwaway runtime standing in for the shared one. The helpers take
    /// the runtime to block on explicitly, so each test just passes this
    /// runtime's handle.
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
        Python::attach(|py| {
            assert_eq!(async { 1 + 2 }.block_on(py, rt.handle()), 3);
        });
    }

    #[test]
    fn block_on_handles_ready_future() {
        Python::initialize();
        let rt = test_runtime();
        Python::attach(|py| {
            // A future that returns ready on the first poll.
            let fut = std::future::ready(9);
            assert_eq!(fut.block_on(py, rt.handle()), 9);
        });
    }

    #[test]
    fn block_on_handles_not_ready_future() {
        Python::initialize();
        let rt = test_runtime();
        Python::attach(|py| {
            // A future that requires polling multiple times to complete.
            let fut = async {
                tokio::task::yield_now().await;
                9
            };
            assert_eq!(fut.block_on(py, rt.handle()), 9);
        });
    }

    #[test]
    fn block_on_pg_result_maps_ok_through() {
        Python::initialize();
        let rt = test_runtime();
        Python::attach(|py| {
            let ok = async { Ok::<i32, tokio_postgres::Error>(5) };
            assert_eq!(ok.block_on_pg_result(py, rt.handle()).unwrap(), 5);
            // The error path (mapping a `tokio_postgres::Error` to a `PyErr`)
            // isn't covered here, because that error type can't be constructed
            // by hand. Exercising it needs a live server.
        });
    }

    #[test]
    fn block_on_next_blocks_when_first_poll_is_pending() {
        Python::initialize();
        let rt = test_runtime();
        Python::attach(|py| {
            // A stream whose first poll is `Pending` (it yields back to the
            // runtime before producing the value). `now_or_never` polls exactly
            // once and so sees `Pending` and gives up, forcing `block_on_next`
            // down its blocking path.
            let stream = stream::once(async {
                tokio::task::yield_now().await;
                Ok::<i32, ()>(7)
            });
            let mut stream = pin!(stream);

            assert_eq!(stream.as_mut().next().now_or_never(), None);
            // `next().now_or_never()` above polled (and so advanced) the *same*
            // pinned stream; `block_on_next` re-polls that same stream via
            // `&mut self`, resuming the yielded future rather than restarting
            // it, so it still resolves to 7.
            assert_eq!(stream.as_mut().block_on_next(py, rt.handle()), Some(Ok(7)));
            assert_eq!(stream.as_mut().block_on_next(py, rt.handle()), None);

            // And, on a fresh stream, `block_on_next` handles a pending first
            // poll on its own, with no preceding `get_next_if_ready`.
            let stream = stream::once(async {
                tokio::task::yield_now().await;
                Ok::<i32, ()>(8)
            });
            let mut stream = pin!(stream);
            assert_eq!(stream.as_mut().block_on_next(py, rt.handle()), Some(Ok(8)));
        });
    }
}
