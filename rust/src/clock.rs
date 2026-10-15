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

//! A clock for the Rust side of Synapse.
//!
//! Rust code often needs the current time somewhere it cannot cheaply ask
//! Python for it, such as on a tokio worker without the GIL. Each
//! homeserver's [`RustRuntime`](crate::runtime::RustRuntime) owns a [`Clock`]
//! that can be read without the GIL.
//!
//! Synapse's unit tests run against a virtual reactor clock, where time only
//! moves when a test says so. The test reactor keeps this clock in step via
//! [`set_virtual_time_msec`]. See `ThreadedMemoryReactorClock` in
//! `tests/server.py`.
//!
//! Tokio's timers know nothing of that virtual clock, so a Rust task that
//! sleeps with [`tokio::time::sleep`] under trial would wait out real time
//! while the test thinks no time passes. [`Clock::sleep`] sleeps on the
//! virtual clock while it is pinned. Code that sleeps takes a [`Sleeper`] so
//! that its Rust unit tests can sleep on tokio's (pausable) clock instead.

use std::{
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use pyo3::pyfunction;
use tokio::sync::oneshot;

/// A sentinel value stored in [`VIRTUAL_MILLIS`] while the clock is
/// reading the real time. This value indicates that the clock should read the
/// real system time rather than a virtual time.
///
/// We choose a value that will never be used in tests, namely the maximum `u64`
/// value. This is 584 million years after the epoch.
const REAL_TIME_SENTINEL: u64 = u64::MAX;

/// The virtual time in milliseconds since the Unix epoch, or
/// [`REAL_TIME_SENTINEL`]. Only ever set by tests, via [`set_virtual_time_msec`].
///
/// We use an AtomicU64 (rather than e.g. `Option<..>`) so that reads are fast
/// and do not require locking.
static VIRTUAL_MILLIS: AtomicU64 = AtomicU64::new(REAL_TIME_SENTINEL);

/// Tasks sleeping on the virtual clock: the virtual time each wakes at, and
/// the channel that wakes it. [`set_virtual_time_msec`] wakes the ones that
/// are due.
///
/// [`Clock::sleep`] reads the virtual time while holding this lock, and
/// [`set_virtual_time_msec`] takes the lock after storing the new time. So a
/// sleeper either sees the new time, or is in the list when the setter scans
/// it, and no wake-up is lost.
static VIRTUAL_SLEEPERS: Mutex<Vec<(u64, oneshot::Sender<()>)>> = Mutex::new(Vec::new());

/// Pin the clock to the given time, in milliseconds since the Unix epoch.
///
/// Only tests call this. Nothing unpins the clock, since a test reactor
/// and the runtimes attached to it live for a single test.
#[pyfunction]
pub fn set_virtual_time_msec(millis: u64) {
    if millis == REAL_TIME_SENTINEL {
        panic!("Cannot set virtual time to the sentinel value REAL_TIME_SENTINEL");
    }

    VIRTUAL_MILLIS.store(millis, Ordering::Relaxed);

    let due = {
        let mut sleepers = VIRTUAL_SLEEPERS.lock().expect("poisoned");
        let (due, pending) = sleepers
            .drain(..)
            .partition::<Vec<_>, _>(|(wake_at, _)| *wake_at <= millis);
        *sleepers = pending;
        due
    };

    for (_, waker) in due {
        // The sleeper may have been dropped, which is fine.
        let _ = waker.send(());
    }
}

/// The current time as the Rust side of a homeserver sees it.
///
/// Like `synapse.util.clock.Clock` on the Python side, this is a wall clock.
/// While these are two separate, independent implementations, they both read
/// the same system clock (on Linux both are
/// `clock_gettime(CLOCK_REALTIME)`).Code that needs a monotonic clock should
/// use [`std::time::Instant`] directly.
///
/// In future, this may be extended to support per-homeserver looping calls
/// (which is why this is not a global clock).
///
/// See [`crate::clock`] docs for more details.
pub struct Clock {}

impl Default for Clock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock {
    /// A clock reading the real time.
    pub fn new() -> Self {
        Clock {}
    }

    /// The current time.
    ///
    /// Reads the same system clock as `synapse.util.clock.Clock.time_msec()`
    /// on the Python side. See the module docs.
    pub fn now(&self) -> SystemTime {
        // Check if virtual time is set, otherwise fall back to real time.
        match VIRTUAL_MILLIS.load(Ordering::Relaxed) {
            // On Linux this is a vDSO call rather than a syscall, so it is
            // cheap enough to make on every read.
            REAL_TIME_SENTINEL => SystemTime::now(),
            virtual_millis => UNIX_EPOCH + Duration::from_millis(virtual_millis),
        }
    }

    /// [`Clock::now`] in milliseconds since the Unix epoch.
    ///
    /// Panics if the system clock is set before the Unix epoch, 1970. This was
    /// checked at startup, so this panic should only ever occur if the system
    /// clock is manually set before the Unix epoch during runtime.
    pub fn now_millis(&self) -> u64 {
        self.now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock was set before the Unix epoch, 1970")
            .as_millis() as u64
    }

    /// Sleep for `duration`.
    ///
    /// While a test has pinned the clock this waits for the virtual time to
    /// reach the deadline, which happens when the test reactor is advanced
    /// past it. Otherwise it is [`tokio::time::sleep`], and must be awaited
    /// on a tokio runtime.
    pub fn sleep(&self, duration: Duration) -> impl Future<Output = ()> + Send + 'static {
        let virtual_wake = {
            let mut sleepers = VIRTUAL_SLEEPERS.lock().expect("poisoned");
            match VIRTUAL_MILLIS.load(Ordering::Relaxed) {
                REAL_TIME_SENTINEL => None,
                now => {
                    let wake_at = now.saturating_add(duration.as_millis() as u64);
                    let (tx, rx) = oneshot::channel();
                    if wake_at <= now {
                        let _ = tx.send(());
                    } else {
                        sleepers.push((wake_at, tx));
                    }
                    Some(rx)
                }
            }
        };

        async move {
            match virtual_wake {
                Some(rx) => {
                    // An error means the sender was dropped, which only
                    // happens if the process is going away.
                    let _ = rx.await;
                }
                None => tokio::time::sleep(duration).await,
            }
        }
    }
}

