#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 OpenMarket Ltd
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


from synapse.util.caches.expiringcache import ExpiringCache

from tests.server import get_clock

from .. import unittest


class ExpiringCacheTestCase(unittest.HomeserverTestCase):
    def test_get_set(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=1,
        )

        cache["key"] = "value"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache["key"], "value")

    def test_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=2,
        )

        cache["key"] = "value"
        cache["key2"] = "value2"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache.get("key2"), "value2")

        cache["key3"] = "value3"
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), "value2")
        self.assertEqual(cache.get("key3"), "value3")

    def test_iterable_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, list[int]] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=5,
            iterable=True,
        )

        cache["key"] = [1]
        cache["key2"] = [2, 3]
        cache["key3"] = [4, 5]

        self.assertEqual(cache.get("key"), [1])
        self.assertEqual(cache.get("key2"), [2, 3])
        self.assertEqual(cache.get("key3"), [4, 5])

        cache["key4"] = [6, 7]
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache.get("key3"), [4, 5])
        self.assertEqual(cache.get("key4"), [6, 7])

    def test_size_callback_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, list[int]] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            max_len=5,
            # Size each value by its sum, so that `len()` and the reported
            # size disagree.
            size_callback=sum,
        )

        cache["key"] = [1]
        cache["key2"] = [2]
        cache["key3"] = [2]

        self.assertEqual(cache.get("key"), [1])
        self.assertEqual(cache.get("key2"), [2])
        self.assertEqual(cache.get("key3"), [2])
        self.assertEqual(len(cache), 5)

        # Three entries of `len()` 1 each, but a size of 6, so the oldest two
        # go.
        cache["key4"] = [3]
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache.get("key3"), [2])
        self.assertEqual(cache.get("key4"), [3])
        self.assertEqual(len(cache), 5)

    def test_iterable_and_size_callback_are_exclusive(self) -> None:
        reactor, clock = get_clock()
        with self.assertRaises(ValueError):
            ExpiringCache(
                cache_name="test",
                server_name="testserver",
                hs=self.hs,
                clock=clock,
                iterable=True,
                size_callback=len,
            )

    def test_time_eviction(self) -> None:
        reactor, clock = get_clock()
        cache: ExpiringCache[str, int] = ExpiringCache(
            cache_name="test",
            server_name="testserver",
            hs=self.hs,
            clock=clock,
            expiry_ms=1000,
        )

        cache["key"] = 1
        reactor.advance(0.5)
        cache["key2"] = 2

        self.assertEqual(cache.get("key"), 1)
        self.assertEqual(cache.get("key2"), 2)

        reactor.advance(0.9)
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), 2)

        reactor.advance(1)
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), None)
