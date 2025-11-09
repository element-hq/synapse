#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

import itertools
import logging
import os
import platform
import threading
from importlib import metadata
from typing import (
    Callable,
    Generic,
    Iterable,
    Mapping,
    Sequence,
    TypeVar,
    cast,
)

import attr
from packaging.version import parse as parse_version
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Metric,
    generate_latest,
)
from prometheus_client.core import (
    REGISTRY,
    GaugeHistogramMetricFamily,
    GaugeMetricFamily,
)

from twisted.python.threadpool import ThreadPool
from twisted.web.resource import Resource
from twisted.web.server import Request

# This module is imported for its side effects; flake8 needn't warn that it's unused.
import synapse.metrics._reactor_metrics  # noqa: F401
from synapse.metrics._gc import MIN_TIME_BETWEEN_GCS, install_gc_manager
from synapse.metrics._types import Collector
from synapse.types import StrSequence
from synapse.util import SYNAPSE_VERSION

logger = logging.getLogger(__name__)

METRICS_PREFIX = "/_synapse/metrics"

HAVE_PROC_SELF_STAT = os.path.exists("/proc/self/stat")

SERVER_NAME_LABEL = "server_name"
"""
The `server_name` label is used to identify the homeserver that the metrics correspond
to. Because we support multiple instances of Synapse running in the same process and all
metrics are in a single global `REGISTRY`, we need to manually label any metrics.

In the case of a Synapse homeserver, this should be set to the homeserver name
(`hs.hostname`).

We're purposely not using the `instance` label for this purpose as that should be "The
<host>:<port> part of the target's URL that was scraped.". Also: "In Prometheus
terms, an endpoint you can scrape is called an *instance*, usually corresponding to a
single process." (source: https://prometheus.io/docs/concepts/jobs_instances/)
"""


CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
"""
Content type of the latest text format for Prometheus metrics.

Pulled directly from the prometheus_client library.
"""


def _set_prometheus_client_use_created_metrics(new_value: bool) -> None:
    """
    Sets whether prometheus_client should expose `_created`-suffixed metrics for
    all gauges, histograms and summaries.

    There is no programmatic way in the old versions of `prometheus_client` to disable
    this without poking at internals; the proper way in the old `prometheus_client`
    versions (> `0.14.0` < `0.18.0`) is to use an environment variable which
    prometheus_client loads at import time. For versions > `0.18.0`, we can use the
    dedicated `disable_created_metrics()`/`enable_created_metrics()`.

    The motivation for disabling these `_created` metrics is that they're a waste of
    space as they're not useful but they take up space in Prometheus. It's not the end
    of the world if this doesn't work.
    """
    import prometheus_client.metrics

    if hasattr(prometheus_client.metrics, "_use_created"):
        prometheus_client.metrics._use_created = new_value
    # Just log an error for old versions that don't support disabling the unecessary
    # metrics. It's not the end of the world if this doesn't work as it just means extra
    # wasted space taken up in Prometheus but things keep working.
    elif parse_version(metadata.version("prometheus_client")) < parse_version("0.14.0"):
        logger.error(
            "Can't disable `_created` metrics in prometheus_client (unsupported `prometheus_client` version, too old)"
        )
    # If the attribute doesn't exist on a newer version, this is a sign that the brittle
    # hack is broken. We should consider updating the minimum version of
    # `prometheus_client` to a version (> `0.18.0`) where we can use dedicated
    # `disable_created_metrics()`/`enable_created_metrics()` functions.
    else:
        raise Exception(
            "Can't disable `_created` metrics in prometheus_client (brittle hack broken?)"
        )


# Set this globally so it applies wherever we generate/collect metrics
_set_prometheus_client_use_created_metrics(False)


class _RegistryProxy:
    @staticmethod
    def collect() -> Iterable[Metric]:
        for metric in REGISTRY.collect():
            if not metric.name.startswith("__"):
                yield metric


