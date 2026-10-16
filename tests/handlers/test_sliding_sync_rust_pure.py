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
from types import SimpleNamespace
from typing import AbstractSet, Mapping, cast

from synapse.api.constants import EventTypes, Membership
from synapse.events import EventBase
from synapse.handlers.sliding_sync import (
    MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER,
    _required_state_changes,
    _required_state_request,
    _state_filter_with_meta,
)
from synapse.synapse_rust import sliding_sync as rust
from synapse.types.handlers.sliding_sync import (
    RoomSyncConfig,
    SlidingSyncConfig,
    StateValues,
)
from synapse.types.state import StateFilter

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
            if python is None and result is True:
                # Python raises on the empty state key before it reaches a
                # remote user ID in its set order. Rust takes the order
                # with the user IDs first.
                self.assertTrue(
                    any(
                        key.startswith("@") and not _is_mine_id(key)
                        for key in config.required_state_map[StateValues.WILDCARD]
                    )
                )
                continue
            self.assertEqual(result, python, config)
        # The generator reaches the case Python raises on.
        self.assertGreater(raised, 0)


_USER_ID = "@alice:test"
_USERS = ["@alice:test", "@bob:test", "@carol:remote", "@dave:remote", "@erin:test"]


def _filter_to_tuple(
    state_filter: StateFilter,
) -> tuple[dict[str, list[str] | None], bool]:
    """A Python `StateFilter` in the form the Rust test exports return."""
    return (
        {
            t: sorted(keys) if keys is not None else None
            for t, keys in state_filter.types.items()
        },
        state_filter.include_others,
    )


def _random_map(rng: random.Random) -> dict[str, set[str]]:
    """A required state map, now and then with more state keys of one type
    than the connection remembers."""
    config = _python_config(1, _random_required_state(rng))
    required_state_map = {t: set(keys) for t, keys in config.required_state_map.items()}
    if rng.random() < 0.1:
        event_type = rng.choice([EventTypes.Member, EventTypes.Name])
        required_state_map[event_type] = {
            f"@many{i}:test" for i in rng.sample(range(150), rng.randint(60, 120))
        }
    return required_state_map


def _random_deltas(rng: random.Random) -> dict[tuple[str, str], str]:
    deltas = {}
    for _ in range(rng.randint(0, 5)):
        event_type = rng.choice(_TYPES[1:])
        state_key = rng.choice(_USERS + ["", "@many7:test"])
        deltas[(event_type, state_key)] = "$event"
    return deltas


def _inheritable(
    prev: AbstractSet[str], request: AbstractSet[str], changed: AbstractSet[str]
) -> set[str]:
    """The previous state keys `_required_state_changes` may keep."""
    invalidated = (prev - request) & changed
    return set(prev - {StateValues.WILDCARD, StateValues.LAZY}) - invalidated


class RequiredStateChangesTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/required_state.rs`: `_required_state_changes`."""

    def test_against_python(self) -> None:
        rng = random.Random(4)
        capped = 0
        for _ in range(3000):
            prev = _random_map(rng)
            # Often a small edit of the previous map.
            if rng.random() < 0.5:
                request = {t: set(keys) for t, keys in prev.items()}
                for _ in range(rng.randint(1, 3)):
                    event_type = rng.choice(_TYPES)
                    keys = request.setdefault(event_type, set())
                    key = rng.choice(_KEYS)
                    if key in keys and len(keys) > 1:
                        keys.discard(key)
                    else:
                        keys.add(key)
            else:
                request = _random_map(rng)
            previously_returned = {u for u in _USERS if rng.random() < 0.4}
            request_lazy = {u for u in _USERS if rng.random() < 0.4}
            deltas = _random_deltas(rng)

            python = _required_state_changes(
                _USER_ID,
                prev_required_state_map=prev,
                request_required_state_map=request,
                previously_returned_lazy_user_ids=previously_returned,
                request_lazy_load_user_ids=request_lazy,
                state_deltas=deltas,
            )
            changed, added, extra, invalidated = rust.required_state_changes_for_test(
                _USER_ID,
                {t: sorted(keys) for t, keys in prev.items()},
                {t: sorted(keys) for t, keys in request.items()},
                previously_returned,
                request_lazy,
                list(deltas),
            )
            context = (prev, request, previously_returned, request_lazy, deltas)

            self.assertEqual(
                added, _filter_to_tuple(python.added_state_filter), context
            )
            self.assertEqual(set(invalidated), python.lazy_members_invalidated, context)
            if deltas:
                self.assertEqual(
                    set(extra), python.extra_users_to_add_to_lazy_cache, context
                )
            else:
                # Without deltas Python reads the state keys of whichever
                # type a set union iterated last: Rust takes the members'.
                # One of the two finds no users.
                if set(extra) != python.extra_users_to_add_to_lazy_cache:
                    self.assertIn(
                        set(),
                        (set(extra), python.extra_users_to_add_to_lazy_cache),
                        context,
                    )

            if python.changed_required_state_map is None:
                self.assertIsNone(changed, context)
                continue
            assert changed is not None
            python_map = _as_sets(python.changed_required_state_map)
            rust_map = _as_sets(changed)
            self.assertEqual(rust_map.keys(), python_map.keys(), context)
            changed_keys: dict[str, set[str]] = {}
            for event_type, state_key in deltas:
                changed_keys.setdefault(event_type, set()).add(state_key)
            for event_type, python_keys in python_map.items():
                rust_keys = rust_map[event_type]
                if rust_keys == python_keys:
                    continue
                # Over the cap, each keeps the requested state keys and an
                # arbitrary slice of the previous ones.
                requested = request.get(event_type, set())
                inheritable = _inheritable(
                    prev.get(event_type, set()),
                    requested,
                    changed_keys.get(event_type, set()),
                )
                self.assertGreater(
                    len(requested | inheritable),
                    MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER,
                    context,
                )
                for keys in (rust_keys, python_keys):
                    self.assertLessEqual(requested, keys)
                    self.assertLessEqual(keys, requested | inheritable)
                    self.assertLessEqual(
                        len(keys),
                        max(len(requested), MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER),
                    )
                capped += 1
        self.assertGreater(capped, 0)


def _event(sender: str, event_type: str, state_key: str | None) -> EventBase:
    """A stand-in for an event, with the fields `_required_state_request`
    reads."""
    return cast(
        EventBase,
        SimpleNamespace(sender=sender, type=event_type, state_key=state_key),
    )


class RequiredStateRequestTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/required_state.rs`: `_required_state_request`
    and `_state_filter_with_meta`."""

    def test_against_python(self) -> None:
        rng = random.Random(5)
        for _ in range(3000):
            membership = rng.choice(
                [Membership.JOIN, Membership.LEAVE, Membership.INVITE, Membership.KNOCK]
            )
            required_state_map = _python_config(
                1, _random_required_state(rng)
            ).required_state_map
            timeline: list[tuple[str, str, str | None]] = []
            for _ in range(rng.randint(0, 4)):
                if rng.random() < 0.3:
                    timeline.append(
                        (rng.choice(_USERS), EventTypes.Member, rng.choice(_USERS))
                    )
                else:
                    timeline.append((rng.choice(_USERS), "m.room.message", None))
            limited = rng.choice([None, True, False])
            initial = rng.random() < 0.5
            deltas = list(_random_deltas(rng))

            python = _required_state_request(
                _USER_ID,
                membership=membership,
                required_state_map=required_state_map,
                timeline=[_event(*event) for event in timeline],
                limited=limited,
                initial=initial,
                state_delta_keys=deltas,
            )
            state_filter, lazy, lazy_user_ids, explicit = (
                rust.required_state_request_for_test(
                    _USER_ID,
                    membership,
                    {t: sorted(keys) for t, keys in required_state_map.items()},
                    timeline,
                    limited,
                    initial,
                    deltas,
                )
            )
            context = (membership, required_state_map, timeline, limited, initial)
            self.assertEqual(
                state_filter, _filter_to_tuple(python.required_state_filter), context
            )
            self.assertEqual(lazy, python.lazy_load_room_members, context)
            self.assertEqual(set(lazy_user_ids), python.lazy_load_user_ids, context)
            self.assertEqual(set(explicit), python.explicit_user_state, context)

            heroes = rng.sample(_USERS, rng.randint(0, 3))
            include_avatar = rng.random() < 0.5
            self.assertEqual(
                rust.state_filter_with_meta_for_test(
                    python.required_state_filter, heroes, include_avatar
                ),
                _filter_to_tuple(
                    _state_filter_with_meta(
                        python.required_state_filter,
                        heroes,
                        include_avatar=include_avatar,
                    )
                ),
                context,
            )
