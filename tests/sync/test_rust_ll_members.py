#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""`LazyLoadedMembersCache` against `rust/src/sync/ll_members.rs`: the
hand-written cases of the Python one, and random operation sequences."""

import random

from parameterized import parameterized

from synapse.handlers.sync import (
    LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE,
    LazyLoadedMembersCache,
)
from synapse.synapse_rust.sync import (
    LazyLoadedMembersCache as RustLazyLoadedMembersCache,
)
from synapse.types import RoomStreamToken

from tests import unittest
from tests.sync.fixtures import (
    SEEDS,
    random_live_token,
)


class LazyLoadedMembersCacheTestCase(unittest.TestCase):
    """`LazyLoadedMembersCache` against `rust/src/sync/ll_members.rs`."""

    @staticmethod
    def _token(stream: int) -> RoomStreamToken:
        return RoomStreamToken(stream=stream)

    def test_forgets_only_entries_after_since(self) -> None:
        cache = RustLazyLoadedMembersCache()
        cache.mark_sent("@a:test", "$a", self._token(5))
        cache.mark_sent("@b:test", "$b", self._token(7))

        cache.start_request(self._token(7))
        self.assertTrue(cache.was_sent("@a:test", "$a"))
        self.assertTrue(cache.was_sent("@b:test", "$b"))

        cache.start_request(self._token(6))
        self.assertTrue(cache.was_sent("@a:test", "$a"))
        self.assertFalse(cache.was_sent("@b:test", "$b"))

        cache.start_request(self._token(4))
        self.assertFalse(cache.was_sent("@a:test", "$a"))

        cache.mark_sent("@a:test", "$a", self._token(5))
        cache.start_request(None)
        self.assertFalse(cache.was_sent("@a:test", "$a"))
        self.assertEqual(len(cache), 0)

    def test_evicts_least_recently_used(self) -> None:
        cache = RustLazyLoadedMembersCache()
        for i in range(LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE):
            cache.mark_sent(f"@u{i}:test", f"$u{i}", self._token(1))
        self.assertTrue(cache.was_sent("@u0:test", "$u0"))
        cache.mark_sent("@new:test", "$new", self._token(1))
        self.assertTrue(cache.was_sent("@u0:test", "$u0"))
        self.assertFalse(cache.was_sent("@u1:test", "$u1"))
        self.assertTrue(cache.was_sent("@new:test", "$new"))

    @parameterized.expand(SEEDS)
    def test_random_operations_agree(self, seed: int) -> None:
        """The same sequence of operations, with multi-writer positions, gives
        the same answers from both caches."""
        from synapse.types import StreamKeyType, StreamToken

        rng = random.Random(seed)
        python = LazyLoadedMembersCache()
        rust = RustLazyLoadedMembersCache()
        users = [f"@u{i}:test" for i in range(LAZY_LOADED_MEMBERS_CACHE_MAX_SIZE + 20)]

        def token(rng: random.Random) -> RoomStreamToken:
            return random_live_token(rng)

        for _ in range(500):
            op = rng.choice(["mark", "mark", "mark", "was", "was", "start"])
            if op == "mark":
                user = rng.choice(users)
                event_id = rng.choice(["$1", "$2", "$3"])
                position = token(rng)
                python.mark_sent(
                    user,
                    event_id,
                    StreamToken.START.copy_and_replace(StreamKeyType.ROOM, position),
                )
                rust.mark_sent(user, event_id, position)
            elif op == "was":
                user = rng.choice(users)
                event_id = rng.choice(["$1", "$2", "$3"])
                self.assertEqual(
                    rust.was_sent(user, event_id), python.was_sent(user, event_id)
                )
            else:
                since = None if rng.random() < 0.1 else token(rng)
                python.start_request(
                    None
                    if since is None
                    else StreamToken.START.copy_and_replace(StreamKeyType.ROOM, since)
                )
                rust.start_request(since)
            self.assertEqual(len(rust), len(python._sent))
