#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Controllable asyncio event loop for testing.

Provides FakeAsyncioLoop — an asyncio-compatible event loop with manual time
control, replacing Twisted's MemoryReactorClock for testing purposes.
"""

import asyncio
import heapq
import time
from collections import deque
from typing import Any, Callable


class _TimerHandle:
    """A scheduled callback with a specific fire time."""

    __slots__ = ["_when", "_callback", "_args", "_cancelled"]

    def __init__(self, when: float, callback: Callable[..., Any], args: tuple[Any, ...]) -> None:
        self._when = when
        self._callback = callback
        self._args = args
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def when(self) -> float:
        return self._when

    def __lt__(self, other: "_TimerHandle") -> bool:
        return self._when < other._when


class FakeAsyncioLoop:
    """A controllable asyncio-like event loop for testing.

    Provides manual time control via advance(seconds), replacing
    Twisted's MemoryReactorClock. Supports:

    - call_later(delay, callback, *args) — schedule delayed callback
    - call_at(when, callback, *args) — schedule absolute-time callback
    - call_soon(callback, *args) — schedule immediate callback
    - advance(seconds) — advance time and fire due callbacks
    - run_until_complete(coro) — run coroutine to completion
    - time() — return current simulated time
    - create_future() — create a Future bound to this loop
    - create_task(coro) — create a Task bound to this loop

    This is NOT a full asyncio.AbstractEventLoop implementation — it only
    implements the subset needed for Synapse's test infrastructure.
    """

    def __init__(self) -> None:
        self._time: float = 1000000.0  # Start at a large value like MemoryReactorClock
        self._scheduled: list[_TimerHandle] = []  # heap of timer handles
        self._ready: deque[tuple[Callable[..., Any], tuple[Any, ...]]] = deque()
        self._running = False
        self._real_loop: asyncio.AbstractEventLoop | None = None

        # For DNS mocking (replaces ThreadedMemoryReactorClock.lookups)
        self.lookups: dict[str, str] = {}

    def time(self) -> float:
        """Return the current simulated time."""
        return self._time

    def call_later(
        self, delay: float, callback: Callable[..., Any], *args: Any
    ) -> _TimerHandle:
        """Schedule callback after delay seconds."""
        return self.call_at(self._time + delay, callback, *args)

    def call_at(
        self, when: float, callback: Callable[..., Any], *args: Any
    ) -> _TimerHandle:
        """Schedule callback at absolute time."""
        handle = _TimerHandle(when, callback, args)
        heapq.heappush(self._scheduled, handle)
        return handle

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> None:
        """Schedule callback for next iteration."""
        self._ready.append((callback, args))

    def call_soon_threadsafe(self, callback: Callable[..., Any], *args: Any) -> None:
        """Thread-safe version of call_soon."""
        self.call_soon(callback, *args)

    def advance(self, seconds: float) -> None:
        """Advance the simulated clock by `seconds` and fire due callbacks.

        This is the key method for test time control, replacing
        MemoryReactorClock.advance() / reactor.pump().
        """
        end_time = self._time + seconds

        # Process ready callbacks first
        self._run_ready()

        # Fire scheduled callbacks that are due
        while self._scheduled and self._scheduled[0].when() <= end_time:
            handle = heapq.heappop(self._scheduled)
            self._time = handle.when()
            if not handle.cancelled():
                handle._callback(*handle._args)
            # Process any newly ready callbacks
            self._run_ready()

        self._time = end_time

    def _run_ready(self) -> None:
        """Run all callbacks in the ready queue."""
        # Process up to 1000 callbacks to prevent infinite loops
        for _ in range(1000):
            if not self._ready:
                break
            callback, args = self._ready.popleft()
            callback(*args)

    def pump(self, timings: list[float] | None = None) -> None:
        """Compatibility method matching MemoryReactorClock.pump().

        Advances time by each value in timings sequentially.
        """
        if timings is None:
            timings = [0.0]
        for t in timings:
            self.advance(t)

    def run_until_complete(self, coro_or_future: Any) -> Any:
        """Run a coroutine or future to completion.

        Uses a real asyncio event loop under the hood for actual coroutine
        execution, but integrates with our fake time for scheduled callbacks.
        """
        if self._real_loop is None:
            self._real_loop = asyncio.new_event_loop()

        # Patch the loop's time to use our fake time
        original_time = self._real_loop.time
        self._real_loop.time = self.time  # type: ignore[assignment]

        try:
            return self._real_loop.run_until_complete(coro_or_future)
        finally:
            self._real_loop.time = original_time  # type: ignore[assignment]

    def create_future(self) -> "asyncio.Future[Any]":
        """Create a Future bound to the underlying real loop."""
        if self._real_loop is None:
            self._real_loop = asyncio.new_event_loop()
        return self._real_loop.create_future()

    def create_task(self, coro: Any) -> "asyncio.Task[Any]":
        """Create a Task bound to the underlying real loop."""
        if self._real_loop is None:
            self._real_loop = asyncio.new_event_loop()
        return self._real_loop.create_task(coro)

    def close(self) -> None:
        """Clean up the underlying real loop."""
        if self._real_loop is not None:
            self._real_loop.close()
            self._real_loop = None

    # Compatibility attributes
    def seconds(self) -> float:
        """Twisted-compatibility: return current time."""
        return self._time

    def getThreadPool(self) -> Any:
        """Stub for code that asks for a thread pool."""
        return None
