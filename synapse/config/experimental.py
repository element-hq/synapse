#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
# Copyright (C) 2025 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

from functools import cache
from typing import Any

import attr

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config import ConfigError
from synapse.config._base import Config, read_file
from synapse.types import JsonDict, StrSequence


@cache
def read_secret_from_file_once(file_path: Any, config_path: StrSequence) -> str:
    """Returns the memoized secret read from file."""
    return read_file(file_path, config_path).strip()


@attr.s(auto_attribs=True, frozen=True, slots=True)
class MSC3866Config:
    """Configuration for MSC3866 (mandating approval for new users)"""

    # Whether the base support for the approval process is enabled. This includes the
    # ability for administrators to check and update the approval of users, even if no
    # approval is currently required.
    enabled: bool = False
    # Whether to require that new users are approved by an admin before their account
    # can be used. Note that this setting is ignored if 'enabled' is false.
    require_approval_for_new_accounts: bool = False


class ExperimentalConfig(Config):
    """Config section for enabling experimental features

    All new experimental features should have a tracking issue with the
    `T-ExperimentalFeatures` label, kept open as long as the experimental
    feature is present in Synapse."""

    section = "experimental"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        experimental = config.get("experimental_features") or {}

        # MSC1763 (retention policy configuration endpoint)
        self.msc1763_enabled: bool = experimental.get("msc1763_enabled", False)

        # MSC3026 (busy presence state)
        self.msc3026_enabled: bool = experimental.get("msc3026_enabled", False)

        # MSC3814 (dehydrated devices with SSSS)
        self.msc3814_enabled: bool = experimental.get("msc3814_enabled", False)

        # MSC2409 (this setting only relates to optionally sending to-device messages).
        # Presence, typing and read receipt EDUs are already sent to application services that
        # have opted in to receive them. If enabled, this adds to-device messages to that list.
        # This is also for MSC4203 which was broken off of MSC2409 but kept the same unstable
        # identifier.
        self.msc2409_to_device_messages_enabled: bool = experimental.get(
            "msc2409_to_device_messages_enabled", False
        )

        # The portion of MSC3202 related to transaction extensions:
        # sending device list changes, one-time key counts and fallback key
        # usage to application services.
        self.msc3202_transaction_extensions: bool = experimental.get(
            "msc3202_transaction_extensions", False
        )

        # MSC3983: Proxying OTK claim requests to exclusive ASes.
        self.msc3983_appservice_otk_claims: bool = experimental.get(
            "msc3983_appservice_otk_claims", False
        )

        # MSC3984: Proxying key queries to exclusive ASes.
        self.msc3984_appservice_key_query: bool = experimental.get(
            "msc3984_appservice_key_query", False
        )

        # MSC3720 (Account status endpoint)
        self.msc3720_enabled: bool = experimental.get("msc3720_enabled", False)

        # MSC2654: Unread counts
        #
        # Note that enabling this will result in an incorrect unread count for
        # previously calculated push actions.
        self.msc2654_enabled: bool = experimental.get("msc2654_enabled", False)

        # MSC2815 (allow room moderators to view redacted event content)
        self.msc2815_enabled: bool = experimental.get("msc2815_enabled", False)

        # MSC3391: Removing account data.
        self.msc3391_enabled = experimental.get("msc3391_enabled", False)

        # MSC3575 (Sliding Sync) alternate endpoints, c.f. MSC4186.
        #
        # This is enabled by default as a replacement for the sliding sync proxy.
        self.msc3575_enabled: bool = experimental.get("msc3575_enabled", True)

        # MSC3773: Thread notifications
        self.msc3773_enabled: bool = experimental.get("msc3773_enabled", False)

        # MSC3664: Pushrules to match on related events
        self.msc3664_enabled: bool = experimental.get("msc3664_enabled", False)

        # MSC3848: Introduce errcodes for specific event sending failures
        self.msc3848_enabled: bool = experimental.get("msc3848_enabled", False)

        # MSC3866: M_USER_AWAITING_APPROVAL error code
        raw_msc3866_config = experimental.get("msc3866", {})
        self.msc3866 = MSC3866Config(**raw_msc3866_config)

        # MSC3881: Remotely toggle push notifications for another client
        self.msc3881_enabled: bool = experimental.get("msc3881_enabled", False)

        # MSC3874: Filtering /messages with rel_types / not_rel_types.
        self.msc3874_enabled: bool = experimental.get("msc3874_enabled", False)

        # MSC3890: Remotely silence local notifications
        # Note: This option requires "experimental_features.msc3391_enabled" to be
        # set to "true", in order to communicate account data deletions to clients.
        self.msc3890_enabled: bool = experimental.get("msc3890_enabled", False)
        if self.msc3890_enabled and not self.msc3391_enabled:
            raise ConfigError(
                "Option 'experimental_features.msc3391' must be set to 'true' to "
                "enable 'experimental_features.msc3890'. MSC3391 functionality is "
                "required to communicate account data deletions to clients."
            )

        # MSC3381: Polls.
        # In practice, supporting polls in Synapse only requires an implementation of
        # MSC3930: Push rules for MSC3391 polls; which is what this option enables.
        self.msc3381_polls_enabled: bool = experimental.get(
            "msc3381_polls_enabled", False
        )

        # MSC4505: default push rules for live location sharing — notify on
        # beacon_info start events (the MSC3672 state event), suppress beacon
        # updates. Unstable rule ids: .org.matrix.msc4505.rule.beacon_info
        # (_one_to_one) / .org.matrix.msc4505.rule.beacon.
        self.msc4505_enabled: bool = experimental.get("msc4505_enabled", False)

        # MSC3912: Relation-based redactions.
        self.msc3912_enabled: bool = experimental.get("msc3912_enabled", False)

        # MSC1767 and friends: Extensible Events
        self.msc1767_enabled: bool = experimental.get("msc1767_enabled", False)
        if self.msc1767_enabled:
            # Enable room version (and thus applicable push rules from MSC3931/3932)
            KNOWN_ROOM_VERSIONS.add_room_version(RoomVersions.MSC1767v10)

        # MSC4242: State DAGs
        self.msc4242_enabled: bool = experimental.get("msc4242_enabled", False)
        if self.msc4242_enabled:
            # Enable the room version
            KNOWN_ROOM_VERSIONS.add_room_version(RoomVersions.MSC4242v12)

        # MSC3391: Removing account data.
        self.msc3391_enabled = experimental.get("msc3391_enabled", False)

        # MSC3861 was replaced by the stable Matrix Authentication Service integration.
        msc3861_config = experimental.get("msc3861", {})
        if msc3861_config:  # non-empty dict
            raise ConfigError(
                "experimental_features.msc3861 was removed. "
                "Use the matrix_authentication_service configuration instead.",
                ("experimental", "msc3861"),
            )

        self.msc4028_push_encrypted_events = experimental.get(
            "msc4028_push_encrypted_events", False
        )

        self.msc4069_profile_inhibit_propagation = experimental.get(
            "msc4069_profile_inhibit_propagation", False
        )

        # MSC4108: Mechanism to allow OIDC sign in and E2EE set up via QR code - 2024 version:
        # See: https://github.com/element-hq/synapse/issues/19434
        self.msc4108_enabled = experimental.get("msc4108_enabled", False)

        self.msc4108_delegation_endpoint: str | None = experimental.get(
            "msc4108_delegation_endpoint", None
        )

        # MSC4370: Get extremities federation endpoint
        # See https://github.com/element-hq/synapse/issues/19524
        self.msc4370_enabled = experimental.get("msc4370_enabled", False)

        auth_delegated = (config.get("matrix_authentication_service") or {}).get(
            "enabled", False
        )

        if (
            self.msc4108_enabled or self.msc4108_delegation_endpoint is not None
        ) and not auth_delegated:
            raise ConfigError(
                "MSC4108 requires matrix_authentication_service to be enabled",
                ("experimental", "msc4108_delegation_endpoint"),
            )

        if self.msc4108_delegation_endpoint is not None and self.msc4108_enabled:
            raise ConfigError(
                "You cannot have MSC4108 both enabled and delegated at the same time",
                ("experimental", "msc4108_delegation_endpoint"),
            )

        # MSC4388: Secure out-of-band channel for sign in with QR:
        # See: https://github.com/element-hq/synapse/issues/19433
        msc4388_mode = experimental.get("msc4388_mode", "off")

        if msc4388_mode not in ["off", "open", "authenticated"]:
            raise ConfigError(
                "msc4388_mode must be one of 'off', 'open' or 'authenticated'",
                ("experimental", "msc4388_mode"),
            )
        self.msc4388_enabled: bool = msc4388_mode != "off"
        self.msc4388_requires_authentication: bool = msc4388_mode == "authenticated"

        if self.msc4388_enabled and not (
            config.get("matrix_authentication_service") or {}
        ).get("enabled", False):
            raise ConfigError(
                "MSC4388 requires matrix_authentication_service to be enabled",
                ("experimental", "msc4388_enabled"),
            )

        # MSC4133: Custom profile fields
        self.msc4133_enabled: bool = experimental.get("msc4133_enabled", False)

        # MSC4143: Matrix RTC Transport using Livekit Backend
        self.msc4143_enabled: bool = experimental.get("msc4143_enabled", False)

        # MSC4169: Backwards-compatible redaction sending using `/send`
        self.msc4169_enabled: bool = experimental.get("msc4169_enabled", False)

        # MSC4210: Remove legacy mentions
        self.msc4210_enabled: bool = experimental.get("msc4210_enabled", False)

        # MSC4222: Adding `state_after` to sync v2
        self.msc4222_enabled: bool = experimental.get("msc4222_enabled", False)

        # MSC4076: Add `disable_badge_count`` to pusher configuration
        self.msc4076_enabled: bool = experimental.get("msc4076_enabled", False)

        # MSC4277: Harmonizing the reporting endpoints
        #
        # If enabled, ignore the score parameter and respond with HTTP 200 on
        # reporting requests regardless of the subject's existence.
        self.msc4277_enabled: bool = experimental.get("msc4277_enabled", False)

        # MSC4235: Add `via` param to hierarchy endpoint
        self.msc4235_enabled: bool = experimental.get("msc4235_enabled", False)

        # MSC4263: Preventing MXID enumeration via key queries
        self.msc4263_limit_key_queries_to_users_who_share_rooms = experimental.get(
            "msc4263_limit_key_queries_to_users_who_share_rooms",
            False,
        )

        # MSC4267: Automatically forgetting rooms on leave
        self.msc4267_enabled: bool = experimental.get("msc4267_enabled", False)

        # MSC4155: Invite filtering
        self.msc4155_enabled: bool = experimental.get("msc4155_enabled", False)

        # MSC4293: Redact on Kick/Ban
        self.msc4293_enabled: bool = experimental.get("msc4293_enabled", False)

        # MSC4306: Thread Subscriptions
        # (and MSC4308: Thread Subscriptions extension to Sliding Sync)
        self.msc4306_enabled: bool = experimental.get("msc4306_enabled", False)

        # MSC4446: Allow moving the fully read marker backwards.
        # Tracked in: https://github.com/element-hq/synapse/issues/19940
        self.msc4446_enabled: bool = experimental.get("msc4446_enabled", False)

        # MSC4354: Sticky Events
        # Tracked in: https://github.com/element-hq/synapse/issues/19409
        # Note that sticky events persisted before this feature is enabled will not be
        # considered sticky by the local homeserver.
        self.msc4354_enabled: bool = experimental.get("msc4354_enabled", False)

        # MSC4450: Identity Provider selection for User-Interactive Authentication
        # with Legacy Single Sign-On (`m.login.sso`)
        # Tracked in: https://github.com/element-hq/synapse/issues/19691
        # Note that this is only applicable to legacy auth, not MAS integration (OAuth 2.0).
        self.msc4450_enabled: bool = experimental.get("msc4450_enabled", False)

        # MSC4455: Preview URL capability
        # Tracked in: https://github.com/element-hq/synapse/issues/19719
        self.msc4452_enabled: bool = experimental.get("msc4452_enabled", False)

        # MSC4491: Invite reasons in room creation
        self.msc4491_enabled: bool = experimental.get("msc4491_enabled", False)
