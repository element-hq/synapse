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

// use pyo3::PyAny;
// use pyo3::{intern, prelude::*};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SynapseConfig {
    pub experimental: ExperimentalConfig,
}

// impl<'py> FromPyObject<'_, 'py> for SynapseConfig<'py> {
//     type Error = PyErr;

//     /// From Python `LoggingTransaction`
//     fn extract(config_python_object: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
//         todo!("...");
//     }
// }

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(rename_all = "snake_case")]
pub enum RoomCreationPreset {
    PrviateChat,
    PublicChat,
    TrustedPrivateChat,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ExperimentalConfig {
    pub msc3881_enabled: bool,
    pub msc3575_enabled: bool,
    pub msc4222_enabled: bool,
}
