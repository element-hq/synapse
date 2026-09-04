#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import itertools
from typing import (
    Collection,
    Iterable,
    Mapping,
    Sequence,
    TypeVar,
)

import attr
from parameterized import parameterized

from twisted.internet import defer

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.event_auth import auth_types_for_event
from synapse.events import EventBase
from synapse.state.v2 import (
    ConflictCache,
    _get_auth_chain_difference,
    _get_power_level_for_sender,
    lexicographical_topological_sort,
    resolve_events_with_store,
)
from synapse.storage.databases.main.event_federation import StateDifference
from synapse.types import EventID, StateMap
from synapse.util.duration import Duration

from tests import unittest
from tests.test_utils.event_builders import make_test_event

ALICE = "@alice:example.com"
BOB = "@bob:example.com"
CHARLIE = "@charlie:example.com"
EVELYN = "@evelyn:example.com"
ZARA = "@zara:example.com"

ROOM_ID = "!test:example.com"

MEMBERSHIP_CONTENT_JOIN = {"membership": Membership.JOIN}
MEMBERSHIP_CONTENT_BAN = {"membership": Membership.BAN}


ORIGIN_SERVER_TS = 0


class FakeClock:
    async def sleep(self, duration: Duration) -> None:
        return None


class FakeEvent:
    """A fake event we use as a convenience.

    NOTE: Again as a convenience we use "node_ids" rather than event_ids to
    refer to events. The event_id has node_id as localpart and example.com
    as domain.
    """

    def __init__(
        self,
        id: str,
        sender: str,
        type: str,
        state_key: str | None,
        content: Mapping[str, object],
    ):
        self.node_id = id
        self.event_id = EventID(id, "example.com").to_string()
        self.sender = sender
        self.type = type
        self.state_key = state_key
        self.content = content
        self.room_id = ROOM_ID

    def to_event(self, auth_events: list[str], prev_events: list[str]) -> EventBase:
        """Given the auth_events and prev_events, convert to a Frozen Event

        Args:
            auth_events: list of event_ids
            prev_events: list of event_ids
        """
        global ORIGIN_SERVER_TS

        ts = ORIGIN_SERVER_TS
        ORIGIN_SERVER_TS = ORIGIN_SERVER_TS + 1

        event_dict = {
            "auth_events": [(a, {}) for a in auth_events],
            "prev_events": [(p, {}) for p in prev_events],
            "event_id": self.event_id,
            "sender": self.sender,
            "type": self.type,
            "content": self.content,
            "origin_server_ts": ts,
            "room_id": ROOM_ID,
        }

        if self.state_key is not None:
            event_dict["state_key"] = self.state_key

        return make_test_event(event_dict)


# All graphs start with this set of events
INITIAL_EVENTS = [
    FakeEvent(
        id="CREATE",
        sender=ALICE,
        type=EventTypes.Create,
        state_key="",
        content={"creator": ALICE},
    ),
    FakeEvent(
        id="IMA",
        sender=ALICE,
        type=EventTypes.Member,
        state_key=ALICE,
        content=MEMBERSHIP_CONTENT_JOIN,
    ),
    FakeEvent(
        id="IPOWER",
        sender=ALICE,
        type=EventTypes.PowerLevels,
        state_key="",
        content={"users": {ALICE: 100}},
    ),
    FakeEvent(
        id="IJR",
        sender=ALICE,
        type=EventTypes.JoinRules,
        state_key="",
        content={"join_rule": JoinRules.PUBLIC},
    ),
    FakeEvent(
        id="IMB",
        sender=BOB,
        type=EventTypes.Member,
        state_key=BOB,
        content=MEMBERSHIP_CONTENT_JOIN,
    ),
    FakeEvent(
        id="IMC",
        sender=CHARLIE,
        type=EventTypes.Member,
        state_key=CHARLIE,
        content=MEMBERSHIP_CONTENT_JOIN,
    ),
    FakeEvent(
        id="IMZ",
        sender=ZARA,
        type=EventTypes.Member,
        state_key=ZARA,
        content=MEMBERSHIP_CONTENT_JOIN,
    ),
    FakeEvent(
        id="START", sender=ZARA, type=EventTypes.Message, state_key=None, content={}
    ),
    FakeEvent(
        id="END", sender=ZARA, type=EventTypes.Message, state_key=None, content={}
    ),
]

