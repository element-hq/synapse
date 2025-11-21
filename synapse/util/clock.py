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


import logging
from typing import (
    Any,
    Callable,
)

from typing_extensions import ParamSpec
from zope.interface import implementer

from twisted.internet import defer, task
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IDelayedCall
from twisted.internet.task import LoopingCall

from synapse.logging import context
from synapse.types import ISynapseThreadlessReactor
from synapse.util import log_failure
from synapse.util.stringutils import random_string_insecure_fast

P = ParamSpec("P")


logger = logging.getLogger(__name__)


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

        self._looping_calls: list[LoopingCall] = []
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

    async def sleep(self, seconds: float) -> None:
        d: defer.Deferred[float] = defer.Deferred()
        # Start task in the `sentinel` logcontext, to avoid leaking the current context
        # into the reactor once it finishes.
        with context.PreserveLoggingContext():
            # We can ignore the lint here since this class is the one location callLater should
            # be called.
            self._reactor.callLater(seconds, d.callback, seconds)  # type: ignore[call-later-not-tracked]
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
        msec: float,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Call a function repeatedly.

        Waits `msec` initially before calling `f` for the first time.

        If the function given to `looping_call` returns an awaitable/deferred, the next
        call isn't scheduled until after the returned awaitable has finished. We get
        this functionality thanks to this function being a thin wrapper around
        `twisted.internet.task.LoopingCall`.

        Note that the function will be called with generic `looping_call` logcontext, so
        if it is anything other than a trivial task, you probably want to wrap it in
        `run_as_background_process` to give it more specific label and track metrics.

        Args:
            f: The function to call repeatedly.
            msec: How long to wait between calls in milliseconds.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, msec, False, *args, **kwargs)

    def looping_call_now(
        self,
        f: Callable[P, object],
        msec: float,
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
            msec: How long to wait between calls in milliseconds.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, msec, True, *args, **kwargs)

    def _looping_call_common(
        self,
        f: Callable[P, object],
        msec: float,
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

        def wrapped_f(*args: P.args, **kwargs: P.kwargs) -> Deferred:
            logger.debug(
                "%s(%s): Executing callback", looping_call_context_string, instance_id
            )

            assert context.current_context() is context.SENTINEL_CONTEXT, (
                "Expected `looping_call` callback from the reactor to start with the sentinel logcontext "
                f"but saw {context.current_context()}. In other words, another task shouldn't have "
                "leaked their logcontext to us."
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
            d = call.start(msec / 1000.0, now=now)
        d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        self._looping_calls.append(call)

        logger.debug(
            "%s(%s): Scheduled looping call every %sms later",
            looping_call_context_string,
            instance_id,
            msec,
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
        delay: float,
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
            delay: How long to wait in seconds.
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

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            logger.debug("call_later(%s): Executing callback", call_id)

            assert context.current_context() is context.SENTINEL_CONTEXT, (
                "Expected `call_later` callback from the reactor to start with the sentinel logcontext "
                f"but saw {context.current_context()}. In other words, another task shouldn't have "
                "leaked their logcontext to us."
            )

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
        call = self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)  # type: ignore[call-later-not-tracked]

        logger.debug(
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
            logger.debug(
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
                logger.debug(
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
            logger.debug("call_when_running(%s): Executing callback", instance_id)

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

        logger.debug(
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
            logger.debug(
                "add_system_event_trigger(%s): Executing %s %s callback",
                instance_id,
                phase,
                event_type,
            )

            assert context.current_context() is context.SENTINEL_CONTEXT, (
                "Expected `add_system_event_trigger` callback from the reactor to start with the sentinel logcontext "
                f"but saw {context.current_context()}. In other words, another task shouldn't have "
                "leaked their logcontext to us."
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

        logger.debug(
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
