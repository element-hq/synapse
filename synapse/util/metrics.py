#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

import logging
from functools import wraps
from types import TracebackType
from typing import (
    Awaitable,
    Callable,
    Dict,
    Generator,
    Optional,
    Protocol,
    Type,
    TypeVar,
)

from prometheus_client import CollectorRegistry, Counter, Metric
from typing_extensions import Concatenate, ParamSpec

from synapse.logging.context import (
    ContextResourceUsage,
    LoggingContext,
    current_context,
)
from synapse.metrics import SERVER_NAME_LABEL, InFlightGauge
from synapse.util import Clock

logger = logging.getLogger(__name__)

# Metrics to see the number of and how much time is spend in various blocks of code.
#
block_counter = Counter(
    "synapse_util_metrics_block_count",
    documentation="The number of times this block has been called.",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""The number of times this block has been called."""

block_timer = Counter(
    "synapse_util_metrics_block_time_seconds",
    documentation="The cumulative time spent executing this block across all calls, in seconds.",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""The cumulative time spent executing this block across all calls, in seconds."""

block_ru_utime = Counter(
    "synapse_util_metrics_block_ru_utime_seconds",
    documentation="Resource usage: user CPU time in seconds used in this block",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""Resource usage: user CPU time in seconds used in this block"""

block_ru_stime = Counter(
    "synapse_util_metrics_block_ru_stime_seconds",
    documentation="Resource usage: system CPU time in seconds used in this block",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""Resource usage: system CPU time in seconds used in this block"""

block_db_txn_count = Counter(
    "synapse_util_metrics_block_db_txn_count",
    documentation="Number of database transactions completed in this block",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""Number of database transactions completed in this block"""

# seconds spent waiting for db txns, excluding scheduling time, in this block
block_db_txn_duration = Counter(
    "synapse_util_metrics_block_db_txn_duration_seconds",
    documentation="Seconds spent waiting for database txns, excluding scheduling time, in this block",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""Seconds spent waiting for database txns, excluding scheduling time, in this block"""

# seconds spent waiting for a db connection, in this block
block_db_sched_duration = Counter(
    "synapse_util_metrics_block_db_sched_duration_seconds",
    documentation="Seconds spent waiting for a db connection, in this block",
    labelnames=["block_name", SERVER_NAME_LABEL],
)
"""Seconds spent waiting for a db connection, in this block"""


# This is dynamically created in InFlightGauge.__init__.
class _BlockInFlightMetric(Protocol):
    """
    Sub-metrics used for the `InFlightGauge` for blocks.
    """

    real_time_max: float
    """The longest observed duration of any single execution of this block, in seconds."""
    real_time_sum: float
    """The cumulative time spent executing this block across all calls, in seconds."""


in_flight: InFlightGauge[_BlockInFlightMetric] = InFlightGauge(
    "synapse_util_metrics_block_in_flight",
    desc="Tracks the number of blocks currently active",
    labels=["block_name", SERVER_NAME_LABEL],
    # Matches the fields in the `_BlockInFlightMetric`
    sub_metrics=["real_time_max", "real_time_sum"],
)
"""Tracks the number of blocks currently active"""

P = ParamSpec("P")
R = TypeVar("R")


class HasClockAndServerName(Protocol):
    clock: Clock
    """
    Used to measure functions
    """
    server_name: str
    """
    The homeserver name that this Measure is associated with (used to label the metric)
    (`hs.hostname`).
    """


def measure_func(
    name: Optional[str] = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate an async method with a `Measure` context manager.

    The Measure is created using `self.clock` and `self.server_name; it should only be
    used to decorate methods in classes defining an instance-level `clock` and
    `server_name` attributes.

    Usage:

    @measure_func()
    async def foo(...):
        ...

    Which is analogous to:

    async def foo(...):
        with Measure(...):
            ...

    Args:
        name: The name of the metric to report (the block name) (used to label the
            metric). Defaults to the name of the decorated function.
    """

    def wrapper(
        func: Callable[Concatenate[HasClockAndServerName, P], Awaitable[R]],
    ) -> Callable[P, Awaitable[R]]:
        block_name = func.__name__ if name is None else name

        @wraps(func)
        async def measured_func(
            self: HasClockAndServerName, *args: P.args, **kwargs: P.kwargs
        ) -> R:
            with Measure(self.clock, name=block_name, server_name=self.server_name):
                r = await func(self, *args, **kwargs)
            return r

        # There are some shenanigans here, because we're decorating a method but
        # explicitly making use of the `self` parameter. The key thing here is that the
        # return type within the return type for `measure_func` itself describes how the
        # decorated function will be called.
        return measured_func  # type: ignore[return-value]

    return wrapper  # type: ignore[return-value]


class Measure:
    __slots__ = [
        "clock",
        "name",
        "server_name",
        "_logging_context",
        "start",
    ]

    def __init__(self, clock: Clock, *, name: str, server_name: str) -> None:
        """
        Args:
            clock: An object with a "time()" method, which returns the current
                time in seconds.
            name: The name of the metric to report (the block name) (used to label the
                metric).
            server_name: The homeserver name that this Measure is associated with (used to
                label the metric) (`hs.hostname`).
        """
        self.clock = clock
        self.name = name
        self.server_name = server_name
        curr_context = current_context()
        if not curr_context:
            logger.warning(
                "Starting metrics collection %r from sentinel context: metrics will be lost",
                name,
            )
            parent_context = None
        else:
            assert isinstance(curr_context, LoggingContext)
            parent_context = curr_context
        self._logging_context = LoggingContext(str(curr_context), parent_context)
        self.start: Optional[float] = None

    def __enter__(self) -> "Measure":
        if self.start is not None:
            raise RuntimeError("Measure() objects cannot be re-used")

        self.start = self.clock.time()
        self._logging_context.__enter__()
        in_flight.register((self.name, self.server_name), self._update_in_flight)

        logger.debug("Entering block %s", self.name)

        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self.start is None:
            raise RuntimeError("Measure() block exited without being entered")

        logger.debug("Exiting block %s", self.name)

        duration = self.clock.time() - self.start
        usage = self.get_resource_usage()

        in_flight.unregister((self.name, self.server_name), self._update_in_flight)
        self._logging_context.__exit__(exc_type, exc_val, exc_tb)

        try:
            labels = {"block_name": self.name, SERVER_NAME_LABEL: self.server_name}
            block_counter.labels(**labels).inc()
            block_timer.labels(**labels).inc(duration)
            block_ru_utime.labels(**labels).inc(usage.ru_utime)
            block_ru_stime.labels(**labels).inc(usage.ru_stime)
            block_db_txn_count.labels(**labels).inc(usage.db_txn_count)
            block_db_txn_duration.labels(**labels).inc(usage.db_txn_duration_sec)
            block_db_sched_duration.labels(**labels).inc(usage.db_sched_duration_sec)
        except ValueError as exc:
            logger.warning("Failed to save metrics! Usage: %s Error: %s", usage, exc)

    def get_resource_usage(self) -> ContextResourceUsage:
        """Get the resources used within this Measure block

        If the Measure block is still active, returns the resource usage so far.
        """
        return self._logging_context.get_resource_usage()

    def _update_in_flight(self, metrics: _BlockInFlightMetric) -> None:
        """Gets called when processing in flight metrics"""
        assert self.start is not None
        duration = self.clock.time() - self.start

        metrics.real_time_max = max(metrics.real_time_max, duration)
        metrics.real_time_sum += duration

        # TODO: Add other in flight metrics.


class DynamicCollectorRegistry(CollectorRegistry):
    """
    Custom Prometheus Collector registry that calls a hook first, allowing you
    to update metrics on-demand.

    Don't forget to register this registry with the main registry!
    """

    def __init__(self) -> None:
        super().__init__()
        self._pre_update_hooks: Dict[str, Callable[[], None]] = {}

    def collect(self) -> Generator[Metric, None, None]:
        """
        Collects metrics, calling pre-update hooks first.
        """

        for pre_update_hook in self._pre_update_hooks.values():
            pre_update_hook()

        yield from super().collect()

    def register_hook(self, metric_name: str, hook: Callable[[], None]) -> None:
        """
        Registers a hook that is called before metric collection.
        """

        self._pre_update_hooks[metric_name] = hook
