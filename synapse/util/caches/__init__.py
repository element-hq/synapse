#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019, 2020 The Matrix.org Foundation C.I.C.
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
import collections
import logging
import typing
from enum import Enum, auto
from sys import intern
from typing import Any, Callable, Sized, TypeVar

import attr
from prometheus_client import REGISTRY
from prometheus_client.core import Gauge

from synapse.config.cache import add_resizable_cache
from synapse.metrics import SERVER_NAME_LABEL
from synapse.util.metrics import DynamicCollectorRegistry

logger = logging.getLogger(__name__)


# Whether to track estimated memory usage of the LruCaches.
TRACK_MEMORY_USAGE = False

# We track cache metrics in a special registry that lets us update the metrics
# just before they are returned from the scrape endpoint.
#
# The `SERVER_NAME_LABEL` is included in the individual metrics added to this registry,
# so we don't need to worry about it on the collector itself.
CACHE_METRIC_REGISTRY = DynamicCollectorRegistry()  # type: ignore[missing-server-name-label]

cache_size = Gauge(
    "synapse_util_caches_cache_size",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
cache_hits = Gauge(
    "synapse_util_caches_cache_hits",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
cache_evicted = Gauge(
    "synapse_util_caches_cache_evicted_size",
    "",
    labelnames=["name", "reason", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
cache_total = Gauge(
    "synapse_util_caches_cache",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
cache_max_size = Gauge(
    "synapse_util_caches_cache_max_size",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
cache_memory_usage = Gauge(
    "synapse_util_caches_cache_size_bytes",
    "Estimated memory usage of the caches",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)

response_cache_size = Gauge(
    "synapse_util_caches_response_cache_size",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
response_cache_hits = Gauge(
    "synapse_util_caches_response_cache_hits",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
response_cache_evicted = Gauge(
    "synapse_util_caches_response_cache_evicted_size",
    "",
    labelnames=["name", "reason", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)
response_cache_total = Gauge(
    "synapse_util_caches_response_cache",
    "",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=CACHE_METRIC_REGISTRY,
)


# Register our custom cache metrics registry with the global registry
REGISTRY.register(CACHE_METRIC_REGISTRY)


class EvictionReason(Enum):
    size = auto()
    time = auto()
    invalidation = auto()


@attr.s(slots=True, auto_attribs=True, kw_only=True)
class CacheMetric:
    """
    Used to track cache metrics
    """

    _cache: Sized
    _cache_type: str
    _cache_name: str
    _collect_callback: Callable | None
    _server_name: str

    hits: int = 0
    misses: int = 0
    eviction_size_by_reason: typing.Counter[EvictionReason] = attr.ib(
        factory=collections.Counter
    )
    memory_usage: int | None = None

    def inc_hits(self) -> None:
        self.hits += 1

    def inc_misses(self) -> None:
        self.misses += 1

    def inc_evictions(self, reason: EvictionReason, size: int = 1) -> None:
        self.eviction_size_by_reason[reason] += size

    def inc_memory_usage(self, memory: int) -> None:
        if self.memory_usage is None:
            self.memory_usage = 0

        self.memory_usage += memory

    def dec_memory_usage(self, memory: int) -> None:
        assert self.memory_usage is not None
        self.memory_usage -= memory

    def clear_memory_usage(self) -> None:
        if self.memory_usage is not None:
            self.memory_usage = 0

    def describe(self) -> list[str]:
        return []

    def collect(self) -> None:
        try:
            labels_base = {
                "name": self._cache_name,
                SERVER_NAME_LABEL: self._server_name,
            }
            if self._cache_type == "response_cache":
                response_cache_size.labels(**labels_base).set(len(self._cache))
                response_cache_hits.labels(**labels_base).set(self.hits)
                for reason in EvictionReason:
                    response_cache_evicted.labels(
                        **{**labels_base, "reason": reason.name}
                    ).set(self.eviction_size_by_reason[reason])
                response_cache_total.labels(**labels_base).set(self.hits + self.misses)
            else:
                cache_size.labels(**labels_base).set(len(self._cache))
                cache_hits.labels(**labels_base).set(self.hits)
                for reason in EvictionReason:
                    cache_evicted.labels(**{**labels_base, "reason": reason.name}).set(
                        self.eviction_size_by_reason[reason]
                    )
                cache_total.labels(**labels_base).set(self.hits + self.misses)
                max_size = getattr(self._cache, "max_size", None)
                if max_size:
                    cache_max_size.labels(**labels_base).set(max_size)

                if TRACK_MEMORY_USAGE:
                    # self.memory_usage can be None if nothing has been inserted
                    # into the cache yet.
                    cache_memory_usage.labels(**labels_base).set(self.memory_usage or 0)
            if self._collect_callback:
                self._collect_callback()
        except Exception as e:
            logger.warning("Error calculating metrics for %s: %s", self._cache_name, e)
            raise


def register_cache(
    *,
    cache_type: str,
    cache_name: str,
    cache: Sized,
    server_name: str,
    collect_callback: Callable | None = None,
    resizable: bool = True,
    resize_callback: Callable | None = None,
) -> CacheMetric:
    """Register a cache object for metric collection and resizing.

    Args:
        cache_type: a string indicating the "type" of the cache. This is used
            only for deduplication so isn't too important provided it's constant.
        cache_name: name of the cache
        cache: cache itself, which must implement __len__(), and may optionally implement
             a max_size property
        server_name: The homeserver name that this cache is associated with
            (used to label the metric) (`hs.hostname`).
        collect_callback: If given, a function which is called during metric
            collection to update additional metrics.
        resizable: Whether this cache supports being resized, in which case either
            resize_callback must be provided, or the cache must support set_max_size().
        resize_callback: A function which can be called to resize the cache.

    Returns:
        an object which provides inc_{hits,misses,evictions} methods
    """
    if resizable:
        if not resize_callback:
            resize_callback = cache.set_cache_factor  # type: ignore
        add_resizable_cache(cache_name, resize_callback)

    metric = CacheMetric(
        cache=cache,
        cache_type=cache_type,
        cache_name=cache_name,
        server_name=server_name,
        collect_callback=collect_callback,
    )
    metric_name = "cache_%s_%s_%s" % (cache_type, cache_name, server_name)
    CACHE_METRIC_REGISTRY.register_hook(server_name, metric_name, metric.collect)
    return metric


KNOWN_KEYS = {
    key: key
    for key in (
        "auth_events",
        "content",
        "depth",
        "event_id",
        "hashes",
        "origin",  # old events were created with an origin field.
        "origin_server_ts",
        "prev_events",
        "room_id",
        "sender",
        "signatures",
        "state_key",
        "type",
        "unsigned",
        "user_id",
    )
}

T = TypeVar("T", str | None, str)


def intern_string(string: T) -> T:
    """Takes a (potentially) unicode string and interns it if it's ascii"""
    if string is None:
        return None

    try:
        return intern(string)
    except UnicodeEncodeError:
        return string


def intern_dict(dictionary: dict[str, Any]) -> dict[str, Any]:
    """Takes a dictionary and interns well known keys and their values"""
    return {
        KNOWN_KEYS.get(key, key): _intern_known_values(key, value)
        for key, value in dictionary.items()
    }


def _intern_known_values(key: str, value: Any) -> Any:
    intern_keys = ("event_id", "room_id", "sender", "user_id", "type", "state_key")

    if key in intern_keys:
        return intern_string(value)

    return value
