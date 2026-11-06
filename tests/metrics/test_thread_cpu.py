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

import os
import threading
import time

from synapse.metrics.thread_cpu import ThreadCPUMetrics
from synapse.util.duration import Duration

from tests import unittest

# How much CPU time to burn on each thread, in seconds.
BURN = Duration(milliseconds=200)

# How far a thread's reported CPU may fall short of its burn. The kernel reports
# CPU time in whole ticks (rounded down), so we allow some tolerance (here ~5
# ticks).
TOLERANCE = Duration(milliseconds=50)


def _burn_cpu() -> None:
    """Spin on the calling thread until it has used `BURN_SECONDS` of CPU."""
    start = time.thread_time()
    while time.thread_time() - start < BURN.as_secs():
        pass


def _collect(metrics: ThreadCPUMetrics) -> dict[str, float]:
    """Scrape `metrics` and map each thread type to its CPU seconds.

    User and system time are summed. The kernel splits a thread's CPU time
    between the two by sampling, so a burn in user code can show up partly as
    system time.
    """
    totals: dict[str, float] = {}
    for metric in metrics.collect():
        for sample in metric.samples:
            if sample.name == "synapse_thread_cpu_seconds_total":
                thread_type = sample.labels["thread_type"]
                totals[thread_type] = totals.get(thread_type, 0.0) + sample.value
    return totals


class ThreadCPUMetricsTestCase(unittest.TestCase):
    def test_cpu_adds_up(self) -> None:
        """CPU used between two scrapes is reported under the right thread
        type, and the types add up to the CPU the whole process used.
        """
        if not os.path.isdir("/proc/self/task"):
            self.skipTest(
                "Skipping as `ThreadCPUMetrics` tests require /proc/self/task (Linux only)"
            )

        # A fresh collector, so threads registered elsewhere don't count. It
        # reports process-level metrics, so it has no `SERVER_NAME_LABEL`.
        metrics = ThreadCPUMetrics()  # type: ignore[missing-server-name-label]

        # First record the base CPU metrics.
        before = _collect(metrics)
        process_before = sum(os.times()[:2])

        # First we setup and burn some CPU on a separate thread.

        # The collector only sees live threads, so the thread waits for
        # `release` until after the second scrape.
        burnt = threading.Event()
        release = threading.Event()

        def run_on_thread() -> None:
            _burn_cpu()
            burnt.set()
            release.wait(timeout=Duration(seconds=10).as_secs())

        # Since `/proc/<tid>/stat` puts the thread name in parenthesis and uses
        # spaces as field separators, choose a thread name with those same characters
        # to ensure we parse it correctly.
        #
        # Note that the thread name only gets passed to the kernel from Python
        # 3.14 onwards.
        thread = threading.Thread(target=run_on_thread, name="a) b) (c")

        # Cleanup functions are run in reverse order, so first we set the
        # release event (to wake the thread up), and then we wait for it exit.
        self.addCleanup(thread.join)
        self.addCleanup(release.set)

        metrics.register_thread("test-thread", thread)

        thread.start()

        # Wait for the separate thread to burn CPU before continuing.
        self.assertTrue(burnt.wait(timeout=Duration(seconds=10).as_secs()))

        # Next burn some CPU on the main thread.
        _burn_cpu()

        # Now collect the CPU metrics after burning on both threads.
        after = _collect(metrics)
        process_delta = sum(os.times()[:2]) - process_before

        # Check that each thread burned at least the expected amount of CPU.

        def delta(thread_type: str) -> Duration:
            secs = after[thread_type] - before.get(thread_type, 0.0)
            return Duration(seconds=secs)

        threads_delta = sum(after.values()) - sum(before.values())
        self.assertGreaterEqual(delta("main"), BURN - TOLERANCE)
        self.assertGreaterEqual(delta("test-thread"), BURN - TOLERANCE)

        # Check that the sum of the per-thread CPU deltas is (more-or-less) the
        # same as the whole process CPU delta.

        # The sum over threads can lag the process total by up to a tick per
        # thread. Allow a couple more ticks for the scrapes themselves.
        tick = 1 / os.sysconf("SC_CLK_TCK")
        slack = (len(os.listdir("/proc/self/task")) + 2) * tick

        self.assertLessEqual(abs(threads_delta - process_delta), slack)
