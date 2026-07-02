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

use std::sync::Arc;

use pyo3::prelude::*;
use pythonize::{pythonize, PythonizeError};
use serde::{Deserialize, Serialize};

use crate::config::{RoomCreationPreset, SynapseHomeServerConfig};
use crate::deferred::create_deferred;
use crate::storage::store::{PerUserExperimentalFeature, Store};

/// `GET /_matrix/client/versions` response
#[derive(Serialize, Deserialize, Clone, Debug)]
struct VersionsResponse {
    versions: Vec<String>,
    /// as per MSC1497
    unstable_features: std::collections::BTreeMap<String, bool>,
}

impl<'py> IntoPyObject<'py> for VersionsResponse {
    type Target = PyAny;
    type Output = Bound<'py, Self::Target>;
    type Error = PythonizeError;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        pythonize(py, &self)
    }
}

#[pyclass]
pub struct VersionsHandler {
    pub global_unstable_feature_map: Arc<UnstableFeatureMap>,
    pub store: Arc<Store>,
    /// The Twisted reactor, used to bridge our `async` response back into a
    /// Twisted deferred that Python can `await`.
    pub reactor: Py<PyAny>,
}

#[pymethods]
impl VersionsHandler {
    /// Assemble a `/versions` response, returning a Twisted deferred that
    /// resolves to the response body (a dict).
    #[pyo3(signature = (user_id=None))]
    fn get_versions<'py>(
        &self,
        py: Python<'py>,
        user_id: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let store = Arc::clone(&self.store);
        let global_unstable_feature_map = Arc::clone(&self.global_unstable_feature_map);

        create_deferred(py, self.reactor.bind(py), async move {
            build_versions_response(&store, &global_unstable_feature_map, user_id.as_deref())
                .await
                .map_err(|err| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "Failed to build /versions response: {err:#}"
                    ))
                })
        })
    }
}

/// Assemble a `/versions` response body.
///
/// Args:
///  * store
///  * global_unstable_feature_map: The global values before any per-user overrides
///  * user_id: The user making the request
async fn build_versions_response(
    store: &Store,
    global_unstable_feature_map: &UnstableFeatureMap,
    user_id: Option<&str>,
) -> Result<VersionsResponse, anyhow::Error> {
    let msc3881_enabled = match user_id {
        Some(user_id) => {
            // Don't both looking anything up if it's enabled for everyone
            if global_unstable_feature_map.msc3881 {
                true
            } else {
                // Look up whether it's explicitly enabled/disabled for this user
                store
                    .is_feature_enabled_for_user(user_id, PerUserExperimentalFeature::MSC3881)
                    .await?
                    // Default to false if there is no entry for this user
                    .unwrap_or(false)
            }
        }
        None => global_unstable_feature_map.msc3881,
    };

    let msc3575_enabled = match user_id {
        Some(user_id) => {
            // Don't both looking anything up if it's enabled for everyone
            if global_unstable_feature_map.msc3575 {
                true
            } else {
                // Look up whether it's explicitly enabled/disabled for this user
                store
                    .is_feature_enabled_for_user(user_id, PerUserExperimentalFeature::MSC3575)
                    .await?
                    // Default to false if there is no entry for this user
                    .unwrap_or(false)
            }
        }
        None => global_unstable_feature_map.msc3575,
    };

    let unstable_feature_map = UnstableFeatureMap {
        msc3575: msc3575_enabled,
        msc3881: msc3881_enabled,
        ..*global_unstable_feature_map
    };

    Ok(VersionsResponse {
        versions: Vec::from([
            // XXX: at some point we need to decide whether we need to include
            // the previous version numbers, given we've defined r0.3.0 to be
            // backwards compatible with r0.2.0.  But need to check how
            // conscientious we've been in compatibility, and decide whether the
            // middle number is the major revision when at 0.X.Y (as opposed to
            // X.Y.Z).  And we need to decide whether it's fair to make clients
            // parse the version string to figure out what's going on.
            "r0.0.1".to_string(),
            "r0.1.0".to_string(),
            "r0.2.0".to_string(),
            "r0.3.0".to_string(),
            "r0.4.0".to_string(),
            "r0.5.0".to_string(),
            "r0.6.0".to_string(),
            "r0.6.1".to_string(),
            "v1.1".to_string(),
            "v1.2".to_string(),
            "v1.3".to_string(),
            "v1.4".to_string(),
            "v1.5".to_string(),
            "v1.6".to_string(),
            "v1.7".to_string(),
            "v1.8".to_string(),
            "v1.9".to_string(),
            "v1.10".to_string(),
            "v1.11".to_string(),
            "v1.12".to_string(),
        ]),
        unstable_features: serde_json::from_value(serde_json::to_value(unstable_feature_map)?)?,
    })
}

