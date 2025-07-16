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


from typing import List, Tuple
from unittest.mock import Mock, patch

from synapse.metrics.jemalloc import JemallocStats
from synapse.types import JsonDict
from synapse.util.caches.lrucache import LruCache, setup_expire_lru_cache_entries
from synapse.util.caches.treecache import TreeCache

from tests import unittest
from tests.unittest import override_config


class LruCacheTestCase(unittest.HomeserverTestCase):
    def test_get_set(self) -> None:
        cache: LruCache[str, str] = LruCache(max_size=1)
        cache["key"] = "value"
        self.assertEqual(cache.get("key"), "value")
        self.assertEqual(cache["key"], "value")

    def test_eviction(self) -> None:
        cache: LruCache[int, int] = LruCache(max_size=2)
        cache[1] = 1
        cache[2] = 2

        self.assertEqual(cache.get(1), 1)
        self.assertEqual(cache.get(2), 2)

        cache[3] = 3

        self.assertEqual(cache.get(1), None)
        self.assertEqual(cache.get(2), 2)
        self.assertEqual(cache.get(3), 3)

    def test_setdefault(self) -> None:
        cache: LruCache[str, int] = LruCache(max_size=1)
        self.assertEqual(cache.setdefault("key", 1), 1)
        self.assertEqual(cache.get("key"), 1)
        self.assertEqual(cache.setdefault("key", 2), 1)
        self.assertEqual(cache.get("key"), 1)
        cache["key"] = 2  # Make sure overriding works.
        self.assertEqual(cache.get("key"), 2)

    def test_pop(self) -> None:
        cache: LruCache[str, int] = LruCache(max_size=1)
        cache["key"] = 1
        self.assertEqual(cache.pop("key"), 1)
        self.assertEqual(cache.pop("key"), None)

    def test_del_multi(self) -> None:
        # The type here isn't quite correct as they don't handle TreeCache well.
        cache: LruCache[Tuple[str, str], str] = LruCache(
            max_size=4, cache_type=TreeCache
        )
        cache[("animal", "cat")] = "mew"
        cache[("animal", "dog")] = "woof"
        cache[("vehicles", "car")] = "vroom"
        cache[("vehicles", "train")] = "chuff"

        self.assertEqual(len(cache), 4)

        self.assertEqual(cache.get(("animal", "cat")), "mew")
        self.assertEqual(cache.get(("vehicles", "car")), "vroom")
        cache.del_multi(("animal",))  # type: ignore[arg-type]
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.get(("animal", "cat")), None)
        self.assertEqual(cache.get(("animal", "dog")), None)
        self.assertEqual(cache.get(("vehicles", "car")), "vroom")
        self.assertEqual(cache.get(("vehicles", "train")), "chuff")
        # Man from del_multi say "Yes".

    def test_clear(self) -> None:
        cache: LruCache[str, int] = LruCache(max_size=1)
        cache["key"] = 1
        cache.clear()
        self.assertEqual(len(cache), 0)

    @override_config({"caches": {"per_cache_factors": {"mycache": 10}}})
    def test_special_size(self) -> None:
        cache: LruCache = LruCache(
            max_size=10, server_name="test_server", cache_name="mycache"
        )
        self.assertEqual(cache.max_size, 100)


class LruCacheCallbacksTestCase(unittest.HomeserverTestCase):
    def test_get(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(max_size=1)

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.get("key", "value")
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_multi_get(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(max_size=1)

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.get("key", callbacks=[m])
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_set(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(max_size=1)

        cache.set("key", "value", callbacks=[m])
        self.assertFalse(m.called)

        cache.set("key", "value")
        self.assertFalse(m.called)

        cache.set("key", "value2")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

    def test_pop(self) -> None:
        m = Mock()
        cache: LruCache[str, str] = LruCache(max_size=1)

        cache.set("key", "value", callbacks=[m])
        self.assertFalse(m.called)

        cache.pop("key")
        self.assertEqual(m.call_count, 1)

        cache.set("key", "value")
        self.assertEqual(m.call_count, 1)

        cache.pop("key")
        self.assertEqual(m.call_count, 1)

    def test_del_multi(self) -> None:
        m1 = Mock()
        m2 = Mock()
        m3 = Mock()
        m4 = Mock()
        # The type here isn't quite correct as they don't handle TreeCache well.
        cache: LruCache[Tuple[str, str], str] = LruCache(
            max_size=4, cache_type=TreeCache
        )

        cache.set(("a", "1"), "value", callbacks=[m1])
        cache.set(("a", "2"), "value", callbacks=[m2])
        cache.set(("b", "1"), "value", callbacks=[m3])
        cache.set(("b", "2"), "value", callbacks=[m4])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)
        self.assertEqual(m4.call_count, 0)

        cache.del_multi(("a",))  # type: ignore[arg-type]

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(m3.call_count, 0)
        self.assertEqual(m4.call_count, 0)

    def test_clear(self) -> None:
        m1 = Mock()
        m2 = Mock()
        cache: LruCache[str, str] = LruCache(max_size=5)

        cache.set("key1", "value", callbacks=[m1])
        cache.set("key2", "value", callbacks=[m2])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)

        cache.clear()

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 1)

    def test_eviction(self) -> None:
        m1 = Mock(name="m1")
        m2 = Mock(name="m2")
        m3 = Mock(name="m3")
        cache: LruCache[str, str] = LruCache(max_size=2)

        cache.set("key1", "value", callbacks=[m1])
        cache.set("key2", "value", callbacks=[m2])

        self.assertEqual(m1.call_count, 0)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key3", "value", callbacks=[m3])

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key3", "value")

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.get("key2")

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 0)

        cache.set("key1", "value", callbacks=[m1])

        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 0)
        self.assertEqual(m3.call_count, 1)


