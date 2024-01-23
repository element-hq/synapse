#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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


from synapse.util.caches.treecache import TreeCache, iterate_tree_cache_entry

from .. import unittest


class TreeCacheTestCase(unittest.TestCase):
    def test_get_set_onelevel(self) -> None:
        cache = TreeCache()
        cache[("a",)] = "A"
        cache[("b",)] = "B"
        self.assertEqual(cache.get(("a",)), "A")
        self.assertEqual(cache.get(("b",)), "B")
        self.assertEqual(len(cache), 2)

    def test_pop_onelevel(self) -> None:
        cache = TreeCache()
        cache[("a",)] = "A"
        cache[("b",)] = "B"
        self.assertEqual(cache.pop(("a",)), "A")
        self.assertEqual(cache.pop(("a",)), None)
        self.assertEqual(cache.get(("b",)), "B")
        self.assertEqual(len(cache), 1)

    def test_get_set_twolevel(self) -> None:
        cache = TreeCache()
        cache[("a", "a")] = "AA"
        cache[("a", "b")] = "AB"
        cache[("b", "a")] = "BA"
        self.assertEqual(cache.get(("a", "a")), "AA")
        self.assertEqual(cache.get(("a", "b")), "AB")
        self.assertEqual(cache.get(("b", "a")), "BA")
        self.assertEqual(len(cache), 3)

    def test_pop_twolevel(self) -> None:
        cache = TreeCache()
        cache[("a", "a")] = "AA"
        cache[("a", "b")] = "AB"
        cache[("b", "a")] = "BA"
        self.assertEqual(cache.pop(("a", "a")), "AA")
        self.assertEqual(cache.get(("a", "a")), None)
        self.assertEqual(cache.get(("a", "b")), "AB")
        self.assertEqual(cache.pop(("b", "a")), "BA")
        self.assertEqual(cache.pop(("b", "a")), None)
        self.assertEqual(len(cache), 1)

    def test_pop_mixedlevel(self) -> None:
        cache = TreeCache()
        cache[("a", "a")] = "AA"
        cache[("a", "b")] = "AB"
        cache[("b", "a")] = "BA"
        self.assertEqual(cache.get(("a", "a")), "AA")
        popped = cache.pop(("a",))
        self.assertEqual(cache.get(("a", "a")), None)
        self.assertEqual(cache.get(("a", "b")), None)
        self.assertEqual(cache.get(("b", "a")), "BA")
        self.assertEqual(len(cache), 1)

        self.assertEqual({"AA", "AB"}, set(iterate_tree_cache_entry(popped)))

    def test_clear(self) -> None:
        cache = TreeCache()
        cache[("a",)] = "A"
        cache[("b",)] = "B"
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_contains(self) -> None:
        cache = TreeCache()
        cache[("a",)] = "A"
        self.assertTrue(("a",) in cache)
        self.assertFalse(("b",) in cache)
