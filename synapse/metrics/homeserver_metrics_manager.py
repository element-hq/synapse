#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import Protocol

from prometheus_client import REGISTRY, CollectorRegistry, Counter

from synapse.metrics import InFlightGauge


# This is dynamically created in InFlightGauge.__init__.
class BlockInFlightMetric(Protocol):
    """
    Sub-metrics used for the `InFlightGauge` for blocks.
    """

    real_time_max: float
    """The longest observed duration of any single execution of this block, in seconds."""
    real_time_sum: float
    """The cumulative time spent executing this block across all calls, in seconds."""


class BlockMetrics:
    def __init__(
        self,
        metrics_collector_registry: CollectorRegistry,
    ) -> None:
        self.block_counter = Counter(
            "synapse_util_metrics_block_count",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )

        self.block_timer = Counter(
            "synapse_util_metrics_block_time_seconds",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )

        self.block_ru_utime = Counter(
            "synapse_util_metrics_block_ru_utime_seconds",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )

        self.block_ru_stime = Counter(
            "synapse_util_metrics_block_ru_stime_seconds",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )

        self.block_db_txn_count = Counter(
            "synapse_util_metrics_block_db_txn_count",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )

        self.block_db_txn_duration = Counter(
            "synapse_util_metrics_block_db_txn_duration_seconds",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )
        """seconds spent waiting for db txns, excluding scheduling time, in this block"""

        self.block_db_sched_duration = Counter(
            "synapse_util_metrics_block_db_sched_duration_seconds",
            "",
            ["block_name"],
            registry=metrics_collector_registry,
        )
        """seconds spent waiting for a db connection, in this block"""

        self.in_flight: InFlightGauge[BlockInFlightMetric] = InFlightGauge(
            "synapse_util_metrics_block_in_flight",
            "",
            labels=["block_name"],
            # Matches the fields in the `BlockInFlightMetric`
            sub_metrics=["real_time_max", "real_time_sum"],
        )
        """Tracks the number of blocks currently active"""


class HomeserverMetricsManager:
    """
    Homeserver-scoped metrics manager.

    This class serves as a container for the homeserver's global metrics objects.
    """

    def __init__(self) -> None:
        # TODO: use `self.metrics_collector_registry = CollectorRegistry(auto_describe=True)`
        # once we refactor our metrics endpoints to use the specified registry.
        self.metrics_collector_registry = REGISTRY

        self.block_metrics = BlockMetrics(
            metrics_collector_registry=self.metrics_collector_registry,
        )
