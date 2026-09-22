/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2025 Element Creations, Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

use once_cell::sync::OnceCell;
use pyo3::{
    types::{IntoPyDict, PyAnyMethods},
    Bound, BoundObject, IntoPyObject, Py, PyAny, PyErr, PyResult, Python,
};

/// A reference to the `synapse.util.duration` module.
static DURATION: OnceCell<Py<PyAny>> = OnceCell::new();

/// Access to the `synapse.util.duration` module.
fn duration_module(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(DURATION
        .get_or_try_init(|| py.import("synapse.util.duration").map(Into::into))?
        .bind(py))
}

/// Mirrors the `synapse.util.duration.Duration` Python class.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct SynapseDuration {
    milliseconds: u64,
}

impl SynapseDuration {
    /// Creates a `SynapseDuration` from a number of milliseconds.
    pub const fn from_milliseconds(milliseconds: u64) -> Self {
        Self { milliseconds }
    }

    /// Returns the duration as a number of milliseconds.
    pub const fn as_millis(&self) -> u64 {
        self.milliseconds
    }

    /// Creates a `SynapseDuration` from a number of hours.
    pub const fn from_hours(hours: u32) -> Self {
        // We take a u32 here so that we know the multiplication won't overflow.
        // We could instead panic, but that is unstable in a const context (for
        // the current MSRV 1.82).
        Self {
            milliseconds: (hours as u64) * 3_600_000,
        }
    }
}

impl<'py> IntoPyObject<'py> for SynapseDuration {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let duration_module = duration_module(py)?;
        let kwargs = [("milliseconds", self.milliseconds)].into_py_dict(py)?;
        let duration_instance = duration_module.call_method("Duration", (), Some(&kwargs))?;
        Ok(duration_instance.into_bound())
    }
}

impl<'py> IntoPyObject<'py> for &SynapseDuration {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let duration_module = duration_module(py)?;
        let kwargs = [("milliseconds", self.milliseconds)].into_py_dict(py)?;
        let duration_instance = duration_module.call_method("Duration", (), Some(&kwargs))?;
        Ok(duration_instance.into_bound())
    }
}
