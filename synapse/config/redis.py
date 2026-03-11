#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from synapse.config._base import Config, ConfigError, read_file
from synapse.types import JsonDict
from synapse.util.check_dependencies import check_requirements

CONFLICTING_PASSWORD_OPTS_ERROR = """\
You have configured both `redis.password` and `redis.password_path`.
These are mutually incompatible.
"""

VALID_REDIS_TYPES = {"standalone", "sentinel", "cluster"}


class RedisConfig(Config):
    section = "redis"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        redis_config = config.get("redis") or {}
        self.redis_enabled = redis_config.get("enabled", False)

        if not self.redis_enabled:
            return

        check_requirements("redis")

        self.redis_host = redis_config.get("host", "localhost")
        self.redis_port = redis_config.get("port", 6379)
        self.redis_path = redis_config.get("path", None)
        self.redis_dbid = redis_config.get("dbid", None)
        self.redis_password = redis_config.get("password")
        if self.redis_password and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("redis", "password"),
            )
        redis_password_path = redis_config.get("password_path")
        if redis_password_path:
            if self.redis_password:
                raise ConfigError(CONFLICTING_PASSWORD_OPTS_ERROR)
            self.redis_password = read_file(
                redis_password_path,
                (
                    "redis",
                    "password_path",
                ),
            ).strip()

        self.redis_type = redis_config.get("type", "standalone")
        if self.redis_type not in VALID_REDIS_TYPES:
            raise ConfigError(
                "Invalid value for redis.type: expected one of "
                "'standalone', 'sentinel', or 'cluster'",
                ("redis", "type"),
            )

        self.redis_sentinel_master = redis_config.get("sentinel_master")
        self.redis_sentinel_hosts = redis_config.get("sentinel_hosts", [])
        self.redis_sentinel_password = redis_config.get("sentinel_password")
        if self.redis_sentinel_password and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("redis", "sentinel_password"),
            )

        # Validate sentinel configuration
        if self.redis_type == "sentinel":
            if not self.redis_sentinel_master:
                raise ConfigError(
                    "redis.sentinel_master is required when redis.type is 'sentinel'",
                    ("redis", "sentinel_master"),
                )
            if not self.redis_sentinel_hosts:
                raise ConfigError(
                    "redis.sentinel_hosts is required when redis.type is 'sentinel'",
                    ("redis", "sentinel_hosts"),
                )
            # Validate sentinel_hosts format - should be list of tuples or strings
            for idx, host_entry in enumerate(self.redis_sentinel_hosts):
                if not isinstance(host_entry, (list, tuple)) or len(host_entry) != 2:
                    raise ConfigError(
                        f"Each entry in redis.sentinel_hosts must be a list/tuple of [host, port], got: {host_entry}",
                        ("redis", "sentinel_hosts", idx),
                    )

        self.redis_use_tls = redis_config.get("use_tls", False)
        self.redis_certificate = redis_config.get("certificate_file", None)
        self.redis_private_key = redis_config.get("private_key_file", None)
        self.redis_ca_file = redis_config.get("ca_file", None)
        self.redis_ca_path = redis_config.get("ca_path", None)
