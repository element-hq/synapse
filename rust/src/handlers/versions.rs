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

use serde::{Deserialize, Serialize};

use crate::config::SynapseConfig;
use crate::storage::store::{PerUserExperimentalFeature, Store};

/// `GET /_matrix/client/versions` response
#[derive(Serialize, Deserialize, Clone, Debug)]
struct VersionsResponse {
    versions: Vec<String>,
    /// as per MSC1497
    unstable_features: std::collections::BTreeMap<String, bool>,
}

/// Assemble a `/versions` response
async fn get_versions(
    store: &Store,
    user_id: Option<&str>,
    config: SynapseConfig,
) -> Result<VersionsResponse, anyhow::Error> {
    let msc3881_enabled = match user_id {
        Some(user_id) => {
            store
                .is_feature_enabled(user_id, PerUserExperimentalFeature::MSC3881)
                .await?
        }
        None => config.experimental.msc3881_enabled,
    };

    let msc3575_enabled = match user_id {
        Some(user_id) => {
            store
                .is_feature_enabled(user_id, PerUserExperimentalFeature::MSC3575)
                .await?
        }
        None => config.experimental.msc3575_enabled,
    };

    // TODO: Calculate these once since they shouldn't change after start-up.
    // e2ee_forced_public = (
    //     RoomCreationPreset.PUBLIC_CHAT
    //     in config.room.encryption_enabled_by_default_for_room_presets
    // );
    // e2ee_forced_private = (
    //     RoomCreationPreset.PRIVATE_CHAT
    //     in config.room.encryption_enabled_by_default_for_room_presets
    // );
    // e2ee_forced_trusted_private = (
    //     RoomCreationPreset.TRUSTED_PRIVATE_CHAT
    //     in config.room.encryption_enabled_by_default_for_room_presets
    // );

    return Ok(VersionsResponse {
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
        unstable_features: std::collections::BTreeMap::from([
            // // Implements support for label-based filtering as described in
            // // MSC2326.
            // ("org.matrix.label_based_filtering".to_string(), true),
            // // Implements support for cross signing as described in MSC1756
            // ("org.matrix.e2e_cross_signing".to_string(), true),
            // // Implements additional endpoints as described in MSC2432
            // ("org.matrix.msc2432".to_string(), true),
            // // Implements additional endpoints as described in MSC2666
            // ("uk.half-shot.msc2666.query_mutual_rooms.stable".to_string(), true),
            // // Whether new rooms will be set to encrypted or not (based on presets).
            // ("io.element.e2ee_forced.public".to_string(), e2ee_forced_public),
            // ("io.element.e2ee_forced.private".to_string(), e2ee_forced_private),
            // ("io.element.e2ee_forced.trusted_private".to_string(), e2ee_forced_trusted_private),
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
            // Adds support for remotely enabling/disabling pushers, as per MSC3881
            ("org.matrix.msc3881".to_string(), msc3881_enabled),
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
            // Simplified sliding sync
            ("org.matrix.simplified_msc3575".to_string(), msc3575_enabled),
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
        ]),
    });
}
