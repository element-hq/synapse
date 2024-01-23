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

import logging
import time
from selectors import SelectSelector, _PollLikeSelector  # type: ignore[attr-defined]
from typing import Any, Callable, Iterable

from prometheus_client import Histogram, Metric
from prometheus_client.core import REGISTRY, GaugeMetricFamily

from twisted.internet import reactor, selectreactor
from twisted.internet.asyncioreactor import AsyncioSelectorReactor

from synapse.metrics._types import Collector

try:
    from selectors import KqueueSelector
except ImportError:

    class KqueueSelector:  # type: ignore[no-redef]
        pass


try:
    from twisted.internet.epollreactor import EPollReactor
except ImportError:

    class EPollReactor:  # type: ignore[no-redef]
        pass


try:
    from twisted.internet.pollreactor import PollReactor
except ImportError:

    class PollReactor:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)

#
# Twisted reactor metrics
#

tick_time = Histogram(
    "python_twisted_reactor_tick_time",
    "Tick time of the Twisted reactor (sec)",
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5],
)


class CallWrapper:
    """A wrapper for a callable which records the time between calls"""

    def __init__(self, wrapped: Callable[..., Any]):
        self.last_polled = time.time()
        self._wrapped = wrapped

    def __call__(self, *args, **kwargs) -> Any:  # type: ignore[no-untyped-def]
        # record the time since this was last called. This gives a good proxy for
        # how long it takes to run everything in the reactor - ie, how long anything
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
        cm = GaugeMetricFamily(
            "python_twisted_reactor_last_seen",
            "Seconds since the Twisted reactor was last seen",
        )
        cm.add_metric([], time.time() - self._call_wrapper.last_polled)
        yield cm


# Twisted has already select a reasonable reactor for us, so assumptions can be
# made about the shape.
wrapper = None
try:
    if isinstance(reactor, (PollReactor, EPollReactor)):
        reactor._poller = ObjWrapper(reactor._poller, "poll")  # type: ignore[attr-defined]
        wrapper = reactor._poller._wrapped_method  # type: ignore[attr-defined]

    elif isinstance(reactor, selectreactor.SelectReactor):
        # Twisted uses a module-level _select function.
        wrapper = selectreactor._select = CallWrapper(selectreactor._select)

    elif isinstance(reactor, AsyncioSelectorReactor):
        # For asyncio look at the underlying asyncio event loop.
        asyncio_loop = reactor._asyncioEventloop  # A sub-class of BaseEventLoop,

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
                "Skipping configuring ReactorLastSeenMetric: unexpected asyncio loop selector: %r via %r",
                selector,
                asyncio_loop,
            )
except Exception as e:
    logger.warning("Configuring ReactorLastSeenMetric failed: %r", e)


if wrapper:
    REGISTRY.register(ReactorLastSeenMetric(wrapper))
