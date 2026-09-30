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
//! It is a *wall* clock, and so is `synapse.util.clock.Clock.time_msec()` on
//! the Python side. Synapse does nothing to keep the two in sync. They agree
//! because they read the same system clock. The Python one calls the
//! reactor's `seconds()`, which is `time.time()`. This one calls
//! [`SystemTime::now`]. On Linux both are `clock_gettime(CLOCK_REALTIME)`.
//! Code that needs a monotonic clock should use [`std::time::Instant`]
//! directly.
//!
//! Synapse's unit tests run against a virtual reactor clock, where time only
//! moves when a test says so. The test reactor keeps this clock in step via
//! [`Clock::set_virtual_time`]. See `ThreadedMemoryReactorClock` in
//! `tests/server.py`.

use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

/// Stored in [`Clock::virtual_millis`] while the clock is reading the real
/// time. No test will pin the clock to it, since as a timestamp it is 584
/// million years after the epoch.
const REAL_TIME: u64 = u64::MAX;

/// The current time as the Rust side of a homeserver sees it.
pub struct Clock {
    /// The virtual time in milliseconds since the Unix epoch, or [`REAL_TIME`].
    /// Only ever set by tests, via [`Clock::set_virtual_time`].
    virtual_millis: AtomicU64,
}

impl Default for Clock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock {
    /// A clock reading the real time.
    pub fn new() -> Self {
        Clock {
            virtual_millis: AtomicU64::new(REAL_TIME),
        }
    }

    /// The current time.
    ///
    /// Reads the same system clock as `synapse.util.clock.Clock.time_msec()`
    /// on the Python side. See the module docs.
    pub fn now(&self) -> SystemTime {
        match self.virtual_millis.load(Ordering::Relaxed) {
            // On Linux this is a vDSO call rather than a syscall, so it is
            // cheap enough to make on every read.
            REAL_TIME => SystemTime::now(),
            virtual_millis => UNIX_EPOCH + Duration::from_millis(virtual_millis),
        }
    }

    /// [`Clock::now`] in milliseconds since the Unix epoch.
    pub fn now_millis(&self) -> u64 {
        self.now()
            .duration_since(UNIX_EPOCH)
            // Only fails if the system clock is set before 1970. Callers
            // would rather have a number than an error.
            .map_or(0, |duration| duration.as_millis() as u64)
    }

    /// Pin the clock to the given time, in milliseconds since the Unix epoch.
    ///
    /// Only tests call this. Nothing unpins the clock, since a test reactor
    /// and the runtimes attached to it live for a single test.
    pub fn set_virtual_time(&self, millis: u64) {
        self.virtual_millis.store(millis, Ordering::Relaxed);
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

        clock.set_virtual_time(12345);
        assert_eq!(clock.now_millis(), 12345);
        assert_eq!(clock.now(), UNIX_EPOCH + Duration::from_millis(12345));

        // Zero is a legitimate virtual time. The memory reactor starts there.
        clock.set_virtual_time(0);
        assert_eq!(clock.now_millis(), 0);
    }
}
