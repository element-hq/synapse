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

import json
import random
from types import SimpleNamespace
from typing import AbstractSet, Any, Coroutine, Mapping, TypeVar, cast

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import SlidingSyncUnknownPosition
from synapse.events import EventBase
from synapse.events.utils import FilteredEvent
from synapse.handlers.sliding_sync import (
    MAX_NUMBER_PREVIOUS_STATE_KEYS_TO_REMEMBER,
    SlidingSyncHandler,
    _required_state_changes,
    _required_state_request,
    _room_config_to_persist,
    _state_filter_with_meta,
    _stripped_state,
    _timeline_bounds,
)
from synapse.handlers.sliding_sync.room_lists import (
    SlidingSyncRoomLists,
    _sync_list_ops,
    filter_membership_for_sync,
)
from synapse.storage.roommember import RoomsForUserSlidingSync
from synapse.synapse_rust import sliding_sync as rust
from synapse.types import (
    PersistedEventPosition,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StrCollection,
    StreamToken,
)
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.types.handlers.sliding_sync import (
    HaveSentRoom,
    HaveSentRoomFlag,
    PerConnectionState,
    RoomLazyMembershipChanges,
    RoomStatusMap,
    RoomSyncConfig,
    SlidingSyncConfig,
    StateValues,
)
from synapse.types.state import StateFilter
from synapse.util.clock import Clock

from tests import unittest

T = TypeVar("T")

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


_ROOMS = [f"!r{i}:test" for i in range(8)]
_HOUR_MS = 60 * 60 * 1000

PyStatus = tuple[str, int | None]


def _random_statuses(rng: random.Random) -> dict[str, PyStatus]:
    statuses: dict[str, PyStatus] = {}
    for room_id in _ROOMS:
        choice = rng.random()
        if choice < 0.3:
            continue
        elif choice < 0.4:
            statuses[room_id] = ("never", None)
        elif choice < 0.7:
            statuses[room_id] = ("live", None)
        else:
            statuses[room_id] = ("previously", rng.randint(1, 50))
    return statuses


def _to_status(status: PyStatus) -> HaveSentRoom[int]:
    flag, token = status
    if flag == "never":
        return HaveSentRoom.never()
    if flag == "live":
        return HaveSentRoom.live()
    assert token is not None
    return HaveSentRoom.previously(token)


def _from_status(status: HaveSentRoom[int]) -> PyStatus:
    return (status.status.value, status.last_token)


class _FakeClock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def time_msec(self) -> int:
        return self.now_ms


class ConnectionStateTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/connection.rs`"""

    def test_room_status_map(self) -> None:
        rng = random.Random(6)
        for _ in range(2000):
            statuses = _random_statuses(rng)
            ops: list[tuple[str, list[str], int | None]] = []
            for _ in range(rng.randint(0, 4)):
                room_ids = rng.sample(_ROOMS, rng.randint(0, 4))
                if rng.random() < 0.5:
                    ops.append(("sent", room_ids, None))
                else:
                    ops.append(("unsent", room_ids, rng.randint(1, 50)))

            python = RoomStatusMap(
                statuses={k: _to_status(v) for k, v in statuses.items()}
            ).get_mutable()
            for op, room_ids, token in ops:
                if op == "sent":
                    python.record_sent_rooms(room_ids)
                else:
                    assert token is not None
                    python.record_unsent_rooms(room_ids, token)

            updates, flattened, length = rust.room_status_map_ops_for_test(
                statuses, ops
            )
            context = (statuses, ops)
            self.assertEqual(
                updates,
                {k: _from_status(v) for k, v in python.get_updates().items()},
                context,
            )
            copied = python.copy()
            self.assertEqual(
                flattened,
                {
                    room_id: _from_status(copied.have_sent_room(room_id))
                    for room_id in _ROOMS
                    if room_id in statuses or room_id in python.get_updates()
                },
                context,
            )
            self.assertEqual(length, len(python), context)

    def test_lazy_membership_changes(self) -> None:
        rng = random.Random(7)
        for _ in range(2000):
            now_ms = 10 * _HOUR_MS
            returned = {
                user_id: rng.choice(
                    [None, now_ms, now_ms - _HOUR_MS, now_ms - _HOUR_MS + 1, 0]
                )
                for user_id in rng.sample(_USERS, rng.randint(0, 4))
            }
            invalidated = set(rng.sample(_USERS, rng.choice([0, 0, 1])))
            python = RoomLazyMembershipChanges(
                returned_user_id_to_last_seen_ts_map=returned,
                invalidated_user_ids=invalidated,
            )
            clock = cast(Clock, _FakeClock(now_ms))
            to_update, has_updates = rust.lazy_membership_changes_for_test(
                returned, invalidated, now_ms
            )
            self.assertEqual(
                to_update, sorted(python.get_returned_user_ids_to_update(clock))
            )
            self.assertEqual(has_updates, python.has_updates(clock))

    def test_per_connection_state(self) -> None:
        rng = random.Random(8)
        now_ms = 10 * _HOUR_MS
        for _ in range(1000):
            rooms = _random_statuses(rng)
            receipts = _random_statuses(rng)
            account_data = _random_statuses(rng)
            sent = [rng.sample(_ROOMS, rng.choice([0, 0, 1, 2])) for _ in range(3)]
            room_configs = {
                room_id: _to_rust(_python_config(1, _random_required_state(rng)))
                for room_id in rng.sample(_ROOMS, rng.randint(0, 3))
            }
            room_config_updates = {
                room_id: _to_rust(_python_config(2, _random_required_state(rng)))
                for room_id in rng.sample(_ROOMS, rng.choice([0, 0, 1]))
            }
            lazy = {
                room_id: (
                    {
                        user_id: rng.choice([None, now_ms])
                        for user_id in rng.sample(_USERS, rng.choice([0, 1]))
                    },
                    set(rng.sample(_USERS, rng.choice([0, 0, 0, 1]))),
                )
                for room_id in rng.sample(_ROOMS, rng.choice([0, 1]))
            }

            def config(rust_config: RustConfig) -> RoomSyncConfig:
                limit, required_state_map = rust_config
                return RoomSyncConfig(
                    limit, {t: set(keys) for t, keys in required_state_map.items()}
                )

            state = PerConnectionState(
                rooms=RoomStatusMap(
                    statuses={
                        k: HaveSentRoom(
                            HaveSentRoomFlag(flag),
                            RoomStreamToken(stream=token) if token else None,
                        )
                        for k, (flag, token) in rooms.items()
                    }
                ),
                # The receipts stream's tokens are ints here.
                receipts=RoomStatusMap(
                    statuses=cast(Any, {k: _to_status(v) for k, v in receipts.items()})
                ),
                account_data=RoomStatusMap(
                    statuses={k: _to_status(v) for k, v in account_data.items()}
                ),
                room_configs={k: config(v) for k, v in room_configs.items()},
            )
            python = state.get_mutable()
            python.rooms.record_sent_rooms(sent[0])
            python.receipts.record_sent_rooms(sent[1])
            python.account_data.record_sent_rooms(sent[2])
            for room_id, rust_config in room_config_updates.items():
                python.room_configs[room_id] = config(rust_config)
            for room_id, (returned, invalidated) in lazy.items():
                python.room_lazy_membership[room_id] = RoomLazyMembershipChanges(
                    returned_user_id_to_last_seen_ts_map=returned,
                    invalidated_user_ids=invalidated,
                )

            self.assertEqual(
                rust.per_connection_state_for_test(
                    rooms,
                    sent[0],
                    receipts,
                    sent[1],
                    account_data,
                    sent[2],
                    room_configs,
                    room_config_updates,
                    lazy,
                    now_ms,
                ),
                (
                    python.has_updates(cast(Clock, _FakeClock(now_ms))),
                    len(state),
                    len(python),
                ),
            )


PyRoom = tuple[str, str | None, str, str | None, int, bool, str | None, bool]


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine that never waits."""
    try:
        coroutine.send(None)
    except StopIteration as e:
        return e.value
    raise AssertionError("the coroutine waited")


def _random_room(rng: random.Random, room_id: str) -> PyRoom:
    membership = rng.choice(
        [Membership.JOIN, Membership.INVITE, Membership.LEAVE, Membership.BAN]
    )
    state_reset = rng.random() < 0.1
    return (
        room_id,
        None if state_reset else rng.choice([_USER_ID, "@bob:test"]),
        membership,
        None if state_reset else f"$event_{room_id}",
        rng.randint(1, 100),
        rng.random() < 0.8,
        rng.choice([None, None, "m.space", "org.example.type"]),
        rng.random() < 0.5,
    )


def _python_room(room: PyRoom) -> RoomsForUserSlidingSync:
    (
        room_id,
        sender,
        membership,
        event_id,
        stream,
        has_known_state,
        room_type,
        is_encrypted,
    ) = room
    return RoomsForUserSlidingSync(
        room_id=room_id,
        sender=sender,
        membership=membership,
        event_id=event_id,
        event_pos=PersistedEventPosition("master", stream),
        room_version_id="10",
        has_known_state=has_known_state,
        room_type=room_type,
        is_encrypted=is_encrypted,
    )


def _random_filters(rng: random.Random) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    types = [None, "m.space", "org.example.type", "org.other"]
    tags = ["m.favourite", "m.lowpriority", "u.work"]
    if rng.random() < 0.4:
        filters["is_dm"] = rng.random() < 0.5
    if rng.random() < 0.4:
        filters["is_encrypted"] = rng.random() < 0.5
    if rng.random() < 0.4:
        filters["is_invite"] = rng.random() < 0.5
    if rng.random() < 0.3:
        filters["room_types"] = rng.sample(types, rng.randint(0, 2))
    if rng.random() < 0.3:
        filters["not_room_types"] = rng.sample(types, rng.randint(0, 2))
    if rng.random() < 0.3:
        filters["tags"] = rng.sample(tags, rng.randint(0, 2))
    if rng.random() < 0.3:
        filters["not_tags"] = rng.sample(tags, rng.randint(0, 2))
    if rng.random() < 0.03:
        filters["spaces"] = ["!space:test"]
    if rng.random() < 0.03:
        filters["room_name_like"] = "name"
    return filters


class _FakeTagStore:
    def __init__(self, room_tags: dict[str, set[str]]) -> None:
        self.room_tags = room_tags

    async def get_tags_for_user(self, user_id: str) -> dict[str, dict[str, Any]]:
        return {
            room_id: {tag: {} for tag in tags}
            for room_id, tags in self.room_tags.items()
        }


class RoomListFilterTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/room_lists/filter.rs`"""

    def test_filter_membership_for_sync(self) -> None:
        rng = random.Random(9)
        for _ in range(500):
            room = _random_room(rng, "!r:test")
            newly_left = rng.random() < 0.3
            self.assertEqual(
                rust.filter_membership_for_sync_for_test(_USER_ID, room, newly_left),
                filter_membership_for_sync(
                    user_id=_USER_ID,
                    room_membership_for_user=_python_room(room),
                    newly_left=newly_left,
                ),
                (room, newly_left),
            )

    def test_filter_rooms_using_tables(self) -> None:
        rng = random.Random(10)
        not_implemented = 0
        for _ in range(2000):
            rooms = [_random_room(rng, room_id) for room_id in _ROOMS]
            filters = _random_filters(rng)
            dm_room_ids = {room_id for room_id in _ROOMS if rng.random() < 0.3}
            room_tags = {
                room_id: set(rng.sample(["m.favourite", "m.lowpriority", "u.work"], 1))
                for room_id in _ROOMS
                if rng.random() < 0.4
            }
            sent_before = {room_id for room_id in _ROOMS if rng.random() < 0.5}

            fake_room_lists = cast(
                SlidingSyncRoomLists,
                SimpleNamespace(store=_FakeTagStore(room_tags)),
            )
            previous_state = PerConnectionState(
                rooms=RoomStatusMap(
                    statuses={room_id: HaveSentRoom.live() for room_id in sent_before}
                )
            )
            try:
                python: set[str] | None = set(
                    _run(
                        SlidingSyncRoomLists.filter_rooms_using_tables(
                            fake_room_lists,
                            _USER_ID,
                            {room[0]: _python_room(room) for room in rooms},
                            previous_state,
                            SlidingSyncConfig.SlidingSyncList.Filters(**filters),
                            cast(StreamToken, None),
                            dm_room_ids,
                        )
                    )
                )
            except NotImplementedError:
                python = None
                not_implemented += 1
            try:
                result: set[str] | None = set(
                    rust.filter_rooms_for_test(
                        rooms, json.dumps(filters), dm_room_ids, room_tags, sent_before
                    )
                )
            except NotImplementedError:
                result = None
            self.assertEqual(result, python, (rooms, filters))
        self.assertGreater(not_implemented, 0)


class _FakeStreamCache:
    def __init__(self, earliest: int, positions: dict[str, int]) -> None:
        self.earliest = earliest
        self.positions = positions

    def get_earliest_known_position(self) -> int:
        return self.earliest

    def get_max_pos_of_last_change(self, room_id: str) -> int | None:
        return self.positions.get(room_id)


class _FakeSortStore:
    def __init__(
        self,
        stream_cache: _FakeStreamCache,
        recheck: dict[str, int | None],
        joined_positions: dict[str, int],
    ) -> None:
        self._events_stream_cache = stream_cache
        self.recheck = recheck
        self.joined_positions = joined_positions

    async def get_last_event_pos_in_room_before_stream_ordering(
        self, room_id: str, end_token: RoomStreamToken
    ) -> tuple[str, PersistedEventPosition] | None:
        stream = self.recheck[room_id]
        if stream is None:
            return None
        return ("$event", PersistedEventPosition("master", stream))

    async def bulk_get_last_event_pos_in_room_before_stream_ordering(
        self, room_ids: list[str], end_token: RoomStreamToken
    ) -> dict[str, int]:
        return {
            room_id: self.joined_positions[room_id]
            for room_id in room_ids
            if room_id in self.joined_positions
        }


class _FakeUpdatesStore:
    def __init__(self, rooms_with_updates: set[str]) -> None:
        self.rooms_with_updates = rooms_with_updates

    async def get_rooms_that_have_updates_since_sliding_sync_table(
        self, room_ids: StrCollection, from_key: RoomStreamToken
    ) -> set[str]:
        return {room_id for room_id in room_ids if room_id in self.rooms_with_updates}


class RoomListSortTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/room_lists/sort.rs` and `lists.rs`"""

    def test_sort_rooms(self) -> None:
        rng = random.Random(11)
        early = 0
        for _ in range(2000):
            rooms = [_random_room(rng, room_id) for room_id in _ROOMS]
            # Joined rooms are the common case.
            rooms = [
                (r[0], r[1], Membership.JOIN, *r[3:]) if rng.random() < 0.6 else r
                for r in rooms
            ]
            joined = [r[0] for r in rooms if r[2] == Membership.JOIN]
            earliest = rng.randint(0, 50)
            to_stream = rng.randint(40, 100)
            cache = {
                room_id: rng.randint(earliest + 1, 120)
                for room_id in joined
                if rng.random() < 0.7
            }
            recheck = {
                room_id: rng.choice([None, rng.randint(1, to_stream)])
                for room_id in _ROOMS
            }
            joined_positions = {
                room_id: rng.randint(1, to_stream)
                for room_id in joined
                if rng.random() < 0.98
            }
            limit = rng.choice([None, 1, 2, 3, 5, 8])

            store = _FakeSortStore(
                _FakeStreamCache(earliest, cache), recheck, joined_positions
            )
            fake_room_lists = cast(SlidingSyncRoomLists, SimpleNamespace(store=store))
            to_token = cast(
                StreamToken,
                SimpleNamespace(room_key=RoomStreamToken(stream=to_stream)),
            )
            try:
                python: list[str] | None = [
                    room.room_id
                    for room in _run(
                        SlidingSyncRoomLists.sort_rooms(
                            fake_room_lists,
                            {room[0]: _python_room(room) for room in rooms},
                            to_token,
                            limit=limit,
                        )
                    )
                ]
            except KeyError:
                python = None
            try:
                result: list[str] | None = rust.sort_rooms_for_test(
                    rooms, earliest, cache, to_stream, limit, recheck, joined_positions
                )
            except KeyError:
                result = None
            self.assertEqual(result, python, (rooms, cache, limit))
            if python is not None and len(python) < len(rooms):
                early += 1
        self.assertGreater(early, 0)

    def test_sync_list_ops(self) -> None:
        rng = random.Random(12)
        for _ in range(2000):
            sorted_room_ids = rng.sample(_ROOMS, rng.randint(0, len(_ROOMS)))
            ranges = []
            for _ in range(rng.randint(1, 3)):
                start = rng.randint(0, 9)
                # Pydantic allows an end before the start, but not below 0.
                ranges.append((start, max(0, start + rng.randint(-1, 4))))
            config = _python_config(rng.randint(0, 10), _random_required_state(rng))
            relevant = {
                room_id: _python_config(rng.randint(0, 10), _random_required_state(rng))
                for room_id in rng.sample(_ROOMS, rng.randint(0, 3))
            }
            rust_relevant = [(k, _to_rust(v)) for k, v in relevant.items()]

            ops = _sync_list_ops(ranges, sorted_room_ids, config, relevant)
            rust_ops, rust_map = rust.sync_list_ops_for_test(
                ranges, sorted_room_ids, _to_rust(config), rust_relevant
            )
            context = (ranges, sorted_room_ids, config, rust_relevant)
            self.assertEqual(
                rust_ops,
                [(tuple(op.range), list(op.room_ids)) for op in ops],
                context,
            )
            self.assertEqual(
                [(k, (limit, _as_sets(m))) for k, (limit, m) in rust_map],
                [
                    (k, (v.timeline_limit, _as_sets(v.required_state_map)))
                    for k, v in relevant.items()
                ],
                context,
            )

    def test_rooms_to_send(self) -> None:
        rng = random.Random(13)
        now_ms = 10 * _HOUR_MS
        reset = 0
        for _ in range(1000):
            many = rng.random() < 0.2
            room_ids = [f"!many{i}:test" for i in range(130)] if many else list(_ROOMS)
            relevant = {
                room_id: _python_config(rng.randint(0, 5), _random_required_state(rng))
                for room_id in room_ids
                if rng.random() < 0.8
            }
            room_configs = {
                room_id: _python_config(rng.randint(0, 5), _random_required_state(rng))
                for room_id in room_ids
                if rng.random() < 0.5
            }
            statuses = _random_statuses(rng)
            if many:
                statuses = dict.fromkeys(room_ids, ("live", None))
            rooms_with_updates = {
                room_id for room_id in room_ids if rng.random() < (0.9 if many else 0.3)
            }
            last_used_ts = rng.choice([None, now_ms - 2 * _HOUR_MS, now_ms - 60_000])

            previous_state = PerConnectionState(
                last_used_ts=last_used_ts,
                rooms=RoomStatusMap(
                    statuses={
                        k: HaveSentRoom(
                            HaveSentRoomFlag(flag),
                            RoomStreamToken(stream=token) if token else None,
                        )
                        for k, (flag, token) in statuses.items()
                    }
                ),
                room_configs=room_configs,
            )
            fake_room_lists = cast(
                SlidingSyncRoomLists,
                SimpleNamespace(
                    store=_FakeUpdatesStore(rooms_with_updates),
                    _clock=_FakeClock(now_ms),
                ),
            )
            from_token = cast(
                StreamToken, SimpleNamespace(room_key=RoomStreamToken(stream=1))
            )
            try:
                python: list[str] | None = list(
                    _run(
                        SlidingSyncRoomLists._filter_relevant_rooms_to_send(
                            fake_room_lists, previous_state, from_token, relevant
                        )
                    )
                )
            except SlidingSyncUnknownPosition:
                python = None
                reset += 1
            try:
                result: list[str] | None = rust.rooms_to_send_for_test(
                    [(k, _to_rust(v)) for k, v in relevant.items()],
                    {k: _to_rust(v) for k, v in room_configs.items()},
                    statuses,
                    last_used_ts,
                    {room_id for room_id in relevant if room_id in rooms_with_updates},
                    now_ms,
                )
            except ValueError:
                result = None
            self.assertEqual(result, python)
        self.assertGreater(reset, 0)


