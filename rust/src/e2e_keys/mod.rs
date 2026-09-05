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
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * [This file includes modifications made by Element Creations Ltd]
 */

use pyo3::{
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, PyResult, Python,
};

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "e2e_keys")?;
    child_module.add_class::<SignatureListItem>()?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.e2e_keys", child_module)?;

    Ok(())
}

/// A pending cross-signing signature.
#[derive(Debug)]
#[pyclass(frozen, get_all)]
pub struct SignatureListItem {
    /// Full key ID of the signing key, e.g. `"ed25519:ABCDEF"`.
    pub signing_key_id: String,

    /// User whose key was signed.
    pub target_user_id: String,

    /// Device ID (or master-key ID) that the signature targets.
    pub target_device_id: String,

    /// Raw signature value.
    pub signature: String,
}

#[pymethods]
impl SignatureListItem {
    #[new]
    fn py_new(
        signing_key_id: String,
        target_user_id: String,
        target_device_id: String,
        signature: String,
    ) -> Self {
        Self {
            signing_key_id,
            target_user_id,
            target_device_id,
            signature,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "SignatureListItem(signing_key_id={:?}, target_user_id={:?}, target_device_id={:?})",
            self.signing_key_id, self.target_user_id, self.target_device_id,
        )
    }
}