# A little bit nasty, but collect() above is static so a Protocol doesn't work.
# _RegistryProxy matches the signature of a CollectorRegistry instance enough
# for it to be usable in the contexts in which we use it.
# TODO Do something nicer about this.
RegistryProxy = cast(CollectorRegistry, _RegistryProxy)


@attr.s(slots=True, hash=True, auto_attribs=True, kw_only=True)
class LaterGauge(Collector):
    """A Gauge which periodically calls a user-provided callback to produce metrics."""

    name: str
    desc: str
    labelnames: StrSequence | None = attr.ib(hash=False)
    _instance_id_to_hook_map: dict[
        str | None,  # instance_id
        Callable[[], Mapping[tuple[str, ...], int | float] | int | float],
    ] = attr.ib(factory=dict, hash=False)
    """
    Map from homeserver instance_id to a callback. Each callback should either return a
    value (if there are no labels for this metric), or dict mapping from a label tuple
    to a value.

    We use `instance_id` instead of `server_name` because it's possible to have multiple
    workers running in the same process with the same `server_name`.
    """

    def collect(self) -> Iterable[Metric]:
        # The decision to add `SERVER_NAME_LABEL` is from the `LaterGauge` usage itself
        # (we don't enforce it here, one level up).
        g = GaugeMetricFamily(self.name, self.desc, labels=self.labelnames)  # type: ignore[missing-server-name-label]

        for homeserver_instance_id, hook in self._instance_id_to_hook_map.items():
            try:
                hook_result = hook()
            except Exception:
                logger.exception(
                    "Exception running callback for LaterGauge(%s) for homeserver_instance_id=%s",
                    self.name,
                    homeserver_instance_id,
                )
                # Continue to return the rest of the metrics that aren't broken
                continue

            if isinstance(hook_result, (int, float)):
                g.add_metric([], hook_result)
            else:
                for k, v in hook_result.items():
                    g.add_metric(k, v)

        yield g

    def register_hook(
        self,
        *,
        homeserver_instance_id: str | None,
        hook: Callable[[], Mapping[tuple[str, ...], int | float] | int | float],
    ) -> None:
        """
        Register a callback/hook that will be called to generate a metric samples for
        the gauge.

        Args:
            homeserver_instance_id: The unique ID for this Synapse process instance
                (`hs.get_instance_id()`) that this hook is associated with. This can be used
                later to lookup all hooks associated with a given server name in order to
                unregister them. This should only be omitted for global hooks that work
                across all homeservers.
            hook: A callback that should either return a value (if there are no
                labels for this metric), or dict mapping from a label tuple to a value
        """
        # We shouldn't have multiple hooks registered for the same homeserver `instance_id`.
        existing_hook = self._instance_id_to_hook_map.get(homeserver_instance_id)
        assert existing_hook is None, (
            f"LaterGauge(name={self.name}) hook already registered for homeserver_instance_id={homeserver_instance_id}. "
            "This is likely a Synapse bug and you forgot to unregister the previous hooks for "
            "the server (especially in tests)."
        )

        self._instance_id_to_hook_map[homeserver_instance_id] = hook

    def unregister_hooks_for_homeserver_instance_id(
        self, homeserver_instance_id: str
    ) -> None:
        """
        Unregister all hooks associated with the given homeserver `instance_id`. This should be
        called when a homeserver is shutdown to avoid extra hooks sitting around.

        Args:
            homeserver_instance_id: The unique ID for this Synapse process instance to
                unregister hooks for (`hs.get_instance_id()`).
        """
        self._instance_id_to_hook_map.pop(homeserver_instance_id, None)

    def __attrs_post_init__(self) -> None:
        REGISTRY.register(self)

        # We shouldn't have multiple metrics with the same name. Typically, metrics
        # should be created globally so you shouldn't be running into this and this will
        # catch any stupid mistakes. The `REGISTRY.register(self)` call above will also
        # raise an error if the metric already exists but to make things explicit, we'll
        # also check here.
        existing_gauge = all_later_gauges_to_clean_up_on_shutdown.get(self.name)
        assert existing_gauge is None, f"LaterGauge(name={self.name}) already exists. "

        # Keep track of the gauge so we can clean it up later.
        all_later_gauges_to_clean_up_on_shutdown[self.name] = self


