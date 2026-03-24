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
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#


import asyncio
import inspect
import logging
import time as time_mod
from typing import (
    Any,
    Callable,
)
from weakref import WeakSet

from typing_extensions import ParamSpec

from synapse.logging import context
from synapse.logging.loggers import ExplicitlyConfiguredLogger
from synapse.util.duration import Duration
from synapse.util.stringutils import random_string_insecure_fast

P = ParamSpec("P")


logger = logging.getLogger(__name__)

original_logger_class = logging.getLoggerClass()
logging.setLoggerClass(ExplicitlyConfiguredLogger)
clock_debug_logger = logging.getLogger("synapse.util.clock.debug")
"""
A logger for debugging what is scheduling calls.

Ideally, these wouldn't be gated behind an `ExplicitlyConfiguredLogger` as including logs
from this logger would be helpful to track when things are being scheduled. However, for
these logs to be meaningful, they need to include a stack trace to show what initiated the
call in the first place.

Since the stack traces can create a lot of noise and make the logs hard to read (unless you're
specifically debugging scheduling issues) we want users to opt-in to seeing these logs. To enable
this, they must explicitly set `synapse.util.clock.debug` in the logging configuration. Note that
this setting won't inherit the log level from the parent logger.
"""
# Restore the original logger class
logging.setLoggerClass(original_logger_class)


class NativeLoopingCall:
    """asyncio-native equivalent of Twisted's LoopingCall.

    Runs a function repeatedly with a fixed interval between completions.
    If the function returns an awaitable, waits for it to complete before
    scheduling the next call (same semantics as LoopingCall).
    """

    def __init__(self, task: "asyncio.Task[None]") -> None:
        self._task = task

    @property
    def running(self) -> bool:
        """Whether the looping call is still running."""
        return not self._task.done()

    def stop(self) -> None:
        """Stop the looping call."""
        self._task.cancel()


