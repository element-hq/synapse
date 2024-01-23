#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020-2021 The Matrix.org Foundation C.I.C.
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
import logging
from typing import Any, Dict, Optional

import attr

from synapse.types import JsonDict

from ._base import Config

logger = logging.getLogger(__name__)

LEGACY_TEMPLATE_DIR_WARNING = """
This server's configuration file is using the deprecated 'template_dir' setting in the
'sso' section. Support for this setting has been deprecated and will be removed in a
future version of Synapse. Server admins should instead use the new
'custom_template_directory' setting documented here:
https://element-hq.github.io/synapse/latest/templates.html
---------------------------------------------------------------------------------------"""


@attr.s(frozen=True, auto_attribs=True)
class SsoAttributeRequirement:
    """Object describing a single requirement for SSO attributes."""

    attribute: str
    # If a value is not given, than the attribute must simply exist.
    value: Optional[str]

    JSON_SCHEMA = {
        "type": "object",
        "properties": {"attribute": {"type": "string"}, "value": {"type": "string"}},
        "required": ["attribute", "value"],
    }


class SSOConfig(Config):
    """SSO Configuration"""

    section = "sso"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        sso_config: Dict[str, Any] = config.get("sso") or {}

        # The sso-specific template_dir
        self.sso_template_dir = sso_config.get("template_dir")
        if self.sso_template_dir is not None:
            logger.warning(LEGACY_TEMPLATE_DIR_WARNING)

        # Read templates from disk
        custom_template_directories = (
            self.root.server.custom_template_directory,
            self.sso_template_dir,
        )

        (
            self.sso_login_idp_picker_template,
            self.sso_redirect_confirm_template,
            self.sso_auth_confirm_template,
            self.sso_error_template,
            sso_account_deactivated_template,
            sso_auth_success_template,
            self.sso_auth_bad_user_template,
        ) = self.read_templates(
            [
                "sso_login_idp_picker.html",
                "sso_redirect_confirm.html",
                "sso_auth_confirm.html",
                "sso_error.html",
                "sso_account_deactivated.html",
                "sso_auth_success.html",
                "sso_auth_bad_user.html",
            ],
            (td for td in custom_template_directories if td),
        )

        # These templates have no placeholders, so render them here
        self.sso_account_deactivated_template = (
            sso_account_deactivated_template.render()
        )
        self.sso_auth_success_template = sso_auth_success_template.render()

        self.sso_client_whitelist = sso_config.get("client_whitelist") or []

        self.sso_update_profile_information = (
            sso_config.get("update_profile_information") or False
        )

        # Attempt to also whitelist the server's login fallback, since that fallback sets
        # the redirect URL to itself (so it can process the login token then return
        # gracefully to the client). This would make it pointless to ask the user for
        # confirmation, since the URL the confirmation page would be showing wouldn't be
        # the client's.
        login_fallback_url = (
            self.root.server.public_baseurl + "_matrix/static/client/login"
        )
        self.sso_client_whitelist.append(login_fallback_url)
