#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 Matrix.org Foundation C.I.C.
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
from typing import NoReturn, Protocol

from prometheus_client.core import Sample

from synapse.metrics import (
    REGISTRY,
    SERVER_NAME_LABEL,
    InFlightGauge,
    LaterGauge,
    all_later_gauges_to_clean_up_on_shutdown,
    generate_latest,
)
from synapse.util.caches.deferred_cache import DeferredCache

from tests import unittest


def get_sample_labels_value(sample: Sample) -> tuple[dict[str, str], float]:
    """Extract the labels and values of a sample.

    prometheus_client 0.5 changed the sample type to a named tuple with more
    members than the plain tuple had in 0.4 and earlier. This function can
    extract the labels and value from the sample for both sample types.

    Args:
        sample: The sample to get the labels and value from.
    Returns:
        A tuple of (labels, value) from the sample.
    """

    # If the sample has a labels and value attribute, use those.
    if hasattr(sample, "labels") and hasattr(sample, "value"):
        return sample.labels, sample.value
    # Otherwise fall back to treating it as a plain 3 tuple.
    else:
        # In older versions of prometheus_client Sample was a 3-tuple.
        labels: dict[str, str]
        value: float
        _, labels, value = sample  # type: ignore[misc]
        return labels, value


class TestMauLimit(unittest.TestCase):
    def test_basic(self) -> None:
        class MetricEntry(Protocol):
            foo: int
            bar: int

        # This is a test and does not matter if it uses `SERVER_NAME_LABEL`.
        gauge: InFlightGauge[MetricEntry] = InFlightGauge(  # type: ignore[missing-server-name-label]
            "test1", "", labels=["test_label"], sub_metrics=["foo", "bar"]
        )

        def handle1(metrics: MetricEntry) -> None:
            metrics.foo += 2
            metrics.bar = max(metrics.bar, 5)

        def handle2(metrics: MetricEntry) -> None:
            metrics.foo += 3
            metrics.bar = max(metrics.bar, 7)

        gauge.register(("key1",), handle1)

        self.assert_dict(
            {
                "test1_total": {("key1",): 1},
                "test1_foo": {("key1",): 2},
                "test1_bar": {("key1",): 5},
            },
            self.get_metrics_from_gauge(gauge),
        )

        gauge.unregister(("key1",), handle1)

        self.assert_dict(
            {
                "test1_total": {("key1",): 0},
                "test1_foo": {("key1",): 0},
                "test1_bar": {("key1",): 0},
            },
            self.get_metrics_from_gauge(gauge),
        )

        gauge.register(("key1",), handle1)
        gauge.register(("key2",), handle2)

        self.assert_dict(
            {
                "test1_total": {("key1",): 1, ("key2",): 1},
                "test1_foo": {("key1",): 2, ("key2",): 3},
                "test1_bar": {("key1",): 5, ("key2",): 7},
            },
            self.get_metrics_from_gauge(gauge),
        )

        gauge.unregister(("key2",), handle2)
        gauge.register(("key1",), handle2)

        self.assert_dict(
            {
                "test1_total": {("key1",): 2, ("key2",): 0},
                "test1_foo": {("key1",): 5, ("key2",): 0},
                "test1_bar": {("key1",): 7, ("key2",): 0},
            },
            self.get_metrics_from_gauge(gauge),
        )

    def get_metrics_from_gauge(
        self, gauge: InFlightGauge
    ) -> dict[str, dict[tuple[str, ...], float]]:
        results = {}

        for r in gauge.collect():
            results[r.name] = {
                tuple(labels[x] for x in gauge.labels): value
                for labels, value in map(get_sample_labels_value, r.samples)
            }

        return results


class BuildInfoTests(unittest.TestCase):
    def test_get_build(self) -> None:
        """
        The synapse_build_info metric reports the OS version, Python version,
        and Synapse version.
        """
        items = list(
            filter(
                lambda x: b"synapse_build_info{" in x,
                generate_latest(REGISTRY).split(b"\n"),
            )
        )
        self.assertEqual(len(items), 1)
        self.assertTrue(b"osversion=" in items[0])
        self.assertTrue(b"pythonversion=" in items[0])
        self.assertTrue(b"version=" in items[0])


