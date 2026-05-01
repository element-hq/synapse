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

//! Class for representing event signatures

use std::{
    collections::HashMap,
    sync::{Arc, RwLock},
};

use pyo3::{
    exceptions::{PyKeyError, PyRuntimeError},
    pyclass, pymethods,
    types::{PyAnyMethods, PyDict, PyMapping, PyMappingMethods},
    Bound, IntoPyObject, PyAny, PyResult, Python,
};
use serde::{Deserialize, Serialize};

/// A class representing the signatures on an event.
#[pyclass(frozen, skip_from_py_object)]
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Signatures {
    inner: Arc<RwLock<HashMap<String, HashMap<String, String>>>>,
}

#[pymethods]
impl Signatures {
    #[new]
    #[pyo3(signature = (signatures = None))]
    fn py_new(signatures: Option<HashMap<String, HashMap<String, String>>>) -> Self {
        let mut signatures = signatures.unwrap_or_default();

        // Prune any entries that have no signatures.
        signatures.retain(|_, server_sigs| !server_sigs.is_empty());

        Self {
            inner: Arc::new(RwLock::new(signatures)),
        }
    }

    /// Check if the signatures contain a signature for the given server name.
    fn __contains__(&self, key: Bound<'_, PyAny>) -> PyResult<bool> {
        let Ok(key) = key.extract::<&str>() else {
            return Ok(false);
        };

        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;
        Ok(signatures.contains_key(key))
    }

    /// Get the number of servers that have signatures.
    fn __len__(&self) -> PyResult<usize> {
        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;
        Ok(signatures.len())
    }

    /// Get the signature for the given server name and key ID, if it exists.
    fn get_signature(&self, server_name: &str, key_id: &str) -> PyResult<Option<String>> {
        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        Ok(signatures
            .get(server_name)
            .and_then(|server_sigs| server_sigs.get(key_id).cloned()))
    }

    /// Get the signatures for the given server name.
    fn __getitem__(&self, key: Bound<'_, PyAny>) -> PyResult<Option<HashMap<String, String>>> {
        let Some(server_name) = key.extract::<&str>().ok() else {
            return Err(PyKeyError::new_err(key.to_string()));
        };

        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        Ok(signatures.get(server_name).cloned())
    }

    /// Add a signature for the given server name and key ID.
    fn add_signature(
        &self,
        server_name: String,
        key_id: String,
        signature: String,
    ) -> PyResult<()> {
        let mut signatures = self
            .inner
            .write()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        signatures
            .entry(server_name)
            .or_default()
            .insert(key_id, signature);

        Ok(())
    }

    /// Update the signatures with the given signatures.
    ///
    /// Will overwrite all existing signatures for the server names provided.
    fn update(&self, other: &Bound<'_, PyMapping>) -> PyResult<()> {
        let mut signatures = self
            .inner
            .write()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        for key in other.keys()? {
            let value = other.get_item(&key)?;
            let server_name = key.extract::<String>()?;
            let server_sigs = value.cast::<PyMapping>()?;

            let mut entry = HashMap::new();
            for key in server_sigs.keys()? {
                let value = server_sigs.get_item(&key)?;
                let key_id = key.extract::<String>()?;
                let signature = value.extract::<String>()?;

                entry.insert(key_id, signature);
            }

            // Only insert the entry if it has at least one signature.
            if !entry.is_empty() {
                signatures.insert(server_name, entry);
            } else {
                signatures.remove(&server_name);
            }
        }

        Ok(())
    }

    /// Return a copy of the signatures as a dictionary.
    fn as_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        (&*signatures).into_pyobject(py)
    }

    fn __repr__(&self) -> PyResult<String> {
        let signatures = self
            .inner
            .read()
            .map_err(|_| PyRuntimeError::new_err("Failed to acquire lock"))?;

        Ok(format!("Signatures({signatures:?})"))
    }
}

#[cfg(test)]
mod tests {
    use pythonize::pythonize;

    use super::*;

    /// Helper that reads the inner map directly.
    fn read_inner(sigs: &Signatures) -> HashMap<String, HashMap<String, String>> {
        sigs.inner.read().expect("lock poisoned").clone()
    }

    /// Helper to create a server signatures map from a list of (key_id, sig)
    /// pairs.
    fn make_server_sigs(data: &[(&str, &str)]) -> HashMap<String, String> {
        let mut map = HashMap::new();
        for (key_id, sig) in data {
            map.insert((*key_id).to_owned(), (*sig).to_owned());
        }
        map
    }

