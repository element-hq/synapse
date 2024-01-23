#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2015, 2016 OpenMarket Ltd
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
from typing import Any

from synapse.types import JsonDict

from ._base import Config


class AuthConfig(Config):
    """Password and login configuration"""

    section = "auth"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        password_config = config.get("password_config", {})
        if password_config is None:
            password_config = {}

        # The default value of password_config.enabled is True, unless msc3861 is enabled.
        msc3861_enabled = (
            (config.get("experimental_features") or {})
            .get("msc3861", {})
            .get("enabled", False)
        )
        passwords_enabled = password_config.get("enabled", not msc3861_enabled)

        # 'only_for_reauth' allows users who have previously set a password to use it,
        # even though passwords would otherwise be disabled.
        passwords_for_reauth_only = passwords_enabled == "only_for_reauth"

        self.password_enabled_for_login = (
            passwords_enabled and not passwords_for_reauth_only
        )
        self.password_enabled_for_reauth = (
            passwords_for_reauth_only or passwords_enabled
        )

        self.password_localdb_enabled = password_config.get("localdb_enabled", True)
        self.password_pepper = password_config.get("pepper", "")

        # Password policy
        self.password_policy = password_config.get("policy") or {}
        self.password_policy_enabled = self.password_policy.get("enabled", False)

        # User-interactive authentication
        ui_auth = config.get("ui_auth") or {}
        self.ui_auth_session_timeout = self.parse_duration(
            ui_auth.get("session_timeout", 0)
        )

        # Logging in with an existing session.
        login_via_existing = config.get("login_via_existing_session", {})
        self.login_via_existing_enabled = login_via_existing.get("enabled", False)
        self.login_via_existing_require_ui_auth = login_via_existing.get(
            "require_ui_auth", True
        )
        self.login_via_existing_token_timeout = self.parse_duration(
            login_via_existing.get("token_timeout", "5m")
        )
