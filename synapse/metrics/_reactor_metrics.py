#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

import asyncio
import logging
import time
from selectors import SelectSelector, _PollLikeSelector  # type: ignore[attr-defined]
from typing import Any, Callable, Iterable

from prometheus_client import Histogram, Metric
from prometheus_client.core import REGISTRY, GaugeMetricFamily

from synapse.metrics._types import Collector

try:
    from selectors import KqueueSelector  # type: ignore[attr-defined]
except ImportError:

    class KqueueSelector:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)

#
# Event loop metrics
#

# This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
tick_time = Histogram(  # type: ignore[missing-server-name-label]
    "python_twisted_reactor_tick_time",
    "Tick time of the asyncio event loop (sec)",
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5],
)


class CallWrapper:
    """A wrapper for a callable which records the time between calls"""

    def __init__(self, wrapped: Callable[..., Any]):
        self.last_polled = time.time()
        self._wrapped = wrapped

    def __call__(self, *args, **kwargs) -> Any:  # type: ignore[no-untyped-def]
        # record the time since this was last called. This gives a good proxy for
        # how long it takes to run everything in the event loop - ie, how long anything
        # waiting for the next tick will have to wait.
        tick_time.observe(time.time() - self.last_polled)

        ret = self._wrapped(*args, **kwargs)

        self.last_polled = time.time()
        return ret


class ObjWrapper:
    """A wrapper for an object which wraps a specified method in CallWrapper.

    Other methods/attributes are passed to the original object.

    This is necessary when the wrapped object does not allow the attribute to be
    overwritten.
    """

    def __init__(self, wrapped: Any, method_name: str):
        self._wrapped = wrapped
        self._method_name = method_name
        self._wrapped_method = CallWrapper(getattr(wrapped, method_name))

    def __getattr__(self, item: str) -> Any:
        if item == self._method_name:
            return self._wrapped_method

        return getattr(self._wrapped, item)


class ReactorLastSeenMetric(Collector):
    def __init__(self, call_wrapper: CallWrapper):
        self._call_wrapper = call_wrapper

    def collect(self) -> Iterable[Metric]:
        # This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
        cm = GaugeMetricFamily(  # type: ignore[missing-server-name-label]
            "python_twisted_reactor_last_seen",
            "Seconds since the asyncio event loop was last seen",
        )
        cm.add_metric([], time.time() - self._call_wrapper.last_polled)
        yield cm


def install_reactor_metrics(target: Any = None) -> None:
    """Install metrics for the asyncio event loop.

    Wraps the underlying selector's poll/select method to measure tick times.

    Args:
        target: Either an asyncio event loop, or a Twisted reactor (for backward
            compatibility). If None, attempts to get the running asyncio event loop.
    """
    wrapper = None
    try:
        # Determine the asyncio event loop to instrument
        asyncio_loop = None
        if target is None:
            try:
                asyncio_loop = asyncio.get_event_loop()
            except RuntimeError:
                logger.warning(
                    "Skipping configuring reactor metrics: no running asyncio event loop"
                )
                return
        elif hasattr(target, '_selector'):
            # It's an asyncio event loop directly
            asyncio_loop = target
        elif hasattr(target, '_asyncioEventloop'):
            # It's a Twisted AsyncioSelectorReactor
            asyncio_loop = target._asyncioEventloop
        else:
            logger.warning(
                "Skipping configuring reactor metrics: unexpected target type: %r",
                target,
            )
            return

        if asyncio_loop is None:
            logger.warning(
                "Skipping configuring reactor metrics: could not find asyncio event loop"
            )
            return

        # A sub-class of BaseSelector.
        selector = asyncio_loop._selector  # type: ignore[attr-defined]

        if isinstance(selector, SelectSelector):
            wrapper = selector._select = CallWrapper(selector._select)  # type: ignore[attr-defined]

        # poll, epoll, and /dev/poll.
        elif isinstance(selector, _PollLikeSelector):
            selector._selector = ObjWrapper(selector._selector, "poll")  # type: ignore[attr-defined]
            wrapper = selector._selector._wrapped_method  # type: ignore[attr-defined]

        elif isinstance(selector, KqueueSelector):
            selector._selector = ObjWrapper(selector._selector, "control")  # type: ignore[attr-defined]
            wrapper = selector._selector._wrapped_method  # type: ignore[attr-defined]

        else:
            # E.g. this does not support the (Windows-only) ProactorEventLoop.
            logger.warning(
                "Skipping configuring reactor metrics: unexpected asyncio loop selector: %r via %r",
                selector,
                asyncio_loop,
            )
    except Exception as e:
        logger.warning("Configuring reactor metrics failed: %r", e)

    if wrapper:
        # This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
        REGISTRY.register(ReactorLastSeenMetric(wrapper))  # type: ignore[missing-server-name-label]


# Install reactor metrics for the global asyncio event loop (if available).
# This may be called before the event loop is running, in which case
# it will be a no-op and should be called again later.
try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        install_reactor_metrics(loop)
except RuntimeError:
    pass  # No event loop yet; metrics will be installed later
