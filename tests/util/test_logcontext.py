#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2022 The Matrix.org Foundation C.I.C.
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

import logging
from contextlib import contextmanager
from typing import Callable, Generator, cast
from unittest.mock import patch

from twisted.internet import defer, reactor as _reactor

import synapse.logging.context as context_module
from synapse.logging.context import (
    SENTINEL_CONTEXT,
    ContextRequest,
    LoggingContext,
    LoggingContextFilter,
    PreserveLoggingContext,
    _Sentinel,
    current_context,
    make_deferred_yieldable,
    nested_logging_context,
    run_coroutine_in_background,
    run_in_background,
    set_current_context,
)
from synapse.types import ISynapseReactor
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.unittest import logcontext_clean

logger = logging.getLogger(__name__)

reactor = cast(ISynapseReactor, _reactor)


class LoggingContextTestCase(unittest.TestCase):
    def _check_test_key(self, value: str) -> None:
        context = current_context()
        assert isinstance(context, LoggingContext) or isinstance(context, _Sentinel), (
            f"Expected LoggingContext({value}) but saw {context}"
        )
        self.assertEqual(
            str(context), value, f"Expected LoggingContext({value}) but saw {context}"
        )

    @logcontext_clean
    def test_with_context(self) -> None:
        with LoggingContext(name="test", server_name="test_server"):
            self._check_test_key("test")

    @logcontext_clean
    async def test_sleep(self) -> None:
        """
        Test `Clock.sleep`
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A callback from the reactor should start with the sentinel context. In
                # other words, another task shouldn't have leaked their context to us.
                self._check_test_key("sentinel")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("sentinel")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        reactor.callLater(0, lambda: defer.ensureDeferred(competing_callback()))  # type: ignore[call-later-not-tracked]

        with LoggingContext(name="foo", server_name="test_server"):
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_looping_call(self) -> None:
        """
        Test `Clock.looping_call`
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `looping_call` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("looping_call")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            lc = clock.looping_call(
                lambda: defer.ensureDeferred(competing_callback()), Duration(seconds=0)
            )
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

        # Stop the looping call to prevent "Reactor was unclean" errors
        lc.stop()

    @logcontext_clean
    async def test_looping_call_now(self) -> None:
        """
        Test `Clock.looping_call_now`
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `looping_call` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("looping_call")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            lc = clock.looping_call_now(
                lambda: defer.ensureDeferred(competing_callback()), Duration(seconds=0)
            )
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

        # Stop the looping call to prevent "Reactor was unclean" errors
        lc.stop()

    @logcontext_clean
    async def test_call_later(self) -> None:
        """
        Test `Clock.call_later`
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `call_later` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("call_later")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("call_later")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            clock.call_later(
                Duration(seconds=0), lambda: defer.ensureDeferred(competing_callback())
            )
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_deferred_callback_await_in_current_logcontext(self) -> None:
        """
        Test that calling the deferred callback in the current logcontext ("foo") and
        waiting for it to finish in a logcontext blocks works as expected.

        Works because "always await your awaitables".

        Demonstrates one pattern that we can use fix the naive case where we just call
        `d.callback(None)` without anything else. See the *Deferred callbacks* section
        of docs/log_contexts.md for more details.
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The deferred callback should have the same logcontext as the caller
                self._check_test_key("foo")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            d: defer.Deferred[None] = defer.Deferred()
            d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
            self._check_test_key("foo")
            d.callback(None)
            # The fix for the naive case is here (i.e. things don't work correctly if we
            # don't await here).
            #
            # Wait for `d` to finish before continuing so the "main" logcontext is
            # still active. This works because `d` already follows our logcontext
            # rules. If not, we would also have to use `make_deferred_yieldable(d)`.
            await d
            self._check_test_key("foo")

        await clock.sleep(Duration(seconds=0))

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_deferred_callback_preserve_logging_context(self) -> None:
        """
        Test that calling the deferred callback inside `PreserveLoggingContext()` (in
        the sentinel context) works as expected.

        Demonstrates one pattern that we can use fix the naive case where we just call
        `d.callback(None)` without anything else. See the *Deferred callbacks* section
        of docs/log_contexts.md for more details.
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The deferred callback should have the same logcontext as the caller
                self._check_test_key("sentinel")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("sentinel")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            d: defer.Deferred[None] = defer.Deferred()
            d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
            self._check_test_key("foo")
            # The fix for the naive case is here (i.e. things don't work correctly if we
            # don't `PreserveLoggingContext()` here).
            #
            # `PreserveLoggingContext` will reset the logcontext to the sentinel before
            # calling the callback, and restore the "foo" logcontext afterwards before
            # continuing the foo block. This solves the problem because when the
            # "competing" logcontext exits, it will restore the sentinel logcontext
            # which is never finished by its nature, so there is no warning and no
            # leakage into the reactor.
            with PreserveLoggingContext():
                d.callback(None)
            self._check_test_key("foo")

        await clock.sleep(Duration(seconds=0))

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_deferred_callback_fire_and_forget_with_current_context(self) -> None:
        """
        Test that it's possible to call the deferred callback with the current context
        while fire-and-forgetting the callback (no adverse effects like leaking the
        logcontext into the reactor or restarting an already finished logcontext).

        Demonstrates one pattern that we can use fix the naive case where we just call
        `d.callback(None)` without anything else. See the *Deferred callbacks* section
        of docs/log_contexts.md for more details.
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The deferred callback should have the same logcontext as the caller
                self._check_test_key("foo")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        # Part of fix for the naive case is here (i.e. things don't work correctly if we
        # don't `PreserveLoggingContext(...)` here).
        #
        # We can extend the lifetime of the "foo" logcontext is to avoid calling the
        # context manager lifetime methods of `LoggingContext` (`__enter__`/`__exit__`).
        # And we can still set the current logcontext by using `PreserveLoggingContext`
        # and passing in the "foo" logcontext.
        with PreserveLoggingContext(
            LoggingContext(name="foo", server_name="test_server")
        ):
            d: defer.Deferred[None] = defer.Deferred()
            d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
            self._check_test_key("foo")
            # Other part of fix for the naive case is here (i.e. things don't work
            # correctly if we don't `run_in_background(...)` here).
            #
            # `run_in_background(...)` will run the whole lambda in the current
            # logcontext and it handles the magic behind the scenes of a) restoring the
            # calling logcontext before returning to the caller and b) resetting the
            # logcontext to the sentinel after the deferred completes and we yield
            # control back to the reactor to avoid leaking the logcontext into the
            # reactor.
            #
            # We're using a lambda here as a little trick so we can still get everything
            # to run in the "foo" logcontext, but return the deferred `d` itself so that
            # `run_in_background` will wait on that to complete before resetting the
            # logcontext to the sentinel.
            #
            # type-ignore[call-overload]: This appears like a mypy type inference bug. A
            # function that returns a deferred is exactly what `run_in_background`
            # expects.
            #
            # type-ignore[func-returns-value]: This appears like a mypy type inference
            # bug. We're always returning the deferred `d`.
            run_in_background(lambda: (d.callback(None), d)[1])  # type: ignore[call-overload, func-returns-value]
            self._check_test_key("foo")

        await clock.sleep(Duration(seconds=0))

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    async def _test_run_in_background(self, function: Callable[[], object]) -> None:
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        with LoggingContext(name="foo", server_name="test_server"):
            # Fire off the function, but don't wait on it.
            deferred = run_in_background(function)
            self._check_test_key("foo")

            def callback(result: object) -> object:
                nonlocal callback_finished
                callback_finished = True
                # Pass through the result
                return result

            # We `addBoth` because when exceptions happen, we still want to mark the
            # callback as finished so that the test can complete and we see the
            # underlying error.
            deferred.addBoth(callback)

            self._check_test_key("foo")

            # Now wait for the function under test to have run, and check that
            # the logcontext is left in a sane state.
            while not callback_finished:
                await clock.sleep(Duration(seconds=0))
                self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_run_in_background_with_blocking_fn(self) -> None:
        async def blocking_function() -> None:
            # Ignore linter error since we are creating a `Clock` for testing purposes.
            await Clock(reactor, server_name="test_server").sleep(Duration(seconds=0))  # type: ignore[multiple-internal-clocks]

        await self._test_run_in_background(blocking_function)

    @logcontext_clean
    async def test_run_in_background_with_non_blocking_fn(self) -> None:
        @defer.inlineCallbacks
        def nonblocking_function() -> Generator["defer.Deferred[object]", object, None]:
            with PreserveLoggingContext():
                yield defer.succeed(None)

        await self._test_run_in_background(nonblocking_function)

    @logcontext_clean
    async def test_run_in_background_with_chained_deferred(self) -> None:
        # a function which returns a deferred which looks like it has been
        # called, but is actually paused
        def testfunc() -> defer.Deferred:
            return make_deferred_yieldable(_chained_deferred_function())

        await self._test_run_in_background(testfunc)

    @logcontext_clean
    async def test_run_in_background_with_coroutine(self) -> None:
        """
        Test `run_in_background` with a coroutine that yields control back to the
        reactor.

        This will stress the logic around incomplete deferreds in `run_in_background`.
        """

        async def testfunc() -> None:
            self._check_test_key("foo")
            # Ignore linter error since we are creating a `Clock` for testing purposes.
            d = defer.ensureDeferred(
                Clock(reactor, server_name="test_server").sleep(Duration(seconds=0))  # type: ignore[multiple-internal-clocks]
            )
            self.assertIs(current_context(), SENTINEL_CONTEXT)
            await d
            self._check_test_key("foo")

        await self._test_run_in_background(testfunc)

    @logcontext_clean
    async def test_run_in_background_with_nonblocking_coroutine(self) -> None:
        """
        Test `run_in_background` with a "nonblocking" coroutine (never yields control
        back to the reactor).

        This will stress the logic around completed deferreds in `run_in_background`.
        """

        async def testfunc() -> None:
            self._check_test_key("foo")

        await self._test_run_in_background(testfunc)

    @logcontext_clean
    async def test_run_coroutine_in_background(self) -> None:
        """
        Test `run_coroutine_in_background` with a coroutine that yields control back to the
        reactor.

        This will stress the logic around incomplete deferreds in `run_coroutine_in_background`.
        """
        # Ignore linter error since we are creating a `Clock` for testing purposes.
        clock = Clock(reactor, server_name="test_server")  # type: ignore[multiple-internal-clocks]

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The callback should have the same logcontext as the caller
                self._check_test_key("foo")

                with LoggingContext(name="competing", server_name="test_server"):
                    await clock.sleep(Duration(seconds=0))
                    self._check_test_key("competing")

                self._check_test_key("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            run_coroutine_in_background(competing_callback())
            self._check_test_key("foo")
            await clock.sleep(Duration(seconds=0))
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    async def test_run_coroutine_in_background_with_nonblocking_coroutine(self) -> None:
        """
        Test `run_coroutine_in_background` with a "nonblocking" coroutine (never yields control
        back to the reactor).

        This will stress the logic around completed deferreds in `run_coroutine_in_background`.
        """
        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The callback should have the same logcontext as the caller
                self._check_test_key("foo")

                with LoggingContext(name="competing", server_name="test_server"):
                    # We `await` here but there is nothing to wait for here since the
                    # deferred is already complete so we should immediately continue
                    # executing in the same context.
                    await defer.succeed(None)

                    self._check_test_key("competing")

                self._check_test_key("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            run_coroutine_in_background(competing_callback())
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    @logcontext_clean
    @defer.inlineCallbacks
    def test_make_deferred_yieldable(
        self,
    ) -> Generator["defer.Deferred[object]", object, None]:
        # a function which returns an incomplete deferred, but doesn't follow
        # the synapse rules.
        def blocking_function() -> defer.Deferred:
            d: defer.Deferred = defer.Deferred()
            reactor.callLater(0, d.callback, None)  # type: ignore[call-later-not-tracked]
            return d

        sentinel_context = current_context()

        with LoggingContext(name="foo", server_name="test_server"):
            d1 = make_deferred_yieldable(blocking_function())
            # make sure that the context was reset by make_deferred_yieldable
            self.assertIs(current_context(), sentinel_context)

            yield d1

            # now it should be restored
            self._check_test_key("foo")

    @logcontext_clean
    @defer.inlineCallbacks
    def test_make_deferred_yieldable_with_chained_deferreds(
        self,
    ) -> Generator["defer.Deferred[object]", object, None]:
        sentinel_context = current_context()

        with LoggingContext(name="foo", server_name="test_server"):
            d1 = make_deferred_yieldable(_chained_deferred_function())
            # make sure that the context was reset by make_deferred_yieldable
            self.assertIs(current_context(), sentinel_context)

            yield d1

            # now it should be restored
            self._check_test_key("foo")

    @logcontext_clean
    def test_nested_logging_context(self) -> None:
        with LoggingContext(name="foo", server_name="test_server"):
            nested_context = nested_logging_context(suffix="bar")
            self.assertEqual(nested_context.name, "foo-bar")


# A stand-in rusage `(ru_utime, ru_stime)` for exercising the `start()`/`stop()`
# code paths that only check whether an rusage is present, without depending on
# `RUSAGE_THREAD` support or the real value of the thread's CPU clock.
_TRUTHY_RUSAGE = (0.0, 0.0)


@contextmanager
def _capture_logcontext_errors() -> Generator[list[str], None, None]:
    """Capture the messages passed to `logcontext_error` while inside the block.

    `logcontext_error` is looked up as a module global at call time, so patching
    the module attribute intercepts every call site (`LoggingContext`,
    `PreserveLoggingContext`, `run_in_background`, ...).
    """
    messages: list[str] = []
    with patch.object(context_module, "logcontext_error", messages.append):
        yield messages


class LogContextErrorMessageTestCase(unittest.TestCase):
    """Characterization tests pinning the exact `logcontext_error` message shapes
    and the abuse-detection code paths.

    These exist to guard against accidental drift in the switch machinery (which
    lives in Rust: `rust/src/logging/context.rs`): the wording, argument order
    and the conditions that trigger each warning are load-bearing — tests here
    and downstream log scraping depend on them. Messages that interpolate a context via `%r` embed the
    object's `repr()` (id/address), so we reconstruct the expected string from the
    *same* live objects rather than hard-coding an address.
    """

    def setUp(self) -> None:
        # These tests poke the switch primitives directly; make sure we always
        # leave the slot back at the sentinel regardless of what a test does.
        self.assertIs(current_context(), SENTINEL_CONTEXT)
        self.addCleanup(set_current_context, SENTINEL_CONTEXT)

    def test_enter_wrong_previous_context(self) -> None:
        with _capture_logcontext_errors() as messages:
            # `ctx` records the sentinel as its `previous_context` (we are at the
            # sentinel now), but we enter it while a *different* context is active.
            ctx = LoggingContext(name="ctx", server_name="s")
            other = LoggingContext(name="other", server_name="s")
            other.__enter__()

            ctx.__enter__()

            self.assertEqual(
                messages,
                [
                    "Expected previous context %r, found %r"
                    % (ctx.previous_context, other)
                ],
            )

    def test_exit_context_was_lost(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            ctx.__enter__()
            # Simulate the context being lost from under us before exit.
            set_current_context(SENTINEL_CONTEXT)

            ctx.__exit__(None, None, None)

            self.assertEqual(messages, ["Expected logging context ctx was lost"])
        self.assertTrue(ctx.finished)

    def test_exit_found_different_context(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            ctx.__enter__()
            other = LoggingContext(name="other", server_name="s")
            other.__enter__()

            ctx.__exit__(None, None, None)

            self.assertEqual(messages, ["Expected logging context ctx but found other"])

    def test_preserve_logging_context_was_lost(self) -> None:
        with _capture_logcontext_errors() as messages:
            target = LoggingContext(name="target", server_name="s")
            preserve = PreserveLoggingContext(target)
            preserve.__enter__()
            set_current_context(SENTINEL_CONTEXT)

            preserve.__exit__(None, None, None)

            self.assertEqual(messages, ["Expected logging context target was lost"])

    def test_preserve_logging_context_found_different_context(self) -> None:
        with _capture_logcontext_errors() as messages:
            target = LoggingContext(name="target", server_name="s")
            preserve = PreserveLoggingContext(target)
            preserve.__enter__()
            other = LoggingContext(name="other", server_name="s")
            other.__enter__()

            preserve.__exit__(None, None, None)

            self.assertEqual(
                messages, ["Expected logging context target but found other"]
            )

    def test_start_on_different_thread(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            # Pretend the context was created on another OS thread.
            ctx.main_thread += 1

            ctx.start(None)

            self.assertEqual(messages, ["Started logcontext ctx on different thread"])

    def test_stop_on_different_thread(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            ctx.main_thread += 1

            ctx.stop(None)

            self.assertEqual(messages, ["Stopped logcontext ctx on different thread"])

    def test_restart_finished_context(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            ctx.finished = True

            ctx.start(None)

            self.assertEqual(messages, ["Re-starting finished log context ctx"])

    def test_restart_already_active_context(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")
            ctx.start(_TRUTHY_RUSAGE)

            ctx.start(_TRUTHY_RUSAGE)

            self.assertEqual(messages, ["Re-starting already-active log context ctx"])

    def test_stop_without_start(self) -> None:
        with _capture_logcontext_errors() as messages:
            ctx = LoggingContext(name="ctx", server_name="s")

            ctx.stop(_TRUTHY_RUSAGE)

            self.assertEqual(
                messages,
                ["Called stop on logcontext ctx without recording a start rusage"],
            )


class LoggingContextFilterTestCase(unittest.TestCase):
    """Characterization tests for `LoggingContextFilter`, the entire observable
    surface for logging. Pins that it fills record attributes from a real
    logcontext, and — crucially for 3rd-party code — that under the sentinel it
    only sets defaults for attributes that aren't already present (`safe_set`)."""

    def setUp(self) -> None:
        self.assertIs(current_context(), SENTINEL_CONTEXT)
        self.filter = LoggingContextFilter()

    def _make_record(self, **attrs: object) -> logging.LogRecord:
        record = logging.LogRecord("test", logging.INFO, "path", 1, "msg", None, None)
        for key, value in attrs.items():
            setattr(record, key, value)
        return record

    def test_fills_record_from_real_context(self) -> None:
        request = ContextRequest(
            request_id="req-1",
            ip_address="1.2.3.4",
            site_tag="site",
            requester="@u:test",
            authenticated_entity="@u:test",
            method="GET",
            url="/path",
            protocol="1.1",
            user_agent="UA",
        )
        with LoggingContext(name="ctx", server_name="myserver", request=request):
            record = self._make_record()
            self.assertIs(self.filter.filter(record), True)

        # Read via `__dict__` since these attributes are only ever set
        # dynamically (they aren't declared on `logging.LogRecord`).
        attrs = record.__dict__
        self.assertEqual(attrs["server_name"], "myserver")
        # For backwards compatibility, `str(context)` is stored as `request`.
        self.assertEqual(attrs["request"], "ctx")
        self.assertEqual(attrs["ip_address"], "1.2.3.4")
        self.assertEqual(attrs["site_tag"], "site")
        self.assertEqual(attrs["requester"], "@u:test")
        self.assertEqual(attrs["authenticated_entity"], "@u:test")
        self.assertEqual(attrs["method"], "GET")
        self.assertEqual(attrs["url"], "/path")
        self.assertEqual(attrs["protocol"], "1.1")
        self.assertEqual(attrs["user_agent"], "UA")

    def test_sentinel_sets_defaults_when_absent(self) -> None:
        record = self._make_record()
        self.assertIs(self.filter.filter(record), True)

        self.assertEqual(
            record.__dict__["server_name"], "unknown_server_from_sentinel_context"
        )
        self.assertEqual(record.__dict__["request"], "sentinel")

    def test_sentinel_does_not_clobber_preset_attributes(self) -> None:
        # Simulate a 3rd-party log record that already carries its own attributes;
        # under the sentinel the filter must leave them untouched.
        record = self._make_record(
            server_name="third-party-server", request="third-party-request"
        )
        self.assertIs(self.filter.filter(record), True)

        self.assertEqual(record.__dict__["server_name"], "third-party-server")
        self.assertEqual(record.__dict__["request"], "third-party-request")


# a function which returns a deferred which has been "called", but
# which had a function which returned another incomplete deferred on
# its callback list, so won't yet call any other new callbacks.
def _chained_deferred_function() -> defer.Deferred:
    d = defer.succeed(None)

    def cb(res: object) -> defer.Deferred:
        d2: defer.Deferred = defer.Deferred()
        reactor.callLater(0, d2.callback, res)  # type: ignore[call-later-not-tracked]
        return d2

    d.addCallback(cb)
    return d