INITIAL_EDGES = ["START", "IMZ", "IMC", "IMB", "IJR", "IPOWER", "IMA", "CREATE"]

ZARA_KEY = (EventTypes.Member, ZARA)
TOPIC_KEY = (EventTypes.Topic, "")


def _member(node_id: str, sender: str, state_key: str, content: dict) -> FakeEvent:
    return FakeEvent(
        id=node_id,
        sender=sender,
        type=EventTypes.Member,
        state_key=state_key,
        content=content,
    )


# Events for the conflict cache tests, all hanging off START.
#
# Bob sets the topic twice. He has no power under IPOWER, so both fail auth
# unless PA, which gives him some, is in the unconflicted state.
#
# Zara joins again on two branches and invites Evelyn from the end of each. The
# invites are what put the second pair of Zara joins into the auth chain
# difference. See `test_conflict_cache_key_repartitioned`.
CACHE_TEST_CASE_EVENTS = [
    FakeEvent(
        id="PA",
        sender=ALICE,
        type=EventTypes.PowerLevels,
        state_key="",
        content={"users": {ALICE: 100, BOB: 50}},
    ),
    FakeEvent(id="T1", sender=BOB, type=EventTypes.Topic, state_key="", content={}),
    FakeEvent(id="T2", sender=BOB, type=EventTypes.Topic, state_key="", content={}),
    _member("ZJ1", ZARA, ZARA, MEMBERSHIP_CONTENT_JOIN),
    _member("ZJ2", ZARA, ZARA, MEMBERSHIP_CONTENT_JOIN),
    _member("INV1", ZARA, EVELYN, {"membership": Membership.INVITE}),
    _member("INV2", ZARA, EVELYN, {"membership": Membership.INVITE}),
]

CACHE_TEST_CASE_EDGES = [
    ["PA", "START"],
    ["T1", "START"],
    ["T2", "START"],
    ["INV1", "ZJ1", "START"],
    ["INV2", "ZJ2", "START"],
]

# A room version that resolves with plain v2, and one that resolves with v2.1.
# They share their auth rules, so the two only differ in the way that matters
# to the conflict cache: v2.1 starts the iterative auth checks from the empty
# state, plain v2 from the unconflicted state.
V2_ROOM = RoomVersions.V11
V21_ROOM = RoomVersions.HydraV11


