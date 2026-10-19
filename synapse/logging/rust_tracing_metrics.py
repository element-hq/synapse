#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Prometheus metrics for the Rust Jaeger reporter."""

import threading
import weakref
from typing import Iterable

from prometheus_client.core import (
    REGISTRY,
    CounterMetricFamily,
    GaugeMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from synapse.metrics import SERVER_NAME_LABEL
from synapse.synapse_rust.runtime import RustRuntime


class _RustTracingCollector(Collector):
    """Reports the reporter counters of every `RustRuntime` with tracing
    enabled. Holds the runtimes weakly."""

    def __init__(self) -> None:
        self._runtimes: "weakref.WeakKeyDictionary[RustRuntime, str]" = (
            weakref.WeakKeyDictionary()
        )
        # Runtimes are added on the reactor thread and read on the metrics
        # thread.
        self._lock = threading.Lock()

    def add(self, runtime: RustRuntime, server_name: str) -> None:
        with self._lock:
            self._runtimes[runtime] = server_name

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            runtimes = list(self._runtimes.items())

        queue_depth = GaugeMetricFamily(
            "synapse_rust_tracing_queue_depth",
            "Spans from Rust code waiting to be sent to the Jaeger agent",
            labels=[SERVER_NAME_LABEL],
        )
        spans_sent = CounterMetricFamily(
            "synapse_rust_tracing_spans_sent",
            "Spans from Rust code sent to the Jaeger agent",
            labels=[SERVER_NAME_LABEL],
        )
        batches_sent = CounterMetricFamily(
            "synapse_rust_tracing_batches_sent",
            "UDP datagrams of spans from Rust code sent to the Jaeger agent",
            labels=[SERVER_NAME_LABEL],
        )
        dropped = CounterMetricFamily(
            "synapse_rust_tracing_spans_dropped",
            "Spans from Rust code dropped without being sent, by reason: the queue "
            "was full, the span alone was too big for a datagram, or the reporter "
            "had shut down",
            labels=["reason", SERVER_NAME_LABEL],
        )
        send_errors = CounterMetricFamily(
            "synapse_rust_tracing_send_errors",
            "Datagrams of spans from Rust code that failed to send",
            labels=[SERVER_NAME_LABEL],
        )

        for runtime, server_name in runtimes:
            stats = runtime.tracing_stats()
            if stats is None:
                continue
            (
                depth,
                sent,
                batches,
                dropped_queue_full,
                dropped_oversize,
                dropped_closed,
                errors,
            ) = stats
            queue_depth.add_metric([server_name], depth)
            spans_sent.add_metric([server_name], sent)
            batches_sent.add_metric([server_name], batches)
            dropped.add_metric(["queue_full", server_name], dropped_queue_full)
            dropped.add_metric(["oversize", server_name], dropped_oversize)
            dropped.add_metric(["closed", server_name], dropped_closed)
            send_errors.add_metric([server_name], errors)

        yield queue_depth
        yield spans_sent
        yield batches_sent
        yield dropped
        yield send_errors


# Its metrics carry `SERVER_NAME_LABEL`, which the collector adds per runtime.
rust_tracing_collector = _RustTracingCollector()  # type: ignore[missing-server-name-label]
REGISTRY.register(rust_tracing_collector)