/// Experimental features the server supports
#[derive(Serialize, Debug, Clone)]
pub struct UnstableFeatureMap {
    /// Implements support for label-based filtering as described in
    /// MSC2326.
    #[serde(rename = "org.matrix.label_based_filtering")]
    msc2326: bool,
    /// Implements support for cross signing as described in MSC1756
    #[serde(rename = "org.matrix.e2e_cross_signing")]
    msc1756: bool,
    /// Implements additional endpoints as described in MSC2432
    #[serde(rename = "org.matrix.msc2432")]
    msc2432: bool,
    /// Implements additional endpoints as described in MSC2666
    #[serde(rename = "uk.half-shot.msc2666.query_mutual_rooms.stable")]
    msc2666: bool,
    // Supports the busy presence state described in MSC3026.
    #[serde(rename = "org.matrix.msc3026.busy_presence")]
    msc3026: bool,
    /// Supports receiving private read receipts as per MSC2285
    // TODO: Remove when MSC2285 becomes a part of the spec
    #[serde(rename = "org.matrix.msc2285.stable")]
    msc2285: bool,
    /// Supports filtering of /publicRooms by room type as per MSC3827
    #[serde(rename = "org.matrix.msc3827.stable")]
    msc3827: bool,
    /// Adds support for thread relations, per MSC3440.
    // TODO: remove when "v1.3" is added above
    #[serde(rename = "org.matrix.msc3440.stable")]
    msc3440: bool,
    /// Support for thread read receipts & notification counts.
    #[serde(rename = "org.matrix.msc3771")]
    msc3771: bool,
    #[serde(rename = "org.matrix.msc3773")]
    msc3773: bool,
    /// Allows moderators to fetch redacted event content as described in MSC2815
    #[serde(rename = "fi.mau.msc2815")]
    msc2815: bool,
    /// Adds a ping endpoint for appservices to check HS->AS connection
    // TODO: remove when "v1.7" is added above
    #[serde(rename = "fi.mau.msc2659.stable")]
    msc2659: bool,
    // TODO: this is no longer needed once unstable MSC3882 does not need to be supported:
    #[serde(rename = "org.matrix.msc3882")]
    msc3882: bool,
    /// Adds support for remotely enabling/disabling pushers, as per MSC3881
    #[serde(rename = "org.matrix.msc3881")]
    msc3881: bool,
    /// Adds support for filtering /messages by event relation.
    #[serde(rename = "org.matrix.msc3874")]
    msc3874: bool,
    // Adds support for relation-based redactions as per MSC3912.
    #[serde(rename = "org.matrix.msc3912")]
    msc3912: bool,
    /// Whether recursively provide relations is supported.
    // TODO This is no longer needed once unstable MSC3981 does not need to be supported.
    #[serde(rename = "org.matrix.msc3981")]
    msc3981: bool,
    /// Adds support for deleting account data.
    #[serde(rename = "org.matrix.msc3391")]
    msc3391: bool,
    /// Allows clients to inhibit profile update propagation.
    #[serde(rename = "org.matrix.msc4069")]
    msc4069: bool,
    // Allows clients to handle push for encrypted events.
    #[serde(rename = "org.matrix.msc4028")]
    msc4028: bool,
    /// MSC4108: Mechanism to allow OIDC sign in and E2EE set up via QR code - 2024 version
    #[serde(rename = "org.matrix.msc4108")]
    msc4108: bool,
    /// MSC4140: Delayed events
    #[serde(rename = "org.matrix.msc4140")]
    msc4140: bool,
    /// Simplified sliding sync
    #[serde(rename = "org.matrix.simplified_msc3575")]
    msc3575: bool,
    /// Arbitrary key-value profile fields.
    #[serde(rename = "uk.tcpip.msc4133")]
    msc4133: bool,
    /// Arbitrary key-value profile fields (stable identifier)
    #[serde(rename = "uk.tcpip.msc4133.stable")]
    msc4133_stable: bool,
    /// MSC4155: Invite filtering
    #[serde(rename = "org.matrix.msc4155")]
    msc4155: bool,
    /// MSC4306: Support for thread subscriptions
    #[serde(rename = "org.matrix.msc4306")]
    msc4306: bool,
    /// MSC4169: Backwards-compatible redaction sending using `/send`
    #[serde(rename = "com.beeper.msc4169")]
    msc4169: bool,
    /// MSC4354: Sticky events
    #[serde(rename = "org.matrix.msc4354")]
    msc4354: bool,
    /// MSC4380: Invite blocking
    #[serde(rename = "org.matrix.msc4380.stable")]
    msc4380: bool,
    /// MSC4445: Sync timeline order
    #[serde(rename = "org.matrix.msc4445.initial_sync_timeline_topological_ordering")]
    msc4445_initial_sync_timeline_topological_ordering: bool,
    /// MSC4491: Invite reasons in room creation
    #[serde(rename = "uk.timedout.msc4491.create_room_invite_reasons")]
    msc4491_enabled: bool,
    /// MSC4143: Matrix RTC transports (LiveKit backend)
    #[serde(rename = "org.matrix.msc4143")]
    msc4143_enabled: bool,