    /// Helper to create a `Signatures` object from a list of (server_name,
    /// key_id, sig) tuples.
    fn create_signatures(data: &[(&str, &str, &str)]) -> Signatures {
        let mut map: HashMap<String, HashMap<String, String>> = HashMap::new();
        for (server_name, key_id, sig) in data {
            map.entry((*server_name).to_owned())
                .or_default()
                .insert((*key_id).to_owned(), (*sig).to_owned());
        }
        Signatures::py_new(Some(map))
    }

    #[test]
    fn test_new_empty() {
        let sigs = Signatures::py_new(None);
        assert!(read_inner(&sigs).is_empty());
        assert_eq!(sigs.__len__().unwrap(), 0);
    }

    #[test]
    fn test_new_with_data() {
        let sigs = create_signatures(&[("example.com", "ed25519:key1", "sig1")]);
        assert_eq!(sigs.__len__().unwrap(), 1);
        assert_eq!(
            sigs.get_signature("example.com", "ed25519:key1").unwrap(),
            Some("sig1".to_string())
        );
    }

    #[test]
    fn test_new_prunes_servers_with_no_signatures() {
        let mut data = HashMap::new();
        data.insert("empty.example.com".to_string(), HashMap::new());
        data.insert(
            "example.com".to_string(),
            make_server_sigs(&[("ed25519:key1", "sig1")]),
        );

        let sigs = Signatures::py_new(Some(data));

        let inner = read_inner(&sigs);
        assert_eq!(inner.len(), 1);
        assert!(inner.contains_key("example.com"));
        assert!(!inner.contains_key("empty.example.com"));
    }

    #[test]
    fn test_add_signature() {
        let sigs = Signatures::py_new(None);
        sigs.add_signature(
            "example.com".to_string(),
            "ed25519:key1".to_string(),
            "sig1".to_string(),
        )
        .unwrap();

        let inner = read_inner(&sigs);
        assert_eq!(inner.len(), 1);
        assert_eq!(
            inner.get("example.com").and_then(|m| m.get("ed25519:key1")),
            Some(&"sig1".to_string())
        );
    }

    #[test]
    fn test_add_signature_to_existing_server() {
        let sigs = create_signatures(&[("example.com", "ed25519:key1", "sig1")]);
        sigs.add_signature(
            "example.com".to_string(),
            "ed25519:key2".to_string(),
            "sig2".to_string(),
        )
        .unwrap();

        let inner = read_inner(&sigs);
        assert_eq!(inner.len(), 1);
        assert_eq!(
            inner.get("example.com").and_then(|m| m.get("ed25519:key1")),
            Some(&"sig1".to_string())
        );
        assert_eq!(
            inner.get("example.com").and_then(|m| m.get("ed25519:key2")),
            Some(&"sig2".to_string())
        );
    }

    #[test]
    fn test_update_signatures_clobbers_existing() {
        let sigs = create_signatures(&[("example.com", "ed25519:key1", "sig1")]);

        // Create a new signatures map with a different signature for the same
        // server.
        let mut other = HashMap::new();
        other.insert(
            "example.com".to_string(),
            make_server_sigs(&[("ed25519:key2", "sig2")]),
        );

        // Update the signatures with the new map.
        Python::initialize();
        Python::attach(|py| {
            let value = pythonize(py, &other).unwrap();
            let value = value.cast::<PyMapping>().unwrap();

            sigs.update(value).unwrap();
        });

        // Check that the old signature has been replaced with the new one.
        let inner = read_inner(&sigs);
        assert_eq!(inner.len(), 1);
        assert_eq!(inner["example.com"].len(), 1);
        assert_eq!(inner["example.com"]["ed25519:key2"], "sig2");
    }

    #[test]
    fn test_serialize() {
        let mut data = HashMap::new();
        data.insert(
            "example.com".to_string(),
            make_server_sigs(&[("ed25519:key1", "sig1")]),
        );
        let sigs = Signatures::py_new(Some(data));

        let json = serde_json::to_string(&sigs).unwrap();
        assert_eq!(json, r#"{"example.com":{"ed25519:key1":"sig1"}}"#);
    }

    #[test]
    fn test_serialize_empty() {
        let sigs = Signatures::py_new(None);
        let json = serde_json::to_string(&sigs).unwrap();
        assert_eq!(json, "{}");
    }

    #[test]
    fn test_deserialize() {
        let json = r#"{"example.com":{"ed25519:key1":"sig1"}}"#;
        let sigs: Signatures = serde_json::from_str(json).unwrap();

        let inner = read_inner(&sigs);
        assert_eq!(inner.len(), 1);
        assert_eq!(
            inner.get("example.com").and_then(|m| m.get("ed25519:key1")),
            Some(&"sig1".to_string())
        );
    }

    #[test]
    fn test_deserialize_empty() {
        let sigs: Signatures = serde_json::from_str("{}").unwrap();
        assert!(read_inner(&sigs).is_empty());
    }
}