all_later_gauges_to_clean_up_on_shutdown: dict[str, LaterGauge] = {}
"""
Track all `LaterGauge` instances so we can remove any associated hooks during homeserver
shutdown.
"""


# `MetricsEntry` only makes sense when it is a `Protocol`,
# but `Protocol` can't be used as a `TypeVar` bound.
MetricsEntry = TypeVar("MetricsEntry")


class InFlightGauge(Generic[MetricsEntry], Collector):
    """Tracks number of things (e.g. requests, Measure blocks, etc) in flight
    at any given time.

    Each InFlightGauge will create a metric called `<name>_total` that counts
    the number of in flight blocks, as well as a metrics for each item in the
    given `sub_metrics` as `<name>_<sub_metric>` which will get updated by the
    callbacks.

    Args:
        name
        desc
        labels
        sub_metrics: A list of sub metrics that the callbacks will update.
    """

    def __init__(
        self,
        name: str,
        desc: str,
        labels: StrSequence,
        sub_metrics: StrSequence,
    ):
        self.name = name
        self.desc = desc
        self.labels = labels
        self.sub_metrics = sub_metrics

        # Create a class which have the sub_metrics values as attributes, which
        # default to 0 on initialization. Used to pass to registered callbacks.
        self._metrics_class: type[MetricsEntry] = attr.make_class(
            "_MetricsEntry",
            attrs={x: attr.ib(default=0) for x in sub_metrics},
            slots=True,
        )

        # Counts number of in flight blocks for a given set of label values
        self._registrations: dict[
            tuple[str, ...], set[Callable[[MetricsEntry], None]]
        ] = {}

        # Protects access to _registrations
        self._lock = threading.Lock()

        REGISTRY.register(self)

    def register(
        self,
        key: tuple[str, ...],
        callback: Callable[[MetricsEntry], None],
    ) -> None:
        """Registers that we've entered a new block with labels `key`.

        `callback` gets called each time the metrics are collected. The same
        value must also be given to `unregister`.

        `callback` gets called with an object that has an attribute per
        sub_metric, which should be updated with the necessary values. Note that
        the metrics object is shared between all callbacks registered with the
        same key.

        Note that `callback` may be called on a separate thread.

        Args:
            key: A tuple of label values, which must match the order of the
                `labels` given to the constructor.
            callback
        """
        assert len(key) == len(self.labels), (
            f"Expected {len(self.labels)} labels in `key`, got {len(key)}: {key}"
        )

        with self._lock:
            self._registrations.setdefault(key, set()).add(callback)

    def unregister(
        self,
        key: tuple[str, ...],
        callback: Callable[[MetricsEntry], None],
    ) -> None:
        """
        Registers that we've exited a block with labels `key`.

        Args:
            key: A tuple of label values, which must match the order of the
                `labels` given to the constructor.
            callback
        """
        assert len(key) == len(self.labels), (
            f"Expected {len(self.labels)} labels in `key`, got {len(key)}: {key}"
        )

        with self._lock:
            self._registrations.setdefault(key, set()).discard(callback)

    def collect(self) -> Iterable[Metric]:
        """Called by prometheus client when it reads metrics.

        Note: may be called by a separate thread.
        """
        # The decision to add `SERVER_NAME_LABEL` is from the `GaugeBucketCollector`
        # usage itself (we don't enforce it here, one level up).
        in_flight = GaugeMetricFamily(  # type: ignore[missing-server-name-label]
            self.name + "_total", self.desc, labels=self.labels
        )

        metrics_by_key = {}

        # We copy so that we don't mutate the list while iterating
        with self._lock:
            keys = list(self._registrations)

        for key in keys:
            with self._lock:
                callbacks = set(self._registrations[key])

            in_flight.add_metric(labels=key, value=len(callbacks))

            metrics = self._metrics_class()
            metrics_by_key[key] = metrics
            for callback in callbacks:
                callback(metrics)

        yield in_flight

        for name in self.sub_metrics:
            # The decision to add `SERVER_NAME_LABEL` is from the `InFlightGauge` usage
            # itself (we don't enforce it here, one level up).
            gauge = GaugeMetricFamily(  # type: ignore[missing-server-name-label]
                "_".join([self.name, name]), "", labels=self.labels
            )
            for key, metrics in metrics_by_key.items():
                gauge.add_metric(labels=key, value=getattr(metrics, name))
            yield gauge