class NativeDelayedCallWrapper:
    """asyncio-native equivalent of DelayedCallWrapper.

    Wraps an asyncio.TimerHandle. Since TimerHandle is immutable (no delay/reset),
    delay() and reset() cancel and reschedule.
    """

    def __init__(
        self,
        handle: asyncio.TimerHandle,
        call_id: int,
        clock: "NativeClock",
        loop: asyncio.AbstractEventLoop,
        scheduled_time: float,
        callback: Callable,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._handle = handle
        self.call_id = call_id
        self.clock = clock
        self._loop = loop
        self._scheduled_time = scheduled_time
        self._callback = callback
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        """Cancel the scheduled call."""
        if not self._cancelled:
            self._handle.cancel()
            self._cancelled = True
            self.clock._call_id_to_delayed_call.pop(self.call_id, None)

    def active(self) -> bool:
        """Check if the call is still pending."""
        return not self._cancelled

    def getTime(self) -> float:
        """Return the scheduled execution time."""
        return self._scheduled_time

    def delay(self, secondsLater: float) -> None:
        """Delay execution by N additional seconds."""
        if self._cancelled:
            return
        remaining = self._scheduled_time - self._loop.time()
        new_delay = remaining + secondsLater
        self._handle.cancel()
        self._scheduled_time = self._loop.time() + new_delay
        self._handle = self._loop.call_later(
            new_delay, self._callback, *self._args
        )

    def reset(self, secondsFromNow: float) -> None:
        """Reset to fire secondsFromNow from now."""
        if self._cancelled:
            return
        self._handle.cancel()
        self._scheduled_time = self._loop.time() + secondsFromNow
        self._handle = self._loop.call_later(
            secondsFromNow, self._callback, *self._args
        )


class NativeClock:
    """asyncio-native Clock implementation.

    Provides scheduling utilities (looping calls, delayed calls, sleep) using
    asyncio primitives (asyncio.sleep, loop.call_later, asyncio.create_task).

    Args:
        server_name: The server name for logging context.
    """

    def __init__(self, reactor: Any = None, server_name: str = "") -> None:
        # reactor arg accepted for backward compatibility but ignored
        self._server_name = server_name
        self._delayed_call_id: int = 0
        self._looping_calls: WeakSet[NativeLoopingCall] = WeakSet()
        self._call_id_to_delayed_call: dict[int, NativeDelayedCallWrapper] = {}
        self._is_shutdown = False
        self._shutdown_callbacks: list[tuple[str, str, Callable, tuple, dict]] = []
        # Lazily initialized when first needed
        self._loop: asyncio.AbstractEventLoop | None = None

        # Internal timer system for fake time support.
        import heapq
        self._fake_time: float = time_mod.time()
        self._pending_sleeps: list[tuple[float, int, asyncio.Future]] = []
        self._pending_call_laters: list[tuple[float, int, Callable, tuple, dict]] = []
        self._sleep_seq = 0  # tiebreaker for heapq when wake_times are equal
        self._use_fake_time = False  # Set to True by tests

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def shutdown(self) -> None:
        self._is_shutdown = True
        self.cancel_all_looping_calls()
        self.cancel_all_delayed_calls()

    def time(self) -> float:
        """Returns the current time in seconds since epoch."""
        if self._use_fake_time:
            return self._fake_time
        return time_mod.time()

    def time_msec(self) -> int:
        """Returns the current time in milliseconds since epoch."""
        return int(self.time() * 1000)

    async def sleep(self, duration: Duration) -> None:
        """Sleep for duration, using fake time if enabled."""
        if self._use_fake_time:
            import heapq
            loop = self._get_loop()
            future: asyncio.Future[None] = loop.create_future()
            wake_time = self._fake_time + duration.as_secs()
            self._sleep_seq += 1
            heapq.heappush(self._pending_sleeps, (wake_time, self._sleep_seq, future))
            await future
        else:
            await asyncio.sleep(duration.as_secs())

    def advance(self, seconds: float) -> None:
        """Advance fake time by seconds, firing any due sleeps and call_laters.

        Used by tests to control time deterministically.
        """
        import heapq

        self._use_fake_time = True
        self._fake_time += seconds

        # Fire any sleeps that are now due
        while self._pending_sleeps and self._pending_sleeps[0][0] <= self._fake_time:
            _, _, future = heapq.heappop(self._pending_sleeps)
            if not future.done():
                future.set_result(None)

        # Fire any call_laters that are now due
        while self._pending_call_laters and self._pending_call_laters[0][0] <= self._fake_time:
            _, _, callback, args, kwargs = heapq.heappop(self._pending_call_laters)
            callback(*args, **kwargs)


    def looping_call(
        self,
        f: Callable[P, object],
        duration: Duration,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> NativeLoopingCall:
        """Call a function repeatedly, waiting `duration` before the first call."""
        return self._looping_call_common(f, duration, False, *args, **kwargs)

    def looping_call_now(
        self,
        f: Callable[P, object],
        duration: Duration,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> NativeLoopingCall:
        """Call a function immediately, then repeatedly thereafter."""
        return self._looping_call_common(f, duration, True, *args, **kwargs)

    def _looping_call_common(
        self,
        f: Callable[P, object],
        duration: Duration,
        now: bool,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> NativeLoopingCall:
        if self._is_shutdown:
            raise Exception("Cannot start looping call. Clock has been shutdown")

        instance_id = random_string_insecure_fast(5)
        interval = duration.as_secs()

        async def _loop() -> None:
            if not now:
                await self.sleep(Duration(seconds=interval))

            while True:
                try:
                    clock_debug_logger.debug(
                        "looping_call(%s): Executing callback", instance_id
                    )
                    with context.PreserveLoggingContext(
                        context.LoggingContext(
                            name="looping_call", server_name=self._server_name
                        )
                    ):
                        result = f(*args, **kwargs)
                        if inspect.isawaitable(result):
                            await result
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("Looping call %s died", instance_id)

                await self.sleep(Duration(seconds=interval))

        loop = asyncio.get_running_loop()
        task_obj = loop.create_task(_loop())
        call = NativeLoopingCall(task_obj)
        self._looping_calls.add(call)

        clock_debug_logger.debug(
            "looping_call(%s): Scheduled looping call every %sms",
            instance_id,
            duration.as_millis(),
            stack_info=True,
        )

        return call

    def cancel_all_looping_calls(self, consumeErrors: bool = True) -> None:
        for call in list(self._looping_calls):
            try:
                call.stop()
            except Exception:
                if not consumeErrors:
                    raise
        self._looping_calls.clear()

    def call_later(
        self,
        delay: Duration,
        callback: Callable,
        *args: Any,
        call_later_cancel_on_shutdown: bool = True,
        **kwargs: Any,
    ) -> NativeDelayedCallWrapper:
        import heapq

        call_id = self._delayed_call_id
        self._delayed_call_id += 1

        if self._is_shutdown:
            raise Exception("Cannot start delayed call. Clock has been shutdown")

        loop = self._get_loop()

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            clock_debug_logger.debug("call_later(%s): Executing callback", call_id)
            try:
                with context.PreserveLoggingContext(
                    context.LoggingContext(
                        name="call_later", server_name=self._server_name
                    )
                ):
                    context.run_in_background(callback, *args, **kwargs)
            finally:
                if call_later_cancel_on_shutdown:
                    self._call_id_to_delayed_call.pop(call_id, None)

        if self._use_fake_time:
            # In fake-time mode, store in pending list for advance() to fire.
            scheduled_time = self._fake_time + delay.as_secs()
            self._sleep_seq += 1
            heapq.heappush(
                self._pending_call_laters,
                (scheduled_time, self._sleep_seq, wrapped_callback, args, kwargs),
            )
            # Create a no-op handle for the wrapper
            handle = loop.call_later(86400, lambda: None)  # dummy, never fires
        else:
            scheduled_time = loop.time() + delay.as_secs()
            handle = loop.call_later(delay.as_secs(), wrapped_callback, *args, **kwargs)

        clock_debug_logger.debug(
            "call_later(%s): Scheduled call for %ss later (tracked: %s)",
            call_id,
            delay.as_secs(),
            call_later_cancel_on_shutdown,
            stack_info=True,
        )

        wrapped_call = NativeDelayedCallWrapper(
            handle, call_id, self, loop, scheduled_time, wrapped_callback, args, kwargs
        )
        if call_later_cancel_on_shutdown:
            self._call_id_to_delayed_call[call_id] = wrapped_call

        return wrapped_call

    def cancel_call_later(
        self, wrapped_call: NativeDelayedCallWrapper, ignore_errs: bool = False
    ) -> None:
        try:
            wrapped_call.cancel()
        except Exception:
            if not ignore_errs:
                raise

    def cancel_all_delayed_calls(self, ignore_errs: bool = True) -> None:
        for call_id, call in list(self._call_id_to_delayed_call.items()):
            try:
                call.cancel()
            except Exception:
                if not ignore_errs:
                    raise
        self._call_id_to_delayed_call.clear()

    def call_when_running(
        self,
        callback: Callable[P, object],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        """Call a function on the next event loop iteration."""
        instance_id = random_string_insecure_fast(5)

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            clock_debug_logger.debug(
                "call_when_running(%s): Executing callback", instance_id
            )
            with context.PreserveLoggingContext(
                context.LoggingContext(
                    name="call_when_running", server_name=self._server_name
                )
            ):
                context.run_in_background(callback, *args, **kwargs)

        loop = self._get_loop()
        if kwargs:
            loop.call_soon(lambda: wrapped_callback(*args, **kwargs))
        else:
            loop.call_soon(wrapped_callback, *args)

    def add_system_event_trigger(
        self,
        phase: str,
        event_type: str,
        callback: Callable[P, object],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> int:
        """Store a callback to be invoked during shutdown.

        Returns an ID that could be used to remove the trigger (not currently
        needed by callers, but matches the Twisted API).
        """
        trigger_id = len(self._shutdown_callbacks)
        self._shutdown_callbacks.append(
            (phase, event_type, callback, args, kwargs)
        )
        clock_debug_logger.debug(
            "add_system_event_trigger: registered %s %s callback",
            phase,
            event_type,
            stack_info=True,
        )
        return trigger_id


# Backward-compatible aliases so existing imports continue to work.
Clock = NativeClock
DelayedCallWrapper = NativeDelayedCallWrapper
