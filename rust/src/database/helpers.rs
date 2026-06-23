//! Shared helpers used by both backend cursors.
//!
//! Kept out of the per-backend files so the same plumbing (description shape,
//! mutex locking, the `PyObject` alias) lives in one place.

use std::sync::{Arc, Mutex, MutexGuard};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

/// Lock a mutex guarding backend state, mapping poisoning to a Python
/// RuntimeError.
pub fn lock<'a, T>(arc: &'a Arc<Mutex<T>>, label: &str) -> PyResult<MutexGuard<'a, T>> {
    arc.lock()
        .map_err(|_| PyRuntimeError::new_err(format!("{label} connection mutex poisoned")))
}

/// Build a PEP-249 `description` list: one 7-tuple per column. Only the
/// column name is populated; the remaining six fields are always `None`.
pub fn build_description(py: Python<'_>, names: &[String]) -> PyResult<Py<PyList>> {
    let py_none = py.None();
    let mut entries: Vec<Py<PyTuple>> = Vec::with_capacity(names.len());
    let mut fields: Vec<Py<PyAny>> = Vec::with_capacity(7);
    for name in names {
        fields.clear();
        fields.push(PyString::new(py, name).into_any().unbind());
        for _ in 0..6 {
            fields.push(py_none.clone_ref(py));
        }
        entries.push(PyTuple::new(py, fields.iter())?.unbind());
    }
    Ok(PyList::new(py, entries)?.unbind())
}
