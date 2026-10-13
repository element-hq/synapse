#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
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

import tempfile

from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig

from tests.unittest import TestCase
from tests.utils import default_config

try:
    import hiredis
except ImportError:
    hiredis = None  # type: ignore


class RedisConfigTestCase(TestCase):
    # hiredis is part of the `redis` extra, which `RedisConfig` requires to
    # parse an enabled redis config.
    if not hiredis:
        skip = "Requires hiredis"

    def _make_config(self, redis_config: dict) -> HomeServerConfig:
        config_dict = default_config(server_name="test")
        config_dict["redis"] = redis_config
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        return config

    def test_username_defaults_to_none(self) -> None:
        """`redis.username` is `None` when not configured."""
        config = self._make_config({"enabled": True, "password": "hunter2"})
        self.assertIsNone(config.redis.redis_username)

    def test_username_is_parsed(self) -> None:
        """`redis.username` is parsed through to `redis_username` when set
        alongside `password`."""
        config = self._make_config(
            {"enabled": True, "username": "alice", "password": "hunter2"}
        )
        self.assertEqual(config.redis.redis_username, "alice")

    def test_username_is_parsed_with_password_path(self) -> None:
        """`redis.username` is also accepted alongside `password_path`, the
        documented alternative to an inline `password`."""
        with tempfile.NamedTemporaryFile(buffering=0) as password_file:
            password_file.write(b"hunter2")

            config = self._make_config(
                {
                    "enabled": True,
                    "username": "alice",
                    "password_path": password_file.name,
                }
            )
            self.assertEqual(config.redis.redis_username, "alice")
            self.assertEqual(config.redis.redis_password, "hunter2")

    def test_username_with_empty_password_is_accepted(self) -> None:
        """`redis.username` with an explicitly empty `password` is allowed: that
        is how a Redis ACL user declared `nopass` is configured."""
        config = self._make_config(
            {"enabled": True, "username": "alice", "password": ""}
        )
        self.assertEqual(config.redis.redis_username, "alice")
        self.assertEqual(config.redis.redis_password, "")

    def test_username_without_password_is_rejected(self) -> None:
        """`redis.username` without any of `password`/`password_path` is
        rejected: Redis ACL authentication requires both."""
        with self.assertRaises(ConfigError):
            self._make_config({"enabled": True, "username": "alice"})
