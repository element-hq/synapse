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

"""The state delta arithmetic of `synapse.handlers.sync` against
`rust/src/sync/state_delta.rs`, on random state maps and deltas."""

import random

from parameterized import parameterized

from synapse.api.constants import EventTypes
from synapse.handlers.sync import (
    _apply_current_state_deltas,
    _calculate_state,
    _roll_back_state_over_deltas,
)
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.synapse_rust.sync import (
    apply_state_deltas_for_test,
    calculate_state_for_test,
    roll_back_state_for_test,
)
from synapse.types import (
    StateKey,
    StateMap,
)

from tests import unittest
from tests.sync.fixtures import (
    ME,
    SEEDS,
)

KEYS: list[StateKey] = [
    (EventTypes.Create, ""),
    (EventTypes.Topic, ""),
    (EventTypes.Name, ""),
    (EventTypes.Member, ME),
    (EventTypes.Member, "@alice:test"),
    (EventTypes.Member, "@bob:test"),
]


def _random_state_map(rng: random.Random, event_ids: list[str]) -> StateMap[str]:
    """A random state map over `KEYS`. Event IDs are shared across maps, as
    they are between the state at different points of one room, so the
    same event ID always carries the same key."""
    return {
        key: event_ids[KEYS.index(key) * 3 + rng.randint(0, 2)]
        for key in rng.sample(KEYS, rng.randint(0, len(KEYS)))
    }


class StateDeltaTestCase(unittest.TestCase):
    """The state delta arithmetic of `synapse.handlers.sync` against
    `rust/src/sync/state_delta.rs`."""

    EVENT_IDS = [f"${i}" for i in range(len(KEYS) * 3)]

    @parameterized.expand(SEEDS)
    def test_random_calculate_state_agrees(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(100):
            contains, start, end, previous = (
                _random_state_map(rng, self.EVENT_IDS) for _ in range(4)
            )
            lazy = rng.choice([True, False])
            context = f"maps={(contains, start, end, previous)} lazy={lazy}"
            expected = _calculate_state(contains, start, end, previous, lazy)
            actual = calculate_state_for_test(contains, start, end, previous, lazy)
            self.assertEqual(set(actual), set(expected), context)
            for key, event_id in actual.items():
                if event_id == expected[key]:
                    continue
                # The start and end of the timeline disagree on this key.
                # Python sends whichever its set iteration visits last; the
                # Rust sends the end's.
                self.assertEqual(event_id, end[key], context)
                self.assertEqual(expected[key], start[key], context)

    @staticmethod
    def _random_deltas(rng: random.Random, event_ids: list[str]) -> list[StateDelta]:
        deltas = []
        for i in range(rng.randint(0, 8)):
            key = rng.choice(KEYS)
            deltas.append(
                StateDelta(
                    stream_id=i,
                    room_id="!a:test",
                    event_type=key[0],
                    state_key=key[1],
                    event_id=rng.choice([None, rng.choice(event_ids)]),
                    prev_event_id=rng.choice([None, rng.choice(event_ids)]),
                )
            )
        return deltas

    @parameterized.expand(SEEDS)
    def test_random_delta_loops_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(100):
            state = _random_state_map(rng, self.EVENT_IDS)
            deltas = self._random_deltas(rng, self.EVENT_IDS)
            rust_deltas = [
                (d.event_type, d.state_key, d.event_id, d.prev_event_id) for d in deltas
            ]
            context = f"state={state} deltas={rust_deltas}"
            self.assertEqual(
                roll_back_state_for_test(state, rust_deltas),
                _roll_back_state_over_deltas(state, deltas),
                context,
            )

            timeline_state = _random_state_map(rng, self.EVENT_IDS)
            initial = _random_state_map(rng, self.EVENT_IDS)
            expected_state = dict(initial)
            expected_changed: set[StateKey] = set()
            expected_cleared = _apply_current_state_deltas(
                expected_state, expected_changed, deltas, timeline_state, ME
            )
            self.assertEqual(
                apply_state_deltas_for_test(initial, rust_deltas, timeline_state, ME),
                (expected_state, expected_changed, expected_cleared),
                context,
            )
