#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from functools import partial
from typing import List, Tuple

from twisted.internet import defer

from synapse.util.caches.deferred_cache import DeferredCache

from tests.unittest import TestCase


class DeferredCacheTestCase(TestCase):
    def test_empty(self) -> None:
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        with self.assertRaises(KeyError):
            cache.get("foo")

    def test_hit(self) -> None:
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        cache.prefill("foo", 123)

        self.assertEqual(self.successResultOf(cache.get("foo")), 123)

    def test_hit_deferred(self) -> None:
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        origin_d: "defer.Deferred[int]" = defer.Deferred()
        set_d = cache.set("k1", origin_d)

        # get should return an incomplete deferred
        get_d = cache.get("k1")
        self.assertFalse(get_d.called)

        # add a callback that will make sure that the set_d gets called before the get_d
        def check1(r: str) -> str:
            self.assertTrue(set_d.called)
            return r

        get_d.addCallback(check1)

        # now fire off all the deferreds
        origin_d.callback(99)
        self.assertEqual(self.successResultOf(origin_d), 99)
        self.assertEqual(self.successResultOf(set_d), 99)
        self.assertEqual(self.successResultOf(get_d), 99)

    def test_callbacks(self) -> None:
        """Invalidation callbacks are called at the right time"""
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        callbacks = set()

        # start with an entry, with a callback
        cache.prefill("k1", 10, callback=lambda: callbacks.add("prefill"))

        # now replace that entry with a pending result
        origin_d: "defer.Deferred[int]" = defer.Deferred()
        set_d = cache.set("k1", origin_d, callback=lambda: callbacks.add("set"))

        # ... and also make a get request
        get_d = cache.get("k1", callback=lambda: callbacks.add("get"))

        # we don't expect the invalidation callback for the original value to have
        # been called yet, even though get() will now return a different result.
        # I'm not sure if that is by design or not.
        self.assertEqual(callbacks, set())

        # now fire off all the deferreds
        origin_d.callback(20)
        self.assertEqual(self.successResultOf(set_d), 20)
        self.assertEqual(self.successResultOf(get_d), 20)

        # now the original invalidation callback should have been called, but none of
        # the others
        self.assertEqual(callbacks, {"prefill"})
        callbacks.clear()

        # another update should invalidate both the previous results
        cache.prefill("k1", 30)
        self.assertEqual(callbacks, {"set", "get"})

    def test_set_fail(self) -> None:
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        callbacks = set()

        # start with an entry, with a callback
        cache.prefill("k1", 10, callback=lambda: callbacks.add("prefill"))

        # now replace that entry with a pending result
        origin_d: defer.Deferred = defer.Deferred()
        set_d = cache.set("k1", origin_d, callback=lambda: callbacks.add("set"))

        # ... and also make a get request
        get_d = cache.get("k1", callback=lambda: callbacks.add("get"))

        # none of the callbacks should have been called yet
        self.assertEqual(callbacks, set())

        # oh noes! fails!
        e = Exception("oops")
        origin_d.errback(e)
        self.assertIs(self.failureResultOf(set_d, Exception).value, e)
        self.assertIs(self.failureResultOf(get_d, Exception).value, e)

        # the callbacks for the failed requests should have been called.
        # I'm not sure if this is deliberate or not.
        self.assertEqual(callbacks, {"get", "set"})
        callbacks.clear()

        # the old value should still be returned now?
        get_d2 = cache.get("k1", callback=lambda: callbacks.add("get2"))
        self.assertEqual(self.successResultOf(get_d2), 10)

        # replacing the value now should run the callbacks for those requests
        # which got the original result
        cache.prefill("k1", 30)
        self.assertEqual(callbacks, {"prefill", "get2"})

    def test_get_immediate(self) -> None:
        cache: DeferredCache[str, int] = DeferredCache(
            name="test", server_name="test_server"
        )
        d1: "defer.Deferred[int]" = defer.Deferred()
        cache.set("key1", d1)

        # get_immediate should return default
        v = cache.get_immediate("key1", 1)
        self.assertEqual(v, 1)

        # now complete the set
        d1.callback(2)

        # get_immediate should return result
        v = cache.get_immediate("key1", 1)
        self.assertEqual(v, 2)

    def test_invalidate(self) -> None:
        cache: DeferredCache[Tuple[str], int] = DeferredCache(
            name="test", server_name="test_server"
        )
        cache.prefill(("foo",), 123)
        cache.invalidate(("foo",))

        with self.assertRaises(KeyError):
            cache.get(("foo",))

    def test_invalidate_all(self) -> None:
        cache: DeferredCache[str, str] = DeferredCache(
            name="testcache", server_name="test_server"
        )

        callback_record = [False, False]

        def record_callback(idx: int) -> None:
            callback_record[idx] = True

        # add a couple of pending entries
        d1: "defer.Deferred[str]" = defer.Deferred()
        cache.set("key1", d1, partial(record_callback, 0))

        d2: "defer.Deferred[str]" = defer.Deferred()
        cache.set("key2", d2, partial(record_callback, 1))

        # lookup should return pending deferreds
        self.assertFalse(cache.get("key1").called)
        self.assertFalse(cache.get("key2").called)

        # let one of the lookups complete
        d2.callback("result2")

        # now the cache will return a completed deferred
        self.assertEqual(self.successResultOf(cache.get("key2")), "result2")

        # now do the invalidation
        cache.invalidate_all()

        # lookup should fail
        with self.assertRaises(KeyError):
            cache.get("key1")
        with self.assertRaises(KeyError):
            cache.get("key2")

        # both callbacks should have been callbacked
        self.assertTrue(callback_record[0], "Invalidation callback for key1 not called")
        self.assertTrue(callback_record[1], "Invalidation callback for key2 not called")

        # letting the other lookup complete should do nothing
        d1.callback("result1")
        with self.assertRaises(KeyError):
            cache.get("key1", None)

    def test_eviction(self) -> None:
        cache: DeferredCache[int, str] = DeferredCache(
            name="test",
            server_name="test_server",
            max_entries=2,
            apply_cache_factor_from_config=False,
        )

        cache.prefill(1, "one")
        cache.prefill(2, "two")
        cache.prefill(3, "three")  # 1 will be evicted

        with self.assertRaises(KeyError):
            cache.get(1)

        cache.get(2)
        cache.get(3)

    def test_eviction_lru(self) -> None:
        cache: DeferredCache[int, str] = DeferredCache(
            name="test",
            server_name="test_server",
            max_entries=2,
            apply_cache_factor_from_config=False,
        )

        cache.prefill(1, "one")
        cache.prefill(2, "two")

        # Now access 1 again, thus causing 2 to be least-recently used
        cache.get(1)

        cache.prefill(3, "three")

        with self.assertRaises(KeyError):
            cache.get(2)

        cache.get(1)
        cache.get(3)

    def test_eviction_iterable(self) -> None:
        cache: DeferredCache[int, List[str]] = DeferredCache(
            name="test",
            server_name="test_server",
            max_entries=3,
            apply_cache_factor_from_config=False,
            iterable=True,
        )

        cache.prefill(1, ["one", "two"])
        cache.prefill(2, ["three"])

        # Now access 1 again, thus causing 2 to be least-recently used
        cache.get(1)

        # Now add an item to the cache, which evicts 2.
        cache.prefill(3, ["four"])
        with self.assertRaises(KeyError):
            cache.get(2)

        # Ensure 1 & 3 are in the cache.
        cache.get(1)
        cache.get(3)

        # Now access 1 again, thus causing 3 to be least-recently used
        cache.get(1)

        # Now add an item with multiple elements to the cache
        cache.prefill(4, ["five", "six"])

        # Both 1 and 3 are evicted since there's too many elements.
        with self.assertRaises(KeyError):
            cache.get(1)
        with self.assertRaises(KeyError):
            cache.get(3)

        # Now add another item to fill the cache again.
        cache.prefill(5, ["seven"])

        # Now access 4, thus causing 5 to be least-recently used
        cache.get(4)

        # Add an empty item.
        cache.prefill(6, [])

        # 5 gets evicted and replaced since an empty element counts as an item.
        with self.assertRaises(KeyError):
            cache.get(5)
        cache.get(4)
        cache.get(6)
