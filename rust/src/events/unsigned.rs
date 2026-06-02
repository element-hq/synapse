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

use std::sync::{Arc, RwLock, RwLockReadGuard};

use pyo3::{
    exceptions::{PyKeyError, PyRuntimeError, PyTypeError},
    pyclass, pymethods,
    types::{PyAnyMethods, PyList, PyListMethods, PyMapping},
    Bound, IntoPyObjectExt, PyAny, PyResult, Python,
};
use pythonize::{depythonize, pythonize};
use serde::{Deserialize, Serialize};
use serde_json::Number;

#[pyclass(frozen, skip_from_py_object)]
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(transparent)]
pub struct Unsigned {
    inner: Arc<RwLock<UnsignedInner>>,
}

/// The fields in the unsigned data of an event that are persisted in the
/// database.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct PersistedUnsignedFields {
    #[serde(skip_serializing_if = "Option::is_none")]
    age_ts: Option<Number>,
    #[serde(skip_serializing_if = "Option::is_none")]
    replaces_state: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    invite_room_state: Option<Vec<serde_json::Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    knock_room_state: Option<Vec<serde_json::Value>>,
}

/// The inner representation of the unsigned data of an event, which includes
/// both the fields that are persisted in the database and the fields that are
/// only used in memory.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct UnsignedInner {
    #[serde(flatten)]
    persisted_fields: PersistedUnsignedFields,
    #[serde(skip_serializing_if = "Option::is_none")]
    prev_content: Option<Box<serde_json::Value>>, // We use Box to minimise stack space
    #[serde(skip_serializing_if = "Option::is_none")]
    prev_sender: Option<String>,
}

/// The fields that exist on the unsigned data of an event.
///
/// This is used when converting from python to rust, to ensure that if we add a
/// new field we don't forget to add it to all the necessary places.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum UnsignedField {
    AgeTs,
    ReplacesState,
    InviteRoomState,
    KnockRoomState,
    PrevContent,
    PrevSender,
}

impl std::str::FromStr for UnsignedField {
    type Err = ();

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "age_ts" => Ok(Self::AgeTs),
            "replaces_state" => Ok(Self::ReplacesState),
            "invite_room_state" => Ok(Self::InviteRoomState),
            "knock_room_state" => Ok(Self::KnockRoomState),
            "prev_content" => Ok(Self::PrevContent),
            "prev_sender" => Ok(Self::PrevSender),
            _ => Err(()),
        }
    }
}

impl Unsigned {
    fn py_read(&self) -> PyResult<RwLockReadGuard<'_, UnsignedInner>> {
        self.inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Unsigned lock poisoned"))
    }

    fn py_write(&self) -> PyResult<std::sync::RwLockWriteGuard<'_, UnsignedInner>> {
        self.inner
            .write()
            .map_err(|_| PyRuntimeError::new_err("Unsigned lock poisoned"))
    }

    /// Create a deep copy of this `Unsigned` to allow modification without
    /// affecting other references to the same unsigned data. This is needed
    /// when we clone an event.
    pub fn deep_copy(&self) -> Self {
        Self {
            inner: Arc::new(RwLock::new(self.py_read().expect("lock poisoned").clone())),
        }
    }
}

#[pymethods]
impl Unsigned {
    #[new]
    fn py_new(unsigned: Bound<'_, PyMapping>) -> PyResult<Self> {
        let inner = depythonize(&unsigned)?;

        Ok(Self {
            inner: Arc::new(RwLock::new(inner)),
        })
    }

    fn __getitem__<'py>(
        &self,
        py: Python<'py>,
        key: Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let key = key
            .extract::<&str>()
            .map_err(|_| PyTypeError::new_err("Unsigned keys must be strings"))?;

        let field: UnsignedField = key
            .parse()
            .map_err(|_| PyKeyError::new_err(format!("Unsigned has no key '{key}'")))?;

        let unsigned = self.py_read()?;

