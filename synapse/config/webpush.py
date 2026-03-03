#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Mathieu Velten
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
import os
from typing import Any

from py_vapid import b64urlencode, serialization

from synapse.config.experimental import HAS_PYWEBPUSH
from synapse.types import JsonDict

from ._base import Config, ConfigError, read_file


class WebpushConfig(Config):
    section = "webpush"

    def read_config(
        self,
        config: JsonDict,
        config_dir_path: str,
        allow_secrets_in_config: bool,
        **kwargs: Any,
    ) -> None:
        webpush_config = config.get("webpush", {})
        self.enabled = webpush_config.get("enabled", False)

        if self.enabled and not self.root.experimental.msc4174_enabled:
            raise ConfigError("webpush is enabled but MSC4174 is not enabled")

        if not HAS_PYWEBPUSH:
            raise ConfigError("webpush is enabled but pywebpush is not installed")

        from py_vapid import Vapid

        self.vapid_contact_email = webpush_config.get("vapid_contact_email")
        if not self.vapid_contact_email:
            raise ConfigError(
                "vapid_contact_email must be configured when WebPush is enabled"
            )
        self.ttl_seconds = webpush_config.get("ttl_seconds", 12 * 60 * 60)

        vapid_private_key = webpush_config.get("vapid_private_key")
        if vapid_private_key and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("vapid_private_key",),
            )
        vapid_private_key_path = webpush_config.get("vapid_private_key_path")
        if vapid_private_key_path and vapid_private_key:
            raise ConfigError(
                "You have configured both `vapid_private_key` and `vapid_private_key_path`. These are mutually incompatible."
            )
        if not vapid_private_key_path:
            assert config_dir_path is not None
            vapid_private_key_path = os.path.join(
                config_dir_path, config["server_name"] + ".vapid.key"
            )
        vapid_private_key = read_file(
            vapid_private_key_path, (vapid_private_key_path,)
        ).strip()

        if not vapid_private_key:
            raise ConfigError(
                "vapid_private_key or vapid_private_key_path must be configured when WebPush is enabled"
            )

        self.vapid = Vapid.from_string(private_key=vapid_private_key)

        vapid_public_key = self.vapid.public_key
        assert vapid_public_key is not None
        self.vapid_app_server_key = b64urlencode(
            vapid_public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

    def generate_files(self, config: dict[str, Any], config_dir_path: str) -> None:
        webpush_config = config.get("webpush", {})
        enabled = webpush_config.get("enabled", False)

        if not enabled:
            return

        if not HAS_PYWEBPUSH:
            raise ConfigError("webpush is enabled but pywebpush is not installed")

        from py_vapid import Vapid

        if "vapid_private_key" in webpush_config:
            return

        vapid_private_key_path = webpush_config.get("vapid_private_key_path")
        if vapid_private_key_path is None:
            vapid_private_key_path = os.path.join(
                config_dir_path, config["server_name"] + ".vapid.key"
            )

        if not self.path_exists(vapid_private_key_path):
            vapid = Vapid()
            vapid.generate_keys()
            with open(
                vapid_private_key_path,
                "w",
                opener=lambda p, f: os.open(p, f, mode=0o640),
            ) as vapid_private_key_file:
                vapid.save_key(vapid_private_key_file)