class CacheMetricsTests(unittest.HomeserverTestCase):
    def test_cache_metric(self) -> None:
        """
        Caches produce metrics reflecting their state when scraped.
        """
        CACHE_NAME = "cache_metrics_test_fgjkbdfg"
        cache: DeferredCache[str, str] = DeferredCache(
            name=CACHE_NAME,
            clock=self.hs.get_clock(),
            server_name=self.hs.hostname,
            max_entries=777,
        )

        metrics_map = get_latest_metrics()

        cache_size_metric = f'synapse_util_caches_cache_size{{name="{CACHE_NAME}",server_name="{self.hs.hostname}"}}'
        cache_max_size_metric = f'synapse_util_caches_cache_max_size{{name="{CACHE_NAME}",server_name="{self.hs.hostname}"}}'

        cache_size_metric_value = metrics_map.get(cache_size_metric)
        self.assertIsNotNone(
            cache_size_metric_value,
            f"Missing metric {cache_size_metric} in cache metrics {metrics_map}",
        )
        cache_max_size_metric_value = metrics_map.get(cache_max_size_metric)
        self.assertIsNotNone(
            cache_max_size_metric_value,
            f"Missing metric {cache_max_size_metric} in cache metrics {metrics_map}",
        )

        self.assertEqual(cache_size_metric_value, "0.0")
        self.assertEqual(cache_max_size_metric_value, "777.0")

        cache.prefill("1", "hi")

        metrics_map = get_latest_metrics()

        cache_size_metric_value = metrics_map.get(cache_size_metric)
        self.assertIsNotNone(
            cache_size_metric_value,
            f"Missing metric {cache_size_metric} in cache metrics {metrics_map}",
        )
        cache_max_size_metric_value = metrics_map.get(cache_max_size_metric)
        self.assertIsNotNone(
            cache_max_size_metric_value,
            f"Missing metric {cache_max_size_metric} in cache metrics {metrics_map}",
        )

        self.assertEqual(cache_size_metric_value, "1.0")
        self.assertEqual(cache_max_size_metric_value, "777.0")

    def test_cache_metric_multiple_servers(self) -> None:
        """
        Test that cache metrics are reported correctly across multiple servers. We will
        have an metrics entry for each homeserver that is labeled with the `server_name`
        label.
        """
        CACHE_NAME = "cache_metric_multiple_servers_test"
        cache1: DeferredCache[str, str] = DeferredCache(
            name=CACHE_NAME, clock=self.clock, server_name="hs1", max_entries=777
        )
        cache2: DeferredCache[str, str] = DeferredCache(
            name=CACHE_NAME, clock=self.clock, server_name="hs2", max_entries=777
        )

        metrics_map = get_latest_metrics()

        hs1_cache_size_metric = (
            f'synapse_util_caches_cache_size{{name="{CACHE_NAME}",server_name="hs1"}}'
        )
        hs2_cache_size_metric = (
            f'synapse_util_caches_cache_size{{name="{CACHE_NAME}",server_name="hs2"}}'
        )
        hs1_cache_max_size_metric = f'synapse_util_caches_cache_max_size{{name="{CACHE_NAME}",server_name="hs1"}}'
        hs2_cache_max_size_metric = f'synapse_util_caches_cache_max_size{{name="{CACHE_NAME}",server_name="hs2"}}'

        # Find the metrics for the caches from both homeservers
        hs1_cache_size_metric_value = metrics_map.get(hs1_cache_size_metric)
        self.assertIsNotNone(
            hs1_cache_size_metric_value,
            f"Missing metric {hs1_cache_size_metric} in cache metrics {metrics_map}",
        )
        hs2_cache_size_metric_value = metrics_map.get(hs2_cache_size_metric)
        self.assertIsNotNone(
            hs2_cache_size_metric_value,
            f"Missing metric {hs2_cache_size_metric} in cache metrics {metrics_map}",
        )
        hs1_cache_max_size_metric_value = metrics_map.get(hs1_cache_max_size_metric)
        self.assertIsNotNone(
            hs1_cache_max_size_metric_value,
            f"Missing metric {hs1_cache_max_size_metric} in cache metrics {metrics_map}",
        )
        hs2_cache_max_size_metric_value = metrics_map.get(hs2_cache_max_size_metric)
        self.assertIsNotNone(
            hs2_cache_max_size_metric_value,
            f"Missing metric {hs2_cache_max_size_metric} in cache metrics {metrics_map}",
        )

        # Sanity check the metric values
        self.assertEqual(hs1_cache_size_metric_value, "0.0")
        self.assertEqual(hs2_cache_size_metric_value, "0.0")
        self.assertEqual(hs1_cache_max_size_metric_value, "777.0")
        self.assertEqual(hs2_cache_max_size_metric_value, "777.0")

        # Add something to both caches to change the numbers
        cache1.prefill("1", "hi")
        cache2.prefill("2", "ho")

        metrics_map = get_latest_metrics()

        # Find the metrics for the caches from both homeservers
        hs1_cache_size_metric_value = metrics_map.get(hs1_cache_size_metric)
        self.assertIsNotNone(
            hs1_cache_size_metric_value,
            f"Missing metric {hs1_cache_size_metric} in cache metrics {metrics_map}",
        )
        hs2_cache_size_metric_value = metrics_map.get(hs2_cache_size_metric)
        self.assertIsNotNone(
            hs2_cache_size_metric_value,
            f"Missing metric {hs2_cache_size_metric} in cache metrics {metrics_map}",
        )
        hs1_cache_max_size_metric_value = metrics_map.get(hs1_cache_max_size_metric)
        self.assertIsNotNone(
            hs1_cache_max_size_metric_value,
            f"Missing metric {hs1_cache_max_size_metric} in cache metrics {metrics_map}",
        )
        hs2_cache_max_size_metric_value = metrics_map.get(hs2_cache_max_size_metric)
        self.assertIsNotNone(
            hs2_cache_max_size_metric_value,
            f"Missing metric {hs2_cache_max_size_metric} in cache metrics {metrics_map}",
        )

        # Sanity check the metric values
        self.assertEqual(hs1_cache_size_metric_value, "1.0")
        self.assertEqual(hs2_cache_size_metric_value, "1.0")
        self.assertEqual(hs1_cache_max_size_metric_value, "777.0")
        self.assertEqual(hs2_cache_max_size_metric_value, "777.0")


