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
pub struct SynapseDuration {
    microseconds: u64,
}

impl SynapseDuration {
    /// For now we only need to create durations from milliseconds.
    pub fn from_milliseconds(milliseconds: u64) -> Self {
        Self {
            microseconds: milliseconds * 1_000,
        }
    }
}

impl<'py> IntoPyObject<'py> for &SynapseDuration {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let duration_module = duration_module(py)?;
        let kwargs = [("microseconds", self.microseconds)].into_py_dict(py)?;
        let duration_instance = duration_module.call_method("Duration", (), Some(&kwargs))?;
        Ok(duration_instance.into_bound())
    }
}
