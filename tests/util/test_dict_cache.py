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


from synapse.util.caches.dictionary_cache import DictionaryCache

from tests import unittest
from tests.server import get_clock


class DictCacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _, clock = get_clock()
        self.cache: DictionaryCache[str, str, str] = DictionaryCache(
            name="foobar", clock=clock, server_name="test_server", max_entries=10
        )

    def test_simple_cache_hit_full(self) -> None:
        key = "test_simple_cache_hit_full"

        v = self.cache.get(key)
        self.assertIs(v.full, False)
        self.assertEqual(v.known_absent, set())
        self.assertEqual({}, v.value)

        seq = self.cache.sequence
        test_value = {"test": "test_simple_cache_hit_full"}
        self.cache.update(seq, key, test_value)

        c = self.cache.get(key)
        self.assertEqual(test_value, c.value)

    def test_simple_cache_hit_partial(self) -> None:
        key = "test_simple_cache_hit_partial"

        seq = self.cache.sequence
        test_value = {"test": "test_simple_cache_hit_partial"}
        self.cache.update(seq, key, test_value)

        c = self.cache.get(key, ["test"])
        self.assertEqual(test_value, c.value)

    def test_simple_cache_miss_partial(self) -> None:
        key = "test_simple_cache_miss_partial"

        seq = self.cache.sequence
        test_value = {"test": "test_simple_cache_miss_partial"}
        self.cache.update(seq, key, test_value)

        c = self.cache.get(key, ["test2"])
        self.assertEqual({}, c.value)

    def test_simple_cache_hit_miss_partial(self) -> None:
        key = "test_simple_cache_hit_miss_partial"

        seq = self.cache.sequence
        test_value = {
            "test": "test_simple_cache_hit_miss_partial",
            "test2": "test_simple_cache_hit_miss_partial2",
            "test3": "test_simple_cache_hit_miss_partial3",
        }
        self.cache.update(seq, key, test_value)

        c = self.cache.get(key, ["test2"])
        self.assertEqual({"test2": "test_simple_cache_hit_miss_partial2"}, c.value)

    def test_multi_insert(self) -> None:
        key = "test_simple_cache_hit_miss_partial"

        seq = self.cache.sequence
        test_value_1 = {"test": "test_simple_cache_hit_miss_partial"}
        self.cache.update(seq, key, test_value_1, fetched_keys={"test"})

        seq = self.cache.sequence
        test_value_2 = {"test2": "test_simple_cache_hit_miss_partial2"}
        self.cache.update(seq, key, test_value_2, fetched_keys={"test2"})

        c = self.cache.get(key, dict_keys=["test", "test2"])
        self.assertEqual(
            {
                "test": "test_simple_cache_hit_miss_partial",
                "test2": "test_simple_cache_hit_miss_partial2",
            },
            c.value,
        )
        self.assertEqual(c.full, False)

    def test_invalidation(self) -> None:
        """Test that the partial dict and full dicts get invalidated
        separately.
        """
        key = "some_key"

        seq = self.cache.sequence
        # start by populating a "full dict" entry
        self.cache.update(seq, key, {"a": "b", "c": "d"})

        # add a bunch of individual entries, also keeping the individual
        # entry for "a" warm.
        for i in range(20):
            self.cache.get(key, ["a"])
            self.cache.update(seq, f"key{i}", {"1": "2"})

        # We should have evicted the full dict...
        r = self.cache.get(key)
        self.assertFalse(r.full)
        self.assertTrue("c" not in r.value)

        # ... but kept the "a" entry that we kept querying.
        r = self.cache.get(key, dict_keys=["a"])
        self.assertFalse(r.full)
        self.assertEqual(r.value, {"a": "b"})
