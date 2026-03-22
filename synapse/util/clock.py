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
from functools import wraps
from typing import (
    Any,
    Callable,
)
from weakref import WeakSet

from typing_extensions import ParamSpec
try:
    from zope.interface import implementer
except ImportError:
    pass

try:
    from twisted.internet import defer, task
    from twisted.internet.defer import Deferred
    from twisted.internet.interfaces import IDelayedCall
    from twisted.internet.task import LoopingCall
except ImportError:
    pass

from synapse.logging import context
from synapse.logging.loggers import ExplicitlyConfiguredLogger
from synapse.types import ISynapseThreadlessReactor
from synapse.util import log_failure
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


def _try_wakeup_deferred(d: Deferred) -> None:
    """Try to wake up a deferred, but ignore any exceptions raised by the
    callback. This is useful when we want to wake up a deferred that may have
    already been cancelled, and we don't care about the result."""
    try:
        d.callback(None)
    except Exception:
        pass


class Clock:
    """
    A Clock wraps a Twisted reactor and provides utilities on top of it.

    This clock should be used in place of calls to the base reactor wherever `LoopingCall`
    or `DelayedCall` are made (such as when calling `reactor.callLater`. This is to
    ensure the calls made by this `HomeServer` instance are tracked and can be cleaned
    up during `HomeServer.shutdown()`.

    We enforce usage of this clock instead of using the reactor directly via lints in
    `scripts-dev/mypy_synapse_plugin.py`.


    Args:
        reactor: The Twisted reactor to use.
    """

    _reactor: ISynapseThreadlessReactor

    def __init__(self, reactor: ISynapseThreadlessReactor, server_name: str) -> None:
        self._reactor = reactor
        self._server_name = server_name

        self._delayed_call_id: int = 0
        """Unique ID used to track delayed calls"""

        self._looping_calls: WeakSet[LoopingCall] = WeakSet()
        """List of active looping calls"""

        self._call_id_to_delayed_call: dict[int, IDelayedCall] = {}
        """
        Mapping from unique call ID to delayed call.

        For "performance", this only tracks a subset of delayed calls: those created
        with `call_later` with `call_later_cancel_on_shutdown=True`.
        """

        self._is_shutdown = False
        """Whether shutdown has been requested by the HomeServer"""

    def shutdown(self) -> None:
        self._is_shutdown = True
        self.cancel_all_looping_calls()
        self.cancel_all_delayed_calls()

    async def sleep(self, duration: Duration) -> None:
        from synapse.util.async_helpers import make_awaitable_promise
        d: Any = make_awaitable_promise()
        # Start task in the `sentinel` logcontext, to avoid leaking the current context
        # into the reactor once it finishes.
        with context.PreserveLoggingContext():
            # We can ignore the lint here since this class is the one location callLater should
            # be called.
            self._reactor.callLater(
                duration.as_secs(),
                lambda _: _try_wakeup_deferred(d),
                duration.as_secs(),
            )  # type: ignore[call-later-not-tracked]
            await d

    def time(self) -> float:
        """Returns the current system time in seconds since epoch."""
        return self._reactor.seconds()

    def time_msec(self) -> int:
        """Returns the current system time in milliseconds since epoch."""
        return int(self.time() * 1000)

    def looping_call(
        self,
        f: Callable[P, object],
        duration: Duration,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Call a function repeatedly.

        Waits `duration` initially before calling `f` for the first time.

        If the function given to `looping_call` returns an awaitable/deferred, the next
        call isn't scheduled until after the returned awaitable has finished. We get
        this functionality thanks to this function being a thin wrapper around
        `twisted.internet.task.LoopingCall`.

        Note that the function will be called with generic `looping_call` logcontext, so
        if it is anything other than a trivial task, you probably want to wrap it in
        `run_as_background_process` to give it more specific label and track metrics.

        Args:
            f: The function to call repeatedly.
            duration: How long to wait between calls.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, duration, False, *args, **kwargs)

    def looping_call_now(
        self,
        f: Callable[P, object],
        duration: Duration,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Call a function immediately, and then repeatedly thereafter.

        As with `looping_call`: subsequent calls are not scheduled until after the
        the Awaitable returned by a previous call has finished.

        Note that the function will be called with generic `looping_call` logcontext, so
        if it is anything other than a trivial task, you probably want to wrap it in
        `run_as_background_process` to give it more specific label and track metrics.

        Args:
            f: The function to call repeatedly.
            duration: How long to wait between calls.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, duration, True, *args, **kwargs)

    def _looping_call_common(
        self,
        f: Callable[P, object],
        duration: Duration,
        now: bool,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Common functionality for `looping_call` and `looping_call_now`"""
        instance_id = random_string_insecure_fast(5)

        if self._is_shutdown:
            raise Exception("Cannot start looping call. Clock has been shutdown")

        looping_call_context_string = "looping_call"
        if now:
            looping_call_context_string = "looping_call_now"

        @wraps(f)
        def wrapped_f(*args: P.args, **kwargs: P.kwargs) -> Deferred:
            clock_debug_logger.debug(
                "%s(%s): Executing callback", looping_call_context_string, instance_id
            )

            # Because this is a callback from the reactor, we will be using the
            # `sentinel` log context at this point. We want the function to log with
            # some logcontext as we want to know which server the logs came from.
            #
            # We use `PreserveLoggingContext` to prevent our new `looping_call`
            # logcontext from finishing as soon as we exit this function, in case `f`
            # returns an awaitable/deferred which would continue running and may try to
            # restore the `loop_call` context when it's done (because it's trying to
            # adhere to the Synapse logcontext rules.)
            #
            # This also ensures that we return to the `sentinel` context when we exit
            # this function and yield control back to the reactor to avoid leaking the
            # current logcontext to the reactor (which would then get picked up and
            # associated with the next thing the reactor does)
            with context.PreserveLoggingContext(
                context.LoggingContext(
                    name="looping_call", server_name=self._server_name
                )
            ):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                return context.run_in_background(f, *args, **kwargs)

        # We can ignore the lint here since this is the one location LoopingCall's
        # should be created.
        call = task.LoopingCall(wrapped_f, *args, **kwargs)  # type: ignore[prefer-synapse-clock-looping-call]
        call.clock = self._reactor
        # If `now=true`, the function will be called here immediately so we need to be
        # in the sentinel context now.
        #
        # We want to start the task in the `sentinel` logcontext, to avoid leaking the
        # current context into the reactor after the function finishes.
        with context.PreserveLoggingContext():
            d = call.start(duration.as_secs(), now=now)
        if hasattr(d, 'addErrback'):
            d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        elif hasattr(d, 'add_done_callback'):
            def _log_err(f: Any) -> None:
                exc = f.exception() if hasattr(f, 'exception') else None
                if exc:
                    logger.exception("Looping call died", exc_info=exc)
            d.add_done_callback(_log_err)
        self._looping_calls.add(call)

        clock_debug_logger.debug(
            "%s(%s): Scheduled looping call every %sms later",
            looping_call_context_string,
            instance_id,
            duration.as_millis(),
            # Find out who is scheduling the call which makes it easy to follow in the
            # logs.
            stack_info=True,
        )

        return call

    def cancel_all_looping_calls(self, consumeErrors: bool = True) -> None:
        """
        Stop all running looping calls.

        Args:
            consumeErrors: Whether to re-raise errors encountered when cancelling the
            scheduled call.
        """
        for call in self._looping_calls:
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
    ) -> "DelayedCallWrapper":
        """Call something later

        Note that the function will be called with generic `call_later` logcontext, so
        if it is anything other than a trivial task, you probably want to wrap it in
        `run_as_background_process` to give it more specific label and track metrics.

        Args:
            delay: How long to wait.
            callback: Function to call
            *args: Postional arguments to pass to function.
            call_later_cancel_on_shutdown: Whether this call should be tracked for cleanup during
                shutdown. In general, all calls should be tracked. There may be a use case
                not to track calls with a `timeout` of 0 (or similarly short) since tracking
                them may result in rapid insertions and removals of tracked calls
                unnecessarily. But unless a specific instance of tracking proves to be an
                issue, we can just track all delayed calls.
            **kwargs: Key arguments to pass to function.
        """
        call_id = self._delayed_call_id
        self._delayed_call_id = self._delayed_call_id + 1

        if self._is_shutdown:
            raise Exception("Cannot start delayed call. Clock has been shutdown")

        @wraps(callback)
        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            clock_debug_logger.debug("call_later(%s): Executing callback", call_id)

            # Because this is a callback from the reactor, we will be using the
            # `sentinel` log context at this point. We want the function to log with
            # some logcontext as we want to know which server the logs came from.
            #
            # We use `PreserveLoggingContext` to prevent our new `call_later`
            # logcontext from finishing as soon as we exit this function, in case `f`
            # returns an awaitable/deferred which would continue running and may try to
            # restore the `call_later` context when it's done (because it's trying to
            # adhere to the Synapse logcontext rules.)
            #
            # This also ensures that we return to the `sentinel` context when we exit
            # this function and yield control back to the reactor to avoid leaking the
            # current logcontext to the reactor (which would then get picked up and
            # associated with the next thing the reactor does)
            try:
                with context.PreserveLoggingContext(
                    context.LoggingContext(
                        name="call_later", server_name=self._server_name
                    )
                ):
                    # We use `run_in_background` to reset the logcontext after `f` (or the
                    # awaitable returned by `f`) completes to avoid leaking the current
                    # logcontext to the reactor
                    context.run_in_background(callback, *args, **kwargs)
            finally:
                if call_later_cancel_on_shutdown:
                    # We still want to remove the call from the tracking map. Even if
                    # the callback raises an exception.
                    self._call_id_to_delayed_call.pop(call_id)

        # We can ignore the lint here since this class is the one location callLater should
        # be called.
        call = self._reactor.callLater(
            delay.as_secs(), wrapped_callback, *args, **kwargs
        )  # type: ignore[call-later-not-tracked]

        clock_debug_logger.debug(
            "call_later(%s): Scheduled call for %ss later (tracked for shutdown: %s)",
            call_id,
            delay,
            call_later_cancel_on_shutdown,
            # Find out who is scheduling the call which makes it easy to follow in the
            # logs.
            stack_info=True,
        )

        wrapped_call = DelayedCallWrapper(call, call_id, self)
        if call_later_cancel_on_shutdown:
            self._call_id_to_delayed_call[call_id] = wrapped_call

        return wrapped_call

    def cancel_call_later(
        self, wrapped_call: "DelayedCallWrapper", ignore_errs: bool = False
    ) -> None:
        try:
            clock_debug_logger.debug(
                "cancel_call_later: cancelling scheduled call %s", wrapped_call.call_id
            )
            wrapped_call.delayed_call.cancel()
        except Exception:
            if not ignore_errs:
                raise

    def cancel_all_delayed_calls(self, ignore_errs: bool = True) -> None:
        """
        Stop all scheduled calls that were marked with `cancel_on_shutdown` when they were created.

        Args:
            ignore_errs: Whether to re-raise errors encountered when cancelling the
            scheduled call.
        """
        # We make a copy here since calling `cancel()` on a delayed_call
        # will result in the call removing itself from the map mid-iteration.
        for call_id, call in list(self._call_id_to_delayed_call.items()):
            try:
                clock_debug_logger.debug(
                    "cancel_all_delayed_calls: cancelling scheduled call %s", call_id
                )
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
        """
        Call a function when the reactor is running.

        If the reactor has not started, the callable will be scheduled to run when it
        does start. Otherwise, the callable will be invoked immediately.

        Args:
            callback: Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        instance_id = random_string_insecure_fast(5)

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            clock_debug_logger.debug(
                "call_when_running(%s): Executing callback", instance_id
            )

            # Since this callback can be invoked immediately if the reactor is already
            # running, we can't always assume that we're running in the sentinel
            # logcontext (i.e. we can't assert that we're in the sentinel context like
            # we can in other methods).
            #
            # We will only be running in the sentinel logcontext if the reactor was not
            # running when `call_when_running` was invoked and later starts up.
            #
            # assert context.current_context() is context.SENTINEL_CONTEXT

            # Because this is a callback from the reactor, we will be using the
            # `sentinel` log context at this point. We want the function to log with
            # some logcontext as we want to know which server the logs came from.
            #
            # We use `PreserveLoggingContext` to prevent our new `call_when_running`
            # logcontext from finishing as soon as we exit this function, in case `f`
            # returns an awaitable/deferred which would continue running and may try to
            # restore the `loop_call` context when it's done (because it's trying to
            # adhere to the Synapse logcontext rules.)
            #
            # This also ensures that we return to the `sentinel` context when we exit
            # this function and yield control back to the reactor to avoid leaking the
            # current logcontext to the reactor (which would then get picked up and
            # associated with the next thing the reactor does)
            with context.PreserveLoggingContext(
                context.LoggingContext(
                    name="call_when_running", server_name=self._server_name
                )
            ):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                context.run_in_background(callback, *args, **kwargs)

        # We can ignore the lint here since this class is the one location
        # callWhenRunning should be called.
        self._reactor.callWhenRunning(wrapped_callback, *args, **kwargs)  # type: ignore[prefer-synapse-clock-call-when-running]

        clock_debug_logger.debug(
            "call_when_running(%s): Scheduled call",
            instance_id,
            # Find out who is scheduling the call which makes it easy to follow in the
            # logs.
            stack_info=True,
        )

    def add_system_event_trigger(
        self,
        phase: str,
        event_type: str,
        callback: Callable[P, object],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Any:
        """
        Add a function to be called when a system event occurs.

        Equivalent to `reactor.addSystemEventTrigger` (see the that docstring for more
        details), but ensures that the callback is run in a logging context.

        Args:
            phase: a time to call the event -- either the string 'before', 'after', or
                'during', describing when to call it relative to the event's execution.
            eventType: this is a string describing the type of event.
            callback: Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.

        Returns:
            an ID that can be used to remove this call with `reactor.removeSystemEventTrigger`.
        """
        instance_id = random_string_insecure_fast(5)

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            clock_debug_logger.debug(
                "add_system_event_trigger(%s): Executing %s %s callback",
                instance_id,
                phase,
                event_type,
            )

            # Because this is a callback from the reactor, we will be using the
            # `sentinel` log context at this point. We want the function to log with
            # some logcontext as we want to know which server the logs came from.
            #
            # We use `PreserveLoggingContext` to prevent our new `system_event`
            # logcontext from finishing as soon as we exit this function, in case `f`
            # returns an awaitable/deferred which would continue running and may try to
            # restore the `loop_call` context when it's done (because it's trying to
            # adhere to the Synapse logcontext rules.)
            #
            # This also ensures that we return to the `sentinel` context when we exit
            # this function and yield control back to the reactor to avoid leaking the
            # current logcontext to the reactor (which would then get picked up and
            # associated with the next thing the reactor does)
            with context.PreserveLoggingContext(
                context.LoggingContext(
                    name="system_event", server_name=self._server_name
                )
            ):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                context.run_in_background(callback, *args, **kwargs)

        clock_debug_logger.debug(
            "add_system_event_trigger(%s) for %s %s",
            instance_id,
            phase,
            event_type,
            # Find out who is scheduling the call which makes it easy to follow in the
            # logs.
            stack_info=True,
        )

        # We can ignore the lint here since this class is the one location
        # `addSystemEventTrigger` should be called.
        return self._reactor.addSystemEventTrigger(
            phase, event_type, wrapped_callback, *args, **kwargs
        )  # type: ignore[prefer-synapse-clock-add-system-event-trigger]


@implementer(IDelayedCall)
class DelayedCallWrapper:
    """Wraps an `IDelayedCall` so that we can intercept the call to `cancel()` and
    properly cleanup the delayed call from the tracking map of the `Clock`.

    args:
        delayed_call: The actual `IDelayedCall`
        call_id: Unique identifier for this delayed call
        clock: The clock instance tracking this call
    """

    def __init__(self, delayed_call: IDelayedCall, call_id: int, clock: Clock):
        self.delayed_call = delayed_call
        self.call_id = call_id
        self.clock = clock

    def cancel(self) -> None:
        """Remove the call from the tracking map and propagate the call to the
        underlying delayed_call.
        """
        self.delayed_call.cancel()
        try:
            self.clock._call_id_to_delayed_call.pop(self.call_id)
        except KeyError:
            # If the delayed call isn't being tracked anymore we can just move on.
            pass

    def getTime(self) -> float:
        """Propagate the call to the underlying delayed_call."""
        return self.delayed_call.getTime()

    def delay(self, secondsLater: float) -> None:
        """Propagate the call to the underlying delayed_call."""
        self.delayed_call.delay(secondsLater)

    def reset(self, secondsFromNow: float) -> None:
        """Propagate the call to the underlying delayed_call."""
        self.delayed_call.reset(secondsFromNow)

    def active(self) -> bool:
        """Propagate the call to the underlying delayed_call."""
        return self.delayed_call.active()


# ===========================================================================
# Phase 2: asyncio-native Clock implementation
#
# NativeClock provides the same public interface as Clock but uses asyncio
# primitives instead of Twisted. It is unused until later phases switch
# hs.get_clock() to return a NativeClock.
# ===========================================================================


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
    """asyncio-native equivalent of Clock.

    Provides the same public interface as Clock but uses asyncio primitives
    (asyncio.sleep, loop.call_later, asyncio.create_task) instead of Twisted.

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
        # Pending sleeps: list of (wake_time, future)
        import heapq
        self._fake_time: float = time_mod.time()
        self._pending_sleeps: list[tuple[float, int, asyncio.Future]] = []
        self._sleep_seq = 0  # tiebreaker for heapq when wake_times are equal
        self._use_fake_time = False  # Set to True by tests

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.get_event_loop()
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
        """Advance fake time by seconds, firing any due sleeps.

        Used by tests to control time deterministically.
        """
        self._use_fake_time = True
        self._fake_time += seconds

        # Fire any sleeps that are now due
        while self._pending_sleeps and self._pending_sleeps[0][0] <= self._fake_time:
            import heapq
            _, _, future = heapq.heappop(self._pending_sleeps)
            if not future.done():
                future.set_result(None)

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

        loop = asyncio.get_event_loop()
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
