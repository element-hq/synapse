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

use std::{collections::BTreeMap, sync::Arc};

use pyo3::{
    exceptions::{PyKeyError, PyTypeError},
    prelude::Borrowed,
    pyclass, pymethods,
    types::{
        PyAnyMethods, PyIterator, PyList, PyListMethods, PyMapping, PySet, PySetMethods, PyTuple,
    },
    Bound, FromPyObject, IntoPyObject, IntoPyObjectExt, Py, PyAny, PyErr, PyResult, Python,
};
use pythonize::{depythonize, pythonize};
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// A generic class for representing immutable JSON objects.
///
/// This is used for representing the `content` field of an event.
///
/// The basic architecture here is to optimize for two things:
/// 1. Fast access of top-level keys (e.g. `event.content["key"]`)
/// 2. Pure Rust implementation.
#[derive(Serialize, Deserialize, Clone, Default)]
#[pyclass(mapping, frozen, skip_from_py_object)]
#[serde(transparent)]
pub struct JsonObject {
    object: Arc<BTreeMap<Box<str>, serde_json::Value>>,
}

// We implement `FromPyObject` to allow `JsonObject` to be used as function
// arguments.
impl<'py> FromPyObject<'_, 'py> for JsonObject {
    type Error = PyErr;

    fn extract(ob: Borrowed<'_, 'py, PyAny>) -> Result<Self, Self::Error> {
        // Fast path: already a JsonObject, so just share the underlying map
        // (cheap, as it's immutable and behind an `Arc`).
        if let Ok(obj) = ob.cast::<JsonObject>() {
            return Ok(JsonObject {
                object: obj.get().object.clone(),
            });
        }

        // Otherwise accept any mapping and convert it via pythonize. Unlike the
        // `#[new]` constructor we don't accept `None` here: an absent value is
        // represented as `Option<JsonObject>` at the field/argument level.
        let mapping = ob
            .cast::<PyMapping>()
            .map_err(|_| PyTypeError::new_err("expected a mapping"))?;
        let object: BTreeMap<Box<str>, Value> = depythonize(&mapping)?;
        Ok(JsonObject {
            object: Arc::new(object),
        })
    }
}

#[pymethods]
impl JsonObject {
    #[new]
    #[pyo3(signature = (content = None))]
    fn new(content: Option<&Bound<'_, PyAny>>) -> PyResult<Self> {
        match content {
            // If no content is provided, default to an empty object.
            None => Ok(Self::default()),
            // Otherwise reuse the `FromPyObject` path, which accepts an
            // existing `JsonObject` or any Python mapping.
            Some(content) => JsonObject::extract(content.as_borrowed()),
        }
    }

    fn __len__(&self) -> usize {
        self.object.len()
    }

    fn __contains__(&self, key: &Bound<'_, PyAny>) -> bool {
        // Match dict semantics: a non-string key is simply "not in" the
        // mapping, rather than raising TypeError.
        let Ok(key_str) = key.extract::<&str>() else {
            return false;
        };
        self.object.contains_key(key_str)
    }

    fn __getitem__<'py>(
        &self,
        py: Python<'py>,
        key: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // We only ever store string keys, so any non-string lookup is a miss.
        // Raise KeyError (not TypeError) to match dict's behaviour.
        let Ok(key_str) = key.extract::<&str>() else {
            return Err(PyKeyError::new_err(key.unbind()));
        };
        let Some(value) = self.object.get(key_str) else {
            return Err(PyKeyError::new_err(key.unbind()));
        };
        Ok(pythonize(py, value)?)
    }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        // The easiest way to get an iterator over the keys is to create a
        // temporary list and call `iter()` on it. This is not the most
        // efficient approach, but is much less boilerplate than implementing a
        // custom iterator type. Since the keys are typically small in number
        // this should be fine in practice.
        let list = PyList::new(py, self.object.keys().map(Box::as_ref))?;
        PyIterator::from_object(&list)
    }

    // The view classes below each hold a `JsonObject` clone. This is cheap
    // because the underlying map is behind an `Arc`, and lets the view outlive
    // the originating object (matching dict_keys/values/items semantics in
    // Python, which also keep the dict alive).

    fn keys(&self) -> JsonObjectKeysView {
        JsonObjectKeysView {
            object: self.clone(),
        }
    }

    fn values(&self) -> JsonObjectValuesView {
        JsonObjectValuesView {
            object: self.clone(),
        }
    }

    fn items(&self) -> JsonObjectItemsView {
        JsonObjectItemsView {
            object: self.clone(),
        }
    }

    #[pyo3(signature = (key, default=None))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        key: Bound<'_, PyAny>,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Non-string keys can never match, so treat them as a miss and return
        // the caller-supplied default rather than raising.
        let Ok(key_str) = key.extract::<&str>() else {
            return Ok(default.into_pyobject(py)?);
        };
        match self.object.get(key_str) {
            Some(value) => Ok(pythonize(py, value)?),
            None => Ok(default.into_pyobject(py)?),
        }
    }

    fn __eq__(&self, other: Bound<'_, PyAny>) -> bool {
        // We support equality against any Python mapping (e.g. plain dicts),
        // so callers can swap a JsonObject in without rewriting comparisons.
        let Ok(mapping) = other.cast::<PyMapping>() else {
            return false;
        };

        let Ok(other_len) = mapping.len() else {
            return false;
        };

        if other_len != self.object.len() {
            return false;
        }

        // We know the "other" is a mapping with the same number of fields as
        // us. So we can convert it into a JsonObject and compare the underlying
        // maps.
        let Ok(other_dict) = depythonize(&other) else {
            return false;
        };

        *self.object == other_dict
    }

    // Since we implement comparisons with other types, we need to disable
    // hashing to avoid violating the invariant that equal objects must have the
    // same hash.
    //
    // Alternatively, we could only allow comparisons with other JsonObjects and
    // allow hashing, but a) its nicer to be able to compare with arbitrary
    // mappings and b) we don't really need hashing for these objects.
    #[classattr]
    const __hash__: Option<Py<PyAny>> = None;

    fn __str__(&self) -> String {
        serde_json::to_string(&self.object).expect("Value should be serializable")
    }

    fn __repr__(&self) -> String {
        format!("JsonObject({})", self.__str__())
    }
}

