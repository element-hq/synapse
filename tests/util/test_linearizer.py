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

"""Tests for the Linearizer (now NativeLinearizer)."""

import asyncio
import unittest

from synapse.util.async_helpers import Linearizer


class LinearizerTestCase(unittest.IsolatedAsyncioTestCase):
    """Tests for the asyncio-native Linearizer."""

    async def test_linearizer(self) -> None:
        """Tests that a task is queued up behind an earlier task."""
        linearizer = Linearizer(name="test_linearizer")

        order: list[int] = []

        async def task(n: int) -> None:
            async with linearizer.queue("key"):
                order.append(n)
                await asyncio.sleep(0.01)

        t1 = asyncio.create_task(task(1))
        t2 = asyncio.create_task(task(2))
        t3 = asyncio.create_task(task(3))

        await asyncio.gather(t1, t2, t3)
        self.assertEqual(order, [1, 2, 3])

    async def test_linearizer_is_queued(self) -> None:
        """Tests `Linearizer.is_queued`."""
        linearizer = Linearizer(name="test_linearizer")

        self.assertFalse(linearizer.is_queued("key"))

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with linearizer.queue("key"):
                acquired.set()
                await release.wait()

        t1 = asyncio.create_task(holder())
        await acquired.wait()

        # Start a second task that will be queued
        async def waiter() -> None:
            async with linearizer.queue("key"):
                pass

        t2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)

        self.assertTrue(linearizer.is_queued("key"))

        release.set()
        await asyncio.gather(t1, t2)

        self.assertFalse(linearizer.is_queued("key"))

    async def test_multiple_entries(self) -> None:
        """Tests Linearizer with max_count > 1."""
        linearizer = Linearizer(name="test_linearizer", max_count=3)

        concurrent = 0
        max_concurrent = 0

        async def task() -> None:
            nonlocal concurrent, max_concurrent
            async with linearizer.queue("key"):
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                await asyncio.sleep(0.01)
                concurrent -= 1

        tasks = [asyncio.create_task(task()) for _ in range(6)]
        await asyncio.gather(*tasks)

        self.assertLessEqual(max_concurrent, 3)
        self.assertGreater(max_concurrent, 1)

    async def test_lots_of_queued_things(self) -> None:
        """Tests many tasks queued on the same key."""
        linearizer = Linearizer(name="test_linearizer")

        order: list[int] = []

        async def task(n: int) -> None:
            async with linearizer.queue("key"):
                order.append(n)

        tasks = [asyncio.create_task(task(i)) for i in range(20)]
        await asyncio.gather(*tasks)

        self.assertEqual(order, list(range(20)))

    async def test_cancellation(self) -> None:
        """Tests that cancelling a waiting task works correctly."""
        linearizer = Linearizer(name="test_linearizer")

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with linearizer.queue("key"):
                acquired.set()
                await release.wait()

        t1 = asyncio.create_task(holder())
        await acquired.wait()

        async def waiter() -> None:
            async with linearizer.queue("key"):
                pass

        t2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)

        # Cancel the waiting task
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        # The linearizer should still work for new tasks
        release.set()
        await t1

        async with linearizer.queue("key"):
            pass  # Should succeed

    async def test_cancellation_during_sleep(self) -> None:
        """Tests cancellation during the sleep(0) after lock acquisition."""
        linearizer = Linearizer(name="test_linearizer")

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with linearizer.queue("key"):
                acquired.set()
                await release.wait()

        t1 = asyncio.create_task(holder())
        await acquired.wait()

        async def waiter() -> None:
            async with linearizer.queue("key"):
                pass

        t2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)

        # Release t1, then immediately cancel t2 during its sleep(0)
        release.set()
        await asyncio.sleep(0)  # Let t2 acquire
        t2.cancel()

        try:
            await t2
        except asyncio.CancelledError:
            pass

        # Linearizer should still work
        async with linearizer.queue("key"):
            pass


if __name__ == "__main__":
    unittest.main()
