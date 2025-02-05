#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

from ._base import Config, ConfigError, read_file

CONFLICTING_SHARED_SECRET_OPTS_ERROR = """\
You have configured both `turn_shared_secret` and `turn_shared_secret_path`.
These are mutually incompatible.
"""


class VoipConfig(Config):
    section = "voip"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        self.turn_uris = config.get("turn_uris", [])
        self.turn_shared_secret = config.get("turn_shared_secret")
        if self.turn_shared_secret and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("turn_shared_secret",),
            )
        turn_shared_secret_path = config.get("turn_shared_secret_path")
        if turn_shared_secret_path:
            if self.turn_shared_secret:
                raise ConfigError(CONFLICTING_SHARED_SECRET_OPTS_ERROR)
            self.turn_shared_secret = read_file(
                turn_shared_secret_path, ("turn_shared_secret_path",)
            ).strip()
        self.turn_username = config.get("turn_username")
        self.turn_password = config.get("turn_password")
        self.turn_user_lifetime = self.parse_duration(
            config.get("turn_user_lifetime", "1h")
        )
        self.turn_allow_guests = config.get("turn_allow_guests", True)
