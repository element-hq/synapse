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

"""Prometheus metrics for CPU time per thread type."""

import logging
import os
import threading
from collections import Counter
from typing import Iterable
from weakref import WeakKeyDictionary

import attr
from prometheus_client.core import REGISTRY, CounterMetricFamily, Metric

from twisted.python.threadpool import ThreadPool

from synapse.metrics._types import Collector

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _ThreadSample:
    """A thread's CPU usage as of one scrape."""

    # When the thread started, in ticks since boot. Tells apart a new thread
    # that reuses the ID of one that exited.
    start_time: int
    # The user and system CPU the thread had used, in ticks.
    utime: int
    stime: int


class ThreadCPUMetrics(Collector):
    """Reports the CPU time used by each type of thread in the process.

    Threads are grouped by what they run: the main (reactor) thread, each
    registered threadpool, the tokio runtime, and `other` for everything else.

    Linux only, as it reads `/proc/self/task`. The kernel only reports CPU time
    for live threads, so the collector sums the CPU time each thread used
    between scrapes. The CPU time a thread uses between the last scrape and
    its exit is not counted.
    """

    def __init__(self) -> None:
        # The kernel reports thread CPU time in clock ticks. This converts
        # ticks to seconds. It is 100 on almost every Linux system.
        self._ticks_per_sec = 100
        try:
            self._ticks_per_sec = os.sysconf("SC_CLK_TCK")
        except (ValueError, TypeError, AttributeError):
            pass

        self._threadpools: "WeakKeyDictionary[ThreadPool, str]" = WeakKeyDictionary()
        self._threads: "WeakKeyDictionary[threading.Thread, str]" = WeakKeyDictionary()

        # Guards the fields below, as the metrics listener may scrape from any
        # thread.
        self._lock = threading.Lock()
        # Thread ID to the thread's sample from the previous scrape.
        self._last_seen: dict[int, _ThreadSample] = {}
        # (thread type, mode) to the total ticks used by threads of that type.
        self._totals: Counter[tuple[str, str]] = Counter()

    def register_threadpool(self, name: str, threadpool: ThreadPool) -> None:
        """Report the threads of `threadpool` as the type `threadpool_<name>`.

        A threadpool keeps the name it was first registered under.
        """
        self._threadpools.setdefault(threadpool, name)

    def register_thread(self, thread_type: str, thread: threading.Thread) -> None:
        """Report `thread` as the type `thread_type`."""
        self._threads[thread] = thread_type

    def _registered_thread_types(self) -> dict[int, str]:
        """Map the native IDs of registered threads and threadpool threads to
        their thread type."""
        types: dict[int, str] = {}
        for thread, thread_type in list(self._threads.items()):
            if thread.native_id is not None and thread.is_alive():
                types[thread.native_id] = thread_type

        for threadpool, name in list(self._threadpools.items()):
            # Twisted never removes exited threads from `threads`. The kernel
            # may have given their IDs to new threads.
            for thread in list(threadpool.threads):
                if thread.native_id is not None and thread.is_alive():
                    types[thread.native_id] = f"threadpool_{name}"

        return types

    def _thread_type(self, tid: int, registered_types: dict[int, str]) -> str:
        """Work out the thread type to report a thread's CPU time under.

        Args:
            tid: The thread's native ID.
            registered_types: The output of `_registered_thread_types`.

        Returns:
            `main`, a registered type, `tokio` or `other`.
        """
        if tid == os.getpid():
            return "main"

        registered_type = registered_types.get(tid)
        if registered_type is not None:
            return registered_type

        try:
            with open(f"/proc/self/task/{tid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            return "other"

        # Tokio names its worker and blocking threads `tokio-rt-worker`, or
        # `tokio-runtime-worker` in older versions.
        if comm.startswith("tokio-"):
            return "tokio"
        return "other"

    def collect(self) -> Iterable[Metric]:
        """Add the CPU time each live thread used since the previous scrape to
        its type's total, and report the totals."""
        try:
            tids = [int(tid) for tid in os.listdir("/proc/self/task")]
        except OSError:
            return

        registered_types = self._registered_thread_types()

        with self._lock:
            last_seen: dict[int, _ThreadSample] = {}
            for tid in tids:
                try:
                    with open(f"/proc/self/task/{tid}/stat") as f:
                        line = f.read()
                except OSError:
                    # The thread exited since we listed it.
                    continue

                # The thread name may contain spaces or brackets, so split after
                # its closing bracket. See `man 5 proc` for the field numbers.
                fields = line.rsplit(")", 1)[1].split()
                utime = int(fields[11])
                stime = int(fields[12])
                start_time = int(fields[19])

                prev = self._last_seen.get(tid)
                if prev is not None and prev.start_time == start_time:
                    prev_utime, prev_stime = prev.utime, prev.stime
                else:
                    prev_utime = prev_stime = 0

                thread_type = self._thread_type(tid, registered_types)
                self._totals[(thread_type, "user")] += utime - prev_utime
                self._totals[(thread_type, "system")] += stime - prev_stime

                last_seen[tid] = _ThreadSample(start_time, utime, stime)

            self._last_seen = last_seen

            # This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
            cpu = CounterMetricFamily(  # type: ignore[missing-server-name-label]
                "synapse_thread_cpu_seconds",
                "CPU time used by each type of thread in the process",
                labels=["thread_type", "mode"],
            )
            for (thread_type, mode), ticks in sorted(self._totals.items()):
                cpu.add_metric([thread_type, mode], ticks / self._ticks_per_sec)

        yield cpu


# This is a process-level metric, so it does not have the `SERVER_NAME_LABEL`.
thread_cpu_metrics = ThreadCPUMetrics()  # type: ignore[missing-server-name-label]
REGISTRY.register(thread_cpu_metrics)
