#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.d
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
from synapse.util.check_dependencies import check_requirements

from ._base import Config, ConfigError


class TracerConfig(Config):
    section = "tracing"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        opentracing_config = config.get("opentracing")
        if opentracing_config is None:
            opentracing_config = {}

        self.opentracer_enabled = opentracing_config.get("enabled", False)

        self.jaeger_config = opentracing_config.get(
            "jaeger_config",
            {"sampler": {"type": "const", "param": 1}, "logging": False},
        )

        self.force_tracing_for_users: set[str] = set()

        if not self.opentracer_enabled:
            return

        check_requirements("opentracing")

        # The tracer is enabled so sanitize the config

        self.opentracer_whitelist: list[str] = opentracing_config.get(
            "homeserver_whitelist", []
        )
        if not isinstance(self.opentracer_whitelist, list):
            raise ConfigError("Tracer homeserver_whitelist config is malformed")

        force_tracing_for_users = opentracing_config.get("force_tracing_for_users", [])
        if not isinstance(force_tracing_for_users, list):
            raise ConfigError(
                "Expected a list", ("opentracing", "force_tracing_for_users")
            )
        for i, u in enumerate(force_tracing_for_users):
            if not isinstance(u, str):
                raise ConfigError(
                    "Expected a string",
                    ("opentracing", "force_tracing_for_users", f"index {i}"),
                )
            self.force_tracing_for_users.add(u)
