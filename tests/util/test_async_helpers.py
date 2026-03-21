#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import traceback
from typing import Any, Coroutine, NoReturn, TypeVar

from parameterized import parameterized_class

try:
    from twisted.internet import defer
    from asyncio import CancelledError
    from twisted.internet.defer import Deferred, ensureDeferred
    from twisted.python.failure import Failure
except ImportError:
    pass

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    PreserveLoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.util.async_helpers import (
    AwakenableSleeper,
    ObservableDeferred,
    concurrently_execute,
    delay_cancellation,
    gather_optional_coroutines,
    stop_cancellation,
    timeout_deferred,
)

from tests.server import get_clock
from tests.unittest import TestCase, logcontext_clean

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ObservableDeferredTest(TestCase):
    def test_succeed(self) -> None:
        origin_d: "Deferred[int]" = Deferred()
        observable = ObservableDeferred(origin_d)

        observer1 = observable.observe()
        observer2 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)

        # check the first observer is called first
        def check_called_first(res: int) -> int:
            self.assertFalse(observer2.called)
            return res

        observer1.addBoth(check_called_first)

        # store the results
        results: list[int | None] = [None, None]

        def check_val(res: int, idx: int) -> int:
            results[idx] = res
            return res

        observer1.addCallback(check_val, 0)
        observer2.addCallback(check_val, 1)

        origin_d.callback(123)
        self.assertEqual(results[0], 123, "observer 1 callback result")
        self.assertEqual(results[1], 123, "observer 2 callback result")

    def test_failure(self) -> None:
        origin_d: Deferred = Deferred()
        observable = ObservableDeferred(origin_d, consumeErrors=True)

        observer1 = observable.observe()
        observer2 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)

        # check the first observer is called first
        def check_called_first(res: int) -> int:
            self.assertFalse(observer2.called)
            return res

        observer1.addBoth(check_called_first)

        # store the results
        results: list[Failure | None] = [None, None]

        def check_failure(res: Failure, idx: int) -> None:
            results[idx] = res
            return None

        observer1.addErrback(check_failure, 0)
        observer2.addErrback(check_failure, 1)

        try:
            raise Exception("gah!")
        except Exception as e:
            origin_d.errback(e)
        assert results[0] is not None
        self.assertEqual(str(results[0].value), "gah!", "observer 1 errback result")
        assert results[1] is not None
        self.assertEqual(str(results[1].value), "gah!", "observer 2 errback result")

    def test_cancellation_observer(self) -> None:
        """Test that cancelling an observer does not affect other observers."""
        origin_d: "Deferred[int]" = Deferred()
        observable = ObservableDeferred(origin_d, consumeErrors=True)

        observer1 = observable.observe()
        observer2 = observable.observe()
        observer3 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)
        self.assertFalse(observer3.called)

        # cancel the second observer
        observer2.cancel()
        self.assertFalse(observer1.called)
        self.failureResultOf(observer2, CancelledError)
        self.assertFalse(observer3.called)
        # check that we remove the cancelled observer from the list of observers
        # as a clean up.
        self.assertEqual(len(observable.observers()), 2)
        self.assertNotIn(observer2, observable.observers())

        # other observers resolve as normal
        origin_d.callback(123)
        self.assertEqual(observer1.result, 123, "observer 1 callback result")
        self.assertEqual(observer3.result, 123, "observer 3 callback result")

        # additional observers resolve as normal
        observer4 = observable.observe()
        self.assertEqual(observer4.result, 123, "observer 4 callback result")

    def test_cancellation_observee(self) -> None:
        """Test that cancelling the original deferred cancels all observers."""
        origin_d: "Deferred[int]" = Deferred()
        observable = ObservableDeferred(origin_d, consumeErrors=True)

        observer1 = observable.observe()
        observer2 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)

        # cancel the original deferred
        origin_d.cancel()
        self.failureResultOf(observer1, CancelledError)
        self.failureResultOf(observer2, CancelledError)


import asyncio as _asyncio
import unittest as _stdlib_unittest


import asyncio as _asyncio
import unittest as _stdlib_unittest


