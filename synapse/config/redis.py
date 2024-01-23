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

from synapse.config._base import Config
from synapse.types import JsonDict
from synapse.util.check_dependencies import check_requirements


class RedisConfig(Config):
    section = "redis"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
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

        self.redis_use_tls = redis_config.get("use_tls", False)
        self.redis_certificate = redis_config.get("certificate_file", None)
        self.redis_private_key = redis_config.get("private_key_file", None)
        self.redis_ca_file = redis_config.get("ca_file", None)
        self.redis_ca_path = redis_config.get("ca_path", None)