        match field {
            UnsignedField::AgeTs => {
                let age_ts = &unsigned
                    .persisted_fields
                    .age_ts
                    .as_ref()
                    .ok_or_else(|| PyKeyError::new_err("age_ts"))?;
                Ok(pythonize(py, age_ts)?)
            }
            UnsignedField::ReplacesState => Ok((unsigned.persisted_fields.replaces_state)
                .as_ref()
                .ok_or_else(|| PyKeyError::new_err("replaces_state"))?
                .into_bound_py_any(py)?),
            UnsignedField::InviteRoomState => Ok(room_state_to_py(
                py,
                unsigned
                    .persisted_fields
                    .invite_room_state
                    .as_ref()
                    .ok_or_else(|| PyKeyError::new_err("invite_room_state"))?,
            )?),
            UnsignedField::KnockRoomState => Ok(room_state_to_py(
                py,
                unsigned
                    .persisted_fields
                    .knock_room_state
                    .as_ref()
                    .ok_or_else(|| PyKeyError::new_err("knock_room_state"))?,
            )?),
            UnsignedField::PrevContent => Ok(pythonize(
                py,
                unsigned
                    .prev_content
                    .as_ref()
                    .ok_or_else(|| PyKeyError::new_err("prev_content"))?,
            )?),
            UnsignedField::PrevSender => Ok((unsigned.prev_sender)
                .as_ref()
                .ok_or_else(|| PyKeyError::new_err("prev_sender"))?
                .into_bound_py_any(py)?),
        }
    }

    fn __contains__(&self, key: Bound<'_, PyAny>) -> PyResult<bool> {
        let Ok(key) = key.extract::<&str>() else {
            return Ok(false);
        };

        let Ok(field) = key.parse::<UnsignedField>() else {
            return Ok(false);
        };

        let unsigned = self.py_read()?;

        let exists = match field {
            UnsignedField::AgeTs => unsigned.persisted_fields.age_ts.is_some(),
            UnsignedField::ReplacesState => unsigned.persisted_fields.replaces_state.is_some(),
            UnsignedField::InviteRoomState => unsigned.persisted_fields.invite_room_state.is_some(),
            UnsignedField::KnockRoomState => unsigned.persisted_fields.knock_room_state.is_some(),
            UnsignedField::PrevContent => unsigned.prev_content.is_some(),
            UnsignedField::PrevSender => unsigned.prev_sender.is_some(),
        };

        Ok(exists)
    }

    fn __setitem__(&self, key: Bound<'_, PyAny>, value: Bound<'_, PyAny>) -> PyResult<()> {
        let key = key
            .extract::<&str>()
            .map_err(|_| PyTypeError::new_err("Unsigned keys must be strings"))?;

        let field: UnsignedField = key
            .parse()
            .map_err(|_| PyKeyError::new_err(format!("Unsigned has no key '{key}'")))?;

        let mut unsigned = self.py_write()?;

        match field {
            UnsignedField::AgeTs => unsigned.persisted_fields.age_ts = Some(depythonize(&value)?),
            UnsignedField::ReplacesState => {
                unsigned.persisted_fields.replaces_state = Some(value.extract()?)
            }
            UnsignedField::InviteRoomState => {
                unsigned.persisted_fields.invite_room_state = Some(room_state_from_py(value)?)
            }
            UnsignedField::KnockRoomState => {
                unsigned.persisted_fields.knock_room_state = Some(room_state_from_py(value)?)
            }
            UnsignedField::PrevContent => {
                unsigned.prev_content = Some(Box::new(depythonize(&value)?))
            }
            UnsignedField::PrevSender => unsigned.prev_sender = Some(value.extract()?),
        }

        Ok(())
    }

    fn __delitem__(&self, key: Bound<'_, PyAny>) -> PyResult<()> {
        let key = key
            .extract::<&str>()
            .map_err(|_| PyTypeError::new_err("Unsigned keys must be strings"))?;

        let field: UnsignedField = key
            .parse()
            .map_err(|_| PyKeyError::new_err(format!("Unsigned has no key '{key}'")))?;

        let mut unsigned = self.py_write()?;

        match field {
            UnsignedField::AgeTs => unsigned.persisted_fields.age_ts = None,
            UnsignedField::ReplacesState => unsigned.persisted_fields.replaces_state = None,
            UnsignedField::InviteRoomState => unsigned.persisted_fields.invite_room_state = None,
            UnsignedField::KnockRoomState => unsigned.persisted_fields.knock_room_state = None,
            UnsignedField::PrevContent => unsigned.prev_content = None,
            UnsignedField::PrevSender => unsigned.prev_sender = None,
        }

        Ok(())
    }

    #[pyo3(signature = (key, default=None))]
    fn get<'py>(
        &self,
        py: Python<'py>,
        key: Bound<'py, PyAny>,
        default: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        match self.__getitem__(py, key) {
            Ok(value) => Ok(Some(value)),
            Err(err) => {
                if err.is_instance_of::<PyKeyError>(py) {
                    Ok(default)
                } else {
                    Err(err)
                }
            }
        }
    }

    pub fn for_persistence<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &self.py_read()?.persisted_fields)?)
    }

    pub fn for_event<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(pythonize(py, &*self.py_read()?)?)
    }
}

fn room_state_to_py<'py>(
    py: Python<'py>,
    state: &[serde_json::Value],
) -> PyResult<Bound<'py, PyAny>> {
    let py_list = PyList::empty(py);

    for item in state {
        py_list.append(pythonize(py, item)?)?;
    }

    py_list.into_bound_py_any(py)
}

