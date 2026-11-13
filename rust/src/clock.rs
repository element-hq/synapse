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
//! The complication here is that we need to support manually changing time in
//! both Python and Rust unit tests. In Python unit tests the virtual time is
//! shared across the whole process (as only one test is run at a time), while
//! in Rust multiple independent tests can run in parallel within the same
//! process.
//!
//! We support this by having each [`Clock`] set to either track the
//! process-wide time (either the system clock or the python set global virtual
//! time), or independently track a virtual time that can be set directly (for
//! Rust unit tests).

use std::{
    sync::OnceLock,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use pyo3::{pyfunction, Python};
use tokio::sync::watch;

/// A time that only moves when a test sets it.
///
/// Clones share the same time.
#[derive(Debug, Clone)]
pub struct VirtualTime {
    /// The time in milliseconds since the Unix epoch. This is a channel so
    /// that [`Clock::sleep`] can wait for the time to change.
    millis: watch::Sender<u64>,
}

impl VirtualTime {
    /// A virtual time that starts at `millis` since the Unix epoch.
    pub fn new(millis: u64) -> Self {
        VirtualTime {
            millis: watch::Sender::new(millis),
        }
    }

    /// Move the time to `millis` since the Unix epoch, and wake up any
    /// sleepers that are now due.
    pub fn set_millis(&self, millis: u64) {
        // Only wake the sleepers if the time has changed. Python's unit
        // tests advance the reactor by zero seconds a lot.
        self.millis
            .send_if_modified(|now| std::mem::replace(now, millis) != millis);
    }

    fn now(&self) -> SystemTime {
        UNIX_EPOCH + Duration::from_millis(*self.millis.borrow())
    }

    /// Wait until the time has moved on by `duration`.
    async fn sleep(&self, duration: Duration) {
        // The virtual time counts whole milliseconds. Round up so that we never
        // wake early.
        let duration_millis =
            u64::try_from(duration.as_nanos().div_ceil(1_000_000)).expect("duration is too large");

        let mut millis = self.millis.subscribe();
        let wake_up_time = *millis.borrow() + duration_millis;

        // Wait for the test to move the time far enough. `wait_for` checks
        // the current time before it waits, so we cannot miss a change that
        // happens on another thread meanwhile.
        millis
            .wait_for(|now| *now >= wake_up_time)
            .await
            .expect("we hold a sender, so the channel cannot close");
    }
}

/// The virtual time that Python's unit tests control via
/// [`set_virtual_time_msec`]. Unset outside of those tests, which is what
/// tells a [`Clock::Process`] to read the real time.
static PYTHON_VIRTUAL_TIME: OnceLock<VirtualTime> = OnceLock::new();

/// Pin every [`Clock::Process`] in this process to the given time, in
/// milliseconds since the Unix epoch.
///
/// Only Python's unit tests call this.
///
/// A Rust unit test must never call this, as it would change the time under
/// every other test in the process. Use a [`Clock::Internal`]. It takes a
/// `Python` argument to indicate it should be run from Rust.
#[pyfunction]
pub fn set_virtual_time_msec(_py: Python, millis: u64) {
    PYTHON_VIRTUAL_TIME
        .get_or_init(|| VirtualTime::new(millis))
        .set_millis(millis);
}

/// The current time as the Rust side of a homeserver sees it.
///
/// Like `synapse.util.clock.Clock` on the Python side, this is a wall clock.
/// While these are two separate, independent implementations, they both read
/// the same system clock (on Linux both are
/// `clock_gettime(CLOCK_REALTIME)`). Code that needs a monotonic clock should
/// use [`std::time::Instant`] directly.
///
/// In future, this may be extended to support per-homeserver looping calls
/// (which is why this is not a global clock).
///
/// See [`crate::clock`] docs for more details.
#[derive(Debug, Default)]
pub enum Clock {
    /// Reads the process clock, which is either the system clock or the time
    /// set by Python unit tests. c.f. [`set_virtual_time_msec`].
    #[default]
    Process,

    /// Follows a [`VirtualTime`] that the caller controls for Rust unit tests.
    ///
    /// Ignores the Python virtual time set by [`set_virtual_time_msec`].
    Internal(VirtualTime),
}

impl Clock {
    /// The current time.
    ///
    /// This will return the current time according to the process clock or the
    /// internal virtual time, depending on the variant of the `Clock`.
    pub fn now(&self) -> SystemTime {
        match (self, PYTHON_VIRTUAL_TIME.get()) {
            (Clock::Process, None) => SystemTime::now(),
            (Clock::Process, Some(virtual_time)) => virtual_time.now(),
            (Clock::Internal(virtual_time), None) => virtual_time.now(),
            (Clock::Internal(_), Some(_)) => {
                panic!("Internal clock should not be used with Python virtual time")
            }
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

    /// Wait until `duration` has passed on this clock.
    ///
    /// Use this in place of [`tokio::time::sleep`], which always waits on the
    /// real time. On a virtual time this finishes once the test has moved
    /// the time to the wake-up time.
    ///
    /// Under Python's unit tests, `reactor.advance()` wakes the sleeper but
    /// does not wait for it. The task carries on later, on a Tokio worker
    /// thread. A test has to keep advancing the reactor until the result has
    /// come back to it.
    ///
    /// # Panics
    ///
    /// On the real time this uses Tokio's timer, so it panics if polled
    /// outside a Tokio runtime. On a virtual time it runs anywhere. A unit
    /// test that polls the future by hand therefore cannot catch a caller
    /// that is not on a runtime.
    pub async fn sleep(&self, duration: Duration) {
        match (self, PYTHON_VIRTUAL_TIME.get()) {
            (Clock::Process, None) =>
            {
                #[allow(clippy::disallowed_methods)]
                tokio::time::sleep(duration).await
            }
            (Clock::Process, Some(virtual_time)) => virtual_time.sleep(duration).await,
            (Clock::Internal(virtual_time), None) => virtual_time.sleep(duration).await,
            (Clock::Internal(_), Some(_)) => {
                panic!("Internal clock should not be used with Python virtual time")
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{
        pin::pin,
        task::{Context, Waker},
    };

    use futures::FutureExt;

    use super::*;

    #[test]
    fn virtual_time_overrides_the_real_clock() {
        // Any real wall clock reads later than this (2020-09-13).
        const PLAUSIBLE_WALL_CLOCK_MILLIS: u64 = 1_600_000_000_000;

        let clock = Clock::Process;
        assert!(clock.now_millis() > PLAUSIBLE_WALL_CLOCK_MILLIS);

        let virtual_time = VirtualTime::new(12345);
        let clock = Clock::Internal(virtual_time.clone());
        assert_eq!(clock.now_millis(), 12345);
        assert_eq!(clock.now(), UNIX_EPOCH + Duration::from_millis(12345));

        // Zero is a legitimate virtual time. The memory reactor starts there.
        virtual_time.set_millis(0);
        assert_eq!(clock.now_millis(), 0);
    }

    #[test]
    fn virtual_time_sleepers_are_woken_up_in_order() {
        let virtual_time = VirtualTime::new(0);
        let clock = Clock::Internal(virtual_time.clone());

        // Schedule two sleepers with different durations. The one with the
        // shorter duration should wake up first.
        let mut sleep1 = pin!(clock.sleep(Duration::from_millis(10)));
        let mut sleep2 = pin!(clock.sleep(Duration::from_millis(5)));

        let mut cx = Context::from_waker(Waker::noop());

        // Initially, both sleepers should be pending because virtual time has
        // not advanced yet.
        assert!(sleep1.poll_unpin(&mut cx).is_pending());
        assert!(sleep2.poll_unpin(&mut cx).is_pending());

        // Advance virtual time to wake up the sleepers.
        virtual_time.set_millis(5);

        // After advancing virtual time to 5 milliseconds, the second sleeper
        // (with a 5ms duration) should be ready, while the first sleeper
        // (with a 10ms duration) should still be pending.
        assert!(sleep1.poll_unpin(&mut cx).is_pending());
        assert!(sleep2.poll_unpin(&mut cx).is_ready());

        virtual_time.set_millis(10);

        // After advancing virtual time to 10 milliseconds, the first sleeper
        // (with a 10ms duration) should now be ready.
        assert!(sleep1.poll_unpin(&mut cx).is_ready());
    }

    #[test]
    fn virtual_time_advance_notifies_the_sleeping_task() {
        let virtual_time = VirtualTime::new(0);
        let clock = Clock::Internal(virtual_time.clone());

        let (waker, awoken_count) = futures_test::task::new_count_waker();
        let mut cx = Context::from_waker(&waker);

        let mut sleep = pin!(clock.sleep(Duration::from_millis(5)));
        assert!(sleep.poll_unpin(&mut cx).is_pending());
        assert_eq!(awoken_count.get(), 0);

        // Setting the time it already has wakes nobody.
        virtual_time.set_millis(0);
        assert_eq!(awoken_count.get(), 0);

        // The other tests poll by hand. A real task is only polled again
        // once its waker is called.
        virtual_time.set_millis(5);
        assert_eq!(awoken_count.get(), 1);
        assert!(sleep.poll_unpin(&mut cx).is_ready());
    }
}
