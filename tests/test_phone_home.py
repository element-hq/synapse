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

import resource
from unittest import mock

from twisted.internet.testing import MemoryReactor

from synapse.app.phone_stats_home import phone_stats_home
from synapse.rest import admin
from synapse.rest.client import login, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class PhoneHomeStatsTestCase(HomeserverTestCase):
    def test_performance_frozen_clock(self) -> None:
        """
        If time doesn't move, don't error out.
        """
        past_stats = [
            (int(self.hs.get_clock().time()), resource.getrusage(resource.RUSAGE_SELF))
        ]
        stats: JsonDict = {}
        self.get_success(phone_stats_home(self.hs, stats, past_stats))
        self.assertEqual(stats["cpu_average"], 0)

    def test_performance_100(self) -> None:
        """
        1 second of usage over 1 second is 100% CPU usage.
        """
        real_res = resource.getrusage(resource.RUSAGE_SELF)
        old_resource = mock.Mock(spec=real_res)
        old_resource.ru_utime = real_res.ru_utime - 1
        old_resource.ru_stime = real_res.ru_stime
        old_resource.ru_maxrss = real_res.ru_maxrss

        past_stats = [(self.hs.get_clock().time(), old_resource)]
        stats: JsonDict = {}
        self.reactor.advance(1)
        # `old_resource` has type `Mock` instead of `struct_rusage`
        self.get_success(
            phone_stats_home(self.hs, stats, past_stats)  # type: ignore[arg-type]
        )
        self.assertApproximates(stats["cpu_average"], 100, tolerance=2.5)


class CommonMetricsTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.metrics_manager = hs.get_common_usage_metrics_manager()
        self.metrics_manager.setup()

    def test_dau(self) -> None:
        """Tests that the daily active users count is correctly updated."""
        self._assert_metric_value("daily_active_users", 0)

        self.register_user("user", "password")
        tok = self.login("user", "password")
        self.make_request("GET", "/sync", access_token=tok)

        self.pump(1)

        self._assert_metric_value("daily_active_users", 1)

    def _assert_metric_value(self, metric_name: str, expected: int) -> None:
        """Compare the given value to the current value of the common usage metric with
        the given name.

        Args:
            metric_name: The metric to look up.
            expected: Expected value for this metric.
        """
        metrics = self.get_success(self.metrics_manager.get_metrics())
        value = getattr(metrics, metric_name)
        self.assertEqual(value, expected)
