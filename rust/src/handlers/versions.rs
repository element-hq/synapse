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

use crate::config::{RoomCreationPreset, SynapseConfig};
use crate::deferred::create_deferred;
use crate::storage::db::python_db_pool::PythonDatabasePoolWrapper;
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
    pub store: Arc<Store<PythonDatabasePoolWrapper>>,
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
    store: &Store<PythonDatabasePoolWrapper>,
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

    log::info!(
        "asdf msc3881_enabled={:?} msc3575_enabled={:?}",
        msc3881_enabled,
        msc3575_enabled
    );

    let unstable_feature_map = UnstableFeatureMap {
        msc3575: msc3575_enabled,
        msc3881: msc3881_enabled,
        // The clone here isn't the best but better than manually composing things
        ..global_unstable_feature_map.clone()
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
    /// Simplified sliding sync
    #[serde(rename = "org.matrix.simplified_msc3575")]
    msc3575: bool,
    /// Adds support for remotely enabling/disabling pushers, as per MSC3881
    #[serde(rename = "org.matrix.msc3881")]
    msc3881: bool,

    // Whether new rooms will be set to encrypted or not (based on presets).
    #[serde(rename = "io.element.e2ee_forced.public")]
    e2ee_forced_public: bool,
    #[serde(rename = "io.element.e2ee_forced.private")]
    e2ee_forced_private: bool,
    #[serde(rename = "io.element.e2ee_forced.trusted_private")]
    e2ee_forced_trusted_private: bool,
    // // Implements support for label-based filtering as described in
    // // MSC2326.
    // ("org.matrix.label_based_filtering".to_string(), true),
    // // Implements support for cross signing as described in MSC1756
    // ("org.matrix.e2e_cross_signing".to_string(), true),
    // // Implements additional endpoints as described in MSC2432
    // ("org.matrix.msc2432".to_string(), true),
    // // Implements additional endpoints as described in MSC2666
    // ("uk.half-shot.msc2666.query_mutual_rooms.stable".to_string(), true),
    // // Supports the busy presence state described in MSC3026.
    // ("org.matrix.msc3026.busy_presence".to_string(), config.experimental.msc3026_enabled),
    // // Supports receiving private read receipts as per MSC2285
    // ("org.matrix.msc2285.stable".to_string(), true),  // TODO: Remove when MSC2285 becomes a part of the spec
    // // Supports filtering of /publicRooms by room type as per MSC3827
    // ("org.matrix.msc3827.stable".to_string(), true),
    // // Adds support for thread relations, per MSC3440.
    // ("org.matrix.msc3440.stable".to_string(), true),  // TODO: remove when "v1.3" is added above
    // // Support for thread read receipts & notification counts.
    // ("org.matrix.msc3771".to_string(), true),
    // ("org.matrix.msc3773".to_string(), config.experimental.msc3773_enabled),
    // // Allows moderators to fetch redacted event content as described in MSC2815
    // ("fi.mau.msc2815".to_string(), config.experimental.msc2815_enabled),
    // // Adds a ping endpoint for appservices to check HS->AS connection
    // ("fi.mau.msc2659.stable".to_string(), true),  // TODO: remove when "v1.7" is added above
    // // TODO: this is no longer needed once unstable MSC3882 does not need to be supported:
    // ("org.matrix.msc3882".to_string(), config.auth.login_via_existing_enabled),
    // // Adds support for filtering /messages by event relation.
    // ("org.matrix.msc3874".to_string(), config.experimental.msc3874_enabled),
    // // Adds support for relation-based redactions as per MSC3912.
    // ("org.matrix.msc3912".to_string(), config.experimental.msc3912_enabled),
    // // Whether recursively provide relations is supported.
    // // TODO This is no longer needed once unstable MSC3981 does not need to be supported.
    // ("org.matrix.msc3981".to_string(), true),
    // // Adds support for deleting account data.
    // ("org.matrix.msc3391".to_string(), config.experimental.msc3391_enabled),
    // // Allows clients to inhibit profile update propagation.
    // ("org.matrix.msc4069".to_string(), config.experimental.msc4069_profile_inhibit_propagation),
    // // Allows clients to handle push for encrypted events.
    // ("org.matrix.msc4028".to_string(), config.experimental.msc4028_push_encrypted_events),
    // // MSC4108: Mechanism to allow OIDC sign in and E2EE set up via QR code - 2024 version
    // ("org.matrix.msc4108".to_string(), (
    //     config.experimental.msc4108_enabled
    //     or (
    //         config.experimental.msc4108_delegation_endpoint
    //         is not None
    //     )
    // )),
    // // MSC4140: Delayed events
    // ("org.matrix.msc4140".to_string(), bool(config.server.max_event_delay_ms)),
    // // Arbitrary key-value profile fields.
    // ("uk.tcpip.msc4133".to_string(), config.experimental.msc4133_enabled),
    // ("uk.tcpip.msc4133.stable".to_string(), true),
    // // MSC4155: Invite filtering
    // ("org.matrix.msc4155".to_string(), config.experimental.msc4155_enabled),
    // // MSC4306: Support for thread subscriptions
    // ("org.matrix.msc4306".to_string(), config.experimental.msc4306_enabled),
    // // MSC4169: Backwards-compatible redaction sending using `/send`
    // ("com.beeper.msc4169".to_string(), config.experimental.msc4169_enabled),
    // // MSC4354: Sticky events
    // ("org.matrix.msc4354".to_string(), config.experimental.msc4354_enabled),
    // // MSC4380: Invite blocking
    // ("org.matrix.msc4380.stable".to_string(), true),
    // // MSC4445: Sync timeline order
    // ("org.matrix.msc4445.initial_sync_timeline_topological_ordering".to_string(), true),
}

/// Convert from [`SynapseConfig`] to the global defaults for unstable features that the
/// server supports [`UnstableFeatureMap`]
pub fn synapse_config_to_global_unstable_feature_map(config: &SynapseConfig) -> UnstableFeatureMap {
    UnstableFeatureMap {
        msc3575: config.experimental.msc3575_enabled,
        msc3881: config.experimental.msc3881_enabled,
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
