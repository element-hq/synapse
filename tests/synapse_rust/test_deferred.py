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

"""Tests of the Deferreds that Rust futures are exposed to Python as."""

import time
from collections.abc import Callable

from twisted.internet.defer import CancelledError, Deferred

from synapse.synapse_rust.deferred import dropped_sleeps_for_test, sleep_for_test

from tests.unittest import HomeserverTestCase


class RustDeferredTestCase(HomeserverTestCase):
    def _pump_until(self, done: Callable[[], bool], timeout: float = 5.0) -> None:
        """Let the tokio threads run, and the reactor run what they dispatch,
        until `done`."""
        deadline = time.monotonic() + timeout
        while not done():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.001)
            self.reactor.advance(0)

    def test_fires_when_the_future_completes(self) -> None:
        d = sleep_for_test(self.hs.get_rust_runtime(), 0.01, False)
        results: list[str] = []
        d.addCallback(results.append)
        self._pump_until(lambda: bool(results))
        self.assertEqual(results, ["slept"])

    def test_cancel_lets_the_task_run_on_by_default(self) -> None:
        dropped_before = dropped_sleeps_for_test()
        d: Deferred[str] = sleep_for_test(self.hs.get_rust_runtime(), 0.5, False)

        d.cancel()
        self.failureResultOf(d, CancelledError)

        # The future is still running: it is dropped only once its sleep ends.
        self.reactor.advance(0)
        self.assertEqual(dropped_sleeps_for_test(), dropped_before)
        self._pump_until(lambda: dropped_sleeps_for_test() > dropped_before)

    def test_cancel_aborts_an_abortable_task(self) -> None:
        dropped_before = dropped_sleeps_for_test()
        d: Deferred[str] = sleep_for_test(self.hs.get_rust_runtime(), 60, True)

        d.cancel()
        self.failureResultOf(d, CancelledError)

        # The future is dropped where it stands, long before its sleep ends.
        self._pump_until(lambda: dropped_sleeps_for_test() > dropped_before)
