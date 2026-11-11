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
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, OnceLock,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use pyo3::{pyfunction, Python};

/// A time that only moves when a test sets it.
///
/// Clones share the same time.
#[derive(Debug, Clone)]
pub struct VirtualTime {
    /// The time in milliseconds since the Unix epoch.
    millis: Arc<AtomicU64>,
}

impl VirtualTime {
    /// A virtual time that starts at `millis` since the Unix epoch.
    pub fn new(millis: u64) -> Self {
        VirtualTime {
            millis: Arc::new(AtomicU64::new(millis)),
        }
    }

    /// Move the time to `millis` since the Unix epoch.
    pub fn set_millis(&self, millis: u64) {
        self.millis.store(millis, Ordering::Relaxed);
    }

    fn now(&self) -> SystemTime {
        UNIX_EPOCH + Duration::from_millis(self.millis.load(Ordering::Relaxed))
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
}

#[cfg(test)]
mod tests {
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
}
