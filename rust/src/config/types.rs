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

use std::str::FromStr;

use pyo3::{exceptions::PyRuntimeError, prelude::*};

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum RoomCreationPreset {
    PrivateChat,
    PublicChat,
    TrustedPrivateChat,
}

impl FromStr for RoomCreationPreset {
    type Err = PyErr;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Ok(match s {
            "private_chat" => RoomCreationPreset::PrivateChat,
            "public_chat" => RoomCreationPreset::PublicChat,
            "trusted_private_chat" => RoomCreationPreset::TrustedPrivateChat,
            other => {
                return Err(PyRuntimeError::new_err(format!(
                    "Unknown variant {other:?} does not translate to `RoomCreationPreset`. \
                     This is a Synapse programming error."
                )))
            }
        })
    }
}

impl<'a, 'py> FromPyObject<'a, 'py> for RoomCreationPreset {
    type Error = PyErr;

    fn extract(value: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        value.extract::<&str>()?.parse()
    }
}