class GaugeHistogramMetricFamilyWithLabels(GaugeHistogramMetricFamily):
    """
    Custom version of `GaugeHistogramMetricFamily` from `prometheus_client` that allows
    specifying labels and label values.

    A single gauge histogram and its samples.

    For use by custom collectors.
    """

    def __init__(
        self,
        *,
        name: str,
        documentation: str,
        gsum_value: float,
        buckets: Sequence[tuple[str, float]] | None = None,
        labelnames: StrSequence = (),
        labelvalues: StrSequence = (),
        unit: str = "",
    ):
        # Sanity check the number of label values matches the number of label names.
        if len(labelvalues) != len(labelnames):
            raise ValueError(
                "The number of label values must match the number of label names"
            )

        # Call the super to validate and set the labelnames. We use this stable API
        # instead of setting the internal `_labelnames` field directly.
        super().__init__(
            name=name,
            documentation=documentation,
            labels=labelnames,
            # Since `GaugeHistogramMetricFamily` doesn't support supplying `labels` and
            # `buckets` at the same time (artificial limitation), we will just set these
            # as `None` and set up the buckets ourselves just below.
            buckets=None,
            gsum_value=None,
        )

        # Create a gauge for each bucket.
        if buckets is not None:
            self.add_metric(labels=labelvalues, buckets=buckets, gsum_value=gsum_value)


class GaugeBucketCollector(Collector):
    """Like a Histogram, but the buckets are Gauges which are updated atomically.

    The data is updated by calling `update_data` with an iterable of measurements.

    We assume that the data is updated less frequently than it is reported to
    Prometheus, and optimise for that case.
    """

    __slots__ = (
        "_name",
        "_documentation",
        "_labelnames",
        "_bucket_bounds",
        "_metric",
    )

    def __init__(
        self,
        *,
        name: str,
        documentation: str,
        labelnames: StrSequence | None,
        buckets: Iterable[float],
        registry: CollectorRegistry = REGISTRY,
    ):
        """
        Args:
            name: base name of metric to be exported to Prometheus. (a _bucket suffix
               will be added.)
            documentation: help text for the metric
            buckets: The top bounds of the buckets to report
            registry: metric registry to register with
        """
        self._name = name
        self._documentation = documentation
        self._labelnames = labelnames if labelnames else ()

        # the tops of the buckets
        self._bucket_bounds = [float(b) for b in buckets]
        if self._bucket_bounds != sorted(self._bucket_bounds):
            raise ValueError("Buckets not in sorted order")

        if self._bucket_bounds[-1] != float("inf"):
            self._bucket_bounds.append(float("inf"))

        # We initially set this to None. We won't report metrics until
        # this has been initialised after a successful data update
        self._metric: GaugeHistogramMetricFamilyWithLabels | None = None

        registry.register(self)

    def collect(self) -> Iterable[Metric]:
        # Don't report metrics unless we've already collected some data
        if self._metric is not None:
            yield self._metric

    def update_data(self, values: Iterable[float], labels: StrSequence = ()) -> None:
        """Update the data to be reported by the metric

        The existing data is cleared, and each measurement in the input is assigned
        to the relevant bucket.

        Args:
            values
            labels
        """
        self._metric = self._values_to_metric(values, labels)

    def _values_to_metric(
        self, values: Iterable[float], labels: StrSequence = ()
    ) -> GaugeHistogramMetricFamilyWithLabels:
        """
        Args:
            values
            labels
        """
        total = 0.0
        bucket_values = [0 for _ in self._bucket_bounds]

        for v in values:
            # assign each value to a bucket
            for i, bound in enumerate(self._bucket_bounds):
                if v <= bound:
                    bucket_values[i] += 1
                    break

            # ... and increment the sum
            total += v

        # now, aggregate the bucket values so that they count the number of entries in
        # that bucket or below.
        accumulated_values = itertools.accumulate(bucket_values)

        # The decision to add `SERVER_NAME_LABEL` is from the `GaugeBucketCollector`
        # usage itself (we don't enforce it here, one level up).
        return GaugeHistogramMetricFamilyWithLabels(  # type: ignore[missing-server-name-label]
            name=self._name,
            documentation=self._documentation,
            labelnames=self._labelnames,
            labelvalues=labels,
            buckets=list(
                zip((str(b) for b in self._bucket_bounds), accumulated_values)
            ),
            gsum_value=total,
        )


