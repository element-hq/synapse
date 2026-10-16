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

use std::collections::{BTreeSet, HashMap, HashSet};

use pyo3::prelude::*;

pub mod types;

/// A Rust-side view of Synapse's Python `HomeServerConfig`.
///
/// This only mirrors the subset of config that the Rust handlers need, rather
/// than the whole thing. Thanks to `#[derive(FromPyObject)]`, each field is
/// pulled directly off the corresponding attribute of the Python `config`
/// object, so you can populate it in one shot with:
/// ```ignore
/// let config: SynapseHomeServerConfig = homeserver.getattr("config")?.extract()?;
/// ```
#[derive(FromPyObject, Clone)]
pub struct SynapseHomeServerConfig {
    pub room: RoomConfig,
    pub auth: AuthConfig,
    pub server: ServerConfig,
    pub experimental: ExperimentalConfig,
    pub retention: RetentionConfig,
    pub federation: FederationConfig,
    pub tls: TlsConfig,
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
    pub msc4140_enabled: bool,
    pub public_baseurl: String,
    pub include_profile_updates_in_sync: bool,
    /// IP ranges outbound federation may not connect to.
    pub federation_ip_range_blocklist: types::IpRangeSet,
    /// Exceptions to `federation_ip_range_blocklist`.
    pub federation_ip_range_allowlist: Option<types::IpRangeSet>,
    pub user_agent_suffix: Option<String>,
    pub proxy_config: types::ProxyConfig,
}

/// `config.federation`: outbound federation request settings.
#[derive(FromPyObject, Clone)]
pub struct FederationConfig {
    /// If set, the only server names we federate with. Python keeps this as
    /// a dict of name to `True`.
    pub federation_domain_whitelist: Option<HashMap<String, bool>>,
    /// The destinations to label per-destination metrics with.
    pub federation_metrics_domains: HashSet<String>,
    pub client_timeout_ms: u64,
    pub max_long_retry_delay_ms: u64,
    pub max_short_retry_delay_ms: u64,
    pub max_long_retries: u32,
    pub max_short_retries: u32,
    pub destination_min_retry_interval_ms: u64,
    pub destination_retry_multiplier: f64,
    pub destination_max_retry_interval_ms: u64,
}

impl FederationConfig {
    /// Whether the federation whitelist (if any) lets us talk to `domain`.
    /// Mirrors `is_domain_allowed_according_to_federation_whitelist`.
    pub fn is_domain_allowed(&self, domain: &str) -> bool {
        self.federation_domain_whitelist
            .as_ref()
            .is_none_or(|whitelist| whitelist.contains_key(domain))
    }
}

/// `config.tls`: the TLS policy for outbound federation.
#[derive(FromPyObject, Clone)]
pub struct TlsConfig {
    pub federation_verify_certificates: bool,
    /// One of `1`, `1.1`, `1.2` or `1.3`.
    pub federation_client_minimum_tls_version: String,
    /// Hosts whose certificates are not verified, as anchored regexes.
    pub federation_certificate_verification_whitelist: Vec<types::PythonRegex>,
    /// The PEM text of `federation_custom_ca_list`, which replaces the
    /// platform trust roots when set.
    pub federation_custom_ca_pems: Option<Vec<String>>,
}

/// `config.retention`: the message retention policy settings.
#[derive(FromPyObject, Clone)]
pub struct RetentionConfig {
    pub retention_enabled: bool,
    pub retention_default_min_lifetime: Option<i64>,
    pub retention_default_max_lifetime: Option<i64>,
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
    pub msc4491_enabled: bool,
    pub msc4143_enabled: bool,
    pub msc4446_enabled: bool,
    pub msc4502_enabled: bool,
    /// `off`, `shadow`, `prepare`, `transaction` or `full`.
    pub rust_event_persistence: String,
}
