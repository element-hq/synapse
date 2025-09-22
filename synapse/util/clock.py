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


from typing import (
    Any,
    Callable,
)

import attr
from typing_extensions import ParamSpec

from twisted.internet import defer, task
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IDelayedCall
from twisted.internet.task import LoopingCall

from synapse.logging import context
from synapse.types import ISynapseThreadlessReactor
from synapse.util import log_failure

P = ParamSpec("P")


@attr.s(slots=True)
class Clock:
    """
    A Clock wraps a Twisted reactor and provides utilities on top of it.

    Args:
        reactor: The Twisted reactor to use.
    """

    _reactor: ISynapseThreadlessReactor = attr.ib()

    async def sleep(self, seconds: float) -> None:
        d: defer.Deferred[float] = defer.Deferred()
        # Start task in the `sentinel` logcontext, to avoid leaking the current context
        # into the reactor once it finishes.
        with context.PreserveLoggingContext():
            self._reactor.callLater(seconds, d.callback, seconds)
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

        def wrapped_f(*args: P.args, **kwargs: P.kwargs) -> Deferred:
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
            with context.PreserveLoggingContext(context.LoggingContext("looping_call")):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                return context.run_in_background(f, *args, **kwargs)

        call = task.LoopingCall(wrapped_f, *args, **kwargs)
        call.clock = self._reactor
        # If `now=true`, the function will be called here immediately so we need to be
        # in the sentinel context now.
        #
        # We want to start the task in the `sentinel` logcontext, to avoid leaking the
        # current context into the reactor after the function finishes.
        with context.PreserveLoggingContext():
            d = call.start(msec / 1000.0, now=now)
        d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        return call

    def call_later(
        self, delay: float, callback: Callable, *args: Any, **kwargs: Any
    ) -> IDelayedCall:
        """Call something later

        Note that the function will be called with generic `call_later` logcontext, so
        if it is anything other than a trivial task, you probably want to wrap it in
        `run_as_background_process` to give it more specific label and track metrics.

        Args:
            delay: How long to wait in seconds.
            callback: Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
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
            # restore the `loop_call` context when it's done (because it's trying to
            # adhere to the Synapse logcontext rules.)
            #
            # This also ensures that we return to the `sentinel` context when we exit
            # this function and yield control back to the reactor to avoid leaking the
            # current logcontext to the reactor (which would then get picked up and
            # associated with the next thing the reactor does)
            with context.PreserveLoggingContext(context.LoggingContext("call_later")):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                context.run_in_background(callback, *args, **kwargs)

        return self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)

    def cancel_call_later(self, timer: IDelayedCall, ignore_errs: bool = False) -> None:
        try:
            timer.cancel()
        except Exception:
            if not ignore_errs:
                raise

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

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
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
                context.LoggingContext("call_when_running")
            ):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                context.run_in_background(callback, *args, **kwargs)

        # We can ignore the lint here since this class is the one location
        # callWhenRunning should be called.
        self._reactor.callWhenRunning(wrapped_callback, *args, **kwargs)  # type: ignore[prefer-synapse-clock-call-when-running]

    def add_system_event_trigger(
        self,
        phase: str,
        event_type: str,
        callback: Callable[P, object],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
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
        """

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
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
            with context.PreserveLoggingContext(context.LoggingContext("system_event")):
                # We use `run_in_background` to reset the logcontext after `f` (or the
                # awaitable returned by `f`) completes to avoid leaking the current
                # logcontext to the reactor
                context.run_in_background(callback, *args, **kwargs)

        # We can ignore the lint here since this class is the one location
        # `addSystemEventTrigger` should be called.
        self._reactor.addSystemEventTrigger(
            phase, event_type, wrapped_callback, *args, **kwargs
        )  # type: ignore[prefer-synapse-clock-add-system-event-trigger]