#
# Detailed CPU metrics
#


class CPUMetrics(Collector):
    def __init__(self) -> None:
        ticks_per_sec = 100
        try:
            # Try and get the system config
            ticks_per_sec = os.sysconf("SC_CLK_TCK")
        except (ValueError, TypeError, AttributeError):
            pass

        self.ticks_per_sec = ticks_per_sec

    def collect(self) -> Iterable[Metric]:
        if not HAVE_PROC_SELF_STAT:
            return

        with open("/proc/self/stat") as s:
            line = s.read()
            raw_stats = line.split(") ", 1)[1].split(" ")

            # This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
            user = GaugeMetricFamily("process_cpu_user_seconds_total", "")  # type: ignore[missing-server-name-label]
            user.add_metric([], float(raw_stats[11]) / self.ticks_per_sec)
            yield user

            # This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
            sys = GaugeMetricFamily("process_cpu_system_seconds_total", "")  # type: ignore[missing-server-name-label]
            sys.add_metric([], float(raw_stats[12]) / self.ticks_per_sec)
            yield sys


# This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
REGISTRY.register(CPUMetrics())  # type: ignore[missing-server-name-label]


#
# Federation Metrics
#

sent_transactions_counter = Counter(
    "synapse_federation_client_sent_transactions", "", labelnames=[SERVER_NAME_LABEL]
)

events_processed_counter = Counter(
    "synapse_federation_client_events_processed", "", labelnames=[SERVER_NAME_LABEL]
)

event_processing_loop_counter = Counter(
    "synapse_event_processing_loop_count",
    "Event processing loop iterations",
    labelnames=["name", SERVER_NAME_LABEL],
)

event_processing_loop_room_count = Counter(
    "synapse_event_processing_loop_room_count",
    "Rooms seen per event processing loop iteration",
    labelnames=["name", SERVER_NAME_LABEL],
)


# Used to track where various components have processed in the event stream,
# e.g. federation sending, appservice sending, etc.
event_processing_positions = Gauge(
    "synapse_event_processing_positions", "", labelnames=["name", SERVER_NAME_LABEL]
)

# Used to track the current max events stream position
event_persisted_position = Gauge(
    "synapse_event_persisted_position", "", labelnames=[SERVER_NAME_LABEL]
)

# Used to track the received_ts of the last event processed by various
# components
event_processing_last_ts = Gauge(
    "synapse_event_processing_last_ts", "", labelnames=["name", SERVER_NAME_LABEL]
)