class TimeoutTest(_stdlib_unittest.IsolatedAsyncioTestCase):
    """Tests for timeout behavior using asyncio.wait_for."""

    async def test_times_out(self) -> None:
        async def slow():
            await _asyncio.sleep(10)

        with self.assertRaises(_asyncio.TimeoutError):
            await _asyncio.wait_for(slow(), timeout=0.01)

    async def test_timeout_preserves_result(self) -> None:
        async def quick():
            return 42

        result = await _asyncio.wait_for(quick(), timeout=1.0)
        self.assertEqual(result, 42)


class NativeAwakenableSleeperTests(_stdlib_unittest.IsolatedAsyncioTestCase):
    """Tests for AwakenableSleeper (now NativeAwakenableSleeper)."""

    async def test_sleep(self) -> None:
        from synapse.util.async_helpers import AwakenableSleeper

        sleeper = AwakenableSleeper()
        # Should return after timeout
        await sleeper.sleep("name", delay_ms=20)

    async def test_explicit_wake(self) -> None:
        from synapse.util.async_helpers import AwakenableSleeper

        sleeper = AwakenableSleeper()
        woke = False

        async def do_sleep():
            nonlocal woke
            await sleeper.sleep("name", delay_ms=5000)
            woke = True

        task = _asyncio.create_task(do_sleep())
        await _asyncio.sleep(0.01)
        sleeper.wake("name")
        await _asyncio.sleep(0.01)
        self.assertTrue(woke)
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            pass

    async def test_multiple_sleepers_wake(self) -> None:
        from synapse.util.async_helpers import AwakenableSleeper

        sleeper = AwakenableSleeper()
        woke = [False, False]

        async def do_sleep(idx):
            await sleeper.sleep("name", delay_ms=5000)
            woke[idx] = True

        t1 = _asyncio.create_task(do_sleep(0))
        t2 = _asyncio.create_task(do_sleep(1))
        await _asyncio.sleep(0.01)
        sleeper.wake("name")
        await _asyncio.sleep(0.01)
        self.assertTrue(woke[0])
        self.assertTrue(woke[1])
        for t in [t1, t2]:
            t.cancel()
            try:
                await t
            except _asyncio.CancelledError:
                pass

    async def test_multiple_sleepers_timeout(self) -> None:
        from synapse.util.async_helpers import AwakenableSleeper

        sleeper = AwakenableSleeper()
        # Both should return after their timeout
        await sleeper.sleep("name", delay_ms=20)
        await sleeper.sleep("name", delay_ms=20)


class GatherCoroutineTests(_stdlib_unittest.IsolatedAsyncioTestCase):
    """Tests for gather_optional_coroutines."""

    async def test_single(self) -> None:
        from synapse.util.async_helpers import gather_optional_coroutines

        async def coro() -> int:
            return 42

        result = await gather_optional_coroutines(coro())
        self.assertEqual(result, (42,))

    async def test_multiple_resolve(self) -> None:
        from synapse.util.async_helpers import gather_optional_coroutines

        async def coro1() -> int:
            return 1

        async def coro2() -> str:
            return "hello"

        result = await gather_optional_coroutines(coro1(), coro2())
        self.assertEqual(result, (1, "hello"))

    async def test_multiple_fail(self) -> None:
        from synapse.util.async_helpers import gather_optional_coroutines

        async def good() -> int:
            return 1

        async def bad() -> str:
            raise ValueError("test error")

        with self.assertRaises(ValueError):
            await gather_optional_coroutines(good(), bad())

    async def test_with_none(self) -> None:
        from synapse.util.async_helpers import gather_optional_coroutines

        async def coro() -> int:
            return 42

        result = await gather_optional_coroutines(coro(), None)
        self.assertEqual(result, (42, None))


class DelayCancellationTests(_stdlib_unittest.IsolatedAsyncioTestCase):
    """Tests for cancellation shielding using asyncio.shield."""

    async def test_shield_protects_inner(self) -> None:
        inner_done = False

        async def inner():
            nonlocal inner_done
            await _asyncio.sleep(0.02)
            inner_done = True

        task = _asyncio.create_task(inner())
        shielded = _asyncio.shield(task)

        await _asyncio.sleep(0.01)
        shielded.cancel()

        # Inner task should still complete
        await _asyncio.sleep(0.02)
        self.assertTrue(inner_done)
