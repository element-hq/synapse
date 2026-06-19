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

use pyo3::{exceptions::PyRuntimeError, prelude::*};
use std::collections::BTreeSet;

#[derive(FromPyObject, Clone)]
pub struct SynapseConfig {
    pub room: RoomConfig,
    pub experimental: ExperimentalConfig,
}

#[derive(Clone, Ord)]
pub enum RoomCreationPreset {
    PrviateChat,
    PublicChat,
    TrustedPrivateChat,
}

impl<'a, 'py> FromPyObject<'a, 'py> for RoomCreationPreset {
    type Error = PyErr;

    /// Extract from a Python `LoggingTransaction` passed as an argument.
    fn extract(value_py: Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        Ok(match value_py.extract()? {
            "private_chat" => RoomCreationPreset::PrviateChat,
            "public_chat" => RoomCreationPreset::PublicChat,
            "trusted_private_chat" => RoomCreationPreset::TrustedPrivateChat,
            other => {
                return Err(PyRuntimeError::new_err(
                    format!("Unknown variant {other:#} does not translate to `RoomCreationPreset`. This is a Synapse programming error."),
                ))
            }
        })
    }
}

#[derive(FromPyObject, Clone)]
pub struct RoomConfig {
    pub encryption_enabled_by_default_for_room_presets: BTreeSet<RoomCreationPreset>,
}

#[derive(FromPyObject, Clone)]
pub struct ExperimentalConfig {
    pub msc3881_enabled: bool,
    pub msc3575_enabled: bool,
    pub msc4222_enabled: bool,
}
