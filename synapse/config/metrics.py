#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

import attr

from synapse.types import JsonDict
from synapse.util.check_dependencies import check_requirements

from ._base import Config, ConfigError


@attr.s
class MetricsFlags:
    known_servers: bool = attr.ib(
        default=False, validator=attr.validators.instance_of(bool)
    )

    @classmethod
    def all_off(cls) -> "MetricsFlags":
        """
        Instantiate the flags with all options set to off.
        """
        return cls(**{x.name: False for x in attr.fields(cls)})


class MetricsConfig(Config):
    section = "metrics"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.enable_metrics = config.get("enable_metrics", False)

        self.report_stats = config.get("report_stats", None)
        self.report_stats_endpoint = config.get(
            "report_stats_endpoint", "https://matrix.org/report-usage-stats/push"
        )
        self.metrics_port = config.get("metrics_port")
        self.metrics_bind_host = config.get("metrics_bind_host", "127.0.0.1")

        if self.enable_metrics:
            _metrics_config = config.get("metrics_flags") or {}
            self.metrics_flags = MetricsFlags(**_metrics_config)
        else:
            self.metrics_flags = MetricsFlags.all_off()

        self.sentry_enabled = "sentry" in config
        if self.sentry_enabled:
            check_requirements("sentry")

            self.sentry_dsn = config["sentry"].get("dsn")
            self.sentry_environment = config["sentry"].get("environment")
            if not self.sentry_dsn:
                raise ConfigError(
                    "sentry.dsn field is required when sentry integration is enabled"
                )

    def generate_config_section(
        self, report_stats: bool | None = None, **kwargs: Any
    ) -> str:
        if report_stats is not None:
            res = "report_stats: %s\n" % ("true" if report_stats else "false")
        else:
            res = "\n"
        return res
