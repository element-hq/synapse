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

//! Run closures on the Twisted reactor thread from Tokio worker threads,
//! without taking the GIL on the worker.
//!
//! Taking the GIL on a worker would block the worker until the Python thread
//! releases it, which under load means waiting for the interpreter's switch
//! interval. This could severely impact the performance of the Tokio runtime.
//!
//! # Implementation
//!
//! The implementation combines a queue of closures with the self-pipe trick,
//! which is the same implementation as Twisted uses for `callFromThread`.
//!
//! ```
//! Tokio worker                          Twisted reactor
//!      │                                      │
//!      TwistedDispatcher                      |
//!      ├─ Add a closure to a Rust queue       │
//!      └─ Write one byte to socket A ───────► socket B becomes readable
//!                                             │
//!                                             TwistedDispatchReader
//!                                             └─ Reader.doRead()
//!                                                  ├─ Drain socket B
//!                                                  ├─ Take queued closures
//!                                                  └─ Execute them
//! ```
//!
//! The [`TwistedDispatcher`] worker holds a queue of closures and the write end of a
//! unix socket pair. A worker pushes a closure and writes one byte. The
//! [`TwistedDispatchReader`] holds the read end and is registered with the
//! Twisted reactor via `addReader`. The Twisted reactor is woken up and calls
//! the `doRead` method on the Twisted reactor thread, which then reads from the
//! queue and runs any pending closures.
//!
//! One pair exists per homeserver, owned by [`crate::runtime::RustRuntime`],
//! which registers the reader on construction and removes it at shutdown.
//!
//! # Homeserver shutdown
//!
//! When the homeserver shuts down, the dispatcher is closed and `dispatch` will
//! return an error. The caller should stop what it is doing as its associated
//! homeserver has been shut down.

use std::{
    io::{ErrorKind, Read, Write},
    os::{
        fd::{AsRawFd, RawFd},
        unix::net::UnixStream,
    },
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{Arc, Mutex},
};

use anyhow::Context;
use log::error;
use pyo3::prelude::*;