/// Something that can sleep. Code that waits between attempts takes one of
/// these rather than calling a timer directly, so that its tests choose the
/// clock. See the module docs.
pub trait Sleeper: Send + Sync + 'static {
    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>>;
}

/// Sleeps on a homeserver's [`Clock`], which follows the virtual clock under
/// trial.
impl Sleeper for Clock {
    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(Clock::sleep(self, duration))
    }
}

/// Sleeps on tokio's clock, for Rust unit tests (which can pause and advance
/// it with `tokio::time::pause`).
pub struct TokioSleeper;

impl Sleeper for TokioSleeper {
    fn sleep(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(tokio::time::sleep(duration))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn virtual_time_overrides_the_real_clock() {
        // Any real wall clock reads later than this (2020-09-13).
        const PLAUSIBLE_WALL_CLOCK_MILLIS: u64 = 1_600_000_000_000;

        let clock = Clock::new();
        assert!(clock.now_millis() > PLAUSIBLE_WALL_CLOCK_MILLIS);

        set_virtual_time_msec(12345);
        assert_eq!(clock.now_millis(), 12345);
        assert_eq!(clock.now(), UNIX_EPOCH + Duration::from_millis(12345));

        // Zero is a legitimate virtual time. The memory reactor starts there.
        set_virtual_time_msec(0);
        assert_eq!(clock.now_millis(), 0);

        // Sleeping on the pinned clock wakes when the virtual time reaches
        // the deadline, and not before. This lives in the same test because
        // the virtual time is process-global.
        let mut sleep = std::pin::pin!(clock.sleep(Duration::from_millis(500)));
        // `Waker::noop` needs a newer rustc than our MSRV.
        let waker = futures::task::noop_waker();
        let mut cx = std::task::Context::from_waker(&waker);
        assert!(sleep.as_mut().poll(&mut cx).is_pending());
        set_virtual_time_msec(499);
        assert!(sleep.as_mut().poll(&mut cx).is_pending());
        set_virtual_time_msec(500);
        assert!(sleep.as_mut().poll(&mut cx).is_ready());

        // A zero-length sleep is ready at once.
        let mut sleep = std::pin::pin!(clock.sleep(Duration::ZERO));
        assert!(sleep.as_mut().poll(&mut cx).is_ready());
    }
}