fn room_state_from_py(value: Bound<'_, PyAny>) -> PyResult<Vec<serde_json::Value>> {
    let py_list = value.cast::<PyList>()?;

    let mut state = Vec::with_capacity(py_list.len());
    for item in py_list.iter() {
        state.push(pythonize::depythonize(&item)?);
    }

    Ok(state)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn test_unsigned_field_from_str_valid() {
        assert_eq!("age_ts".parse(), Ok(UnsignedField::AgeTs));
        assert_eq!("replaces_state".parse(), Ok(UnsignedField::ReplacesState));
        assert_eq!(
            "invite_room_state".parse(),
            Ok(UnsignedField::InviteRoomState)
        );
        assert_eq!(
            "knock_room_state".parse(),
            Ok(UnsignedField::KnockRoomState)
        );
        assert_eq!("prev_content".parse(), Ok(UnsignedField::PrevContent));
        assert_eq!("prev_sender".parse(), Ok(UnsignedField::PrevSender));
    }

    #[test]
    fn test_unsigned_field_from_str_invalid() {
        assert_eq!("".parse::<UnsignedField>(), Err(()));
        assert_eq!("unknown".parse::<UnsignedField>(), Err(()));
        // Case-sensitive: upper-case should not match.
        assert_eq!("AGE_TS".parse::<UnsignedField>(), Err(()));
        // Must be an exact match, no whitespace.
        assert_eq!(" age_ts".parse::<UnsignedField>(), Err(()));
    }

    #[test]
    fn test_persisted_fields_serialize_empty_is_empty_object() {
        let fields = PersistedUnsignedFields::default();
        let json = serde_json::to_value(&fields).unwrap();
        assert_eq!(json, json!({}));
    }

    #[test]
    fn test_persisted_fields_serialize_populated() {
        let fields = PersistedUnsignedFields {
            age_ts: Some(1234.into()),
            replaces_state: Some("$prev:example.com".to_string()),
            invite_room_state: Some(vec![json!({"type": "m.room.name"})]),
            knock_room_state: Some(vec![json!({"type": "m.room.topic"})]),
        };
        let json = serde_json::to_value(&fields).unwrap();
        assert_eq!(
            json,
            json!({
                "age_ts": 1234,
                "replaces_state": "$prev:example.com",
                "invite_room_state": [{"type": "m.room.name"}],
                "knock_room_state": [{"type": "m.room.topic"}],
            })
        );
    }

    #[test]
    fn test_unsigned_inner_flattens_persisted_fields() {
        let inner = UnsignedInner {
            persisted_fields: PersistedUnsignedFields {
                age_ts: Some(99.into()),
                ..Default::default()
            },
            prev_content: Some(Box::new(json!({"body": "hi"}))),
            prev_sender: Some("@alice:example.com".to_string()),
        };

        let json = serde_json::to_value(&inner).unwrap();
        assert_eq!(
            json,
            json!({
                "age_ts": 99,
                "prev_content": {"body": "hi"},
                "prev_sender": "@alice:example.com",
            })
        );
    }

    #[test]
    fn test_unsigned_inner_roundtrip() {
        let original = UnsignedInner {
            persisted_fields: PersistedUnsignedFields {
                age_ts: Some(10.into()),
                replaces_state: Some("$state:example.com".to_string()),
                invite_room_state: None,
                knock_room_state: None,
            },
            prev_content: Some(Box::new(json!({"membership": "join"}))),
            prev_sender: None,
        };

        let json = serde_json::to_string(&original).unwrap();
        let roundtripped: UnsignedInner = serde_json::from_str(&json).unwrap();

        assert_eq!(roundtripped.persisted_fields.age_ts, Some(10.into()));
        assert_eq!(
            roundtripped.persisted_fields.replaces_state.as_deref(),
            Some("$state:example.com")
        );
        assert_eq!(
            roundtripped.prev_content.as_deref(),
            Some(&json!({"membership": "join"}))
        );
        assert_eq!(roundtripped.prev_sender, None);
    }

    #[test]
    fn test_unsigned_serializes_transparently() {
        // `Unsigned` is `#[serde(transparent)]` over its inner, so serializing
        // an empty default should yield an empty object rather than a wrapper.
        let unsigned = Unsigned::default();
        let json = serde_json::to_value(&unsigned).unwrap();
        assert_eq!(json, json!({}));
    }

    #[test]
    fn test_unsigned_deserialize_from_flat_object() {
        let json = json!({
            "age_ts": 5,
            "prev_sender": "@bob:example.com",
        });
        let unsigned: Unsigned = serde_json::from_value(json).unwrap();
        let inner = unsigned.inner.read().unwrap();
        assert_eq!(inner.persisted_fields.age_ts, Some(5.into()));
        assert_eq!(inner.prev_sender.as_deref(), Some("@bob:example.com"));
    }
}
