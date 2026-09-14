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

"""Throwaway collectors shared by the tests that scrape `RegistryProxy`."""

from typing import Iterable

from prometheus_client import Metric
from prometheus_client.core import GaugeMetricFamily


class BrokenCollector:
    """A collector that raises partway through `collect()`."""

    def __init__(self, name: str) -> None:
        self._name = name

    def describe(self) -> Iterable[Metric]:
        # Registering on the default REGISTRY (auto_describe=True) would otherwise call
        # collect(), which raises, to work out metric names.
        #
        # This is a test and does not matter if it uses `SERVER_NAME_LABEL`.
        yield GaugeMetricFamily(self._name, "unused")  # type: ignore[missing-server-name-label]

    def collect(self) -> Iterable[Metric]:
        d = {"a": 1, "b": 2}
        for _ in d:
            d["c"] = 3  # dictionary changed size during iteration
        # This is a test and does not matter if it uses `SERVER_NAME_LABEL`.
        yield GaugeMetricFamily(self._name, "unused")  # type: ignore[missing-server-name-label]


class HealthyCollector:
    """A collector that yields a single gauge family."""

    def __init__(self, name: str) -> None:
        self._name = name

    def collect(self) -> Iterable[Metric]:
        # This is a test and does not matter if it uses `SERVER_NAME_LABEL`.
        yield GaugeMetricFamily(self._name, "unused")  # type: ignore[missing-server-name-label]
