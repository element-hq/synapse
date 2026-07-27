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

//! A typed wrapper around the Twisted reactor.

use pyo3::{call::PyCallArgs, exceptions::PyTypeError, intern, prelude::*};

/// The reactor methods Rust code relies on.
///
/// Extracting a [`Reactor`] from a Python object fails if any of these are
/// missing, so mistakes surface as a `TypeError` at the FFI boundary rather
/// than as an `AttributeError` on a tokio worker thread later.
const REQUIRED_METHODS: &[&str] = &["callFromThread", "addSystemEventTrigger"];

/// The Twisted reactor, as seen from Rust.
///
/// This is not a static guarantee that the object behaves like a reactor (it
/// is a foreign Python object), but the duck type is checked once at
/// extraction, and this module is the single place that names the Twisted
/// API surface that Rust code depends on.
pub struct Reactor(Py<PyAny>);

impl<'a, 'py> FromPyObject<'a, 'py> for Reactor {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        for name in REQUIRED_METHODS {
            if !obj.hasattr(*name)? {
                return Err(PyTypeError::new_err(format!(
                    "expected a Twisted reactor, but {} has no `{name}` method",
                    obj.get_type()
                )));
            }
        }

        Ok(Reactor(obj.to_owned().unbind()))
    }
}

impl Reactor {
    /// `reactor.callFromThread(f, *args)`: schedule a call on the reactor
    /// thread. This is the only reactor method that is safe to call from
    /// other threads (e.g. tokio workers).
    ///
    /// `args` is the full argument tuple, starting with the callable itself.
    pub fn call_from_thread<'py>(
        &self,
        py: Python<'py>,
        args: impl PyCallArgs<'py>,
    ) -> PyResult<()> {
        self.0
            .bind(py)
            .call_method1(intern!(py, "callFromThread"), args)?;

        Ok(())
    }

    /// Register `callable` to run after the reactor has shut down, via
    /// `reactor.addSystemEventTrigger("after", "shutdown", callable)`.
    pub fn add_shutdown_trigger(
        &self,
        py: Python<'_>,
        callable: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.0.bind(py).call_method1(
            intern!(py, "addSystemEventTrigger"),
            (intern!(py, "after"), intern!(py, "shutdown"), callable),
        )?;

        Ok(())
    }

    pub fn clone_ref(&self, py: Python<'_>) -> Reactor {
        Reactor(self.0.clone_ref(py))
    }
}
