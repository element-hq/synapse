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

"""The pure logic of the Rust sliding sync implementation
(`rust/src/sliding_sync/`) against the Python it ports, over generated
inputs."""

import random
from typing import AbstractSet, Mapping

from synapse.api.constants import EventTypes
from synapse.synapse_rust import sliding_sync as rust
from synapse.types.handlers.sliding_sync import (
    RoomSyncConfig,
    SlidingSyncConfig,
    StateValues,
)

from tests import unittest

# The values the generators draw state types and keys from: the wildcards,
# local and remote users, and plain strings.
_TYPES = [
    StateValues.WILDCARD,
    EventTypes.Member,
    EventTypes.Name,
    EventTypes.Topic,
    "m.space.child",
]
_KEYS = [
    StateValues.WILDCARD,
    StateValues.LAZY,
    StateValues.ME,
    "",
    "@alice:test",
    "@bob:test",
    "@carol:remote",
    "@dave:remote",
    "alice",
    "@nohost",
]
_SERVER_NAME = "test"

RustConfig = tuple[int, dict[str, list[str]]]


def _is_mine_id(user_id: str) -> bool:
    parts = user_id.split(":", 1)
    return len(parts) == 2 and parts[1] == _SERVER_NAME


def _random_required_state(rng: random.Random) -> list[tuple[str, str]]:
    return [(rng.choice(_TYPES), rng.choice(_KEYS)) for _ in range(rng.randint(0, 6))]


def _python_config(
    timeline_limit: int, required_state: list[tuple[str, str]]
) -> RoomSyncConfig:
    return RoomSyncConfig.from_room_config(
        SlidingSyncConfig.RoomSubscription(
            timeline_limit=timeline_limit, required_state=required_state
        )
    )


def _to_rust(config: RoomSyncConfig) -> RustConfig:
    return (
        config.timeline_limit,
        {t: sorted(keys) for t, keys in config.required_state_map.items()},
    )


def _as_sets(
    required_state_map: Mapping[str, AbstractSet[str] | list[str]],
) -> dict[str, set[str]]:
    return {t: set(keys) for t, keys in required_state_map.items()}


class RoomSyncConfigTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/config.rs`"""

    def test_from_room_config(self) -> None:
        rng = random.Random(1)
        for _ in range(2000):
            limit = rng.randint(0, 20)
            required_state = _random_required_state(rng)
            python = _python_config(limit, required_state)
            timeline_limit, required_state_map = (
                rust.room_sync_config_from_room_config_for_test(limit, required_state)
            )
            self.assertEqual(timeline_limit, python.timeline_limit)
            self.assertEqual(
                _as_sets(required_state_map),
                _as_sets(python.required_state_map),
                required_state,
            )

    def test_combine(self) -> None:
        rng = random.Random(2)
        for _ in range(2000):
            a = _python_config(rng.randint(0, 20), _random_required_state(rng))
            b = _python_config(rng.randint(0, 20), _random_required_state(rng))
            python = a.combine_room_sync_config(b)
            timeline_limit, required_state_map = rust.combine_room_sync_config_for_test(
                _to_rust(a), _to_rust(b)
            )
            self.assertEqual(timeline_limit, python.timeline_limit)
            self.assertEqual(
                _as_sets(required_state_map),
                _as_sets(python.required_state_map),
                (a, b),
            )

    def test_must_await_full_state(self) -> None:
        rng = random.Random(3)
        raised = 0
        for _ in range(2000):
            config = _python_config(1, _random_required_state(rng))
            try:
                python: bool | None = config.must_await_full_state(_is_mine_id)
            except IndexError:
                # An empty state key under the wildcard type.
                python = None
                raised += 1
            try:
                result: bool | None = rust.must_await_full_state_for_test(
                    _to_rust(config), _SERVER_NAME
                )
            except ValueError:
                result = None
            self.assertEqual(result, python, config)
        # The generator reaches the case Python raises on.
        self.assertGreater(raised, 0)
