#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from functools import wraps
from unittest.mock import Mock

from parameterized import parameterized

from twisted.internet import defer

from synapse.util.caches.response_cache import ResponseCache, ResponseCacheContext
from synapse.util.cancellation import cancellable
from synapse.util.duration import Duration

from tests.server import get_clock
from tests.unittest import TestCase


class ResponseCacheTestCase(TestCase):
    """
    A TestCase class for ResponseCache.

    The test-case function naming has some logic to it in it's parts, here's some notes about it:
        wait: Denotes tests that have an element of "waiting" before its wrapped result becomes available
              (Generally these just use .delayed_return instead of .instant_return in it's wrapped call.)
        expire: Denotes tests that test expiry after assured existence.
                (These have cache with a short timeout_ms=, shorter than will be tested through advancing the clock)
    """

    def setUp(self) -> None:
        self.reactor, self.clock = get_clock()

    def with_cache(self, name: str, ms: int = 0) -> ResponseCache:
        return ResponseCache(
            clock=self.clock,
            name=name,
            server_name="test_server",
            timeout=Duration(milliseconds=ms),
        )

    @staticmethod
    async def instant_return(o: str) -> str:
        return o

    @cancellable
    async def delayed_return(
        self,
        o: str,
        duration: Duration = Duration(seconds=1),  # noqa
    ) -> str:
        await self.clock.sleep(duration)
        return o

    def test_cache_hit(self) -> None:
        cache = self.with_cache("keeping_cache", ms=9001)

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.instant_return, expected_result)
        )

        self.assertEqual(
            expected_result,
            self.successResultOf(wrap_d),
            "initial wrap result should be the same",
        )

        # a second call should return the result without a call to the wrapped function
        unexpected = Mock(spec=())
        wrap2_d = defer.ensureDeferred(cache.wrap(0, unexpected))
        unexpected.assert_not_called()
        self.assertEqual(
            expected_result,
            self.successResultOf(wrap2_d),
            "cache should still have the result",
        )

    def test_cache_miss(self) -> None:
        cache = self.with_cache("trashing_cache", ms=0)

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.instant_return, expected_result)
        )

        self.assertEqual(
            expected_result,
            self.successResultOf(wrap_d),
            "initial wrap result should be the same",
        )
        self.assertCountEqual([], cache.keys(), "cache should not have the result now")

    def test_cache_expire(self) -> None:
        cache = self.with_cache("short_cache", ms=1000)

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.instant_return, expected_result)
        )

        self.assertEqual(expected_result, self.successResultOf(wrap_d))

        # a second call should return the result without a call to the wrapped function
        unexpected = Mock(spec=())
        wrap2_d = defer.ensureDeferred(cache.wrap(0, unexpected))
        unexpected.assert_not_called()
        self.assertEqual(
            expected_result,
            self.successResultOf(wrap2_d),
            "cache should still have the result",
        )

        # cache eviction timer is handled
        self.reactor.pump((2,))
        self.assertCountEqual([], cache.keys(), "cache should not have the result now")

    def test_cache_wait_hit(self) -> None:
        cache = self.with_cache("neutral_cache")

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.delayed_return, expected_result)
        )

        self.assertNoResult(wrap_d)

        # function wakes up, returns result
        self.reactor.pump((2,))

        self.assertEqual(expected_result, self.successResultOf(wrap_d))

    def test_cache_wait_expire(self) -> None:
        cache = self.with_cache("medium_cache", ms=3000)

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.delayed_return, expected_result)
        )
        self.assertNoResult(wrap_d)

        # stop at 1 second to callback cache eviction callLater at that time, then another to set time at 2
        self.reactor.pump((1, 1))

        self.assertEqual(expected_result, self.successResultOf(wrap_d))

        # a second call should immediately return the result without a call to the
        # wrapped function
        unexpected = Mock(spec=())
        wrap2_d = defer.ensureDeferred(cache.wrap(0, unexpected))
        unexpected.assert_not_called()
        self.assertEqual(
            expected_result,
            self.successResultOf(wrap2_d),
            "cache should still have the result",
        )

        # (1 + 1 + 2) > 3.0, cache eviction timer is handled
        self.reactor.pump((2,))
        self.assertCountEqual([], cache.keys(), "cache should not have the result now")

    @parameterized.expand([(True,), (False,)])
    def test_cache_context_nocache(self, should_cache: bool) -> None:
        """If the callback clears the should_cache bit, the result should not be cached"""
        cache = self.with_cache("medium_cache", ms=3000)

        expected_result = "howdy"

        call_count = 0

        async def non_caching(o: str, cache_context: ResponseCacheContext[int]) -> str:
            nonlocal call_count
            call_count += 1
            await self.clock.sleep(Duration(seconds=1))
            cache_context.should_cache = should_cache
            return o

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, non_caching, expected_result, cache_context=True)
        )
        # there should be no result to start with
        self.assertNoResult(wrap_d)

        # a second call should also return a pending deferred
        wrap2_d = defer.ensureDeferred(
            cache.wrap(0, non_caching, expected_result, cache_context=True)
        )
        self.assertNoResult(wrap2_d)

        # and there should have been exactly one call
        self.assertEqual(call_count, 1)

        # let the call complete
        self.reactor.advance(1)

        # both results should have completed
        self.assertEqual(expected_result, self.successResultOf(wrap_d))
        self.assertEqual(expected_result, self.successResultOf(wrap2_d))

        if should_cache:
            unexpected = Mock(spec=())
            wrap3_d = defer.ensureDeferred(cache.wrap(0, unexpected))
            unexpected.assert_not_called()
            self.assertEqual(
                expected_result,
                self.successResultOf(wrap3_d),
                "cache should still have the result",
            )

        else:
            self.assertCountEqual(
                [], cache.keys(), "cache should not have the result now"
            )

    def test_cache_func_errors(self) -> None:
        """If the callback raises an error, the error should be raised to all
        callers and the result should not be cached"""
        cache = self.with_cache("error_cache", ms=3000)

        expected_error = Exception("oh no")

        async def erring(o: str) -> str:
            await self.clock.sleep(Duration(seconds=1))
            raise expected_error

        wrap_d = defer.ensureDeferred(cache.wrap(0, erring, "ignored"))
        self.assertNoResult(wrap_d)

        # a second call should also return a pending deferred
        wrap2_d = defer.ensureDeferred(cache.wrap(0, erring, "ignored"))
        self.assertNoResult(wrap2_d)

        # let the call complete
        self.reactor.advance(1)

        # both results should have completed with the error
        self.assertFailure(wrap_d, Exception)
        self.assertFailure(wrap2_d, Exception)

    def test_cache_cancel_first_wait(self) -> None:
        """Test that cancellation of the deferred returned by wrap() on the
        first call does not immediately cause a cancellation error to be raised
        when its cancelled and the wrapped function continues execution (unless
        it times out).
        """
        cache = self.with_cache("cancel_cache", ms=3000)

        expected_result = "howdy"

        wrap_d = defer.ensureDeferred(
            cache.wrap(0, self.delayed_return, expected_result)
        )

        # cancel the deferred before it has a chance to return
        wrap_d.cancel()

        # The cancel should be ignored for now, and the inner function should
        # still be running.
        self.assertNoResult(wrap_d)

        # Advance the clock until the inner function should have returned, but
        # not long enough for the cache entry to have expired.
        self.reactor.advance(2)

        # The deferred we're waiting on should now return a cancelled error.
        self.assertFailure(wrap_d, defer.CancelledError)

        # However future callers should get the result.
        wrap_d2 = defer.ensureDeferred(
            cache.wrap(0, self.delayed_return, expected_result)
        )
        self.assertEqual(expected_result, self.successResultOf(wrap_d2))

    def test_cache_cancel_first_wait_expire(self) -> None:
        """Test that cancellation of the deferred returned by wrap() and the
        entry expiring before the wrapped function returns.

        The wrapped function should be cancelled.
        """
        cache = self.with_cache("cancel_expire_cache", ms=300)

        expected_result = "howdy"

        # Wrap the function so that we can keep track of when it completes or
        # errors.
        completed = False
        cancelled = False

        @wraps(self.delayed_return)
        async def wrapped(o: str) -> str:
            nonlocal completed, cancelled

            try:
                return await self.delayed_return(o)
            except defer.CancelledError:
                cancelled = True
                raise
            finally:
                completed = True

        wrap_d = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))

        # cancel the deferred before it has a chance to return
        wrap_d.cancel()

        # The cancel should be ignored for now, and the inner function should
        # still be running.
        self.assertNoResult(wrap_d)
        self.assertFalse(completed, "wrapped function should not have completed yet")

        # Advance the clock until the cache entry should have expired, but not
        # long enough for the inner function to have returned.
        self.reactor.advance(0.7)

        # The deferred we're waiting on should now return a cancelled error.
        self.assertFailure(wrap_d, defer.CancelledError)
        self.assertTrue(completed, "wrapped function should have completed")
        self.assertTrue(cancelled, "wrapped function should have been cancelled")

    def test_cache_cancel_first_wait_other_observers(self) -> None:
        """Test that cancellation of the deferred returned by wrap() does not
        cause a cancellation error to be raised if there are other observers
        still waiting on the result.
        """
        cache = self.with_cache("cancel_other_cache", ms=300)

        expected_result = "howdy"

        # Wrap the function so that we can keep track of when it completes or
        # errors.
        completed = False
        cancelled = False

        @wraps(self.delayed_return)
        async def wrapped(o: str) -> str:
            nonlocal completed, cancelled

            try:
                return await self.delayed_return(o)
            except defer.CancelledError:
                cancelled = True
                raise
            finally:
                completed = True

        wrap_d1 = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))
        wrap_d2 = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))

        # cancel the first deferred before it has a chance to return
        wrap_d1.cancel()

        # The cancel should be ignored for now, and the inner function should
        # still be running.
        self.assertNoResult(wrap_d1)
        self.assertNoResult(wrap_d2)
        self.assertFalse(completed, "wrapped function should not have completed yet")

        # Advance the clock until the cache entry should have expired, but not
        # long enough for the inner function to have returned.
        self.reactor.advance(0.7)

        # Neither deferred should have returned yet, since the inner function
        # should still be running.
        self.assertNoResult(wrap_d1)
        self.assertNoResult(wrap_d2)
        self.assertFalse(completed, "wrapped function should not have completed yet")

        # Now advance the clock until the inner function should have returned.
        self.reactor.advance(2.5)

        # The wrapped function should have completed without cancellation.
        self.assertTrue(completed, "wrapped function should have completed")
        self.assertFalse(cancelled, "wrapped function should not have been cancelled")

        # The first deferred we're waiting on should now return a cancelled error.
        self.assertFailure(wrap_d1, defer.CancelledError)

        # The second deferred should return the result.
        self.assertEqual(expected_result, self.successResultOf(wrap_d2))

    def test_cache_add_and_cancel(self) -> None:
        """Test that waiting on the cache and cancelling repeatedly keeps the
        cache entry alive.
        """
        cache = self.with_cache("cancel_add_cache", ms=300)

        expected_result = "howdy"

        # Wrap the function so that we can keep track of when it completes or
        # errors.
        completed = False
        cancelled = False

        @wraps(self.delayed_return)
        async def wrapped(o: str) -> str:
            nonlocal completed, cancelled

            try:
                return await self.delayed_return(o)
            except defer.CancelledError:
                cancelled = True
                raise
            finally:
                completed = True

        # Repeatedly await for the result and cancel it, which should keep the
        # cache entry alive even though the total time exceeds the cache
        # timeout.
        deferreds = []
        for _ in range(8):
            # Await the deferred.
            wrap_d = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))

            # cancel the deferred before it has a chance to return
            self.reactor.advance(0.05)
            wrap_d.cancel()
            deferreds.append(wrap_d)

            # The cancel should not cause the inner function to be cancelled
            # yet.
            self.assertFalse(
                completed, "wrapped function should not have completed yet"
            )
            self.assertFalse(
                cancelled, "wrapped function should not have been cancelled yet"
            )

            # Advance the clock until the cache entry should have expired, but not
            # long enough for the inner function to have returned.
            self.reactor.advance(0.05)

        # Now advance the clock until the inner function should have returned.
        self.reactor.advance(0.2)

        # All the deferreds we're waiting on should now return a cancelled error.
        for wrap_d in deferreds:
            self.assertFailure(wrap_d, defer.CancelledError)

        # The wrapped function should have completed without cancellation.
        self.assertTrue(completed, "wrapped function should have completed")
        self.assertFalse(cancelled, "wrapped function should not have been cancelled")

        # Querying the cache should return the completed result
        wrap_d = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))
        self.assertEqual(expected_result, self.successResultOf(wrap_d))

    def test_cache_cancel_non_cancellable(self) -> None:
        """Test that cancellation of the deferred returned by wrap() on a
        non-cancellable entry does not cause a cancellation error to be raised
        when it's cancelled and the wrapped function continues execution.
        """
        cache = self.with_cache("cancel_non_cancellable_cache", ms=300)

        expected_result = "howdy"

        # Wrap the function so that we can keep track of when it completes or
        # errors.
        completed = False
        cancelled = False

        async def wrapped(o: str) -> str:
            nonlocal completed, cancelled

            try:
                return await self.delayed_return(o)
            except defer.CancelledError:
                cancelled = True
                raise
            finally:
                completed = True

        wrap_d = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))

        # cancel the deferred before it has a chance to return
        wrap_d.cancel()

        # The cancel should be ignored for now, and the inner function should
        # still be running.
        self.assertNoResult(wrap_d)
        self.assertFalse(completed, "wrapped function should not have completed yet")

        # Advance the clock until the inner function should have returned, but
        # not long enough for the cache entry to have expired.
        self.reactor.advance(2)

        # The deferred we're waiting on should be cancelled, but a new call to
        # the cache should return the result.
        self.assertFailure(wrap_d, defer.CancelledError)
        wrap_d2 = defer.ensureDeferred(cache.wrap(0, wrapped, expected_result))
        self.assertEqual(expected_result, self.successResultOf(wrap_d2))

    def test_cache_cancel_then_error(self) -> None:
        """Test that cancellation of the deferred returned by wrap() that then
        subsequently errors is correctly propagated to a second caller.
        """

        cache = self.with_cache("cancel_then_error_cache", ms=3000)

        expected_error = Exception("oh no")

        # Wrap the function so that we can keep track of when it completes or
        # errors.
        completed = False
        cancelled = False

        @wraps(self.delayed_return)
        async def wrapped(o: str) -> str:
            nonlocal completed, cancelled

            try:
                await self.delayed_return(o)
                raise expected_error
            except defer.CancelledError:
                cancelled = True
                raise
            finally:
                completed = True

        wrap_d1 = defer.ensureDeferred(cache.wrap(0, wrapped, "ignored"))
        wrap_d2 = defer.ensureDeferred(cache.wrap(0, wrapped, "ignored"))

        # cancel the first deferred before it has a chance to return
        wrap_d1.cancel()

        # The cancel should be ignored for now, and the inner function should
        # still be running.
        self.assertNoResult(wrap_d1)
        self.assertNoResult(wrap_d2)
        self.assertFalse(completed, "wrapped function should not have completed yet")

        # Advance the clock until the inner function should have returned.
        self.reactor.advance(2)

        # The wrapped function should have completed with an error without cancellation.
        self.assertTrue(completed, "wrapped function should have completed")
        self.assertFalse(cancelled, "wrapped function should not have been cancelled")

        # The first deferred we're waiting on should now return a cancelled error.
        self.assertFailure(wrap_d1, defer.CancelledError)

        # The second deferred should return the error.
        self.assertFailure(wrap_d2, Exception)
