#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
# Copyright (C) 2023-2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Tests for the ReadWriteLock (now NativeReadWriteLock)."""

import asyncio
import unittest

from synapse.util.async_helpers import ReadWriteLock


class ReadWriteLockTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_rwlock(self) -> None:
        rwlock = ReadWriteLock()
        order: list[str] = []

        async def reader(n: str) -> None:
            async with rwlock.read("key"):
                order.append(f"r{n}_start")
                await asyncio.sleep(0.01)
                order.append(f"r{n}_end")

        async def writer(n: str) -> None:
            async with rwlock.write("key"):
                order.append(f"w{n}_start")
                await asyncio.sleep(0.01)
                order.append(f"w{n}_end")

        r1 = asyncio.create_task(reader("1"))
        r2 = asyncio.create_task(reader("2"))
        await asyncio.gather(r1, r2)
        self.assertIn("r1_start", order)
        self.assertIn("r2_start", order)

    async def test_readers_concurrent(self) -> None:
        rwlock = ReadWriteLock()
        concurrent = 0
        max_concurrent = 0

        async def reader() -> None:
            nonlocal concurrent, max_concurrent
            async with rwlock.read("key"):
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                await asyncio.sleep(0.01)
                concurrent -= 1

        tasks = [asyncio.create_task(reader()) for _ in range(3)]
        await asyncio.gather(*tasks)
        self.assertEqual(max_concurrent, 3)

    async def test_writer_exclusive(self) -> None:
        rwlock = ReadWriteLock()
        order: list[str] = []

        async def writer(name: str) -> None:
            async with rwlock.write("key"):
                order.append(f"{name}_start")
                await asyncio.sleep(0.01)
                order.append(f"{name}_end")

        t1 = asyncio.create_task(writer("w1"))
        t2 = asyncio.create_task(writer("w2"))
        await asyncio.gather(t1, t2)
        self.assertEqual(order, ["w1_start", "w1_end", "w2_start", "w2_end"])

    async def test_writer_blocks_reader(self) -> None:
        rwlock = ReadWriteLock()
        order: list[str] = []
        writer_acquired = asyncio.Event()
        writer_release = asyncio.Event()

        async def writer() -> None:
            async with rwlock.write("key"):
                order.append("writer_start")
                writer_acquired.set()
                await writer_release.wait()
                order.append("writer_end")

        async def reader() -> None:
            await writer_acquired.wait()
            async with rwlock.read("key"):
                order.append("reader")

        w_task = asyncio.create_task(writer())
        r_task = asyncio.create_task(reader())
        await asyncio.sleep(0.01)
        writer_release.set()
        await asyncio.gather(w_task, r_task)
        self.assertEqual(order, ["writer_start", "writer_end", "reader"])

    async def test_reader_blocks_writer(self) -> None:
        rwlock = ReadWriteLock()
        order: list[str] = []
        reader_acquired = asyncio.Event()
        reader_release = asyncio.Event()

        async def reader() -> None:
            async with rwlock.read("key"):
                order.append("reader_start")
                reader_acquired.set()
                await reader_release.wait()
                order.append("reader_end")

        async def writer() -> None:
            await reader_acquired.wait()
            async with rwlock.write("key"):
                order.append("writer")

        r_task = asyncio.create_task(reader())
        w_task = asyncio.create_task(writer())
        await asyncio.sleep(0.01)
        reader_release.set()
        await asyncio.gather(r_task, w_task)
        self.assertEqual(order, ["reader_start", "reader_end", "writer"])

    async def test_lock_handoff_to_nonblocking_writer(self) -> None:
        rwlock = ReadWriteLock()
        async with rwlock.write("key"):
            pass
        async with rwlock.write("key"):
            pass
        async with rwlock.read("key"):
            pass

    async def test_cancellation_while_waiting_for_write_lock(self) -> None:
        rwlock = ReadWriteLock()
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with rwlock.write("key"):
                acquired.set()
                await release.wait()

        t1 = asyncio.create_task(holder())
        await acquired.wait()

        async def waiter() -> None:
            async with rwlock.write("key"):
                pass

        t2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        release.set()
        await t1

    async def test_cancellation_while_waiting_for_read_lock(self) -> None:
        rwlock = ReadWriteLock()
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def writer() -> None:
            async with rwlock.write("key"):
                acquired.set()
                await release.wait()

        t1 = asyncio.create_task(writer())
        await acquired.wait()

        async def reader() -> None:
            async with rwlock.read("key"):
                pass

        t2 = asyncio.create_task(reader())
        await asyncio.sleep(0)
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        release.set()
        await t1


if __name__ == "__main__":
    unittest.main()