# Used to track the lag processing events. This is the time difference
# between the last processed event's received_ts and the time it was
# finished being processed.
event_processing_lag = Gauge(
    "synapse_event_processing_lag", "", labelnames=["name", SERVER_NAME_LABEL]
)

event_processing_lag_by_event = Histogram(
    "synapse_event_processing_lag_by_event",
    "Time between an event being persisted and it being queued up to be sent to the relevant remote servers",
    labelnames=["name", SERVER_NAME_LABEL],
)

# Build info of the running server.
#
# This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`. We
# consider this process-level because all Synapse homeservers running in the process
# will use the same Synapse version.
build_info = Gauge(  # type: ignore[missing-server-name-label]
    "synapse_build_info", "Build information", ["pythonversion", "version", "osversion"]
)
build_info.labels(
    " ".join([platform.python_implementation(), platform.python_version()]),
    SYNAPSE_VERSION,
    " ".join([platform.system(), platform.release()]),
).set(1)

# 3PID send info
threepid_send_requests = Histogram(
    "synapse_threepid_send_requests_with_tries",
    documentation="Number of requests for a 3pid token by try count. Note if"
    " there is a request with try count of 4, then there would have been one"
    " each for 1, 2 and 3",
    buckets=(1, 2, 3, 4, 5, 10),
    labelnames=("type", "reason", SERVER_NAME_LABEL),
)

threadpool_total_threads = Gauge(
    "synapse_threadpool_total_threads",
    "Total number of threads currently in the threadpool",
    labelnames=["name", SERVER_NAME_LABEL],
)

threadpool_total_working_threads = Gauge(
    "synapse_threadpool_working_threads",
    "Number of threads currently working in the threadpool",
    labelnames=["name", SERVER_NAME_LABEL],
)

threadpool_total_min_threads = Gauge(
    "synapse_threadpool_min_threads",
    "Minimum number of threads configured in the threadpool",
    labelnames=["name", SERVER_NAME_LABEL],
)

threadpool_total_max_threads = Gauge(
    "synapse_threadpool_max_threads",
    "Maximum number of threads configured in the threadpool",
    labelnames=["name", SERVER_NAME_LABEL],
)


def register_threadpool(*, name: str, server_name: str, threadpool: ThreadPool) -> None:
    """
    Add metrics for the threadpool.

    Args:
        name: The name of the threadpool, used to identify it in the metrics.
        server_name: The homeserver name (used to label metrics) (this should be `hs.hostname`).
        threadpool: The threadpool to register metrics for.
    """

    threadpool_total_min_threads.labels(
        name=name, **{SERVER_NAME_LABEL: server_name}
    ).set(threadpool.min)
    threadpool_total_max_threads.labels(
        name=name, **{SERVER_NAME_LABEL: server_name}
    ).set(threadpool.max)

    threadpool_total_threads.labels(
        name=name, **{SERVER_NAME_LABEL: server_name}
    ).set_function(lambda: len(threadpool.threads))
    threadpool_total_working_threads.labels(
        name=name, **{SERVER_NAME_LABEL: server_name}
    ).set_function(lambda: len(threadpool.working))


class MetricsResource(Resource):
    """
    Twisted ``Resource`` that serves prometheus metrics.
    """

    isLeaf = True

    def __init__(self, registry: CollectorRegistry = REGISTRY):
        self.registry = registry

    def render_GET(self, request: Request) -> bytes:
        request.setHeader(b"Content-Type", CONTENT_TYPE_LATEST.encode("ascii"))
        response = generate_latest(self.registry)
        request.setHeader(b"Content-Length", str(len(response)))
        return response


__all__ = [
    "Collector",
    "MetricsResource",
    "generate_latest",
    "LaterGauge",
    "InFlightGauge",
    "GaugeBucketCollector",
    "MIN_TIME_BETWEEN_GCS",
    "install_gc_manager",
]
