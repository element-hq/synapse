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
from importlib import metadata
from typing import Dict, Tuple
from unittest.mock import patch

from pkg_resources import parse_version
from prometheus_client.core import Sample
from typing_extensions import Protocol

from synapse.app._base import _set_prometheus_client_use_created_metrics
from synapse.metrics import REGISTRY, InFlightGauge, generate_latest
from synapse.util.caches.deferred_cache import DeferredCache

from tests import unittest


def get_sample_labels_value(sample: Sample) -> Tuple[Dict[str, str], float]:
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
        labels: Dict[str, str]
        value: float
        _, labels, value = sample  # type: ignore[misc]
        return labels, value


class TestMauLimit(unittest.TestCase):
    def test_basic(self) -> None:
        class MetricEntry(Protocol):
            foo: int
            bar: int

        gauge: InFlightGauge[MetricEntry] = InFlightGauge(
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
    ) -> Dict[str, Dict[Tuple[str, ...], float]]:
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
        cache: DeferredCache[str, str] = DeferredCache(CACHE_NAME, max_entries=777)

        items = {
            x.split(b"{")[0].decode("ascii"): x.split(b" ")[1].decode("ascii")
            for x in filter(
                lambda x: b"cache_metrics_test_fgjkbdfg" in x,
                generate_latest(REGISTRY).split(b"\n"),
            )
        }

        self.assertEqual(items["synapse_util_caches_cache_size"], "0.0")
        self.assertEqual(items["synapse_util_caches_cache_max_size"], "777.0")

        cache.prefill("1", "hi")

        items = {
            x.split(b"{")[0].decode("ascii"): x.split(b" ")[1].decode("ascii")
            for x in filter(
                lambda x: b"cache_metrics_test_fgjkbdfg" in x,
                generate_latest(REGISTRY).split(b"\n"),
            )
        }

        self.assertEqual(items["synapse_util_caches_cache_size"], "1.0")
        self.assertEqual(items["synapse_util_caches_cache_max_size"], "777.0")


class PrometheusMetricsHackTestCase(unittest.HomeserverTestCase):
    if parse_version(metadata.version("prometheus_client")) < parse_version("0.14.0"):
        skip = "prometheus-client too old"

    def test_created_metrics_disabled(self) -> None:
        """
        Tests that a brittle hack, to disable `_created` metrics, works.
        This involves poking at the internals of prometheus-client.
        It's not the end of the world if this doesn't work.

        This test gives us a way to notice if prometheus-client changes
        their internals.
        """
        import prometheus_client.metrics

        PRIVATE_FLAG_NAME = "_use_created"

        # By default, the pesky `_created` metrics are enabled.
        # Check this assumption is still valid.
        self.assertTrue(getattr(prometheus_client.metrics, PRIVATE_FLAG_NAME))

        with patch("prometheus_client.metrics") as mock:
            setattr(mock, PRIVATE_FLAG_NAME, True)
            _set_prometheus_client_use_created_metrics(False)
            self.assertFalse(getattr(mock, PRIVATE_FLAG_NAME, False))
