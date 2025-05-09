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

CONFLICTING_RECAPTCHA_PRIVATE_KEY_OPTS_ERROR = """\
You have configured both `recaptcha_private_key` and
`recaptcha_private_key_path`. These are mutually incompatible.
"""

CONFLICTING_RECAPTCHA_PUBLIC_KEY_OPTS_ERROR = """\
You have configured both `recaptcha_public_key` and `recaptcha_public_key_path`.
These are mutually incompatible.
"""


class CaptchaConfig(Config):
    section = "captcha"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        recaptcha_private_key = config.get("recaptcha_private_key")
        if recaptcha_private_key and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("recaptcha_private_key",),
            )
        recaptcha_private_key_path = config.get("recaptcha_private_key_path")
        if recaptcha_private_key_path:
            if recaptcha_private_key:
                raise ConfigError(CONFLICTING_RECAPTCHA_PRIVATE_KEY_OPTS_ERROR)
            recaptcha_private_key = read_file(
                recaptcha_private_key_path, ("recaptcha_private_key_path",)
            ).strip()
        if recaptcha_private_key is not None and not isinstance(
            recaptcha_private_key, str
        ):
            raise ConfigError("recaptcha_private_key must be a string.")
        self.recaptcha_private_key = recaptcha_private_key

        recaptcha_public_key = config.get("recaptcha_public_key")
        if recaptcha_public_key and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("recaptcha_public_key",),
            )
        recaptcha_public_key_path = config.get("recaptcha_public_key_path")
        if recaptcha_public_key_path:
            if recaptcha_public_key:
                raise ConfigError(CONFLICTING_RECAPTCHA_PUBLIC_KEY_OPTS_ERROR)
            recaptcha_public_key = read_file(
                recaptcha_public_key_path, ("recaptcha_public_key_path",)
            ).strip()
        if recaptcha_public_key is not None and not isinstance(
            recaptcha_public_key, str
        ):
            raise ConfigError("recaptcha_public_key must be a string.")
        self.recaptcha_public_key = recaptcha_public_key

        self.enable_registration_captcha = config.get(
            "enable_registration_captcha", False
        )
        self.recaptcha_siteverify_api = config.get(
            "recaptcha_siteverify_api",
            "https://www.recaptcha.net/recaptcha/api/siteverify",
        )
        self.recaptcha_template = self.read_template("recaptcha.html")
