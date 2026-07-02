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

use std::collections::BTreeSet;

use pyo3::prelude::*;

pub mod types;

/// A Rust-side view of Synapse's Python `HomeServerConfig`.
///
/// This only mirrors the subset of config that the Rust handlers need, rather
/// than the whole thing. Thanks to `#[derive(FromPyObject)]`, each field is
/// pulled directly off the corresponding attribute of the Python `config`
/// object, so you can populate it in one shot with:
/// ```
/// let config: SynapseHomeServerConfig = homeserver.getattr("config")?.extract()?;
/// ```
#[derive(FromPyObject, Clone)]
pub struct SynapseHomeServerConfig {
    pub room: RoomConfig,
    pub auth: AuthConfig,
    pub server: ServerConfig,
    pub experimental: ExperimentalConfig,
}

#[derive(FromPyObject, Clone)]
pub struct RoomConfig {
    pub encryption_enabled_by_default_for_room_presets: BTreeSet<types::RoomCreationPreset>,
}

#[derive(FromPyObject, Clone)]
pub struct AuthConfig {
    pub login_via_existing_enabled: bool,
}
#[derive(FromPyObject, Clone)]
pub struct ServerConfig {
    pub max_event_delay_ms: Option<u64>,
}

#[derive(FromPyObject, Clone)]
pub struct ExperimentalConfig {
    pub msc3026_enabled: bool,
    pub msc3773_enabled: bool,
    pub msc2815_enabled: bool,
    pub msc3881_enabled: bool,
    pub msc3874_enabled: bool,
    pub msc3912_enabled: bool,
    pub msc3391_enabled: bool,
    pub msc4069_profile_inhibit_propagation: bool,
    pub msc4028_push_encrypted_events: bool,
    pub msc4108_enabled: bool,
    pub msc4108_delegation_endpoint: Option<String>,
    pub msc3575_enabled: bool,
    pub msc4133_enabled: bool,
    pub msc4155_enabled: bool,
    pub msc4306_enabled: bool,
    pub msc4169_enabled: bool,
    pub msc4354_enabled: bool,
    pub msc4222_enabled: bool,
    pub msc4491_enabled: bool,
    pub msc4143_enabled: bool,
}