class LruCacheSizedTestCase(unittest.HomeserverTestCase):
    def test_evict(self) -> None:
        cache: LruCache[str, List[int]] = LruCache(max_size=5, size_callback=len)
        cache["key1"] = [0]
        cache["key2"] = [1, 2]
        cache["key3"] = [3]
        cache["key4"] = [4]

        self.assertEqual(cache["key1"], [0])
        self.assertEqual(cache["key2"], [1, 2])
        self.assertEqual(cache["key3"], [3])
        self.assertEqual(cache["key4"], [4])
        self.assertEqual(len(cache), 5)

        cache["key5"] = [5, 6]

        self.assertEqual(len(cache), 4)
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache["key3"], [3])
        self.assertEqual(cache["key4"], [4])
        self.assertEqual(cache["key5"], [5, 6])

    def test_zero_size_drop_from_cache(self) -> None:
        """Test that `drop_from_cache` works correctly with 0-sized entries."""
        cache: LruCache[str, List[int]] = LruCache(
            max_size=5, size_callback=lambda x: 0
        )
        cache["key1"] = []

        self.assertEqual(len(cache), 0)
        assert isinstance(cache.cache, dict)
        cache.cache["key1"].drop_from_cache()
        self.assertIsNone(
            cache.pop("key1"), "Cache entry should have been evicted but wasn't"
        )


class TimeEvictionTestCase(unittest.HomeserverTestCase):
    """Test that time based eviction works correctly."""

    def default_config(self) -> JsonDict:
        config = super().default_config()

        config.setdefault("caches", {})["expiry_time"] = "30m"

        return config

    def test_evict(self) -> None:
        setup_expire_lru_cache_entries(self.hs)

        cache: LruCache[str, int] = LruCache(max_size=5, clock=self.hs.get_clock())

        # Check that we evict entries we haven't accessed for 30 minutes.
        cache["key1"] = 1
        cache["key2"] = 2

        self.reactor.advance(20 * 60)

        self.assertEqual(cache.get("key1"), 1)

        self.reactor.advance(20 * 60)

        # We have only touched `key1` in the last 30m, so we expect that to
        # still be in the cache while `key2` should have been evicted.
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), None)

        # Check that re-adding an expired key works correctly.
        cache["key2"] = 3
        self.assertEqual(cache.get("key2"), 3)

        self.reactor.advance(20 * 60)

        self.assertEqual(cache.get("key2"), 3)

        self.reactor.advance(20 * 60)

        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), 3)


class MemoryEvictionTestCase(unittest.HomeserverTestCase):
    @override_config(
        {
            "caches": {
                "cache_autotuning": {
                    "max_cache_memory_usage": "700M",
                    "target_cache_memory_usage": "500M",
                    "min_cache_ttl": "5m",
                }
            }
        }
    )
    @patch("synapse.util.caches.lrucache.get_jemalloc_stats")
    def test_evict_memory(self, jemalloc_interface: Mock) -> None:
        mock_jemalloc_class = Mock(spec=JemallocStats)
        jemalloc_interface.return_value = mock_jemalloc_class

        # set the return value of get_stat() to be greater than max_cache_memory_usage
        mock_jemalloc_class.get_stat.return_value = 924288000

        setup_expire_lru_cache_entries(self.hs)
        cache: LruCache[str, int] = LruCache(max_size=4, clock=self.hs.get_clock())

        cache["key1"] = 1
        cache["key2"] = 2

        # advance the reactor less than the min_cache_ttl
        self.reactor.advance(60 * 2)

        # our items should still be in the cache
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 2)

        # advance the reactor past the min_cache_ttl
        self.reactor.advance(60 * 6)

        # the items should be cleared from cache
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), None)

        # add more stuff to caches
        cache["key1"] = 1
        cache["key2"] = 2

        # set the return value of get_stat() to be lower than target_cache_memory_usage
        mock_jemalloc_class.get_stat.return_value = 10000

        # advance the reactor past the min_cache_ttl
        self.reactor.advance(60 * 6)

        # the items should still be in the cache
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 2)


class ExtraIndexLruCacheTestCase(unittest.HomeserverTestCase):
    def test_invalidate_simple(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=10, extra_index_cb=lambda k, v: str(v)
        )
        cache["key1"] = 1
        cache["key2"] = 2

        cache.invalidate_on_extra_index("key1")
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 2)

        cache.invalidate_on_extra_index("1")
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), 2)

    def test_invalidate_multi(self) -> None:
        cache: LruCache[str, int] = LruCache(
            max_size=10, extra_index_cb=lambda k, v: str(v)
        )
        cache["key1"] = 1
        cache["key2"] = 1
        cache["key3"] = 2

        cache.invalidate_on_extra_index("key1")
        self.assertEqual(cache.get("key1"), 1)
        self.assertEqual(cache.get("key2"), 1)
        self.assertEqual(cache.get("key3"), 2)

        cache.invalidate_on_extra_index("1")
        self.assertEqual(cache.get("key1"), None)
        self.assertEqual(cache.get("key2"), None)
        self.assertEqual(cache.get("key3"), 2)
