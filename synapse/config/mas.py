#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

from typing import Any

from pydantic import (
    AnyHttpUrl,
    Field,
    FilePath,
    StrictBool,
    StrictStr,
    ValidationError,
    model_validator,
)
from typing_extensions import Self

from synapse.config.experimental import read_secret_from_file_once
from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError, RootConfig


class MasConfigModel(ParseModel):
    enabled: StrictBool = False
    endpoint: AnyHttpUrl = AnyHttpUrl("http://localhost:8080")
    secret: StrictStr | None = Field(default=None)
    # We set `strict=False` to allow `str` instances.
    secret_path: FilePath | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def verify_secret(self) -> Self:
        if not self.enabled:
            return self
        if not self.secret and not self.secret_path:
            raise ValueError(
                "You must set a `secret` or `secret_path` when enabling the Matrix "
                "Authentication Service integration."
            )
        if self.secret and self.secret_path:
            raise ValueError(
                "`secret` and `secret_path` cannot be set at the same time."
            )
        return self


class MasConfig(Config):
    section = "mas"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        mas_config = config.get("matrix_authentication_service", {})
        if mas_config is None:
            mas_config = {}

        try:
            parsed = MasConfigModel(**mas_config)
        except ValidationError as e:
            raise ConfigError(
                "Could not validate Matrix Authentication Service configuration",
                path=("matrix_authentication_service",),
            ) from e

        if parsed.secret and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("matrix_authentication_service", "secret"),
            )

        self.enabled = parsed.enabled
        self.endpoint = parsed.endpoint
        self._secret = parsed.secret
        self._secret_path = parsed.secret_path

        self.check_config_conflicts(self.root)

    def check_config_conflicts(
        self,
        root: RootConfig,
    ) -> None:
        """Checks for any configuration conflicts with other parts of Synapse.

        Raises:
            ConfigError: If there are any configuration conflicts.
        """

        if not self.enabled:
            return

        if root.experimental.msc3861.enabled:
            raise ConfigError(
                "Experimental MSC3861 was replaced by Matrix Authentication Service."
                "Please disable MSC3861 or disable Matrix Authentication Service.",
                ("experimental", "msc3861"),
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

    def secret(self) -> str:
        if self._secret is not None:
            return self._secret
        elif self._secret_path is not None:
            return read_secret_from_file_once(
                str(self._secret_path),
                ("matrix_authentication_service", "secret_path"),
            )
        else:
            raise RuntimeError(
                "Neither `secret` nor `secret_path` are set, this is a bug.",
            )