    // Whether new rooms will be set to encrypted or not (based on presets).
    #[serde(rename = "io.element.e2ee_forced.public")]
    e2ee_forced_public: bool,
    #[serde(rename = "io.element.e2ee_forced.private")]
    e2ee_forced_private: bool,
    #[serde(rename = "io.element.e2ee_forced.trusted_private")]
    e2ee_forced_trusted_private: bool,
}

/// Convert from [`SynapseHomeServerConfig`] to the global defaults for unstable features that the
/// server supports [`UnstableFeatureMap`]
pub fn synapse_config_to_global_unstable_feature_map(
    config: &SynapseHomeServerConfig,
) -> UnstableFeatureMap {
    UnstableFeatureMap {
        msc2326: true,
        msc1756: true,
        msc2432: true,
        msc2666: true,
        msc3026: config.experimental.msc3026_enabled,
        msc2285: true,
        msc3827: true,
        msc3440: true,
        msc3771: true,
        msc3773: config.experimental.msc3773_enabled,
        msc2815: config.experimental.msc2815_enabled,
        msc2659: true,
        msc3882: config.auth.login_via_existing_enabled,
        msc3881: config.experimental.msc3881_enabled,
        msc3874: config.experimental.msc3874_enabled,
        msc3912: config.experimental.msc3912_enabled,
        msc3981: true,
        msc3391: config.experimental.msc3391_enabled,
        msc4069: config.experimental.msc4069_profile_inhibit_propagation,
        msc4028: config.experimental.msc4028_push_encrypted_events,
        msc4108: config.experimental.msc4108_enabled
            || (config.experimental.msc4108_delegation_endpoint.is_some()),
        msc4140: config
            .server
            .max_event_delay_ms
            .is_some_and(|max_event_delay_ms| max_event_delay_ms > 0),
        msc3575: config.experimental.msc3575_enabled,
        msc4133: config.experimental.msc4133_enabled,
        msc4133_stable: true,
        msc4155: config.experimental.msc4155_enabled,
        msc4306: config.experimental.msc4306_enabled,
        msc4169: config.experimental.msc4169_enabled,
        msc4354: config.experimental.msc4354_enabled,
        msc4380: true,
        msc4445_initial_sync_timeline_topological_ordering: true,
        msc4491_enabled: config.experimental.msc4491_enabled,
        msc4143_enabled: config.experimental.msc4143_enabled,
        e2ee_forced_public: config
            .room
            .encryption_enabled_by_default_for_room_presets
            .contains(&RoomCreationPreset::PublicChat),
        e2ee_forced_private: config
            .room
            .encryption_enabled_by_default_for_room_presets
            .contains(&RoomCreationPreset::PrivateChat),
        e2ee_forced_trusted_private: config
            .room
            .encryption_enabled_by_default_for_room_presets
            .contains(&RoomCreationPreset::TrustedPrivateChat),
    }
}
