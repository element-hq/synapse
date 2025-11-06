#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from os import path
from typing import Any

from synapse.config import ConfigError
from synapse.types import JsonDict

from ._base import Config


class ConsentConfig(Config):
    section = "consent"

    def __init__(self, *args: Any):
        super().__init__(*args)

        self.user_consent_version: str | None = None
        self.user_consent_template_dir: str | None = None
        self.user_consent_server_notice_content: JsonDict | None = None
        self.user_consent_server_notice_to_guests = False
        self.block_events_without_consent_error: str | None = None
        self.user_consent_at_registration = False
        self.user_consent_policy_name = "Privacy Policy"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        consent_config = config.get("user_consent")
        self.terms_template = self.read_template("terms.html")

        if consent_config is None:
            return
        self.user_consent_version = str(consent_config["version"])
        self.user_consent_template_dir = self.abspath(consent_config["template_dir"])
        if not isinstance(self.user_consent_template_dir, str) or not path.isdir(
            self.user_consent_template_dir
        ):
            raise ConfigError(
                "Could not find template directory '%s'"
                % (self.user_consent_template_dir,)
            )
        self.user_consent_server_notice_content = consent_config.get(
            "server_notice_content"
        )
        self.block_events_without_consent_error = consent_config.get(
            "block_events_error"
        )
        self.user_consent_server_notice_to_guests = bool(
            consent_config.get("send_server_notice_to_guests", False)
        )
        self.user_consent_at_registration = bool(
            consent_config.get("require_at_registration", False)
        )
        self.user_consent_policy_name = consent_config.get(
            "policy_name", "Privacy Policy"
        )
