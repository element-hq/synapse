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

"""`synapse.types.RoomStreamToken` and the pagination helpers of `stream.py`
against `rust/src/types/stream_token.rs`, on random tokens with instance
maps, where a bug would be silent."""

import random
from collections.abc import Mapping
from typing import Any
from unittest.mock import Mock

from parameterized import parameterized

from twisted.internet import defer

from synapse.storage.databases.main.stream import (
    _filter_results,
    _filter_results_by_stream,
    generate_next_token,
    generate_pagination_bounds,
    generate_pagination_where_clause,
)
from synapse.storage.engines import PostgresEngine
from synapse.synapse_rust.types import (
    filter_results_for_test,
    generate_next_token_for_test,
    pagination_bounds_for_test,
    room_stream_token_ops_for_test,
    room_stream_token_string_for_test,
    round_trip_room_stream_tokens_for_test,
)
from synapse.types import (
    PersistedEventPosition,
    RoomStreamToken,
)

from tests import unittest
from tests.sync.fixtures import (
    SEEDS,
    WRITERS,
    random_live_token,
)


class _FakeInstanceStore:
    """The two store methods `RoomStreamToken` serialization uses, over a
    fixed instance map. (The real table is Postgres-only.)"""

    def __init__(self, instance_ids: Mapping[str, int]) -> None:
        self.instance_ids = instance_ids

    async def get_id_for_instance(self, instance_name: str) -> int:
        return self.instance_ids[instance_name]

    async def get_name_from_instance_id(self, instance_id: int) -> str:
        return next(n for n, i in self.instance_ids.items() if i == instance_id)


class RoomStreamTokenTestCase(unittest.TestCase):
    """`synapse.types.RoomStreamToken` and the pagination helpers of
    `stream.py` against `rust/src/types/stream_token.rs`."""

    instance_ids = {name: index + 1 for index, name in enumerate(WRITERS)}
    store: Any = _FakeInstanceStore(instance_ids)

    @staticmethod
    def _random_historic(rng: random.Random) -> RoomStreamToken:
        return RoomStreamToken(
            topological=rng.randint(0, 10), stream=rng.randint(0, 20)
        )

    @classmethod
    def _random_token(cls, rng: random.Random) -> RoomStreamToken:
        if rng.random() < 0.3:
            return cls._random_historic(rng)
        return random_live_token(rng)

    @parameterized.expand(SEEDS)
    def test_random_live_tokens_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(100):
            a = random_live_token(rng)
            b = random_live_token(rng)
            max_stream = rng.randint(0, 30)
            before, advanced, bounded, max_pos = room_stream_token_ops_for_test(
                a, b, max_stream
            )
            context = f"a={a} b={b} max_stream={max_stream}"
            self.assertEqual(before, a.is_before_or_eq(b), context)
            self.assertEqual(advanced, a.copy_and_advance(b), context)
            self.assertEqual(bounded, a.bound_stream_token(max_stream), context)
            self.assertEqual(max_pos, a.get_max_stream_pos(), context)

    @parameterized.expand(SEEDS)
    def test_random_tokens_serialize_the_same(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(50):
            token = self._random_token(rng)
            expected = self.successResultOf(
                defer.ensureDeferred(token.to_string(self.store))
            )
            string, parsed = room_stream_token_string_for_test(token, self.instance_ids)
            self.assertEqual(string, expected, str(token))
            self.assertEqual(
                parsed,
                self.successResultOf(
                    defer.ensureDeferred(RoomStreamToken.parse(self.store, string))
                ),
            )
            self.assertEqual(
                round_trip_room_stream_tokens_for_test([token, parsed]), [token, parsed]
            )

    @parameterized.expand(SEEDS)
    def test_random_filtering_agrees(self, seed: int) -> None:
        """The multi-writer filtering, where a bug would be silent."""
        rng = random.Random(seed)
        for _ in range(200):
            lower = self._random_token(rng) if rng.random() < 0.8 else None
            upper = self._random_token(rng) if rng.random() < 0.8 else None
            instance = rng.choice(WRITERS + [None])
            topological = rng.randint(0, 10)
            stream = rng.randint(0, 30)
            context = (
                f"lower={lower} upper={upper} {instance} ({topological}, {stream})"
            )
            by_both, by_stream = filter_results_for_test(
                lower, upper, instance, topological, stream
            )
            self.assertEqual(
                by_both,
                _filter_results(lower, upper, instance, topological, stream),
                context,
            )
            # The stream-only variant rejects historic tokens, but only when
            # it gets as far as reading them.
            try:
                expected_by_stream: bool | None = _filter_results_by_stream(
                    lower, upper, instance, stream
                )
            except AssertionError:
                expected_by_stream = None
            self.assertEqual(by_stream, expected_by_stream, context)

    @parameterized.expand(SEEDS)
    def test_random_pagination_bounds_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        engine = Mock(spec=PostgresEngine)
        for _ in range(100):
            direction = rng.choice(["f", "b"])
            from_token = self._random_token(rng) if rng.random() < 0.8 else None
            to_token = self._random_token(rng) if rng.random() < 0.8 else None
            instance = rng.choice(WRITERS)
            stream = rng.randint(0, 30)
            context = f"{direction} from={from_token} to={to_token}"
            order, from_bound, to_bound, clause, after = pagination_bounds_for_test(
                direction, from_token, to_token, instance, stream
            )
            py_direction = {"f": "f", "b": "b"}[direction]
            from synapse.api.constants import Direction

            expected = generate_pagination_bounds(
                Direction(py_direction), from_token, to_token
            )
            self.assertEqual((order, from_bound, to_bound), expected, context)
            self.assertEqual(
                clause,
                generate_pagination_where_clause(
                    Direction(py_direction),
                    ("topological_ordering", "stream_ordering"),
                    expected[1],
                    expected[2],
                    engine,
                ),
                context,
            )
            if from_token is not None and from_token.topological is None:
                self.assertEqual(
                    after,
                    PersistedEventPosition(instance, stream).persisted_after(
                        from_token
                    ),
                    context,
                )
            last_topo = rng.choice([None, rng.randint(0, 10)])
            self.assertEqual(
                generate_next_token_for_test(direction, last_topo, stream),
                generate_next_token(Direction(py_direction), last_topo, stream),
                context,
            )
