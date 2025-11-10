#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
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

import enum
from functools import cache
from typing import TYPE_CHECKING, Any, Optional

import attr
import attr.validators

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.config import ConfigError
from synapse.config._base import Config, RootConfig, read_file
from synapse.types import JsonDict, StrSequence

# Determine whether authlib is installed.
try:
    import authlib  # noqa: F401

    HAS_AUTHLIB = True
except ImportError:
    HAS_AUTHLIB = False

if TYPE_CHECKING:
    # Only import this if we're type checking, as it might not be installed at runtime.
    from authlib.jose.rfc7517 import JsonWebKey


@cache
def read_secret_from_file_once(file_path: Any, config_path: StrSequence) -> str:
    """Returns the memoized secret read from file."""
    return read_file(file_path, config_path).strip()


class ClientAuthMethod(enum.Enum):
    """List of supported client auth methods."""

    CLIENT_SECRET_POST = "client_secret_post"
    CLIENT_SECRET_BASIC = "client_secret_basic"
    CLIENT_SECRET_JWT = "client_secret_jwt"
    PRIVATE_KEY_JWT = "private_key_jwt"


def _parse_jwks(jwks: JsonDict | None) -> Optional["JsonWebKey"]:
    """A helper function to parse a JWK dict into a JsonWebKey."""

    if jwks is None:
        return None

    from authlib.jose.rfc7517 import JsonWebKey

    return JsonWebKey.import_key(jwks)


def _check_client_secret(
    instance: "MSC3861", _attribute: attr.Attribute, _value: str | None
) -> None:
    if instance._client_secret and instance._client_secret_path:
        raise ConfigError(
            (
                "You have configured both "
                "`experimental_features.msc3861.client_secret` and "
                "`experimental_features.msc3861.client_secret_path`. "
                "These are mutually incompatible."
            ),
            ("experimental", "msc3861", "client_secret"),
        )
    # Check client secret can be retrieved
    instance.client_secret()


def _check_admin_token(
    instance: "MSC3861", _attribute: attr.Attribute, _value: str | None
) -> None:
    if instance._admin_token and instance._admin_token_path:
        raise ConfigError(
            (
                "You have configured both "
                "`experimental_features.msc3861.admin_token` and "
                "`experimental_features.msc3861.admin_token_path`. "
                "These are mutually incompatible."
            ),
            ("experimental", "msc3861", "admin_token"),
        )
    # Check client secret can be retrieved
    instance.admin_token()


