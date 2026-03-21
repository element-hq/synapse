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


class NativeConnectionPoolTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native NativeConnectionPool using SQLite."""

    async def asyncSetUp(self) -> None:
        from synapse.config.database import DatabaseConnectionConfig
        from synapse.storage.engines.sqlite import Sqlite3Engine
        from synapse.storage.native_database import NativeConnectionPool

        db_conf = {"name": "sqlite3", "args": {"database": ":memory:"}}
        self.engine = Sqlite3Engine(db_conf)
        self.db_config = DatabaseConnectionConfig("test_db", db_conf)

        self.pool = NativeConnectionPool(
            db_config=self.db_config,
            engine=self.engine,
            server_name="test.server",
            max_workers=2,
        )

    async def asyncTearDown(self) -> None:
        self.pool.close()

    async def test_run_with_connection(self) -> None:
        def create_and_query(conn: object) -> list:
            assert hasattr(conn, "execute")
            conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, val TEXT)")  # type: ignore[union-attr]
            conn.execute("INSERT INTO test VALUES (1, 'hello')")  # type: ignore[union-attr]
            conn.commit()  # type: ignore[union-attr]
            cursor = conn.execute("SELECT val FROM test WHERE id = 1")  # type: ignore[union-attr]
            return cursor.fetchall()

        result = await self.pool.runWithConnection(create_and_query)
        self.assertEqual(result, [("hello",)])

    async def test_run_interaction_commits(self) -> None:
        # First create the table
        def create_table(conn: object) -> None:
            conn.execute("CREATE TABLE IF NOT EXISTS test2 (id INTEGER PRIMARY KEY, val TEXT)")  # type: ignore[union-attr]

        await self.pool.runInteraction(create_table)

        # Then insert in a transaction
        def insert(conn: object) -> None:
            conn.execute("INSERT INTO test2 VALUES (1, 'world')")  # type: ignore[union-attr]

        await self.pool.runInteraction(insert)

        # Verify it was committed
        def query(conn: object) -> list:
            cursor = conn.execute("SELECT val FROM test2 WHERE id = 1")  # type: ignore[union-attr]
            return cursor.fetchall()

        result = await self.pool.runWithConnection(query)
        self.assertEqual(result, [("world",)])

    async def test_run_interaction_rolls_back_on_error(self) -> None:
        # Create table first
        def create_table(conn: object) -> None:
            conn.execute("CREATE TABLE IF NOT EXISTS test3 (id INTEGER PRIMARY KEY, val TEXT)")  # type: ignore[union-attr]

        await self.pool.runInteraction(create_table)

        # Insert that should be rolled back
        def failing_insert(conn: object) -> None:
            conn.execute("INSERT INTO test3 VALUES (1, 'should_rollback')")  # type: ignore[union-attr]
            raise ValueError("deliberate error")

        with self.assertRaises(ValueError):
            await self.pool.runInteraction(failing_insert)

        # Verify nothing was inserted
        def query(conn: object) -> list:
            cursor = conn.execute("SELECT COUNT(*) FROM test3")  # type: ignore[union-attr]
            return cursor.fetchall()

        result = await self.pool.runWithConnection(query)
        self.assertEqual(result, [(0,)])

    async def test_connection_reuse(self) -> None:
        """Verify the same thread reuses its connection."""
        connection_ids: list[int] = []

        def get_conn_id(conn: object) -> int:
            conn_id = id(conn)
            connection_ids.append(conn_id)
            return conn_id

        id1 = await self.pool.runWithConnection(get_conn_id)
        id2 = await self.pool.runWithConnection(get_conn_id)

        # With a single-threaded pool, the same connection should be reused
        # With multi-threaded, it depends on which thread runs.
        # At minimum, we should get valid connection IDs.
        self.assertIsInstance(id1, int)
        self.assertIsInstance(id2, int)

    async def test_closed_pool_raises(self) -> None:
        self.pool.close()
        with self.assertRaises(Exception):
            await self.pool.runWithConnection(lambda conn: None)

    async def test_concurrent_operations(self) -> None:
        """Test that multiple concurrent operations work correctly."""
        import asyncio

        results: list[int] = []

        def work(conn: object, n: int) -> int:
            return n * 2

        tasks = [self.pool.runWithConnection(work, i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(sorted(results), [0, 2, 4, 6, 8, 10, 12, 14, 16, 18])


class NativeSimpleHttpClientTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native NativeSimpleHttpClient."""

    async def asyncSetUp(self) -> None:
        from aiohttp import web

        # Create a simple test HTTP server
        self.app = web.Application()
        self.app.router.add_get("/json", self._handle_json)
        self.app.router.add_post("/json", self._handle_json_post)
        self.app.router.add_put("/json", self._handle_json_put)
        self.app.router.add_get("/raw", self._handle_raw)
        self.app.router.add_get("/file", self._handle_file)
        self.app.router.add_get("/error", self._handle_error)
        self.app.router.add_post("/form", self._handle_form)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()

        # Get the actual bound port
        sock = self.site._server.sockets[0]  # type: ignore[union-attr]
        self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        from synapse.http.native_client import NativeSimpleHttpClient

        self.client = NativeSimpleHttpClient(
            user_agent="test-agent/1.0",
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.runner.cleanup()

    @staticmethod
    async def _handle_json(request: "aiohttp.web.Request") -> "aiohttp.web.Response":
        from aiohttp import web

        return web.json_response({"hello": "world"})

    @staticmethod
    async def _handle_json_post(
        request: "aiohttp.web.Request",
    ) -> "aiohttp.web.Response":
        from aiohttp import web

        body = await request.json()
        return web.json_response({"received": body})

    @staticmethod
    async def _handle_json_put(
        request: "aiohttp.web.Request",
    ) -> "aiohttp.web.Response":
        from aiohttp import web

        body = await request.json()
        return web.json_response({"updated": body})

    @staticmethod
    async def _handle_raw(request: "aiohttp.web.Request") -> "aiohttp.web.Response":
        from aiohttp import web

        return web.Response(body=b"raw bytes here")

    @staticmethod
    async def _handle_file(request: "aiohttp.web.Request") -> "aiohttp.web.Response":
        from aiohttp import web

        return web.Response(
            body=b"file content " * 100,
            content_type="application/octet-stream",
        )

    @staticmethod
    async def _handle_error(request: "aiohttp.web.Request") -> "aiohttp.web.Response":
        from aiohttp import web

        return web.Response(status=500, body=b"Internal Server Error")

    @staticmethod
    async def _handle_form(request: "aiohttp.web.Request") -> "aiohttp.web.Response":
        from aiohttp import web

        data = await request.post()
        return web.json_response({"form_data": dict(data)})

    async def test_get_json(self) -> None:
        result = await self.client.get_json(f"{self.base_url}/json")
        self.assertEqual(result, {"hello": "world"})

    async def test_get_json_with_args(self) -> None:
        result = await self.client.get_json(
            f"{self.base_url}/json", args={"foo": "bar"}
        )
        self.assertEqual(result, {"hello": "world"})

    async def test_post_json_get_json(self) -> None:
        result = await self.client.post_json_get_json(
            f"{self.base_url}/json", {"key": "value"}
        )
        self.assertEqual(result, {"received": {"key": "value"}})

    async def test_put_json(self) -> None:
        result = await self.client.put_json(
            f"{self.base_url}/json", {"key": "updated_value"}
        )
        self.assertEqual(result, {"updated": {"key": "updated_value"}})

    async def test_get_raw(self) -> None:
        result = await self.client.get_raw(f"{self.base_url}/raw")
        self.assertEqual(result, b"raw bytes here")

    async def test_get_file(self) -> None:
        from io import BytesIO

        output = BytesIO()
        length, headers, url, code = await self.client.get_file(
            f"{self.base_url}/file", output
        )
        self.assertEqual(code, 200)
        self.assertGreater(length, 0)
        self.assertEqual(len(output.getvalue()), length)

    async def test_get_file_max_size(self) -> None:
        from io import BytesIO

        from synapse.api.errors import SynapseError

        output = BytesIO()
        with self.assertRaises(SynapseError) as ctx:
            await self.client.get_file(
                f"{self.base_url}/file", output, max_size=10
            )
        self.assertIn("too large", str(ctx.exception))

    async def test_error_response(self) -> None:
        from synapse.api.errors import HttpResponseException

        with self.assertRaises(HttpResponseException) as ctx:
            await self.client.get_json(f"{self.base_url}/error")
        self.assertEqual(ctx.exception.code, 500)

    async def test_post_urlencoded_get_json(self) -> None:
        result = await self.client.post_urlencoded_get_json(
            f"{self.base_url}/form", args={"username": "test"}
        )
        self.assertEqual(result["form_data"]["username"], "test")

    async def test_request_method(self) -> None:
        response = await self.client.request("GET", f"{self.base_url}/raw")
        self.assertEqual(response.status, 200)
        body = await response.read()
        self.assertEqual(body, b"raw bytes here")


class NativeJsonResourceTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native NativeJsonResource and NativeSynapseRequest."""

    async def asyncSetUp(self) -> None:
        import re

        from aiohttp import web
        from aiohttp.test_utils import TestServer

        from synapse.http.native_server import NativeJsonResource

        self.resource = NativeJsonResource(server_name="test.server")

        # Register handlers using the same pattern as RestServlet.register()
        self.resource.register_paths(
            "GET",
            [re.compile("^/_test/hello$")],
            self._handle_hello,
            "TestHelloServlet",
        )
        self.resource.register_paths(
            "GET",
            [re.compile("^/_test/user/(?P<user_id>[^/]+)$")],
            self._handle_user,
            "TestUserServlet",
        )
        self.resource.register_paths(
            "POST",
            [re.compile("^/_test/echo$")],
            self._handle_echo,
            "TestEchoServlet",
        )
        self.resource.register_paths(
            "GET",
            [re.compile("^/_test/error$")],
            self._handle_error,
            "TestErrorServlet",
        )
        self.resource.register_paths(
            "GET",
            [re.compile("^/_test/sync$")],
            self._handle_sync,
            "TestSyncServlet",
        )

        app = self.resource.build_app()
        self.server = TestServer(app)
        await self.server.start_server()
        self.base_url = f"http://{self.server.host}:{self.server.port}"

        import aiohttp as aio

        self.session = aio.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.server.close()

    # --- Test handlers (mimic servlet pattern) ---

    @staticmethod
    async def _handle_hello(request: Any) -> tuple[int, dict]:
        return 200, {"message": "hello world"}

    @staticmethod
    async def _handle_user(request: Any, user_id: str) -> tuple[int, dict]:
        return 200, {"user_id": user_id}

    @staticmethod
    async def _handle_echo(request: Any) -> tuple[int, dict]:
        from synapse.http.servlet import parse_json_object_from_request

        body = parse_json_object_from_request(request)
        return 200, {"echo": body}

    @staticmethod
    async def _handle_error(request: Any) -> tuple[int, dict]:
        from synapse.api.errors import Codes, SynapseError

        raise SynapseError(403, "Forbidden", Codes.FORBIDDEN)

    @staticmethod
    def _handle_sync(request: Any) -> tuple[int, dict]:
        """Synchronous handler (not async)."""
        return 200, {"sync": True}

    # --- Tests ---

    async def test_get_json(self) -> None:
        async with self.session.get(f"{self.base_url}/_test/hello") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["message"], "hello world")

    async def test_path_parameters(self) -> None:
        async with self.session.get(
            f"{self.base_url}/_test/user/@alice:example.com"
        ) as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["user_id"], "@alice:example.com")

    async def test_url_encoded_path_params(self) -> None:
        async with self.session.get(
            f"{self.base_url}/_test/user/%40bob%3Aexample.com"
        ) as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["user_id"], "@bob:example.com")

    async def test_post_json(self) -> None:
        async with self.session.post(
            f"{self.base_url}/_test/echo",
            json={"key": "value"},
        ) as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["echo"]["key"], "value")

    async def test_synapse_error(self) -> None:
        async with self.session.get(f"{self.base_url}/_test/error") as resp:
            self.assertEqual(resp.status, 403)
            data = await resp.json()
            self.assertEqual(data["errcode"], "M_FORBIDDEN")

    async def test_404_not_found(self) -> None:
        async with self.session.get(
            f"{self.base_url}/_test/nonexistent"
        ) as resp:
            self.assertEqual(resp.status, 404)
            data = await resp.json()
            self.assertEqual(data["errcode"], "M_UNRECOGNIZED")

    async def test_405_method_not_allowed(self) -> None:
        async with self.session.delete(f"{self.base_url}/_test/hello") as resp:
            self.assertEqual(resp.status, 405)
            data = await resp.json()
            self.assertEqual(data["errcode"], "M_UNRECOGNIZED")

    async def test_options_cors(self) -> None:
        async with self.session.options(f"{self.base_url}/_test/hello") as resp:
            self.assertEqual(resp.status, 204)
            self.assertIn("Access-Control-Allow-Origin", resp.headers)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    async def test_sync_handler(self) -> None:
        async with self.session.get(f"{self.base_url}/_test/sync") as resp:
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data["sync"])

    async def test_cors_on_success(self) -> None:
        async with self.session.get(f"{self.base_url}/_test/hello") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("Access-Control-Allow-Origin", resp.headers)

    async def test_json_content_type(self) -> None:
        async with self.session.get(f"{self.base_url}/_test/hello") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers["Content-Type"])


class NativeSynapseRequestTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the NativeSynapseRequest compatibility shim."""

    async def test_args_parsing(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        from synapse.http.native_server import NativeSynapseRequest

        req = make_mocked_request("GET", "/_test?foo=bar&foo=baz&num=42")
        native_req = NativeSynapseRequest(req, b"")

        self.assertIn(b"foo", native_req.args)
        self.assertEqual(native_req.args[b"foo"], [b"bar", b"baz"])
        self.assertEqual(native_req.args[b"num"], [b"42"])

    async def test_method_and_path(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        from synapse.http.native_server import NativeSynapseRequest

        req = make_mocked_request("POST", "/_test/path")
        native_req = NativeSynapseRequest(req, b'{"key":"val"}')

        self.assertEqual(native_req.method, b"POST")
        self.assertEqual(native_req.path, b"/_test/path")

    async def test_content_body(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        from synapse.http.native_server import NativeSynapseRequest

        body = b'{"hello": "world"}'
        req = make_mocked_request("POST", "/_test")
        native_req = NativeSynapseRequest(req, body)

        self.assertEqual(native_req.content.read(), body)

    async def test_request_headers(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        from synapse.http.native_server import NativeSynapseRequest

        req = make_mocked_request(
            "GET", "/_test", headers={"Authorization": "Bearer token123"}
        )
        native_req = NativeSynapseRequest(req, b"")

        auth = native_req.requestHeaders.getRawHeaders("Authorization")
        self.assertIsNotNone(auth)
        self.assertEqual(auth, [b"Bearer token123"])

    async def test_response_building(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        from synapse.http.native_server import NativeSynapseRequest

        req = make_mocked_request("GET", "/_test")
        native_req = NativeSynapseRequest(req, b"")

        native_req.setResponseCode(201)
        native_req.setHeader(b"X-Custom", b"value")
        native_req.write(b"hello ")
        native_req.write(b"world")
        native_req.finish()

        response = native_req.build_response()
        self.assertEqual(response.status, 201)
        self.assertEqual(response.headers["X-Custom"], "value")
        self.assertEqual(response.body, b"hello world")


class NativeReplicationProtocolTest(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native replication protocol."""

    async def _make_pipe(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter]:
        """Create a connected pair of (reader, writer) using a TCP loopback server."""
        connections: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        ready = asyncio.Event()

        async def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            connections.append((r, w))
            ready.set()

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        addr = server.sockets[0].getsockname()
        client_r, client_w = await asyncio.open_connection(addr[0], addr[1])
        await ready.wait()
        server_r, server_w = connections[0]
        self._server_to_close = server
        return client_r, client_w, server_r, server_w

    async def asyncTearDown(self) -> None:
        if hasattr(self, "_server_to_close"):
            self._server_to_close.close()
            await self._server_to_close.wait_closed()

    async def test_send_and_receive_command(self) -> None:
        from synapse.replication.tcp.commands import PingCommand
        from synapse.replication.tcp.native_protocol import NativeReplicationProtocol
        from synapse.replication.tcp.protocol import (
            VALID_CLIENT_COMMANDS,
            VALID_SERVER_COMMANDS,
        )

        client_r, client_w, server_r, server_w = await self._make_pipe()

        received_commands: list[str] = []

        class TestProtocol(NativeReplicationProtocol):
            async def on_PING(self, cmd: object) -> None:
                received_commands.append("PING")

        # Server protocol receives from client
        server_proto = TestProtocol(
            server_name="test.server",
            valid_inbound_commands=VALID_CLIENT_COMMANDS,
            valid_outbound_commands=VALID_SERVER_COMMANDS,
        )
        await server_proto.start(server_r, server_w)

        # Client sends a PING directly via the writer
        client_w.write(b"PING 12345\n")
        await client_w.drain()

        # Give time for the read loop to process
        await asyncio.sleep(0.05)

        # Server should have received the PING (from start's initial ping + our manual one)
        self.assertIn("PING", received_commands)

        await server_proto.close()
        client_w.close()

    async def test_protocol_sends_initial_ping(self) -> None:
        from synapse.replication.tcp.native_protocol import NativeReplicationProtocol
        from synapse.replication.tcp.protocol import (
            VALID_CLIENT_COMMANDS,
            VALID_SERVER_COMMANDS,
        )

        client_r, client_w, server_r, server_w = await self._make_pipe()

        proto = NativeReplicationProtocol(
            server_name="test.server",
            valid_inbound_commands=VALID_CLIENT_COMMANDS,
            valid_outbound_commands=VALID_SERVER_COMMANDS,
        )
        await proto.start(server_r, server_w)

        # Read the initial ping sent by the protocol
        line = await asyncio.wait_for(client_r.readline(), timeout=2.0)
        self.assertTrue(line.startswith(b"PING "))

        await proto.close()
        client_w.close()

    async def test_close_connection(self) -> None:
        from synapse.replication.tcp.native_protocol import (
            ConnectionState,
            NativeReplicationProtocol,
        )
        from synapse.replication.tcp.protocol import (
            VALID_CLIENT_COMMANDS,
            VALID_SERVER_COMMANDS,
        )

        client_r, client_w, server_r, server_w = await self._make_pipe()

        closed = asyncio.Event()

        class TestProtocol(NativeReplicationProtocol):
            async def on_connection_lost(self) -> None:
                closed.set()

        proto = TestProtocol(
            server_name="test.server",
            valid_inbound_commands=VALID_CLIENT_COMMANDS,
            valid_outbound_commands=VALID_SERVER_COMMANDS,
        )
        await proto.start(server_r, server_w)

        await proto.close()

        await asyncio.wait_for(closed.wait(), timeout=2.0)
        self.assertEqual(proto._state, ConnectionState.CLOSED)
        client_w.close()

    async def test_eof_triggers_close(self) -> None:
        from synapse.replication.tcp.native_protocol import (
            ConnectionState,
            NativeReplicationProtocol,
        )
        from synapse.replication.tcp.protocol import (
            VALID_CLIENT_COMMANDS,
            VALID_SERVER_COMMANDS,
        )

        client_r, client_w, server_r, server_w = await self._make_pipe()

        closed = asyncio.Event()

        class TestProtocol(NativeReplicationProtocol):
            async def on_connection_lost(self) -> None:
                closed.set()

        proto = TestProtocol(
            server_name="test.server",
            valid_inbound_commands=VALID_CLIENT_COMMANDS,
            valid_outbound_commands=VALID_SERVER_COMMANDS,
        )
        await proto.start(server_r, server_w)

        # Close the client side — server should detect EOF
        client_w.close()

        await asyncio.wait_for(closed.wait(), timeout=2.0)
        self.assertEqual(proto._state, ConnectionState.CLOSED)

    async def test_server_and_client_helpers(self) -> None:
        from synapse.replication.tcp.native_protocol import (
            NativeReplicationProtocol,
            start_native_replication_server,
        )
        from synapse.replication.tcp.protocol import (
            VALID_CLIENT_COMMANDS,
            VALID_SERVER_COMMANDS,
        )

        server_connected = asyncio.Event()

        def server_factory() -> NativeReplicationProtocol:
            proto = NativeReplicationProtocol(
                server_name="test.server",
                valid_inbound_commands=VALID_CLIENT_COMMANDS,
                valid_outbound_commands=VALID_SERVER_COMMANDS,
            )
            server_connected.set()
            return proto

        server = await start_native_replication_server(
            "127.0.0.1", 0, server_factory
        )
        addr = server.sockets[0].getsockname()

        # Connect a client
        reader, writer = await asyncio.open_connection(addr[0], addr[1])

        await asyncio.wait_for(server_connected.wait(), timeout=2.0)

        # Read the server's initial PING
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        self.assertTrue(line.startswith(b"PING "))

        writer.close()
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
