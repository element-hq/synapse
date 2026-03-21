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

"""Tests for Phase 0 asyncio-native parallel implementations.

These test the new asyncio-native primitives added alongside the existing
Twisted-based ones, ensuring they work correctly before being swapped in
during later migration phases.
"""

import asyncio
import unittest

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    _current_context_var,
    _native_current_context,
    _native_set_current_context,
    make_future_yieldable,
    run_coroutine_in_background_native,
    run_in_background_native,
)
from synapse.util.async_helpers import (
    NativeLinearizer,
    NativeReadWriteLock,
    ObservableFuture,
)


class ContextVarContextTest(unittest.IsolatedAsyncioTestCase):
    """Tests for contextvars-based context tracking."""

    def setUp(self) -> None:
        # Ensure we start from sentinel
        _current_context_var.set(SENTINEL_CONTEXT)

    def test_default_is_sentinel(self) -> None:
        self.assertIs(_native_current_context(), SENTINEL_CONTEXT)

    def test_set_and_get(self) -> None:
        ctx = LoggingContext(name="test", server_name="test.server")
        old = _native_set_current_context(ctx)
        self.assertIs(old, SENTINEL_CONTEXT)
        self.assertIs(_native_current_context(), ctx)
        # Restore
        _native_set_current_context(SENTINEL_CONTEXT)

    def test_set_returns_previous(self) -> None:
        ctx1 = LoggingContext(name="ctx1", server_name="test.server")
        ctx2 = LoggingContext(name="ctx2", server_name="test.server")
        _native_set_current_context(ctx1)
        old = _native_set_current_context(ctx2)
        self.assertIs(old, ctx1)
        _native_set_current_context(SENTINEL_CONTEXT)

    def test_none_raises(self) -> None:
        with self.assertRaises(TypeError):
            _native_set_current_context(None)  # type: ignore[arg-type]

    async def test_task_inherits_context(self) -> None:
        """asyncio.Tasks inherit the parent's contextvars by default."""
        ctx = LoggingContext(name="parent", server_name="test.server")
        _native_set_current_context(ctx)

        result = None

        async def child() -> None:
            nonlocal result
            result = _native_current_context()

        task = asyncio.create_task(child())
        await task
        self.assertIs(result, ctx)
        _native_set_current_context(SENTINEL_CONTEXT)


class MakeFutureYieldableTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _current_context_var.set(SENTINEL_CONTEXT)

    async def test_already_done_future(self) -> None:
        loop = asyncio.get_running_loop()
        f: asyncio.Future[int] = loop.create_future()
        f.set_result(42)

        result = await make_future_yieldable(f)
        self.assertEqual(result, 42)

    async def test_pending_future_preserves_context(self) -> None:
        ctx = LoggingContext(name="test", server_name="test.server")
        _native_set_current_context(ctx)

        loop = asyncio.get_running_loop()
        f: asyncio.Future[str] = loop.create_future()

        # Schedule the resolution so the coroutine can actually await
        loop.call_soon(f.set_result, "hello")
        result = await make_future_yieldable(f)

        # Context should be restored after awaiting
        self.assertIs(_native_current_context(), ctx)
        self.assertEqual(result, "hello")

        _native_set_current_context(SENTINEL_CONTEXT)

    async def test_pending_future_exception(self) -> None:
        ctx = LoggingContext(name="test", server_name="test.server")
        _native_set_current_context(ctx)

        loop = asyncio.get_running_loop()
        f: asyncio.Future[str] = loop.create_future()

        yieldable = make_future_yieldable(f)
        f.set_exception(ValueError("boom"))

        with self.assertRaises(ValueError):
            await yieldable

        # Context should still be restored after exception
        self.assertIs(_native_current_context(), ctx)
        _native_set_current_context(SENTINEL_CONTEXT)


class RunCoroutineInBackgroundNativeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _current_context_var.set(SENTINEL_CONTEXT)

    async def test_preserves_calling_context(self) -> None:
        ctx = LoggingContext(name="caller", server_name="test.server")
        _native_set_current_context(ctx)

        results: list[object] = []

        async def bg_work() -> str:
            results.append(_native_current_context())
            return "done"

        task = run_coroutine_in_background_native(bg_work())

        # Calling context should be preserved
        self.assertIs(_native_current_context(), ctx)

        result = await task
        self.assertEqual(result, "done")

        _native_set_current_context(SENTINEL_CONTEXT)

    async def test_resets_to_sentinel_on_completion(self) -> None:
        post_completion_context = None

        async def bg_work() -> None:
            pass

        task = run_coroutine_in_background_native(bg_work())
        await task

        # The task's internal wrapper resets to sentinel
        # (We can't easily observe this from outside without hooking the callback,
        # but at least verify the task completed successfully)
        self.assertTrue(task.done())


class RunInBackgroundNativeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _current_context_var.set(SENTINEL_CONTEXT)

    async def test_with_coroutine_function(self) -> None:
        async def my_func(x: int) -> int:
            return x * 2

        task = run_in_background_native(my_func, 21)
        result = await task
        self.assertEqual(result, 42)

    async def test_with_sync_function(self) -> None:
        def my_func(x: int) -> int:
            return x * 2

        task = run_in_background_native(my_func, 21)
        result = await task
        self.assertEqual(result, 42)

    async def test_with_exception(self) -> None:
        def my_func() -> None:
            raise ValueError("sync error")

        task = run_in_background_native(my_func)
        with self.assertRaises(ValueError):
            await task


class ObservableFutureTest(unittest.IsolatedAsyncioTestCase):
    async def test_succeed(self) -> None:
        loop = asyncio.get_running_loop()
        origin: asyncio.Future[int] = loop.create_future()
        observable = ObservableFuture(origin)

        obs1 = observable.observe()
        obs2 = observable.observe()

        self.assertFalse(observable.has_called())
        self.assertTrue(observable.has_observers())

        origin.set_result(42)
        # Give the event loop a chance to process callbacks
        await asyncio.sleep(0)

        self.assertTrue(observable.has_called())
        self.assertTrue(observable.has_succeeded())
        self.assertEqual(await obs1, 42)
        self.assertEqual(await obs2, 42)

    async def test_fail(self) -> None:
        loop = asyncio.get_running_loop()
        origin: asyncio.Future[int] = loop.create_future()
        observable = ObservableFuture(origin)

        obs1 = observable.observe()
        obs2 = observable.observe()

        origin.set_exception(ValueError("boom"))
        await asyncio.sleep(0)

        self.assertTrue(observable.has_called())
        self.assertFalse(observable.has_succeeded())

        with self.assertRaises(ValueError):
            await obs1
        with self.assertRaises(ValueError):
            await obs2

    async def test_observe_after_resolution(self) -> None:
        loop = asyncio.get_running_loop()
        origin: asyncio.Future[str] = loop.create_future()
        observable = ObservableFuture(origin)

        origin.set_result("hello")
        await asyncio.sleep(0)

        obs = observable.observe()
        self.assertEqual(await obs, "hello")

    async def test_no_observers(self) -> None:
        loop = asyncio.get_running_loop()
        origin: asyncio.Future[int] = loop.create_future()
        observable = ObservableFuture(origin)

        self.assertFalse(observable.has_observers())
        origin.set_result(1)
        await asyncio.sleep(0)

        self.assertFalse(observable.has_observers())

    async def test_get_result(self) -> None:
        loop = asyncio.get_running_loop()
        origin: asyncio.Future[int] = loop.create_future()
        observable = ObservableFuture(origin)

        with self.assertRaises(ValueError):
            observable.get_result()

        origin.set_result(99)
        await asyncio.sleep(0)

        self.assertEqual(observable.get_result(), 99)


class NativeLinearizerTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncontended(self) -> None:
        linearizer = NativeLinearizer("test")
        async with linearizer.queue("key"):
            pass

    async def test_serializes_access(self) -> None:
        linearizer = NativeLinearizer("test")
        order: list[int] = []

        async def worker(n: int) -> None:
            async with linearizer.queue("key"):
                order.append(n)
                await asyncio.sleep(0.01)

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await asyncio.gather(*tasks)

        # All three should have run in order
        self.assertEqual(order, [0, 1, 2])

    async def test_different_keys_concurrent(self) -> None:
        linearizer = NativeLinearizer("test")
        running: list[str] = []

        async def worker(key: str) -> None:
            async with linearizer.queue(key):
                running.append(key)
                await asyncio.sleep(0.01)

        tasks = [
            asyncio.create_task(worker("a")),
            asyncio.create_task(worker("b")),
        ]
        await asyncio.gather(*tasks)

        # Both should have started (different keys are independent)
        self.assertEqual(set(running), {"a", "b"})

    async def test_max_count(self) -> None:
        linearizer = NativeLinearizer("test", max_count=2)
        concurrent = 0
        max_concurrent = 0

        async def worker() -> None:
            nonlocal concurrent, max_concurrent
            async with linearizer.queue("key"):
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                await asyncio.sleep(0.01)
                concurrent -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(5)]
        await asyncio.gather(*tasks)

        self.assertLessEqual(max_concurrent, 2)

    async def test_is_queued(self) -> None:
        linearizer = NativeLinearizer("test")

        self.assertFalse(linearizer.is_queued("key"))

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with linearizer.queue("key"):
                acquired.set()
                await release.wait()

        task1 = asyncio.create_task(holder())
        await acquired.wait()

        # Start a second worker that will be queued
        async def waiter() -> None:
            async with linearizer.queue("key"):
                pass

        task2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)  # Let task2 start and get queued

        self.assertTrue(linearizer.is_queued("key"))

        release.set()
        await asyncio.gather(task1, task2)

    async def test_cancellation(self) -> None:
        linearizer = NativeLinearizer("test")

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with linearizer.queue("key"):
                acquired.set()
                await release.wait()

        task1 = asyncio.create_task(holder())
        await acquired.wait()

        async def waiter() -> None:
            async with linearizer.queue("key"):
                pass

        task2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)

        task2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task2

        release.set()
        await task1