class LaterGaugeTests(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.later_gauge = LaterGauge(
            name="foo",
            desc="",
            labelnames=[SERVER_NAME_LABEL],
        )

    def tearDown(self) -> None:
        super().tearDown()

        REGISTRY.unregister(self.later_gauge)
        all_later_gauges_to_clean_up_on_shutdown.pop(self.later_gauge.name, None)

    def test_later_gauge_multiple_servers(self) -> None:
        """
        Test that LaterGauge metrics are reported correctly across multiple servers. We
        will have an metrics entry for each homeserver that is labeled with the
        `server_name` label.
        """
        self.later_gauge.register_hook(
            homeserver_instance_id="123", hook=lambda: {("hs1",): 1}
        )
        self.later_gauge.register_hook(
            homeserver_instance_id="456", hook=lambda: {("hs2",): 2}
        )

        metrics_map = get_latest_metrics()

        # Find the metrics from both homeservers
        hs1_metric = 'foo{server_name="hs1"}'
        hs1_metric_value = metrics_map.get(hs1_metric)
        self.assertIsNotNone(
            hs1_metric_value,
            f"Missing metric {hs1_metric} in metrics {metrics_map}",
        )
        self.assertEqual(hs1_metric_value, "1.0")

        hs2_metric = 'foo{server_name="hs2"}'
        hs2_metric_value = metrics_map.get(hs2_metric)
        self.assertIsNotNone(
            hs2_metric_value,
            f"Missing metric {hs2_metric} in metrics {metrics_map}",
        )
        self.assertEqual(hs2_metric_value, "2.0")

    def test_later_gauge_hook_exception(self) -> None:
        """
        Test that LaterGauge metrics are collected across multiple servers even if one
        hooks is throwing an exception.
        """

        def raise_exception() -> NoReturn:
            raise Exception("fake error generating data")

        # Make the hook for hs1 throw an exception
        self.later_gauge.register_hook(
            homeserver_instance_id="123", hook=raise_exception
        )
        # Metrics from hs2 still work fine
        self.later_gauge.register_hook(
            homeserver_instance_id="456", hook=lambda: {("hs2",): 2}
        )

        metrics_map = get_latest_metrics()

        # Since we encountered an exception while trying to collect metrics from hs1, we
        # don't expect to see it here.
        hs1_metric = 'foo{server_name="hs1"}'
        hs1_metric_value = metrics_map.get(hs1_metric)
        self.assertIsNone(
            hs1_metric_value,
            (
                "Since we encountered an exception while trying to collect metrics from hs1"
                f"we don't expect to see it the metrics_map {metrics_map}"
            ),
        )

        # We should still see metrics from hs2 though
        hs2_metric = 'foo{server_name="hs2"}'
        hs2_metric_value = metrics_map.get(hs2_metric)
        self.assertIsNotNone(
            hs2_metric_value,
            f"Missing metric {hs2_metric} in cache metrics {metrics_map}",
        )
        self.assertEqual(hs2_metric_value, "2.0")


def get_latest_metrics() -> dict[str, str]:
    """
    Collect the latest metrics from the registry and parse them into an easy to use map.
    The key includes the metric name and labels.

    Example output:
    {
        "synapse_util_caches_cache_size": "0.0",
        "synapse_util_caches_cache_max_size{name="some_cache",server_name="hs1"}": "777.0",
        ...
    }
    """
    metric_map = {
        x.split(b" ")[0].decode("ascii"): x.split(b" ")[1].decode("ascii")
        for x in filter(
            lambda x: len(x) > 0 and not x.startswith(b"#"),
            generate_latest(REGISTRY).split(b"\n"),
        )
    }

    return metric_map
