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

import twisted.python.failure
from twisted.internet import defer, reactor as _reactor

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    PreserveLoggingContext,
    _Sentinel,
    current_context,
    make_deferred_yieldable,
    nested_logging_context,
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
        with LoggingContext(name="test", server_name="test_server"):
            self._check_test_key("test")

    @logcontext_clean
    async def test_sleep(self) -> None:
        """
        Test `Clock.sleep`
        """
        clock = Clock(reactor, server_name="test_server")

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
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("sentinel")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        reactor.callLater(0, lambda: defer.ensureDeferred(competing_callback()))

        with LoggingContext(name="foo", server_name="test_server"):
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
        clock = Clock(reactor, server_name="test_server")

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
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
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
        clock = Clock(reactor, server_name="test_server")

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
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("looping_call")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
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
        clock = Clock(reactor, server_name="test_server")

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
                    await clock.sleep(0)
                    self._check_test_key("competing")

                self._check_test_key("call_later")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
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

    def _test_run_in_background(self, function: Callable[[], object]) -> defer.Deferred:
        sentinel_context = current_context()

        callback_completed = False

        with LoggingContext(name="foo", server_name="test_server"):
            # fire off function, but don't wait on it.
            d2 = run_in_background(function)

            def cb(res: object) -> object:
                nonlocal callback_completed
                callback_completed = True
                return res

            d2.addCallback(cb)

            self._check_test_key("foo")

        # now wait for the function under test to have run, and check that
        # the logcontext is left in a sane state.
        d2 = defer.Deferred()

        def check_logcontext() -> None:
            if not callback_completed:
                reactor.callLater(0.01, check_logcontext)
                return

            # make sure that the context was reset before it got thrown back
            # into the reactor
            try:
                self.assertIs(current_context(), sentinel_context)
                d2.callback(None)
            except BaseException:
                d2.errback(twisted.python.failure.Failure())

        reactor.callLater(0.01, check_logcontext)

        # test is done once d2 finishes
        return d2

    @logcontext_clean
    def test_run_in_background_with_blocking_fn(self) -> defer.Deferred:
        async def blocking_function() -> None:
            await Clock(reactor, server_name="test_server").sleep(0)

        return self._test_run_in_background(blocking_function)

    @logcontext_clean
    def test_run_in_background_with_non_blocking_fn(self) -> defer.Deferred:
        @defer.inlineCallbacks
        def nonblocking_function() -> Generator["defer.Deferred[object]", object, None]:
            with PreserveLoggingContext():
                yield defer.succeed(None)

        return self._test_run_in_background(nonblocking_function)

    @logcontext_clean
    def test_run_in_background_with_chained_deferred(self) -> defer.Deferred:
        # a function which returns a deferred which looks like it has been
        # called, but is actually paused
        def testfunc() -> defer.Deferred:
            return make_deferred_yieldable(_chained_deferred_function())

        return self._test_run_in_background(testfunc)

    @logcontext_clean
    def test_run_in_background_with_coroutine(self) -> defer.Deferred:
        async def testfunc() -> None:
            self._check_test_key("foo")
            d = defer.ensureDeferred(Clock(reactor, server_name="test_server").sleep(0))
            self.assertIs(current_context(), SENTINEL_CONTEXT)
            await d
            self._check_test_key("foo")

        return self._test_run_in_background(testfunc)

    @logcontext_clean
    def test_run_in_background_with_nonblocking_coroutine(self) -> defer.Deferred:
        async def testfunc() -> None:
            self._check_test_key("foo")

        return self._test_run_in_background(testfunc)

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