impl JsonObject {
    pub fn get_field(&self, key: &str) -> Option<&serde_json::Value> {
        self.object.get(key)
    }

    /// Returns a `serde_json::Value::Object` containing a clone of this
    /// object's entries.
    pub fn as_map(&self) -> &BTreeMap<Box<str>, Value> {
        &self.object
    }

    /// Whether the object has no entries.
    pub fn is_empty(&self) -> bool {
        self.object.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&Box<str>, &Value)> {
        self.object.iter()
    }
}

impl<'a> IntoIterator for &'a JsonObject {
    type Item = (&'a Box<str>, &'a serde_json::Value);
    type IntoIter = std::collections::btree_map::Iter<'a, Box<str>, serde_json::Value>;

    fn into_iter(self) -> Self::IntoIter {
        self.object.as_ref().iter()
    }
}

/// Helper class returned by `JsonObject.keys()` to act as a view into the keys
/// of the object.
///
/// This needs to both be iterable *and* operate like a set.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct JsonObjectKeysView {
    object: JsonObject,
}

#[pymethods]
impl JsonObjectKeysView {
    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        // Create the iterator by making a temporary python list of the keys and
        // calling `iter()` on it.
        let list = PyList::new(py, self.object.object.keys().map(Box::as_ref))?;
        PyIterator::from_object(&list)
    }

    fn __len__(&self) -> usize {
        self.object.__len__()
    }

    fn __contains__(&self, key: &Bound<'_, PyAny>) -> bool {
        self.object.__contains__(key)
    }

    fn __eq__(&self, other: Bound<'_, PyAny>) -> bool {
        let other_len = match other.len() {
            Ok(len) => len,
            Err(_) => return false,
        };

        if self.object.__len__() != other_len {
            return false;
        }

        for key in self.object.object.keys() {
            if !matches!(other.contains(key.as_ref()), Ok(true)) {
                return false;
            }
        }

        true
    }

    // The set operators below match the behaviour of `dict.keys()` in Python:
    // they accept any object that supports `__contains__` (for `&`) or is
    // iterable (for `|`, `-`, `^`), not just sets. Each returns a fresh
    // `PySet` so the caller gets a normal mutable Python set back.
    //
    // The `__r*__` variants are reflected operators, called by Python when
    // the left-hand operand doesn't know how to combine with us. Since these
    // operations are commutative for sets (or symmetric in the case of `^`),
    // they just delegate. The asymmetric ops (`-`) need a separate impl.

    fn __and__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        // Iterate our (typically small) key set and probe `other`, which may
        // be any container supporting `__contains__`.
        let mut result = Vec::new();

        for key in self.object.object.keys() {
            if matches!(other.contains(key.as_ref()), Ok(true)) {
                result.push(key.as_ref());
            }
        }

        PySet::new(py, &result)
    }

    fn __rand__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        self.__and__(py, other)
    }

    fn __or__<'py>(&self, py: Python<'py>, other: Bound<'_, PyAny>) -> PyResult<Bound<'py, PySet>> {
        // Union needs to enumerate both sides, so the right operand must be
        // iterable (a bare `__contains__` is not enough).
        let Ok(other_iter) = other.try_iter() else {
            return Err(PyTypeError::new_err("Right operand must be iterable"));
        };

        let result = PySet::new(py, self.object.object.keys().map(Box::as_ref))?;

        // PySet handles dedup, so we can blindly add every element from the
        // other iterable.
        for item in other_iter {
            let item = item?;
            result.add(item)?;
        }

        Ok(result)
    }

    fn __ror__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        self.__or__(py, other)
    }

    fn __sub__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        // `self - other`: keep our keys that are not in `other`. Only `other`
        // needs to support `__contains__` here.
        let mut result = Vec::new();

        for key in self.object.object.keys() {
            if matches!(other.contains(key.as_ref()), Ok(true)) {
                continue;
            }
            result.push(key.as_ref());
        }

        PySet::new(py, &result)
    }

    fn __rsub__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        // `other - self`: we need to enumerate `other`, so it must be
        // iterable. Not symmetric with `__sub__`, hence a separate impl.
        let Ok(other_iter) = other.try_iter() else {
            return Err(PyTypeError::new_err("Left operand must be iterable"));
        };

        let result = PySet::empty(py)?;

        for item in other_iter {
            let item = item?;
            if self.object.__contains__(&item) {
                continue;
            }
            result.add(item)?;
        }

        Ok(result)
    }

    fn __xor__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        // Symmetric difference: elements in exactly one side. Implemented as
        // two filtered passes — one over our keys, one over `other`.
        let Ok(other_iter) = other.try_iter() else {
            return Err(PyTypeError::new_err("Right operand must be iterable"));
        };

        let result = PySet::empty(py)?;

        for key in self.object.object.keys() {
            if matches!(other.contains(key.as_ref()), Ok(true)) {
                continue;
            }
            result.add(key.as_ref())?;
        }

        for item in other_iter {
            let item = item?;
            if self.object.__contains__(&item) {
                continue;
            }
            result.add(item)?;
        }

        Ok(result)
    }

    fn __rxor__<'py>(
        &self,
        py: Python<'py>,
        other: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PySet>> {
        self.__xor__(py, other)
    }

    fn isdisjoint(&self, other: Bound<'_, PyAny>) -> bool {
        for key in self.object.object.keys() {
            if matches!(other.contains(key.as_ref()), Ok(true)) {
                return false;
            }
        }

        true
    }
}

