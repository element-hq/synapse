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

from typing import List, cast

from synapse.util import Clock
from synapse.util.caches.expiringcache import ExpiringCache

from tests.utils import MockClock

from .. import unittest


class ExpiringCacheTestCase(unittest.HomeserverTestCase):
    def test_get_set(self) -> None:
        clock = MockClock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            "test", cast(Clock, clock), max_len=1
        )

        cache["key"] = "value"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache["key"], "value")

    def test_eviction(self) -> None:
        clock = MockClock()
        cache: ExpiringCache[str, str] = ExpiringCache(
            "test", cast(Clock, clock), max_len=2
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
        clock = MockClock()
        cache: ExpiringCache[str, List[int]] = ExpiringCache(
            "test", cast(Clock, clock), max_len=5, iterable=True
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

    def test_time_eviction(self) -> None:
        clock = MockClock()
        cache: ExpiringCache[str, int] = ExpiringCache(
            "test", cast(Clock, clock), expiry_ms=1000
        )

        cache["key"] = 1
        clock.advance_time(0.5)
        cache["key2"] = 2

        self.assertEqual(cache.get("key"), 1)
        self.assertEqual(cache.get("key2"), 2)

        clock.advance_time(0.9)
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), 2)

        clock.advance_time(1)
        self.assertEqual(cache.get("key"), None)
        self.assertEqual(cache.get("key2"), None)
