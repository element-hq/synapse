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

from unittest.mock import Mock

from synapse.util.caches.ttlcache import TTLCache

from tests import unittest


class CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_timer = Mock(side_effect=lambda: 100.0)
        self.cache: TTLCache[str, str] = TTLCache("test_cache", self.mock_timer)

    def test_get(self) -> None:
        """simple set/get tests"""
        self.cache.set("one", "1", 10)
        self.cache.set("two", "2", 20)
        self.cache.set("three", "3", 30)

        self.assertEqual(len(self.cache), 3)

        self.assertTrue("one" in self.cache)
        self.assertEqual(self.cache.get("one"), "1")
        self.assertEqual(self.cache["one"], "1")
        self.assertEqual(self.cache.get_with_expiry("one"), ("1", 110, 10))
        self.assertEqual(self.cache._metrics.hits, 3)
        self.assertEqual(self.cache._metrics.misses, 0)

        self.cache.set("two", "2.5", 20)
        self.assertEqual(self.cache["two"], "2.5")
        self.assertEqual(self.cache._metrics.hits, 4)

        # non-existent-item tests
        self.assertEqual(self.cache.get("four", "4"), "4")
        self.assertIs(self.cache.get("four", None), None)

        with self.assertRaises(KeyError):
            self.cache["four"]

        with self.assertRaises(KeyError):
            self.cache.get("four")

        with self.assertRaises(KeyError):
            self.cache.get_with_expiry("four")

        self.assertEqual(self.cache._metrics.hits, 4)
        self.assertEqual(self.cache._metrics.misses, 5)

    def test_expiry(self) -> None:
        self.cache.set("one", "1", 10)
        self.cache.set("two", "2", 20)
        self.cache.set("three", "3", 30)

        self.assertEqual(len(self.cache), 3)
        self.assertEqual(self.cache["one"], "1")
        self.assertEqual(self.cache["two"], "2")

        # enough for the first entry to expire, but not the rest
        self.mock_timer.side_effect = lambda: 110.0

        self.assertEqual(len(self.cache), 2)
        self.assertFalse("one" in self.cache)
        self.assertEqual(self.cache["two"], "2")
        self.assertEqual(self.cache["three"], "3")

        self.assertEqual(self.cache.get_with_expiry("two"), ("2", 120, 20))

        self.assertEqual(self.cache._metrics.hits, 5)
        self.assertEqual(self.cache._metrics.misses, 0)