class _FakeBumpStore:
    def __init__(
        self,
        latest_room_bump_stamp: int | None,
        have_finished_bg_jobs: bool,
        last_event_stream: int | None,
    ) -> None:
        self.latest_room_bump_stamp = latest_room_bump_stamp
        self.have_finished_bg_jobs = have_finished_bg_jobs
        self.last_event_stream = last_event_stream

    async def get_latest_bump_stamp_for_room(self, room_id: str) -> int | None:
        return self.latest_room_bump_stamp

    async def have_finished_sliding_sync_background_jobs(self) -> bool:
        return self.have_finished_bg_jobs

    async def get_last_event_pos_in_room_before_stream_ordering(
        self, room_id: str, end_token: RoomStreamToken, event_types: StrCollection
    ) -> tuple[str, PersistedEventPosition] | None:
        assert set(event_types) == SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
        if self.last_event_stream is None:
            return None
        return ("$event", PersistedEventPosition("master", self.last_event_stream))


class RoomHelpersTestCase(unittest.TestCase):
    """`rust/src/sliding_sync/room.rs`"""

    def test_timeline_bounds(self) -> None:
        rng = random.Random(14)
        for _ in range(2000):
            status = _random_statuses(rng).get("!r0:test", ("never", None))
            from_stream = rng.choice([None, rng.randint(1, 50)])
            skip = rng.random() < 0.2
            prev = rng.choice(
                [None, _python_config(rng.randint(0, 10), _random_required_state(rng))]
            )
            config = _python_config(rng.randint(0, 10), _random_required_state(rng))

            flag, token = status
            from_bound, initial, ignore = _timeline_bounds(
                HaveSentRoom(
                    HaveSentRoomFlag(flag),
                    RoomStreamToken(stream=token) if token else None,
                ),
                from_token=cast(
                    SlidingSyncStreamToken,
                    SimpleNamespace(
                        stream_token=SimpleNamespace(
                            room_key=RoomStreamToken(stream=from_stream)
                        )
                    ),
                )
                if from_stream is not None
                else None,
                skip_previous_bounds=skip,
                prev_room_sync_config=prev,
                room_sync_config=config,
            )
            self.assertEqual(
                rust.timeline_bounds_for_test(
                    status,
                    from_stream,
                    skip,
                    _to_rust(prev) if prev else None,
                    _to_rust(config),
                ),
                (from_bound.stream if from_bound else None, initial, ignore),
                (status, from_stream, skip, prev, config),
            )

    def test_stripped_state(self) -> None:
        rng = random.Random(15)
        for _ in range(500):
            membership = rng.choice(
                [Membership.INVITE, Membership.KNOCK, Membership.JOIN]
            )
            unsigned: dict[str, Any] = {}
            for key in ("invite_room_state", "knock_room_state"):
                choice = rng.random()
                if choice < 0.5:
                    unsigned[key] = [
                        {"type": "m.room.name", "state_key": "", "content": {}},
                        "not an event",
                    ][: rng.randint(0, 2)]
                elif choice < 0.7:
                    unsigned[key] = {"not": "a list"}
            event = {
                "type": EventTypes.Member,
                "state_key": _USER_ID,
                "sender": "@bob:test",
                "content": {"membership": membership, "displayname": "Alice"},
                "unsigned": unsigned,
            }
            python = _stripped_state(
                cast(
                    EventBase,
                    SimpleNamespace(
                        membership=membership,
                        unsigned=unsigned,
                        type=event["type"],
                        state_key=event["state_key"],
                        sender=event["sender"],
                        content=event["content"],
                    ),
                )
            )
            self.assertEqual(
                json.loads(rust.stripped_state_for_test(json.dumps(event))), python
            )

    def test_get_bump_stamp(self) -> None:
        rng = random.Random(16)
        types = [*sorted(SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES), EventTypes.Member]
        for _ in range(2000):
            timeline = [
                (rng.choice(types), rng.choice([-3, 0, rng.randint(1, 60)]))
                for _ in range(rng.randint(0, 4))
            ]
            check_outside = rng.random() < 0.7
            latest = rng.choice([None, -1, 0, rng.randint(1, 60)])
            have_finished = rng.random() < 0.7
            to_stream = rng.randint(1, 60)
            last_event = rng.choice([None, -2, rng.randint(1, 60)])

            fake_handler = cast(
                SlidingSyncHandler,
                SimpleNamespace(
                    store=_FakeBumpStore(latest, have_finished, last_event)
                ),
            )
            python = _run(
                SlidingSyncHandler._get_bump_stamp(
                    fake_handler,
                    "!r:test",
                    cast(
                        StreamToken,
                        SimpleNamespace(room_key=RoomStreamToken(stream=to_stream)),
                    ),
                    [
                        cast(
                            FilteredEvent,
                            SimpleNamespace(
                                event=SimpleNamespace(
                                    type=event_type,
                                    internal_metadata=SimpleNamespace(
                                        stream_ordering=stream
                                    ),
                                )
                            ),
                        )
                        for event_type, stream in timeline
                    ],
                    check_outside_timeline=check_outside,
                )
            )
            self.assertEqual(
                rust.get_bump_stamp_for_test(
                    timeline,
                    check_outside,
                    latest,
                    have_finished,
                    to_stream,
                    last_event,
                ),
                python,
                (timeline, check_outside, latest, have_finished, to_stream, last_event),
            )

    def test_room_config_to_persist(self) -> None:
        rng = random.Random(17)
        for _ in range(2000):
            config = _python_config(rng.randint(0, 10), _random_required_state(rng))
            prev = rng.choice(
                [None, _python_config(rng.randint(0, 10), _random_required_state(rng))]
            )
            changed: dict[str, set[str]] | None = rng.choice(
                [None, {}, _random_map(rng)]
            )
            ignore = rng.random() < 0.3
            limited = rng.choice([None, True, False])

            python = _room_config_to_persist(
                config,
                prev_room_sync_config=prev,
                changed_required_state_map=changed,
                ignore_timeline_bound=ignore,
                limited=limited,
            )
            result = rust.room_config_to_persist_for_test(
                _to_rust(config),
                _to_rust(prev) if prev else None,
                {t: sorted(keys) for t, keys in changed.items()}
                if changed is not None
                else None,
                ignore,
                limited,
            )
            context = (config, prev, changed, ignore, limited)
            if python is None:
                self.assertIsNone(result, context)
                continue
            assert result is not None
            self.assertEqual(
                (result[0], _as_sets(result[1])),
                (python.timeline_limit, _as_sets(python.required_state_map)),
                context,
            )