class StateTestCase(unittest.TestCase):
    def test_ban_vs_pl(self) -> None:
        events = [
            FakeEvent(
                id="PA",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="MA",
                sender=ALICE,
                type=EventTypes.Member,
                state_key=ALICE,
                content={"membership": Membership.JOIN},
            ),
            FakeEvent(
                id="MB",
                sender=ALICE,
                type=EventTypes.Member,
                state_key=BOB,
                content={"membership": Membership.BAN},
            ),
            FakeEvent(
                id="PB",
                sender=BOB,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
        ]

        edges = [["END", "MB", "MA", "PA", "START"], ["END", "PB", "PA"]]

        expected_state_ids = ["PA", "MA", "MB"]

        self.do_check(events, edges, expected_state_ids)

    def test_join_rule_evasion(self) -> None:
        events = [
            FakeEvent(
                id="JR",
                sender=ALICE,
                type=EventTypes.JoinRules,
                state_key="",
                content={"join_rules": JoinRules.PRIVATE},
            ),
            FakeEvent(
                id="ME",
                sender=EVELYN,
                type=EventTypes.Member,
                state_key=EVELYN,
                content={"membership": Membership.JOIN},
            ),
        ]

        edges = [["END", "JR", "START"], ["END", "ME", "START"]]

        expected_state_ids = ["JR"]

        self.do_check(events, edges, expected_state_ids)

    def test_offtopic_pl(self) -> None:
        events = [
            FakeEvent(
                id="PA",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="PB",
                sender=BOB,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50, CHARLIE: 50}},
            ),
            FakeEvent(
                id="PC",
                sender=CHARLIE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50, CHARLIE: 0}},
            ),
        ]

        edges = [["END", "PC", "PB", "PA", "START"], ["END", "PA"]]

        expected_state_ids = ["PC"]

        self.do_check(events, edges, expected_state_ids)

    def test_topic_basic(self) -> None:
        events = [
            FakeEvent(
                id="T1", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA1",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T2", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA2",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 0}},
            ),
            FakeEvent(
                id="PB",
                sender=BOB,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T3", sender=BOB, type=EventTypes.Topic, state_key="", content={}
            ),
        ]

        edges = [["END", "PA2", "T2", "PA1", "T1", "START"], ["END", "T3", "PB", "PA1"]]

        expected_state_ids = ["PA2", "T2"]

        self.do_check(events, edges, expected_state_ids)

    def test_topic_reset(self) -> None:
        events = [
            FakeEvent(
                id="T1", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T2", sender=BOB, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="MB",
                sender=ALICE,
                type=EventTypes.Member,
                state_key=BOB,
                content={"membership": Membership.BAN},
            ),
        ]

        edges = [["END", "MB", "T2", "PA", "T1", "START"], ["END", "T1"]]

        expected_state_ids = ["T1", "MB", "PA"]

        self.do_check(events, edges, expected_state_ids)

    def test_topic(self) -> None:
        events = [
            FakeEvent(
                id="T1", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA1",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T2", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA2",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 0}},
            ),
            FakeEvent(
                id="PB",
                sender=BOB,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T3", sender=BOB, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="MZ1",
                sender=ZARA,
                type=EventTypes.Message,
                state_key=None,
                content={},
            ),
            FakeEvent(
                id="T4", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
        ]

        edges = [
            ["END", "T4", "MZ1", "PA2", "T2", "PA1", "T1", "START"],
            ["END", "MZ1", "T3", "PB", "PA1"],
        ]

        expected_state_ids = ["T4", "PA2"]

        self.do_check(events, edges, expected_state_ids)

    def test_mainline_sort(self) -> None:
        """Tests that the mainline ordering works correctly."""

        events = [
            FakeEvent(
                id="T1", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA1",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T2", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="PA2",
                sender=ALICE,
                type=EventTypes.PowerLevels,
                state_key="",
                content={
                    "users": {ALICE: 100, BOB: 50},
                    "events": {EventTypes.PowerLevels: 100},
                },
            ),
            FakeEvent(
                id="PB",
                sender=BOB,
                type=EventTypes.PowerLevels,
                state_key="",
                content={"users": {ALICE: 100, BOB: 50}},
            ),
            FakeEvent(
                id="T3", sender=BOB, type=EventTypes.Topic, state_key="", content={}
            ),
            FakeEvent(
                id="T4", sender=ALICE, type=EventTypes.Topic, state_key="", content={}
            ),
        ]

        edges = [
            ["END", "T3", "PA2", "T2", "PA1", "T1", "START"],
            ["END", "T4", "PB", "PA1"],
        ]

        # We expect T3 to be picked as the other topics are pointing at older
        # power levels. Note that without mainline ordering we'd pick T4 due to
        # it being sent *after* T3.
        expected_state_ids = ["T3", "PA2"]

        self.do_check(events, edges, expected_state_ids)

    # The conflict cache tests below resolve `CACHE_TEST_CASE_EVENTS` with a plain dict
    # as the cache. A miss adds an entry to it and a hit does not, so its size
    # afterwards is the number of times the conflicted set was resolved.

    def _build_cache_scenario(self) -> None:
        self.event_map, self.state_at_event = self.build_event_graph(
            CACHE_TEST_CASE_EVENTS, CACHE_TEST_CASE_EDGES
        )

    def _state(self, *node_ids: str) -> StateMap[str]:
        """The state at START with the given events applied on top."""
        state = dict(self.state_at_event["START"])
        for node_id in node_ids:
            event = self.event_map[EventID(node_id, "example.com").to_string()]
            state[(event.type, event.state_key)] = event.event_id
        return state

    def _resolve_with_cache(
        self,
        room_version: RoomVersion,
        state_sets: Sequence[StateMap[str]],
        conflict_cache: ConflictCache,
    ) -> StateMap[str]:
        return self.successResultOf(
            defer.ensureDeferred(
                resolve_events_with_store(
                    FakeClock(),
                    ROOM_ID,
                    room_version,
                    state_sets,
                    event_map=None,
                    state_res_store=TestStateResolutionStore(self.event_map),
                    conflict_cache=conflict_cache,
                )
            )
        )

    @parameterized.expand((V2_ROOM, V21_ROOM))
    def test_conflict_cache_shared_across_unconflicted_state(
        self, room_version: RoomVersion
    ) -> None:
        """Two calls with the same conflicted set share a cache entry, and each
        still gets its own unconflicted state back.

        Zara's membership is the unconflicted key that differs. Nothing in the
        conflict over the topic is authed against it, so under plain v2 it
        stays out of the cache key too.
        """
        self._build_cache_scenario()
        conflict_cache: dict[bytes, StateMap[str]] = {}

        with_zara = [self._state("PA", "T1"), self._state("PA", "T2")]
        without_zara = [
            {key: value for key, value in state.items() if key != ZARA_KEY}
            for state in with_zara
        ]

        first = self._resolve_with_cache(room_version, with_zara, conflict_cache)
        second = self._resolve_with_cache(room_version, without_zara, conflict_cache)
        self.assertEqual(len(conflict_cache), 1, "expected a cache hit")

        self.assertIn(ZARA_KEY, first)
        self.assertNotIn(ZARA_KEY, second)

        # Everything else agrees.
        self.assertEqual({k: v for k, v in first.items() if k != ZARA_KEY}, second)

    def test_conflict_cache_keys_on_base_state(self) -> None:
        """Changing an unconflicted key that the auth checks read is a miss
        under plain v2, and the two resolutions reach different answers."""
        self._build_cache_scenario()

        powerless = [self._state("T1"), self._state("T2")]
        powered = [self._state("PA", "T1"), self._state("PA", "T2")]

        conflict_cache: dict[bytes, StateMap[str]] = {}
        under_ipower = self._resolve_with_cache(V2_ROOM, powerless, conflict_cache)
        under_pa = self._resolve_with_cache(V2_ROOM, powered, conflict_cache)
        self.assertEqual(len(conflict_cache), 2, "expected a cache miss")

        # Bob's topics fail auth under IPOWER and pass under PA.
        self.assertNotIn(TOPIC_KEY, under_ipower)
        self.assertIn(TOPIC_KEY, under_pa)

    def test_conflict_cache_key_repartitioned(self) -> None:
        """The same conflicted set can split into different conflicted keys.

        Zara's membership is unconflicted in the first call, with the joins
        that compete for it reaching the conflicted set through the auth chain
        difference of the two invites. In the second call it is a conflicted
        key. Both calls have the same cache key, so what is cached has to carry
        Zara's key even though the first call's own answer for it came from
        its unconflicted state.
        """
        self._build_cache_scenario()

        agreed = [self._state("INV1"), self._state("INV2")]
        conflicting = [self._state("ZJ1", "INV1"), self._state("ZJ2", "INV2")]

        conflict_cache: dict[bytes, StateMap[str]] = {}
        agreed_result = self._resolve_with_cache(V21_ROOM, agreed, conflict_cache)
        warm = self._resolve_with_cache(V21_ROOM, conflicting, conflict_cache)
        self.assertEqual(len(conflict_cache), 1, "expected a cache hit")

        # The unconflicted state wins for the call that had one.
        self.assertEqual(agreed_result[ZARA_KEY], self._state()[ZARA_KEY])

        # The second call gets the winner from the cached resolution, the same
        # one it computes from cold.
        conflict_cache.clear()
        cold = self._resolve_with_cache(V21_ROOM, conflicting, conflict_cache)
        self.assertNotEqual(warm[ZARA_KEY], agreed_result[ZARA_KEY])
        self.assertEqual(warm, cold)

    def build_event_graph(
        self,
        events: list[FakeEvent],
        edges: list[list[str]],
    ) -> tuple[dict[str, EventBase], dict[str, StateMap[str]]]:
        """Build the graph of `INITIAL_EVENTS` plus `events`.

        Args:
            events
            edges: A list of chains of event edges, e.g.
                `[[A, B, C]]` are edges A->B and B->C.

        Returns:
            The events by event ID, and the state after each node ID.
        """
        # We want to sort the events into topological order for processing.
        graph: dict[str, set[str]] = {}

        fake_event_map: dict[str, FakeEvent] = {}

        for ev in itertools.chain(INITIAL_EVENTS, events):
            graph[ev.node_id] = set()
            fake_event_map[ev.node_id] = ev

        for a, b in pairwise(INITIAL_EDGES):
            graph[a].add(b)

        for edge_list in edges:
            for a, b in pairwise(edge_list):
                graph[a].add(b)

        event_map: dict[str, EventBase] = {}
        state_at_event: dict[str, StateMap[str]] = {}

        # We copy the map as the sort consumes the graph
        graph_copy = {k: set(v) for k, v in graph.items()}

        for node_id in lexicographical_topological_sort(graph_copy, key=lambda e: e):
            fake_event = fake_event_map[node_id]
            event_id = fake_event.event_id

            prev_events = list(graph[node_id])

            state_before: StateMap[str]
            if len(prev_events) == 0:
                state_before = {}
            elif len(prev_events) == 1:
                state_before = dict(state_at_event[prev_events[0]])
            else:
                state_d = resolve_events_with_store(
                    FakeClock(),
                    ROOM_ID,
                    RoomVersions.V2,
                    [state_at_event[n] for n in prev_events],
                    event_map=event_map,
                    state_res_store=TestStateResolutionStore(event_map),
                )

                state_before = self.successResultOf(defer.ensureDeferred(state_d))

            state_after = dict(state_before)
            if fake_event.state_key is not None:
                state_after[(fake_event.type, fake_event.state_key)] = event_id

            # This type ignore is a bit sad. Things we have tried:
            # 1. Define a `GenericEvent` Protocol satisfied by FakeEvent, EventBase and
            #    EventBuilder. But this is Hard because the relevant attributes are
            #    DictProperty[T] descriptors on EventBase but normal Ts on FakeEvent.
            # 2. Define a `GenericEvent` Protocol describing `FakeEvent` only, and
            #    change this function to accept Event | EventBase | EventBuilder.
            #    This seems reasonable to me, but mypy isn't happy. I think that's
            #    a mypy bug, see https://github.com/python/mypy/issues/5570
            # Instead, resort to a type-ignore.
            auth_types = set(auth_types_for_event(RoomVersions.V6, fake_event))  # type: ignore[arg-type]

            auth_events = []
            for key in auth_types:
                if key in state_before:
                    auth_events.append(state_before[key])

            event = fake_event.to_event(auth_events, prev_events)

            state_at_event[node_id] = state_after
            event_map[event_id] = event

        return event_map, state_at_event

    def do_check(
        self,
        events: list[FakeEvent],
        edges: list[list[str]],
        expected_state_ids: list[str],
    ) -> None:
        """Take a list of events and edges and calculate the state of the
        graph at END, and asserts it matches `expected_state_ids`

        Args:
            events
            edges: A list of chains of event edges, e.g.
                `[[A, B, C]]` are edges A->B and B->C.
            expected_state_ids: The expected state at END, (excluding
                the keys that haven't changed since START).
        """
        event_map, state_at_event = self.build_event_graph(events, edges)

        expected_state = {}
        for node_id in expected_state_ids:
            # expected_state_ids are node IDs rather than event IDs,
            # so we have to convert
            event_id = EventID(node_id, "example.com").to_string()
            event = event_map[event_id]

            key = (event.type, event.state_key)

            expected_state[key] = event_id

        start_state = state_at_event["START"]
        end_state = {
            key: value
            for key, value in state_at_event["END"].items()
            if key in expected_state or start_state.get(key) != value
        }

        self.assertEqual(expected_state, end_state)


class LexicographicalTestCase(unittest.TestCase):
    def test_simple(self) -> None:
        graph: dict[str, set[str]] = {
            "l": {"o"},
            "m": {"n", "o"},
            "n": {"o"},
            "o": set(),
            "p": {"o"},
        }

        res = list(lexicographical_topological_sort(graph, key=lambda x: x))

        self.assertEqual(["o", "l", "n", "m", "p"], res)


class SimpleParamStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # We build up a simple DAG.

        event_map = {}

        create_event = FakeEvent(
            id="CREATE",
            sender=ALICE,
            type=EventTypes.Create,
            state_key="",
            content={"creator": ALICE},
        ).to_event([], [])
        event_map[create_event.event_id] = create_event

        alice_member = FakeEvent(
            id="IMA",
            sender=ALICE,
            type=EventTypes.Member,
            state_key=ALICE,
            content=MEMBERSHIP_CONTENT_JOIN,
        ).to_event([create_event.event_id], [create_event.event_id])
        event_map[alice_member.event_id] = alice_member

        join_rules = FakeEvent(
            id="IJR",
            sender=ALICE,
            type=EventTypes.JoinRules,
            state_key="",
            content={"join_rule": JoinRules.PUBLIC},
        ).to_event(
            auth_events=[create_event.event_id, alice_member.event_id],
            prev_events=[alice_member.event_id],
        )
        event_map[join_rules.event_id] = join_rules

        # Bob and Charlie join at the same time, so there is a fork
        bob_member = FakeEvent(
            id="IMB",
            sender=BOB,
            type=EventTypes.Member,
            state_key=BOB,
            content=MEMBERSHIP_CONTENT_JOIN,
        ).to_event(
            auth_events=[create_event.event_id, join_rules.event_id],
            prev_events=[join_rules.event_id],
        )
        event_map[bob_member.event_id] = bob_member

        charlie_member = FakeEvent(
            id="IMC",
            sender=CHARLIE,
            type=EventTypes.Member,
            state_key=CHARLIE,
            content=MEMBERSHIP_CONTENT_JOIN,
        ).to_event(
            auth_events=[create_event.event_id, join_rules.event_id],
            prev_events=[join_rules.event_id],
        )
        event_map[charlie_member.event_id] = charlie_member

        self.event_map = event_map
        self.create_event = create_event
        self.alice_member = alice_member
        self.join_rules = join_rules
        self.bob_member = bob_member
        self.charlie_member = charlie_member

        self.state_at_bob = {
            (e.type, e.state_key): e.event_id
            for e in [create_event, alice_member, join_rules, bob_member]
        }

        self.state_at_charlie = {
            (e.type, e.state_key): e.event_id
            for e in [create_event, alice_member, join_rules, charlie_member]
        }

        self.expected_combined_state = {
            (e.type, e.state_key): e.event_id
            for e in [
                create_event,
                alice_member,
                join_rules,
                bob_member,
                charlie_member,
            ]
        }

    def test_event_map_none(self) -> None:
        # Test that we correctly handle passing `None` as the event_map

        state_d = resolve_events_with_store(
            FakeClock(),
            ROOM_ID,
            RoomVersions.V2,
            [self.state_at_bob, self.state_at_charlie],
            event_map=None,
            state_res_store=TestStateResolutionStore(self.event_map),
        )

        state = self.successResultOf(defer.ensureDeferred(state_d))

        self.assert_dict(self.expected_combined_state, state)


class AuthChainDifferenceTestCase(unittest.TestCase):
    """We test that `_get_auth_chain_difference` correctly handles unpersisted
    events.
    """

    def test_simple(self) -> None:
        # Test getting the auth difference for a simple chain with a single
        # unpersisted event:
        #
        #  Unpersisted | Persisted
        #              |
        #           C -|-> B -> A

        a = FakeEvent(
            id="A",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([], [])

        b = FakeEvent(
            id="B",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([a.event_id], [])

        c = FakeEvent(
            id="C",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([b.event_id], [])

        persisted_events = {a.event_id: a, b.event_id: b}
        unpersited_events = {c.event_id: c}

        state_sets = [
            {("a", ""): a.event_id, ("b", ""): b.event_id},
            {("c", ""): c.event_id},
        ]

        store = TestStateResolutionStore(persisted_events)

        diff_d = _get_auth_chain_difference(
            ROOM_ID,
            state_sets,
            unpersited_events,
            store,
            None,
        )
        difference = self.successResultOf(defer.ensureDeferred(diff_d))

        self.assertEqual(difference, {c.event_id})

    def test_multiple_unpersisted_chain(self) -> None:
        # Test getting the auth difference for a simple chain with multiple
        # unpersisted events:
        #
        #  Unpersisted | Persisted
        #              |
        #      D -> C -|-> B -> A

        a = FakeEvent(
            id="A",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([], [])

        b = FakeEvent(
            id="B",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([a.event_id], [])

        c = FakeEvent(
            id="C",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([b.event_id], [])

        d = FakeEvent(
            id="D",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([c.event_id], [])

        persisted_events = {a.event_id: a, b.event_id: b}
        unpersited_events = {c.event_id: c, d.event_id: d}

        state_sets = [
            {("a", ""): a.event_id, ("b", ""): b.event_id},
            {("c", ""): c.event_id, ("d", ""): d.event_id},
        ]

        store = TestStateResolutionStore(persisted_events)

        diff_d = _get_auth_chain_difference(
            ROOM_ID,
            state_sets,
            unpersited_events,
            store,
            None,
        )
        difference = self.successResultOf(defer.ensureDeferred(diff_d))

        self.assertEqual(difference, {d.event_id, c.event_id})

    def test_unpersisted_events_different_sets(self) -> None:
        # Test getting the auth difference for with multiple unpersisted events
        # in different branches:
        #
        #  Unpersisted | Persisted
        #              |
        #     D --> C -|-> B -> A
        #     E ----^ -|---^
        #              |

        a = FakeEvent(
            id="A",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([], [])

        b = FakeEvent(
            id="B",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([a.event_id], [])

        c = FakeEvent(
            id="C",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([b.event_id], [])

        d = FakeEvent(
            id="D",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([c.event_id], [])

        e = FakeEvent(
            id="E",
            sender=ALICE,
            type=EventTypes.Member,
            state_key="",
            content={},
        ).to_event([c.event_id, b.event_id], [])

        persisted_events = {a.event_id: a, b.event_id: b}
        unpersited_events = {c.event_id: c, d.event_id: d, e.event_id: e}

        state_sets = [
            {("a", ""): a.event_id, ("b", ""): b.event_id, ("e", ""): e.event_id},
            {("c", ""): c.event_id, ("d", ""): d.event_id},
        ]

        store = TestStateResolutionStore(persisted_events)

        diff_d = _get_auth_chain_difference(
            ROOM_ID,
            state_sets,
            unpersited_events,
            store,
            None,
        )
        difference = self.successResultOf(defer.ensureDeferred(diff_d))

        self.assertEqual(difference, {d.event_id, e.event_id})

    def test_get_power_level_for_sender(self) -> None:
        """Test that we use the correct definition of `creator` depending
        on room version"""
        store = TestStateResolutionStore({})
        for room_version in [RoomVersions.V10, RoomVersions.V11]:
            create_event = make_test_event(
                {
                    "room_id": ROOM_ID,
                    "sender": ALICE,
                    "type": EventTypes.Create,
                    "state_key": "",
                    "content": {
                        "room_version": room_version.identifier,
                    }
                    # conditionally add 'creator' if the version doesn't use implicit room creators
                    | (
                        {"creator": ALICE}
                        if not room_version.implicit_room_creator
                        else {}
                    ),
                },
                room_version=room_version,
            )
            member_event = make_test_event(
                {
                    "room_id": ROOM_ID,
                    "sender": ALICE,
                    "type": EventTypes.Member,
                    "state_key": ALICE,
                    "content": {
                        "membership": "join",
                    },
                    "auth_events": [create_event.event_id],
                    "prev_events": [create_event.event_id],
                },
                room_version=room_version,
            )
            pl_event = make_test_event(
                {
                    "room_id": ROOM_ID,
                    "sender": ALICE,
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": {
                        "users": {
                            ALICE: 100,
                            BOB: 50,
                        },
                        "users_default": 10,
                    },
                    "auth_events": [create_event.event_id, member_event.event_id],
                    "prev_events": [member_event.event_id],
                },
                room_version=room_version,
            )

            event_map = {
                create_event.event_id: create_event,
                member_event.event_id: member_event,
                pl_event.event_id: pl_event,
            }
            want_pls = {
                ALICE: 100,
                BOB: 50,
                CHARLIE: 10,
            }
            for user_id, want_pl in want_pls.items():
                test_event = make_test_event(
                    {
                        "room_id": ROOM_ID,
                        "sender": user_id,
                        "type": EventTypes.Topic,
                        "state_key": "",
                        "content": {"topic": "Test"},
                        "auth_events": [
                            create_event.event_id,
                            member_event.event_id,
                            pl_event.event_id,
                        ],
                        "prev_events": [pl_event.event_id],
                    },
                    room_version=room_version,
                )
                event_map[test_event.event_id] = test_event
                got_pl = self.successResultOf(
                    defer.ensureDeferred(
                        _get_power_level_for_sender(
                            ROOM_ID, test_event.event_id, event_map, store
                        )
                    )
                )
                self.assertEqual(
                    got_pl,
                    want_pl,
                    f"wrong pl for {user_id} on v{room_version.identifier}",
                )

            # the creator alone without PL is 100, everyone else is 0
            want_pls = {
                ALICE: 100,
                BOB: 0,
                CHARLIE: 0,
            }
            for user_id, want_pl in want_pls.items():
                test_event = make_test_event(
                    {
                        "room_id": ROOM_ID,
                        "sender": user_id,
                        "type": EventTypes.Topic,
                        "state_key": "",
                        "content": {"topic": "Test"},
                        "auth_events": [
                            create_event.event_id,
                            member_event.event_id,
                            pl_event.event_id,
                        ],
                        "prev_events": [pl_event.event_id],
                    },
                    room_version=room_version,
                )
                got_pl = self.successResultOf(
                    defer.ensureDeferred(
                        _get_power_level_for_sender(
                            ROOM_ID,
                            test_event.event_id,
                            {
                                test_event.event_id: test_event,
                                create_event.event_id: create_event,
                            },
                            store,
                        )
                    )
                )
                self.assertEqual(
                    got_pl,
                    want_pl,
                    f"wrong pl for {user_id} with no PL event on v{room_version.identifier}",
                )


T = TypeVar("T")


def pairwise(iterable: Iterable[T]) -> Iterable[tuple[T, T]]:
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = itertools.tee(iterable)
    next(b, None)
    return zip(a, b)


@attr.s
class TestStateResolutionStore:
    event_map: dict[str, EventBase] = attr.ib()

    def get_events(
        self, event_ids: Collection[str], allow_rejected: bool = False
    ) -> "defer.Deferred[dict[str, EventBase]]":
        """Get events from the database

        Args:
            event_ids: The event_ids of the events to fetch
            allow_rejected: If True return rejected events.

        Returns:
            Dict from event_id to event.
        """

        return defer.succeed(
            {eid: self.event_map[eid] for eid in event_ids if eid in self.event_map}
        )

    def _get_auth_chain(self, event_ids: Iterable[str]) -> list[str]:
        """Gets the full auth chain for a set of events (including rejected
        events).

        Includes the given event IDs in the result.

        Note that:
            1. All events must be state events.
            2. For v1 rooms this may not have the full auth chain in the
               presence of rejected events

        Args:
            event_ids: The event IDs of the events to fetch the auth
                chain for. Must be state events.
        Returns:
            List of event IDs of the auth chain.
        """

        # Simple DFS for auth chain
        result = set()
        stack = list(event_ids)
        while stack:
            event_id = stack.pop()
            if event_id in result:
                continue

            result.add(event_id)

            event = self.event_map[event_id]
            for aid in event.auth_event_ids():
                stack.append(aid)

        return list(result)

    def get_auth_chain_difference(
        self,
        room_id: str,
        auth_sets: list[set[str]],
        conflicted_state: set[str] | None,
        additional_backwards_reachable_conflicted_events: set[str] | None,
    ) -> "defer.Deferred[StateDifference]":
        chains = [frozenset(self._get_auth_chain(a)) for a in auth_sets]

        common = set(chains[0]).intersection(*chains[1:])
        return defer.succeed(
            StateDifference(
                auth_difference=set(chains[0]).union(*chains[1:]) - common,
                conflicted_subgraph=set(),
            ),
        )
