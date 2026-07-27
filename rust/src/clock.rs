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

//! A clock for the Rust crate.
//!
//! Rust code frequently needs the current time, but often runs somewhere it
//! cannot cheaply ask Python for it: on a Tokio worker without the GIL, or deep
//! inside a data structure where reaching back out to a
//! `synapse.util.clock.Clock` would mean threading a `Py<PyAny>` through
//! everything. This module gives such code a single, GIL-free way to read the
//! time.
//!
//! The clock is a *wall* clock, deliberately: it reports the same thing as
//! `Clock.time_msec()` (which is `reactor.seconds()`), so times can cross the
//! Python/Rust boundary and mean the same thing on both sides. Like Python's,
//! it is therefore subject to the system clock being stepped. Code that needs a
//! monotonic clock (e.g. for measuring durations) should use
//! [`std::time::Instant`] directly.
//!
//! Synapse's unit tests drive a virtual reactor clock, and time in tests only
//! moves when a test says so. To keep that property, tests pin this clock to
//! the reactor's time via [`set_virtual_time_msec`]; see
//! `ThreadedMemoryReactorClock` in `tests/server.py`.

use std::{
    sync::atomic::{AtomicI64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use pyo3::{
    exceptions::PyValueError,
    pyfunction,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    wrap_pyfunction, Bound, PyResult, Python,
};

/// The current virtual time in milliseconds since the Unix epoch, or a negative
/// value if we should be using the real clock.
///
/// Only ever set by tests, via [`set_virtual_time_msec`].
static VIRTUAL_NOW_MILLIS: AtomicI64 = AtomicI64::new(-1);

/// The current time, in milliseconds since the Unix epoch.
///
/// This is the same clock as `synapse.util.clock.Clock.time_msec()`.
pub fn now_unix_millis() -> u64 {
    // A relaxed load of a read-mostly cache line plus a perfectly predicted
    // branch; in the normal case this is dominated by the `SystemTime::now()`
    // below, which on Linux is a vDSO call rather than a syscall.
    let virtual_now = VIRTUAL_NOW_MILLIS.load(Ordering::Relaxed);
    if virtual_now >= 0 {
        return virtual_now as u64;
    }

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        // The system clock being before the Unix epoch is not something we can
        // sensibly recover from, and callers would rather have a number than an
        // error. Clamp instead.
        .map_or(0, |duration| duration.as_millis() as u64)
}

/// The current time, as a [`SystemTime`].
///
/// See [`now_unix_millis`].
pub fn now_system_time() -> SystemTime {
    UNIX_EPOCH + Duration::from_millis(now_unix_millis())
}

/// Pin the Rust clock to the given time, in milliseconds since the Unix epoch.
///
/// Passing `None` restores the real clock.
///
/// This exists for tests, which run against a virtual reactor clock: without
/// it, Rust code would see wall-clock time while the Python it is called from
/// sees test time.
#[pyfunction]
#[pyo3(signature = (millis))]
pub fn set_virtual_time_msec(millis: Option<i64>) -> PyResult<()> {
    let value = match millis {
        Some(millis) if millis < 0 => {
            return Err(PyValueError::new_err(
                "virtual time must not be before the Unix epoch",
            ))
        }
        Some(millis) => millis,
        None => -1,
    };

    VIRTUAL_NOW_MILLIS.store(value, Ordering::Relaxed);

    Ok(())
}

/// The current time as the Rust clock sees it, in milliseconds since the Unix
/// epoch.
///
/// Exposed so that tests can assert that Rust and Python agree about the time.
#[pyfunction]
pub fn time_msec() -> u64 {
    now_unix_millis()
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "clock")?;

    child_module.add_function(wrap_pyfunction!(set_virtual_time_msec, m)?)?;
    child_module.add_function(wrap_pyfunction!(time_msec, m)?)?;

    m.add_submodule(&child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import clock` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.clock", child_module)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Everything in this module talks to one process-wide static, so the
    /// virtual/real transitions have to be exercised by a single test rather
    /// than racing several against each other.
    #[test]
    fn virtual_time_overrides_the_real_clock() {
        // By default we report something that looks like a real wall clock.
        assert!(now_unix_millis() > 1_600_000_000_000);

        set_virtual_time_msec(Some(12345)).unwrap();
        assert_eq!(now_unix_millis(), 12345);
        assert_eq!(now_system_time(), UNIX_EPOCH + Duration::from_millis(12345));

        // Zero is a legitimate virtual time: the memory reactor starts there.
        set_virtual_time_msec(Some(0)).unwrap();
        assert_eq!(now_unix_millis(), 0);

        assert!(set_virtual_time_msec(Some(-1)).is_err());
        assert_eq!(now_unix_millis(), 0, "a rejected value must not be stored");

        set_virtual_time_msec(None).unwrap();
        assert!(now_unix_millis() > 1_600_000_000_000);
    }
}