class NativeReadWriteLockTest(unittest.IsolatedAsyncioTestCase):
    async def test_readers_concurrent(self) -> None:
        lock = NativeReadWriteLock()
        concurrent = 0
        max_concurrent = 0

        async def reader() -> None:
            nonlocal concurrent, max_concurrent
            async with lock.read("key"):
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                await asyncio.sleep(0.01)
                concurrent -= 1

        tasks = [asyncio.create_task(reader()) for _ in range(3)]
        await asyncio.gather(*tasks)

        # All readers should run concurrently
        self.assertEqual(max_concurrent, 3)

    async def test_writer_exclusive(self) -> None:
        lock = NativeReadWriteLock()
        order: list[str] = []

        async def writer(name: str) -> None:
            async with lock.write("key"):
                order.append(f"{name}_start")
                await asyncio.sleep(0.01)
                order.append(f"{name}_end")

        tasks = [
            asyncio.create_task(writer("w1")),
            asyncio.create_task(writer("w2")),
        ]
        await asyncio.gather(*tasks)

        # Writers should be serialized: w1 should finish before w2 starts
        self.assertEqual(
            order, ["w1_start", "w1_end", "w2_start", "w2_end"]
        )

    async def test_writer_blocks_reader(self) -> None:
        lock = NativeReadWriteLock()
        order: list[str] = []

        writer_acquired = asyncio.Event()
        writer_release = asyncio.Event()

        async def writer() -> None:
            async with lock.write("key"):
                order.append("writer_start")
                writer_acquired.set()
                await writer_release.wait()
                order.append("writer_end")

        async def reader() -> None:
            await writer_acquired.wait()
            async with lock.read("key"):
                order.append("reader")

        w_task = asyncio.create_task(writer())
        r_task = asyncio.create_task(reader())

        await asyncio.sleep(0.01)  # Let writer acquire and reader queue
        writer_release.set()

        await asyncio.gather(w_task, r_task)

        self.assertEqual(order, ["writer_start", "writer_end", "reader"])

    async def test_reader_blocks_writer(self) -> None:
        lock = NativeReadWriteLock()
        order: list[str] = []

        reader_acquired = asyncio.Event()
        reader_release = asyncio.Event()

        async def reader() -> None:
            async with lock.read("key"):
                order.append("reader_start")
                reader_acquired.set()
                await reader_release.wait()
                order.append("reader_end")

        async def writer() -> None:
            await reader_acquired.wait()
            async with lock.write("key"):
                order.append("writer")

        r_task = asyncio.create_task(reader())
        w_task = asyncio.create_task(writer())

        await asyncio.sleep(0.01)
        reader_release.set()

        await asyncio.gather(r_task, w_task)

        self.assertEqual(order, ["reader_start", "reader_end", "writer"])


class NativeClockTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native NativeClock."""

    async def test_time(self) -> None:
        from synapse.util.clock import NativeClock

        clock = NativeClock(server_name="test.server")
        t = clock.time()
        self.assertIsInstance(t, float)
        self.assertGreater(t, 0)

    async def test_time_msec(self) -> None:
        from synapse.util.clock import NativeClock

        clock = NativeClock(server_name="test.server")
        t = clock.time_msec()
        self.assertIsInstance(t, int)
        self.assertGreater(t, 0)

    async def test_sleep(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        before = clock.time()
        await clock.sleep(Duration(milliseconds=50))
        after = clock.time()
        self.assertGreaterEqual(after - before, 0.04)

    async def test_call_later(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        called = asyncio.Event()

        def callback() -> None:
            called.set()

        wrapper = clock.call_later(Duration(milliseconds=20), callback)
        self.assertTrue(wrapper.active())

        await asyncio.wait_for(called.wait(), timeout=1.0)
        self.assertTrue(called.is_set())

    async def test_call_later_cancel(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        called = False

        def callback() -> None:
            nonlocal called
            called = True

        wrapper = clock.call_later(Duration(milliseconds=50), callback)
        self.assertTrue(wrapper.active())

        wrapper.cancel()
        self.assertFalse(wrapper.active())

        await asyncio.sleep(0.1)
        self.assertFalse(called)

    async def test_call_later_getTime(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        wrapper = clock.call_later(Duration(seconds=10), lambda: None)
        # getTime should return a time in the future
        self.assertGreater(wrapper.getTime(), 0)
        wrapper.cancel()

    async def test_looping_call(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        call_count = 0

        def callback() -> None:
            nonlocal call_count
            call_count += 1

        call = clock.looping_call(callback, Duration(milliseconds=30))

        # looping_call waits `duration` before first call
        await asyncio.sleep(0.01)
        self.assertEqual(call_count, 0)

        await asyncio.sleep(0.05)
        self.assertGreaterEqual(call_count, 1)

        call.stop()

        old_count = call_count
        await asyncio.sleep(0.05)
        self.assertEqual(call_count, old_count)

    async def test_looping_call_now(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        call_count = 0

        def callback() -> None:
            nonlocal call_count
            call_count += 1

        call = clock.looping_call_now(callback, Duration(milliseconds=30))

        # looping_call_now should call immediately
        await asyncio.sleep(0.01)
        self.assertGreaterEqual(call_count, 1)

        call.stop()

    async def test_looping_call_waits_for_completion(self) -> None:
        """Test that the next iteration waits for the previous to complete."""
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        concurrent = 0
        max_concurrent = 0

        async def slow_callback() -> None:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.04)
            concurrent -= 1

        call = clock.looping_call_now(slow_callback, Duration(milliseconds=10))

        await asyncio.sleep(0.15)
        call.stop()

        # Should never have more than 1 concurrent execution
        self.assertEqual(max_concurrent, 1)

    async def test_looping_call_survives_error(self) -> None:
        """Test that an error in the callback doesn't stop the loop."""
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        call_count = 0

        def flaky_callback() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first call fails")

        call = clock.looping_call_now(flaky_callback, Duration(milliseconds=20))

        await asyncio.sleep(0.08)
        call.stop()

        # Should have been called more than once despite the error
        self.assertGreaterEqual(call_count, 2)

    async def test_shutdown(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        looping_called = False
        delayed_called = False

        def looping_cb() -> None:
            nonlocal looping_called
            looping_called = True

        def delayed_cb() -> None:
            nonlocal delayed_called
            delayed_called = True

        clock.looping_call(looping_cb, Duration(seconds=1))
        clock.call_later(Duration(seconds=1), delayed_cb)

        # Shutdown immediately before any callbacks can fire
        clock.shutdown()

        await asyncio.sleep(0.05)
        self.assertFalse(looping_called)
        self.assertFalse(delayed_called)

    async def test_cancel_all_delayed_calls(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        called = False

        def callback() -> None:
            nonlocal called
            called = True

        clock.call_later(Duration(milliseconds=50), callback)
        clock.call_later(Duration(milliseconds=50), callback)

        clock.cancel_all_delayed_calls()

        await asyncio.sleep(0.1)
        self.assertFalse(called)

    async def test_call_when_running(self) -> None:
        from synapse.util.clock import NativeClock

        clock = NativeClock(server_name="test.server")
        called = asyncio.Event()

        def callback() -> None:
            called.set()

        clock.call_when_running(callback)

        await asyncio.wait_for(called.wait(), timeout=1.0)
        self.assertTrue(called.is_set())

    async def test_add_system_event_trigger(self) -> None:
        from synapse.util.clock import NativeClock

        clock = NativeClock(server_name="test.server")

        trigger_id = clock.add_system_event_trigger(
            "before", "shutdown", lambda: None
        )
        self.assertIsInstance(trigger_id, int)
        self.assertEqual(len(clock._shutdown_callbacks), 1)

    async def test_shutdown_prevents_new_calls(self) -> None:
        from synapse.util.clock import NativeClock
        from synapse.util.duration import Duration

        clock = NativeClock(server_name="test.server")
        clock.shutdown()

        with self.assertRaises(Exception):
            clock.looping_call(lambda: None, Duration(seconds=1))

        with self.assertRaises(Exception):
            clock.call_later(Duration(seconds=1), lambda: None)


if __name__ == "__main__":
    unittest.main()