/// A queued closure. Runs on the Twisted reactor thread.
type DispatchedCall = Box<dyn FnOnce(Python<'_>) + Send + 'static>;

/// The Tokio-facing half: a queue of closures plus the write end of the wakeup
/// socket.
pub struct TwistedDispatcher {
    /// The queue of closures to run on the Twisted reactor thread. Or None if
    /// the dispatcher has been closed, indicating the homeserver has been
    /// shutdown.
    queue: Mutex<Option<Vec<DispatchedCall>>>,
    write_end: UnixStream,
}

impl TwistedDispatcher {
    /// Queue `f` to run on the Twisted reactor thread, and wake the reactor.
    ///
    /// Never takes the GIL, so this is safe to call from a Tokio worker.
    ///
    /// Returns an error if the dispatcher has been closed, which indicates the
    /// homeserver has been shutdown.
    pub fn dispatch<F>(&self, f: F) -> anyhow::Result<()>
    where
        F: FnOnce(Python<'_>) + Send + 'static,
    {
        // First add the closure to the queue.
        {
            let mut queue = self.queue.lock().expect("dispatcher poisoned");
            let Some(queue) = &mut *queue else {
                return Err(anyhow::anyhow!("dispatcher is closed"));
            };

            queue.push(Box::new(f));
        }

        // Second, write a byte to the socket to wakeup the Twisted reactor.
        //
        // Errors are ignored. `WouldBlock` means the socket buffer is full, so
        // a wakeup is already pending. Nothing else can fail while the reader
        // holds the other end.
        let _ = (&self.write_end).write(b"x");

        Ok(())
    }
}

/// The Twisted-reactor-facing half of a [`TwistedDispatcher`].
#[pyclass(frozen)]
pub struct TwistedDispatchReader {
    dispatcher: Arc<TwistedDispatcher>,
    read_end: UnixStream,
}

#[pymethods]
impl TwistedDispatchReader {
    fn fileno(&self) -> RawFd {
        self.read_end.as_raw_fd()
    }

    #[pyo3(name = "logPrefix")]
    fn log_prefix(&self) -> &'static str {
        "synapse-rust-twisted-dispatch"
    }

    /// Called by the Twisted reactor when the wakeup socket has something in it.
    ///
    /// Runs on the reactor thread.
    #[pyo3(name = "doRead")]
    fn do_read(&self, py: Python<'_>) {
        // Drain the socket before taking the queue. This way around avoids
        // races between the socket being drained and closures being added to
        // the queue.
        let mut buf = [0u8; 64];
        loop {
            match (&self.read_end).read(&mut buf) {
                Ok(0) => break,
                Ok(_) => continue,
                // Interrupted: try again.
                Err(err) if err.kind() == ErrorKind::Interrupted => continue,
                // WouldBlock: the socket buffer is empty, so we can stop
                // draining.
                Err(err) if err.kind() == ErrorKind::WouldBlock => break,
                Err(err) => {
                    // This should not happen, as the socket is never closed
                    // except when this struct is dropped.
                    //
                    // We can't do much here except log the error and break out
                    // of the loop.
                    error!("Error reading from wakeup socket: {:?}", err);
                    break;
                }
            }
        }

        // Take from the queue. Ensuring the lock is released before the
        // closures run. This may be `None` if the dispatcher has been closed.
        let jobs = self
            .dispatcher
            .queue
            .lock()
            .expect("dispatcher poisoned")
            .as_mut()
            .map(std::mem::take);
        if let Some(jobs) = jobs {
            self.run_dispatched(py, jobs);
        }

        // Returning `None` keeps the reader registered.
    }

    /// Called by the Twisted reactor at shutdown. The socket closes when the
    /// owning `RustRuntime` drops the reader, so there is nothing to do here.
    #[pyo3(name = "connectionLost")]
    fn connection_lost(&self, _reason: &Bound<'_, PyAny>) {}
}

impl TwistedDispatchReader {
    /// Close the dispatcher so that no further closures can be queued, then
    /// run the ones already queued.
    pub fn close_and_drain(&self, py: Python<'_>) {
        let Some(jobs) = self
            .dispatcher
            .queue
            .lock()
            .expect("dispatcher poisoned")
            .take()
        else {
            return;
        };

        self.run_dispatched(py, jobs);
    }

    /// Run closures taken from the queue. Must be called with the queue lock
    /// released.
    fn run_dispatched(&self, py: Python<'_>, jobs: Vec<DispatchedCall>) {
        for job in jobs {
            // A panic here would leave `doRead` as an exception, and Twisted
            // responds to that by dropping the reader. No later closure would
            // ever run. Log it instead.
            //
            // `catch_unwind(AssertUnwindSafe(..))` is exactly the same as what
            // pyo3 does when catching panics in Rust code called from Python.
            //
            // `UnwindSafe` guards against code after `catch_unwind` reading
            // state that the panicking code was part way through mutating.
            // Since we never read `job` after this, it is safe to catch the
            // panic. `Python` is already `UnwindSafe`.
            let ran = catch_unwind(AssertUnwindSafe(move || job(py)));
            if ran.is_err() {
                log::error!("panic while running a dispatched Rust completion");
            }
        }
    }
}

/// Build a connected [`TwistedDispatcher`]/[`TwistedDispatchReader`] pair.
pub fn new_pair() -> PyResult<(Arc<TwistedDispatcher>, TwistedDispatchReader)> {
    let (read_end, write_end) =
        UnixStream::pair().context("creating the Tokio wakeup socket pair")?;

    // Neither end may block. The writer is a Tokio worker and the reader is
    // the Twisted reactor thread.
    read_end
        .set_nonblocking(true)
        .context("making the Tokio wakeup socket non-blocking")?;
    write_end
        .set_nonblocking(true)
        .context("making the Tokio wakeup socket non-blocking")?;

    let dispatcher = Arc::new(TwistedDispatcher {
        queue: Mutex::new(Some(Vec::new())),
        write_end,
    });
    let reader = TwistedDispatchReader {
        dispatcher: Arc::clone(&dispatcher),
        read_end,
    };

    Ok((dispatcher, reader))
}
