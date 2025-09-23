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
from typing import Callable, Generator, cast

from twisted.internet import defer, reactor as _reactor

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    PreserveLoggingContext,
    _Sentinel,
    current_context,
    make_deferred_yieldable,
    nested_logging_context,
    run_coroutine_in_background,
    run_in_background,
)
from synapse.types import ISynapseReactor
from synapse.util.clock import Clock

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
        with LoggingContext("test"):
            self._check_test_key("test")

    @logcontext_clean
    async def test_sleep(self) -> None:
        """
        Test `Clock.sleep`
        """
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A callback from the reactor should start with the sentinel context. In
                # other words, another task shouldn't have leaked their context to us.
                self._check_test_key("sentinel")

                with LoggingContext("competing"):
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("sentinel")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        reactor.callLater(0, lambda: defer.ensureDeferred(competing_callback()))

        with LoggingContext("foo"):
            await clock.sleep(0)
            self._check_test_key("foo")
            await clock.sleep(0)
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
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `looping_call` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("looping_call")

                with LoggingContext("competing"):
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext("foo"):
            lc = clock.looping_call(
                lambda: defer.ensureDeferred(competing_callback()), 0
            )
            self._check_test_key("foo")
            await clock.sleep(0)
            self._check_test_key("foo")
            await clock.sleep(0)
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
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `looping_call` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("looping_call")

                with LoggingContext("competing"):
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext("foo"):
            lc = clock.looping_call_now(
                lambda: defer.ensureDeferred(competing_callback()), 0
            )
            self._check_test_key("foo")
            await clock.sleep(0)
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
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # A `call_later` callback should have *some* logcontext since we should know
                # which server spawned this loop and which server the logs came from.
                self._check_test_key("call_later")

                with LoggingContext("competing"):
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("call_later")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext("foo"):
            clock.call_later(0, lambda: defer.ensureDeferred(competing_callback()))
            self._check_test_key("foo")
            await clock.sleep(0)
            self._check_test_key("foo")
            await clock.sleep(0)
            self._check_test_key("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_test_key("sentinel")

    async def _test_run_in_background(self, function: Callable[[], object]) -> None:
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        with LoggingContext("foo"):
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
                await clock.sleep(0)
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
            await Clock(reactor).sleep(0)

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
            d = defer.ensureDeferred(Clock(reactor).sleep(0))
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
        clock = Clock(reactor)

        # Sanity check that we start in the sentinel context
        self._check_test_key("sentinel")

        callback_finished = False

        async def competing_callback() -> None:
            nonlocal callback_finished
            try:
                # The callback should have the same logcontext as the caller
                self._check_test_key("foo")

                with LoggingContext("competing"):
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext("foo"):
            run_coroutine_in_background(competing_callback())
            self._check_test_key("foo")
            await clock.sleep(0)
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

                with LoggingContext("competing"):
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

        with LoggingContext("foo"):
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
            reactor.callLater(0, d.callback, None)
            return d

        sentinel_context = current_context()

        with LoggingContext("foo"):
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

        with LoggingContext("foo"):
            d1 = make_deferred_yieldable(_chained_deferred_function())
            # make sure that the context was reset by make_deferred_yieldable
            self.assertIs(current_context(), sentinel_context)

            yield d1

            # now it should be restored
            self._check_test_key("foo")

    @logcontext_clean
    def test_nested_logging_context(self) -> None:
        with LoggingContext("foo"):
            nested_context = nested_logging_context(suffix="bar")
            self.assertEqual(nested_context.name, "foo-bar")


# a function which returns a deferred which has been "called", but
# which had a function which returned another incomplete deferred on
# its callback list, so won't yet call any other new callbacks.
def _chained_deferred_function() -> defer.Deferred:
    d = defer.succeed(None)

    def cb(res: object) -> defer.Deferred:
        d2: defer.Deferred = defer.Deferred()
        reactor.callLater(0, d2.callback, res)
        return d2

    d.addCallback(cb)
    return d