@attr.s(slots=True, frozen=True)
class MSC3861:
    """Configuration for MSC3861: Matrix architecture change to delegate authentication via OIDC"""

    enabled: bool = attr.ib(default=False, validator=attr.validators.instance_of(bool))
    """Whether to enable MSC3861 auth delegation."""

    @enabled.validator
    def _check_enabled(self, attribute: attr.Attribute, value: bool) -> None:
        # Only allow enabling MSC3861 if authlib is installed
        if value and not HAS_AUTHLIB:
            raise ConfigError(
                "MSC3861 is enabled but authlib is not installed. "
                "Please install authlib to use MSC3861.",
                ("experimental", "msc3861", "enabled"),
            )

    issuer: str = attr.ib(default="", validator=attr.validators.instance_of(str))
    """The URL of the OIDC Provider."""

    issuer_metadata: JsonDict | None = attr.ib(default=None)
    """The issuer metadata to use, otherwise discovered from /.well-known/openid-configuration as per MSC2965."""

    client_id: str = attr.ib(
        default="",
        validator=attr.validators.instance_of(str),
    )
    """The client ID to use when calling the introspection endpoint."""

    client_auth_method: ClientAuthMethod = attr.ib(
        default=ClientAuthMethod.CLIENT_SECRET_POST, converter=ClientAuthMethod
    )
    """The auth method used when calling the introspection endpoint."""

    _client_secret: str | None = attr.ib(
        default=None,
        validator=[
            attr.validators.optional(attr.validators.instance_of(str)),
            _check_client_secret,
        ],
    )
    """
    The client secret to use when calling the introspection endpoint,
    when using any of the client_secret_* client auth methods.
    """

    _client_secret_path: str | None = attr.ib(
        default=None,
        validator=[
            attr.validators.optional(attr.validators.instance_of(str)),
            _check_client_secret,
        ],
    )
    """
    Alternative to `client_secret`: allows the secret to be specified in an
    external file.
    """

    jwk: Optional["JsonWebKey"] = attr.ib(default=None, converter=_parse_jwks)
    """
    The JWKS to use when calling the introspection endpoint,
    when using the private_key_jwt client auth method.
    """

    @client_auth_method.validator
    def _check_client_auth_method(
        self, attribute: attr.Attribute, value: ClientAuthMethod
    ) -> None:
        # Check that the right client credentials are provided for the client auth method.
        if not self.enabled:
            return

        if value == ClientAuthMethod.PRIVATE_KEY_JWT and self.jwk is None:
            raise ConfigError(
                "A JWKS must be provided when using the private_key_jwt client auth method",
                ("experimental", "msc3861", "client_auth_method"),
            )

        if (
            value
            in (
                ClientAuthMethod.CLIENT_SECRET_POST,
                ClientAuthMethod.CLIENT_SECRET_BASIC,
                ClientAuthMethod.CLIENT_SECRET_JWT,
            )
            and self.client_secret() is None
        ):
            raise ConfigError(
                f"A client secret must be provided when using the {value} client auth method",
                ("experimental", "msc3861", "client_auth_method"),
            )

    introspection_endpoint: str | None = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str)),
    )
    """The URL of the introspection endpoint used to validate access tokens."""

    account_management_url: str | None = attr.ib(
        default=None,
        validator=attr.validators.optional(attr.validators.instance_of(str)),
    )
    """The URL of the My Account page on the OIDC Provider as per MSC2965."""

    _admin_token: str | None = attr.ib(
        default=None,
        validator=[
            attr.validators.optional(attr.validators.instance_of(str)),
            _check_admin_token,
        ],
    )
    """
    A token that should be considered as an admin token.
    This is used by the OIDC provider, to make admin calls to Synapse.
    """

    _admin_token_path: str | None = attr.ib(
        default=None,
        validator=[
            attr.validators.optional(attr.validators.instance_of(str)),
            _check_admin_token,
        ],
    )
    """
    Alternative to `admin_token`: allows the secret to be specified in an
    external file.
    """

    def client_secret(self) -> str | None:
        """Returns the secret given via `client_secret` or `client_secret_path`."""
        if self._client_secret_path:
            return read_secret_from_file_once(
                self._client_secret_path,
                ("experimental_features", "msc3861", "client_secret_path"),
            )
        return self._client_secret

    def admin_token(self) -> str | None:
        """Returns the admin token given via `admin_token` or `admin_token_path`."""
        if self._admin_token_path:
            return read_secret_from_file_once(
                self._admin_token_path,
                ("experimental_features", "msc3861", "admin_token_path"),
            )
        return self._admin_token

    def check_config_conflicts(
        self, root: RootConfig, allow_secrets_in_config: bool
    ) -> None:
        """Checks for any configuration conflicts with other parts of Synapse.

        Raises:
            ConfigError: If there are any configuration conflicts.
        """

        if not self.enabled:
            return

        if self._client_secret and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("experimental", "msc3861", "client_secret"),
            )

        if self.jwk and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("experimental", "msc3861", "jwk"),
            )

        if self._admin_token and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("experimental", "msc3861", "admin_token"),
            )

        if (
            root.auth.password_enabled_for_reauth
            or root.auth.password_enabled_for_login
        ):
            raise ConfigError(
                "Password auth cannot be enabled when OAuth delegation is enabled",
                ("password_config", "enabled"),
            )

        if root.registration.enable_registration:
            raise ConfigError(
                "Registration cannot be enabled when OAuth delegation is enabled",
                ("enable_registration",),
            )

        # We only need to test the user consent version, as if it must be set if the user_consent section was present in the config
        if root.consent.user_consent_version is not None:
            raise ConfigError(
                "User consent cannot be enabled when OAuth delegation is enabled",
                ("user_consent",),
            )

        if (
            root.oidc.oidc_enabled
            or root.saml2.saml2_enabled
            or root.cas.cas_enabled
            or root.jwt.jwt_enabled
        ):
            raise ConfigError("SSO cannot be enabled when OAuth delegation is enabled")

        if bool(root.authproviders.password_providers):
            raise ConfigError(
                "Password auth providers cannot be enabled when OAuth delegation is enabled"
            )

        if root.captcha.enable_registration_captcha:
            raise ConfigError(
                "CAPTCHA cannot be enabled when OAuth delegation is enabled",
                ("captcha", "enable_registration_captcha"),
            )

        if root.auth.login_via_existing_enabled:
            raise ConfigError(
                "Login via existing session cannot be enabled when OAuth delegation is enabled",
                ("login_via_existing_session", "enabled"),
            )

        if root.registration.refresh_token_lifetime:
            raise ConfigError(
                "refresh_token_lifetime cannot be set when OAuth delegation is enabled",
                ("refresh_token_lifetime",),
            )

        if root.registration.nonrefreshable_access_token_lifetime:
            raise ConfigError(
                "nonrefreshable_access_token_lifetime cannot be set when OAuth delegation is enabled",
                ("nonrefreshable_access_token_lifetime",),
            )

        if root.registration.session_lifetime:
            raise ConfigError(
                "session_lifetime cannot be set when OAuth delegation is enabled",
                ("session_lifetime",),
            )

        if root.registration.enable_3pid_changes:
            raise ConfigError(
                "enable_3pid_changes cannot be enabled when OAuth delegation is enabled",
                ("enable_3pid_changes",),
            )


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
    """Config section for enabling experimental features"""

    section = "experimental"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        experimental = config.get("experimental_features") or {}

        # MSC3026 (busy presence state)
        self.msc3026_enabled: bool = experimental.get("msc3026_enabled", False)

        # MSC2697 (device dehydration)
        # Enabled by default since this option was added after adding the feature.
        # It is not recommended that both MSC2697 and MSC3814 both be enabled at
        # once.
        self.msc2697_enabled: bool = experimental.get("msc2697_enabled", True)

        # MSC3814 (dehydrated devices with SSSS)
        # This is an alternative method to achieve the same goals as MSC2697.
        # It is not recommended that both MSC2697 and MSC3814 both be enabled at
        # once.
        self.msc3814_enabled: bool = experimental.get("msc3814_enabled", False)

        if self.msc2697_enabled and self.msc3814_enabled:
            raise ConfigError(
                "MSC2697 and MSC3814 should not both be enabled.",
                (
                    "experimental_features",
                    "msc3814_enabled",
                ),
            )

        # MSC3244 (room version capabilities)
        self.msc3244_enabled: bool = experimental.get("msc3244_enabled", True)

        # MSC3266 (room summary api)
        self.msc3266_enabled: bool = experimental.get("msc3266_enabled", False)

        # MSC2409 (this setting only relates to optionally sending to-device messages).
        # Presence, typing and read receipt EDUs are already sent to application services that
        # have opted in to receive them. If enabled, this adds to-device messages to that list.
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

        # MSC3852: Expose last seen user agent field on /_matrix/client/v3/devices.
        self.msc3852_enabled: bool = experimental.get("msc3852_enabled", False)

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

        # MSC3912: Relation-based redactions.
        self.msc3912_enabled: bool = experimental.get("msc3912_enabled", False)

        # MSC1767 and friends: Extensible Events
        self.msc1767_enabled: bool = experimental.get("msc1767_enabled", False)
        if self.msc1767_enabled:
            # Enable room version (and thus applicable push rules from MSC3931/3932)
            version_id = RoomVersions.MSC1767v10.identifier
            KNOWN_ROOM_VERSIONS[version_id] = RoomVersions.MSC1767v10

        # MSC3391: Removing account data.
        self.msc3391_enabled = experimental.get("msc3391_enabled", False)

        # MSC3861: Matrix architecture change to delegate authentication via OIDC
        try:
            self.msc3861 = MSC3861(**experimental.get("msc3861", {}))
        except ValueError as exc:
            raise ConfigError(
                "Invalid MSC3861 configuration", ("experimental", "msc3861")
            ) from exc

        # Check that none of the other config options conflict with MSC3861 when enabled
        self.msc3861.check_config_conflicts(
            self.root, allow_secrets_in_config=allow_secrets_in_config
        )

        self.msc4028_push_encrypted_events = experimental.get(
            "msc4028_push_encrypted_events", False
        )

        self.msc4069_profile_inhibit_propagation = experimental.get(
            "msc4069_profile_inhibit_propagation", False
        )

        # MSC4108: Mechanism to allow OIDC sign in and E2EE set up via QR code
        self.msc4108_enabled = experimental.get("msc4108_enabled", False)

        self.msc4108_delegation_endpoint: str | None = experimental.get(
            "msc4108_delegation_endpoint", None
        )

        auth_delegated = self.msc3861.enabled or (
            config.get("matrix_authentication_service") or {}
        ).get("enabled", False)

        if (
            self.msc4108_enabled or self.msc4108_delegation_endpoint is not None
        ) and not auth_delegated:
            raise ConfigError(
                "MSC4108 requires MSC3861 or matrix_authentication_service to be enabled",
                ("experimental", "msc4108_delegation_endpoint"),
            )

        if self.msc4108_delegation_endpoint is not None and self.msc4108_enabled:
            raise ConfigError(
                "You cannot have MSC4108 both enabled and delegated at the same time",
                ("experimental", "msc4108_delegation_endpoint"),
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
