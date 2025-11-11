#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C
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
from typing import TYPE_CHECKING

import attr

from synapse.metrics import SERVER_NAME_LABEL

if TYPE_CHECKING:
    from synapse.server import HomeServer

from prometheus_client import Gauge

# Gauge to expose daily active users metrics
current_dau_gauge = Gauge(
    "synapse_admin_daily_active_users",
    "Current daily active users count",
    labelnames=[SERVER_NAME_LABEL],
)


@attr.s(auto_attribs=True)
class CommonUsageMetrics:
    """Usage metrics shared between the phone home stats and the prometheus exporter."""

    daily_active_users: int


class CommonUsageMetricsManager:
    """Collects common usage metrics."""

    def __init__(self, hs: "HomeServer") -> None:
        self.server_name = hs.hostname
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._hs = hs

    async def get_metrics(self) -> CommonUsageMetrics:
        """Get the CommonUsageMetrics object. If no collection has happened yet, do it
        before returning the metrics.

        Returns:
            The CommonUsageMetrics object to read common metrics from.
        """
        return await self._collect()

    def setup(self) -> None:
        """Keep the gauges for common usage metrics up to date."""
        self._hs.run_as_background_process(
            desc="common_usage_metrics_update_gauges",
            func=self._update_gauges,
        )
        self._clock.looping_call(
            self._hs.run_as_background_process,
            5 * 60 * 1000,
            desc="common_usage_metrics_update_gauges",
            func=self._update_gauges,
        )

    async def _collect(self) -> CommonUsageMetrics:
        """Collect the common metrics and either create the CommonUsageMetrics object to
        use if it doesn't exist yet, or update it.
        """
        dau_count = await self._store.count_daily_users()

        return CommonUsageMetrics(
            daily_active_users=dau_count,
        )

    async def _update_gauges(self) -> None:
        """Update the Prometheus gauges."""
        metrics = await self._collect()

        current_dau_gauge.labels(
            **{SERVER_NAME_LABEL: self.server_name},
        ).set(float(metrics.daily_active_users))