/// Helper class returned by `JsonObject.values()` to act as a view into the
/// values of the object.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct JsonObjectValuesView {
    object: JsonObject,
}

#[pymethods]
impl JsonObjectValuesView {
    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        // Create the iterator by making a temporary python list of the keys and
        // calling `iter()` on it.
        let list = PyList::empty(py);
        for v in self.object.object.values() {
            let py_value = pythonize(py, v)?.into_bound_py_any(py)?;
            list.append(py_value)?;
        }

        PyIterator::from_object(&list)
    }

    fn __len__(&self) -> usize {
        self.object.__len__()
    }

    fn __contains__(&self, other: Bound<'_, PyAny>) -> bool {
        // We compare by JSON equality rather than Python identity: convert
        // the candidate into a `serde_json::Value` once and scan our values.
        // Anything that fails to depythonize cannot match by definition.
        let other_value: serde_json::Value = match depythonize(&other) {
            Ok(v) => v,
            Err(_) => return false,
        };
        self.object.object.values().any(|v| *v == other_value)
    }
}

/// Helper class returned by `JsonObject.items()` to act as a view into the
/// items of the object.
///
/// Technically this should be a set-like view according to Python semantics,
/// unless the values are unhashable. Since the values are immutable we could
/// support it, but it's more work and nobody seems to actually use the set
/// operations on `dict_items` in practice.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct JsonObjectItemsView {
    object: JsonObject,
}

#[pymethods]
impl JsonObjectItemsView {
    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        // Create the iterator by making a temporary python list of the keys and
        // calling `iter()` on it.
        let list = PyList::empty(py);
        for (k, v) in self.object.object.iter() {
            let py_key = k.as_ref().into_bound_py_any(py)?;
            let py_value = pythonize(py, v)?.into_bound_py_any(py)?;
            let item = PyTuple::new(py, [py_key, py_value])?;
            list.append(item)?;
        }

        PyIterator::from_object(&list)
    }

    fn __len__(&self) -> usize {
        self.object.__len__()
    }

    fn __contains__(&self, other: Bound<'_, PyAny>) -> bool {
        // `(key, value) in items` — only a 2-tuple can possibly match. We
        // look the key up directly (avoiding a full scan) and then compare
        // the stored value against `value` using JSON equality.
        let Ok((key, value)) = other.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>() else {
            return false;
        };
        let Ok(key_str) = key.extract::<&str>() else {
            return false;
        };
        let Some(stored) = self.object.object.get(key_str) else {
            return false;
        };
        let other_value: serde_json::Value = match depythonize(&value) {
            Ok(v) => v,
            Err(_) => return false,
        };
        *stored == other_value
    }
}
