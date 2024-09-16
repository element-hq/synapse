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

import datetime
from typing import (
    Collection,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Set,
    Tuple,
    TypeVar,
    Union,
    cast,
)

import attr
from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.room_versions import (
    KNOWN_ROOM_VERSIONS,
    EventFormatVersions,
    RoomVersion,
)
from synapse.events import EventBase
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.types import Cursor
from synapse.synapse_rust.events import EventInternalMetadata
from synapse.types import JsonDict
from synapse.util import Clock, json_encoder

import tests.unittest
import tests.utils

# The silly auth graph we use to test the auth difference algorithm,
# where the top are the most recent events.
#
#   A   B
#    \ /
#  D  E
#  \  |
#   ` F   C
#     |  /|
#     G ´ |
#     | \ |
#     H   I
#     |   |
#     K   J

AUTH_GRAPH: Dict[str, List[str]] = {
    "a": ["e"],
    "b": ["e"],
    "c": ["g", "i"],
    "d": ["f"],
    "e": ["f"],
    "f": ["g"],
    "g": ["h", "i"],
    "h": ["k"],
    "i": ["j"],
    "k": [],
    "j": [],
}

DEPTH_GRAPH = {
    "a": 7,
    "b": 7,
    "c": 4,
    "d": 6,
    "e": 6,
    "f": 5,
    "g": 3,
    "h": 2,
    "i": 2,
    "k": 1,
    "j": 1,
}

T = TypeVar("T")


def get_all_topologically_sorted_orders(
    nodes: Iterable[T],
    graph: Mapping[T, Collection[T]],
) -> List[List[T]]:
    """Given a set of nodes and a graph, return all possible topological
    orderings.
    """

    # This is implemented by Kahn's algorithm, and forking execution each time
    # we have a choice over which node to consider next.

    degree_map = {node: 0 for node in nodes}
    reverse_graph: Dict[T, Set[T]] = {}

    for node, edges in graph.items():
        if node not in degree_map:
            continue

        for edge in set(edges):
            if edge in degree_map:
                degree_map[node] += 1

            reverse_graph.setdefault(edge, set()).add(node)
        reverse_graph.setdefault(node, set())

    zero_degree = [node for node, degree in degree_map.items() if degree == 0]

    return _get_all_topologically_sorted_orders_inner(
        reverse_graph, zero_degree, degree_map
    )


def _get_all_topologically_sorted_orders_inner(
    reverse_graph: Dict[T, Set[T]],
    zero_degree: List[T],
    degree_map: Dict[T, int],
) -> List[List[T]]:
    new_paths = []

    # Rather than only choosing *one* item from the list of nodes with zero
    # degree, we "fork" execution and run the algorithm for each node in the
    # zero degree.
    for node in zero_degree:
        new_degree_map = degree_map.copy()
        new_zero_degree = zero_degree.copy()
        new_zero_degree.remove(node)

        for edge in reverse_graph.get(node, []):
            if edge in new_degree_map:
                new_degree_map[edge] -= 1
                if new_degree_map[edge] == 0:
                    new_zero_degree.append(edge)

        paths = _get_all_topologically_sorted_orders_inner(
            reverse_graph, new_zero_degree, new_degree_map
        )
        for path in paths:
            path.insert(0, node)

        new_paths.extend(paths)

    if not new_paths:
        return [[]]

    return new_paths


def get_all_topologically_consistent_subsets(
    nodes: Iterable[T],
    graph: Mapping[T, Collection[T]],
) -> Set[FrozenSet[T]]:
    """Get all subsets of the graph where if node N is in the subgraph, then all
    nodes that can reach that node (i.e. for all X there exists a path X -> N)
    are in the subgraph.
    """
    all_topological_orderings = get_all_topologically_sorted_orders(nodes, graph)

    graph_subsets = set()
    for ordering in all_topological_orderings:
        ordering.reverse()

        for idx in range(len(ordering)):
            graph_subsets.add(frozenset(ordering[:idx]))

    return graph_subsets


@attr.s(auto_attribs=True, frozen=True, slots=True)
class _BackfillSetupInfo:
    room_id: str
    depth_map: Dict[str, int]


class EventFederationWorkerStoreTestCase(tests.unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persist_events = hs.get_datastores().persist_events
        assert persist_events is not None
        self.persist_events = persist_events

    def test_get_prev_events_for_room(self) -> None:
        room_id = "@ROOM:local"

        # add a bunch of events and hashes to act as forward extremities
        def insert_event(txn: Cursor, i: int) -> None:
            event_id = "$event_%i:local" % i

            txn.execute(
                (
                    "INSERT INTO events ("
                    "   room_id, event_id, type, depth, topological_ordering,"
                    "   content, processed, outlier, stream_ordering) "
                    "VALUES (?, ?, 'm.test', ?, ?, 'test', ?, ?, ?)"
                ),
                (room_id, event_id, i, i, True, False, i),
            )

            txn.execute(
                (
                    "INSERT INTO event_forward_extremities (room_id, event_id) "
                    "VALUES (?, ?)"
                ),
                (room_id, event_id),
            )

        for i in range(20):
            self.get_success(
                self.store.db_pool.runInteraction("insert", insert_event, i)
            )

        # this should get the last ten
        r = self.get_success(self.store.get_prev_events_for_room(room_id))
        self.assertEqual(10, len(r))
        for i in range(10):
            self.assertEqual("$event_%i:local" % (19 - i), r[i])

    def test_get_rooms_with_many_extremities(self) -> None:
        room1 = "#room1"
        room2 = "#room2"
        room3 = "#room3"

        def insert_event(txn: LoggingTransaction, i: int, room_id: str) -> None:
            event_id = "$event_%i:local" % i

            # We need to insert into events table to get around the foreign key constraint.
            self.store.db_pool.simple_insert_txn(
                txn,
                table="events",
                values={
                    "instance_name": "master",
                    "stream_ordering": self.store._stream_id_gen.get_next_txn(txn),
                    "topological_ordering": 1,
                    "depth": 1,
                    "event_id": event_id,
                    "room_id": room_id,
                    "type": EventTypes.Message,
                    "processed": True,
                    "outlier": False,
                    "origin_server_ts": 0,
                    "received_ts": 0,
                    "sender": "@user:local",
                    "contains_url": False,
                    "state_key": None,
                    "rejection_reason": None,
                },
            )

            txn.execute(
                (
                    "INSERT INTO event_forward_extremities (room_id, event_id) "
                    "VALUES (?, ?)"
                ),
                (room_id, event_id),
            )

        for i in range(20):
            self.get_success(
                self.store.db_pool.runInteraction("insert", insert_event, i, room1)
            )
            self.get_success(
                self.store.db_pool.runInteraction(
                    "insert", insert_event, i + 100, room2
                )
            )
            self.get_success(
                self.store.db_pool.runInteraction(
                    "insert", insert_event, i + 200, room3
                )
            )

        # Test simple case
        r = self.get_success(self.store.get_rooms_with_many_extremities(5, 5, []))
        self.assertEqual(len(r), 3)

        # Does filter work?

        r = self.get_success(self.store.get_rooms_with_many_extremities(5, 5, [room1]))
        self.assertTrue(room2 in r)
        self.assertTrue(room3 in r)
        self.assertEqual(len(r), 2)

        r = self.get_success(
            self.store.get_rooms_with_many_extremities(5, 5, [room1, room2])
        )
        self.assertEqual(r, [room3])

        # Does filter and limit work?

        r = self.get_success(self.store.get_rooms_with_many_extremities(5, 1, [room1]))
        self.assertTrue(r == [room2] or r == [room3])

    def _setup_auth_chain(self, use_chain_cover_index: bool) -> str:
        room_id = "@ROOM:local"

        # Mark the room as maybe having a cover index.

        def store_room(txn: LoggingTransaction) -> None:
            self.store.db_pool.simple_insert_txn(
                txn,
                "rooms",
                {
                    "room_id": room_id,
                    "creator": "room_creator_user_id",
                    "is_public": True,
                    "room_version": "6",
                    "has_auth_chain_index": use_chain_cover_index,
                },
            )

        self.get_success(self.store.db_pool.runInteraction("store_room", store_room))

        # We rudely fiddle with the appropriate tables directly, as that's much
        # easier than constructing events properly.

        def insert_event(txn: LoggingTransaction) -> None:
            stream_ordering = 0

            for event_id in AUTH_GRAPH:
                stream_ordering += 1
                depth = DEPTH_GRAPH[event_id]

                self.store.db_pool.simple_insert_txn(
                    txn,
                    table="events",
                    values={
                        "event_id": event_id,
                        "room_id": room_id,
                        "depth": depth,
                        "topological_ordering": depth,
                        "type": "m.test",
                        "processed": True,
                        "outlier": False,
                        "stream_ordering": stream_ordering,
                    },
                )

            events = [
                cast(EventBase, FakeEvent(event_id, room_id, AUTH_GRAPH[event_id]))
                for event_id in AUTH_GRAPH
            ]
            new_event_links = (
                self.persist_events.calculate_chain_cover_index_for_events_txn(
                    txn, room_id, [e for e in events if e.is_state()]
                )
            )
            self.persist_events._persist_event_auth_chain_txn(
                txn,
                events,
                new_event_links,
            )

        self.get_success(
            self.store.db_pool.runInteraction(
                "insert",
                insert_event,
            )
        )

        return room_id

    @parameterized.expand([(True,), (False,)])
    def test_auth_chain_ids(self, use_chain_cover_index: bool) -> None:
        room_id = self._setup_auth_chain(use_chain_cover_index)

        # a and b have the same auth chain.
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["a"]))
        self.assertCountEqual(auth_chain_ids, ["e", "f", "g", "h", "i", "j", "k"])
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["b"]))
        self.assertCountEqual(auth_chain_ids, ["e", "f", "g", "h", "i", "j", "k"])
        auth_chain_ids = self.get_success(
            self.store.get_auth_chain_ids(room_id, ["a", "b"])
        )
        self.assertCountEqual(auth_chain_ids, ["e", "f", "g", "h", "i", "j", "k"])

        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["c"]))
        self.assertCountEqual(auth_chain_ids, ["g", "h", "i", "j", "k"])

        # d and e have the same auth chain.
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["d"]))
        self.assertCountEqual(auth_chain_ids, ["f", "g", "h", "i", "j", "k"])
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["e"]))
        self.assertCountEqual(auth_chain_ids, ["f", "g", "h", "i", "j", "k"])

        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["f"]))
        self.assertCountEqual(auth_chain_ids, ["g", "h", "i", "j", "k"])

        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["g"]))
        self.assertCountEqual(auth_chain_ids, ["h", "i", "j", "k"])

        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["h"]))
        self.assertEqual(auth_chain_ids, {"k"})

        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["i"]))
        self.assertEqual(auth_chain_ids, {"j"})

        # j and k have no parents.
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["j"]))
        self.assertEqual(auth_chain_ids, set())
        auth_chain_ids = self.get_success(self.store.get_auth_chain_ids(room_id, ["k"]))
        self.assertEqual(auth_chain_ids, set())

        # More complex input sequences.
        auth_chain_ids = self.get_success(
            self.store.get_auth_chain_ids(room_id, ["b", "c", "d"])
        )
        self.assertCountEqual(auth_chain_ids, ["e", "f", "g", "h", "i", "j", "k"])

        auth_chain_ids = self.get_success(
            self.store.get_auth_chain_ids(room_id, ["h", "i"])
        )
        self.assertCountEqual(auth_chain_ids, ["k", "j"])

        # e gets returned even though include_given is false, but it is in the
        # auth chain of b.
        auth_chain_ids = self.get_success(
            self.store.get_auth_chain_ids(room_id, ["b", "e"])
        )
        self.assertCountEqual(auth_chain_ids, ["e", "f", "g", "h", "i", "j", "k"])

        # Test include_given.
        auth_chain_ids = self.get_success(
            self.store.get_auth_chain_ids(room_id, ["i"], include_given=True)
        )
        self.assertCountEqual(auth_chain_ids, ["i", "j"])

    @parameterized.expand([(True,), (False,)])
    def test_auth_difference(self, use_chain_cover_index: bool) -> None:
        room_id = self._setup_auth_chain(use_chain_cover_index)

        # Now actually test that various combinations give the right result:
        self.assert_auth_diff_is_expected(room_id)

    @parameterized.expand(
        [
            [graph_subset]
            for graph_subset in get_all_topologically_consistent_subsets(
                AUTH_GRAPH, AUTH_GRAPH
            )
        ]
    )
    def test_auth_difference_partial(self, graph_subset: Collection[str]) -> None:
        """Test that if we only have a chain cover index on a partial subset of
        the room we still get the correct auth chain difference.

        We do this by removing the chain cover index for every valid subset of the
        graph.
        """
        room_id = self._setup_auth_chain(True)

        for event_id in graph_subset:
            # Remove chain cover from that event.
            self.get_success(
                self.store.db_pool.simple_delete(
                    table="event_auth_chains",
                    keyvalues={"event_id": event_id},
                    desc="test_auth_difference_partial_remove",
                )
            )
            self.get_success(
                self.store.db_pool.simple_insert(
                    table="event_auth_chain_to_calculate",
                    values={
                        "event_id": event_id,
                        "room_id": room_id,
                        "type": "",
                        "state_key": "",
                    },
                    desc="test_auth_difference_partial_remove",
                )
            )

        self.assert_auth_diff_is_expected(room_id)

    def assert_auth_diff_is_expected(self, room_id: str) -> None:
        """Assert the auth chain difference returns the correct answers."""
        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"c"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c", "e", "f"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a", "c"}, {"b"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a", "c"}, {"b", "c"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"d"}])
        )
        self.assertSetEqual(difference, {"a", "b", "d", "e"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"c"}, {"d"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c", "d", "e", "f"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"e"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}])
        )
        self.assertSetEqual(difference, set())

    def test_auth_difference_partial_cover(self) -> None:
        """Test that we correctly handle rooms where not all events have a chain
        cover calculated. This can happen in some obscure edge cases, including
        during the background update that calculates the chain cover for old
        rooms.
        """

        # We allow partial covers for this test
        self.hs.get_datastores().main.tests_allow_no_chain_cover_index = True

        room_id = "@ROOM:local"

        # The silly auth graph we use to test the auth difference algorithm,
        # where the top are the most recent events.
        #
        #   A   B
        #    \ /
        #  D  E
        #  \  |
        #   ` F   C
        #     |  /|
        #     G ´ |
        #     | \ |
        #     H   I
        #     |   |
        #     K   J

        auth_graph: Dict[str, List[str]] = {
            "a": ["e"],
            "b": ["e"],
            "c": ["g", "i"],
            "d": ["f"],
            "e": ["f"],
            "f": ["g"],
            "g": ["h", "i"],
            "h": ["k"],
            "i": ["j"],
            "k": [],
            "j": [],
        }

        depth_map = {
            "a": 7,
            "b": 7,
            "c": 4,
            "d": 6,
            "e": 6,
            "f": 5,
            "g": 3,
            "h": 2,
            "i": 2,
            "k": 1,
            "j": 1,
        }

        # We rudely fiddle with the appropriate tables directly, as that's much
        # easier than constructing events properly.

        def insert_event(txn: LoggingTransaction) -> None:
            # First insert the room and mark it as having a chain cover.
            self.store.db_pool.simple_insert_txn(
                txn,
                "rooms",
                {
                    "room_id": room_id,
                    "creator": "room_creator_user_id",
                    "is_public": True,
                    "room_version": "6",
                    "has_auth_chain_index": True,
                },
            )

            stream_ordering = 0

            for event_id in auth_graph:
                stream_ordering += 1
                depth = depth_map[event_id]

                self.store.db_pool.simple_insert_txn(
                    txn,
                    table="events",
                    values={
                        "event_id": event_id,
                        "room_id": room_id,
                        "depth": depth,
                        "topological_ordering": depth,
                        "type": "m.test",
                        "processed": True,
                        "outlier": False,
                        "stream_ordering": stream_ordering,
                    },
                )

            # Insert all events apart from 'B'
            events = [
                cast(EventBase, FakeEvent(event_id, room_id, auth_graph[event_id]))
                for event_id in auth_graph
                if event_id != "b"
            ]
            new_event_links = (
                self.persist_events.calculate_chain_cover_index_for_events_txn(
                    txn, room_id, [e for e in events if e.is_state()]
                )
            )
            self.persist_events._persist_event_auth_chain_txn(
                txn,
                events,
                new_event_links,
            )

            # Now we insert the event 'B' without a chain cover, by temporarily
            # pretending the room doesn't have a chain cover.

            self.store.db_pool.simple_update_txn(
                txn,
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"has_auth_chain_index": False},
            )

            events = [cast(EventBase, FakeEvent("b", room_id, auth_graph["b"]))]
            new_event_links = (
                self.persist_events.calculate_chain_cover_index_for_events_txn(
                    txn, room_id, [e for e in events if e.is_state()]
                )
            )
            self.persist_events._persist_event_auth_chain_txn(
                txn, events, new_event_links
            )

            self.store.db_pool.simple_update_txn(
                txn,
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"has_auth_chain_index": True},
            )

        self.get_success(
            self.store.db_pool.runInteraction(
                "insert",
                insert_event,
            )
        )

        # Now actually test that various combinations give the right result:

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"c"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c", "e", "f"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a", "c"}, {"b"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a", "c"}, {"b", "c"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"d"}])
        )
        self.assertSetEqual(difference, {"a", "b", "d", "e"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"c"}, {"d"}])
        )
        self.assertSetEqual(difference, {"a", "b", "c", "d", "e", "f"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}, {"b"}, {"e"}])
        )
        self.assertSetEqual(difference, {"a", "b"})

        difference = self.get_success(
            self.store.get_auth_chain_difference(room_id, [{"a"}])
        )
        self.assertSetEqual(difference, set())

    @parameterized.expand(
        [(room_version,) for room_version in KNOWN_ROOM_VERSIONS.values()]
    )
    def test_prune_inbound_federation_queue(self, room_version: RoomVersion) -> None:
        """Test that pruning of inbound federation queues work"""

        room_id = "some_room_id"

        def prev_event_format(prev_event_id: str) -> Union[Tuple[str, dict], str]:
            """Account for differences in prev_events format across room versions"""
            if room_version.event_format == EventFormatVersions.ROOM_V1_V2:
                return prev_event_id, {}

            return prev_event_id

        # Insert a bunch of events that all reference the previous one.
        self.get_success(
            self.store.db_pool.simple_insert_many(
                table="federation_inbound_events_staging",
                keys=(
                    "origin",
                    "room_id",
                    "received_ts",
                    "event_id",
                    "event_json",
                    "internal_metadata",
                ),
                values=[
                    (
                        "some_origin",
                        room_id,
                        0,
                        f"$fake_event_id_{i + 1}",
                        json_encoder.encode(
                            {"prev_events": [prev_event_format(f"$fake_event_id_{i}")]}
                        ),
                        "{}",
                    )
                    for i in range(500)
                ],
                desc="test_prune_inbound_federation_queue",
            )
        )

        # Calling prune once should return True, i.e. a prune happen. The second
        # time it shouldn't.
        pruned = self.get_success(
            self.store.prune_staged_events_in_room(room_id, room_version)
        )
        self.assertTrue(pruned)

        pruned = self.get_success(
            self.store.prune_staged_events_in_room(room_id, room_version)
        )
        self.assertFalse(pruned)

        # Assert that we only have a single event left in the queue, and that it
        # is the last one.
        count = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="federation_inbound_events_staging",
                keyvalues={"room_id": room_id},
                retcol="COUNT(*)",
                desc="test_prune_inbound_federation_queue",
            )
        )
        self.assertEqual(count, 1)

        next_staged_event_info = self.get_success(
            self.store.get_next_staged_event_id_for_room(room_id)
        )
        assert next_staged_event_info
        _, event_id = next_staged_event_info
        self.assertEqual(event_id, "$fake_event_id_500")

    def _setup_room_for_backfill_tests(self) -> _BackfillSetupInfo:
        """
        Sets up a room with various events and backward extremities to test
        backfill functions against.

        Returns:
            _BackfillSetupInfo including the `room_id` to test against and
            `depth_map` of events in the room
        """
        room_id = "!backfill-room-test:some-host"

        # The silly graph we use to test grabbing backward extremities,
        # where the top is the oldest events.
        #    1 (oldest)
        #    |
        #    2 ⹁
        #    |  \
        #    |   [b1, b2, b3]
        #    |   |
        #    |   A
        #    |  /
        #    3 {
        #    |  \
        #    |   [b4, b5, b6]
        #    |   |
        #    |   B
        #    |  /
        #    4 ´
        #    |
        #    5 (newest)

        event_graph: Dict[str, List[str]] = {
            "1": [],
            "2": ["1"],
            "3": ["2", "A"],
            "4": ["3", "B"],
            "5": ["4"],
            "A": ["b1", "b2", "b3"],
            "b1": ["2"],
            "b2": ["2"],
            "b3": ["2"],
            "B": ["b4", "b5", "b6"],
            "b4": ["3"],
            "b5": ["3"],
            "b6": ["3"],
        }

        depth_map: Dict[str, int] = {
            "1": 1,
            "2": 2,
            "b1": 3,
            "b2": 3,
            "b3": 3,
            "A": 4,
            "3": 5,
            "b4": 6,
            "b5": 6,
            "b6": 6,
            "B": 7,
            "4": 8,
            "5": 9,
        }

        # The events we have persisted on our server.
        # The rest are events in the room but not backfilled tet.
        our_server_events = {"5", "4", "B", "3", "A"}

        complete_event_dict_map: Dict[str, JsonDict] = {}
        stream_ordering = 0
        for event_id, prev_event_ids in event_graph.items():
            depth = depth_map[event_id]

            complete_event_dict_map[event_id] = {
                "event_id": event_id,
                "type": "test_regular_type",
                "room_id": room_id,
                "sender": "@sender",
                "prev_event_ids": prev_event_ids,
                "auth_event_ids": [],
                "origin_server_ts": stream_ordering,
                "depth": depth,
                "stream_ordering": stream_ordering,
                "content": {"body": "event" + event_id},
            }

            stream_ordering += 1

        def populate_db(txn: LoggingTransaction) -> None:
            # Insert the room to satisfy the foreign key constraint of
            # `event_failed_pull_attempts`
            self.store.db_pool.simple_insert_txn(
                txn,
                "rooms",
                {
                    "room_id": room_id,
                    "creator": "room_creator_user_id",
                    "is_public": True,
                    "room_version": "6",
                },
            )

            # Insert our server events
            for event_id in our_server_events:
                event_dict = complete_event_dict_map[event_id]

                self.store.db_pool.simple_insert_txn(
                    txn,
                    table="events",
                    values={
                        "event_id": event_dict.get("event_id"),
                        "type": event_dict.get("type"),
                        "room_id": event_dict.get("room_id"),
                        "depth": event_dict.get("depth"),
                        "topological_ordering": event_dict.get("depth"),
                        "stream_ordering": event_dict.get("stream_ordering"),
                        "processed": True,
                        "outlier": False,
                    },
                )

            # Insert the event edges
            for event_id in our_server_events:
                for prev_event_id in event_graph[event_id]:
                    self.store.db_pool.simple_insert_txn(
                        txn,
                        table="event_edges",
                        values={
                            "event_id": event_id,
                            "prev_event_id": prev_event_id,
                            "room_id": room_id,
                        },
                    )

            # Insert the backward extremities
            prev_events_of_our_events = {
                prev_event_id
                for our_server_event in our_server_events
                for prev_event_id in complete_event_dict_map[our_server_event][
                    "prev_event_ids"
                ]
            }
            backward_extremities = prev_events_of_our_events - our_server_events
            for backward_extremity in backward_extremities:
                self.store.db_pool.simple_insert_txn(
                    txn,
                    table="event_backward_extremities",
                    values={
                        "event_id": backward_extremity,
                        "room_id": room_id,
                    },
                )

        self.get_success(
            self.store.db_pool.runInteraction(
                "_setup_room_for_backfill_tests_populate_db",
                populate_db,
            )
        )

        return _BackfillSetupInfo(room_id=room_id, depth_map=depth_map)

    def test_get_backfill_points_in_room(self) -> None:
        """
        Test to make sure only backfill points that are older and come before
        the `current_depth` are returned.
        """
        setup_info = self._setup_room_for_backfill_tests()
        room_id = setup_info.room_id
        depth_map = setup_info.depth_map

        # Try at "B"
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["B"], limit=100)
        )
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        self.assertEqual(backfill_event_ids, ["b6", "b5", "b4", "2", "b3", "b2", "b1"])

        # Try at "A"
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["A"], limit=100)
        )
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        # Event "2" has a depth of 2 but is not included here because we only
        # know the approximate depth of 5 from our event "3".
        self.assertListEqual(backfill_event_ids, ["b3", "b2", "b1"])

    def test_get_backfill_points_in_room_excludes_events_we_have_attempted(
        self,
    ) -> None:
        """
        Test to make sure that events we have attempted to backfill (and within
        backoff timeout duration) do not show up as an event to backfill again.
        """
        setup_info = self._setup_room_for_backfill_tests()
        room_id = setup_info.room_id
        depth_map = setup_info.depth_map

        # Record some attempts to backfill these events which will make
        # `get_backfill_points_in_room` exclude them because we
        # haven't passed the backoff interval.
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b5", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b4", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b3", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b2", "fake cause")
        )

        # No time has passed since we attempted to backfill ^

        # Try at "B"
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["B"], limit=100)
        )
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        # Only the backfill points that we didn't record earlier exist here.
        self.assertEqual(backfill_event_ids, ["b6", "2", "b1"])

    def test_get_backfill_points_in_room_attempted_event_retry_after_backoff_duration(
        self,
    ) -> None:
        """
        Test to make sure after we fake attempt to backfill event "b3" many times,
        we can see retry and see the "b3" again after the backoff timeout duration
        has exceeded.
        """
        setup_info = self._setup_room_for_backfill_tests()
        room_id = setup_info.room_id
        depth_map = setup_info.depth_map

        # Record some attempts to backfill these events which will make
        # `get_backfill_points_in_room` exclude them because we
        # haven't passed the backoff interval.
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b3", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b1", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b1", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b1", "fake cause")
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(room_id, "b1", "fake cause")
        )

        # Now advance time by 2 hours and we should only be able to see "b3"
        # because we have waited long enough for the single attempt (2^1 hours)
        # but we still shouldn't see "b1" because we haven't waited long enough
        # for this many attempts. We didn't do anything to "b2" so it should be
        # visible regardless.
        self.reactor.advance(datetime.timedelta(hours=2).total_seconds())

        # Try at "A" and make sure that "b1" is not in the list because we've
        # already attempted many times
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["A"], limit=100)
        )
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        self.assertEqual(backfill_event_ids, ["b3", "b2"])

        # Now advance time by 20 hours (above 2^4 because we made 4 attemps) and
        # see if we can now backfill it
        self.reactor.advance(datetime.timedelta(hours=20).total_seconds())

        # Try at "A" again after we advanced enough time and we should see "b3" again
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["A"], limit=100)
        )
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        self.assertEqual(backfill_event_ids, ["b3", "b2", "b1"])

    def test_get_backfill_points_in_room_works_after_many_failed_pull_attempts_that_could_naively_overflow(
        self,
    ) -> None:
        """
        A test that reproduces https://github.com/matrix-org/synapse/issues/13929 (Postgres only).

        Test to make sure we can still get backfill points after many failed pull
        attempts that cause us to backoff to the limit. Even if the backoff formula
        would tell us to wait for more seconds than can be expressed in a 32 bit
        signed int.
        """
        setup_info = self._setup_room_for_backfill_tests()
        room_id = setup_info.room_id
        depth_map = setup_info.depth_map

        # Pretend that we have tried and failed 10 times to backfill event b1.
        for _ in range(10):
            self.get_success(
                self.store.record_event_failed_pull_attempt(room_id, "b1", "fake cause")
            )

        # If the backoff periods grow without limit:
        # After the first failed attempt, we would have backed off for 1 << 1 = 2 hours.
        # After the second failed attempt we would have backed off for 1 << 2 = 4 hours,
        # so after the 10th failed attempt we should backoff for 1 << 10 == 1024 hours.
        # Wait 1100 hours just so we have a nice round number.
        self.reactor.advance(datetime.timedelta(hours=1100).total_seconds())

        # 1024 hours in milliseconds is 1024 * 3600000, which exceeds the largest 32 bit
        # signed integer. The bug we're reproducing is that this overflow causes an
        # error in postgres preventing us from fetching a set of backwards extremities
        # to retry fetching.
        backfill_points = self.get_success(
            self.store.get_backfill_points_in_room(room_id, depth_map["A"], limit=100)
        )

        # We should aim to fetch all backoff points: b1's latest backoff period has
        # expired, and we haven't tried the rest.
        backfill_event_ids = [backfill_point[0] for backfill_point in backfill_points]
        self.assertEqual(backfill_event_ids, ["b3", "b2", "b1"])

    def test_get_event_ids_with_failed_pull_attempts(self) -> None:
        """
        Test to make sure we properly get event_ids based on whether they have any
        failed pull attempts.
        """
        # Create the room
        user_id = self.register_user("alice", "test")
        tok = self.login("alice", "test")
        room_id = self.helper.create_room_as(room_creator=user_id, tok=tok)

        self.get_success(
            self.store.record_event_failed_pull_attempt(
                room_id, "$failed_event_id1", "fake cause"
            )
        )
        self.get_success(
            self.store.record_event_failed_pull_attempt(
                room_id, "$failed_event_id2", "fake cause"
            )
        )

        event_ids_with_failed_pull_attempts = self.get_success(
            self.store.get_event_ids_with_failed_pull_attempts(
                event_ids=[
                    "$failed_event_id1",
                    "$fresh_event_id1",
                    "$failed_event_id2",
                    "$fresh_event_id2",
                ]
            )
        )

        self.assertEqual(
            event_ids_with_failed_pull_attempts,
            {"$failed_event_id1", "$failed_event_id2"},
        )

    def test_get_event_ids_to_not_pull_from_backoff(self) -> None:
        """
        Test to make sure only event IDs we should backoff from are returned.
        """
        # Create the room
        user_id = self.register_user("alice", "test")
        tok = self.login("alice", "test")
        room_id = self.helper.create_room_as(room_creator=user_id, tok=tok)

        failure_time = self.clock.time_msec()
        self.get_success(
            self.store.record_event_failed_pull_attempt(
                room_id, "$failed_event_id", "fake cause"
            )
        )

        event_ids_with_backoff = self.get_success(
            self.store.get_event_ids_to_not_pull_from_backoff(
                room_id=room_id, event_ids=["$failed_event_id", "$normal_event_id"]
            )
        )

        self.assertEqual(
            event_ids_with_backoff,
            # We expect a 2^1 hour backoff after a single failed attempt.
            {"$failed_event_id": failure_time + 2 * 60 * 60 * 1000},
        )

    def test_get_event_ids_to_not_pull_from_backoff_retry_after_backoff_duration(
        self,
    ) -> None:
        """
        Test to make sure no event IDs are returned after the backoff duration has
        elapsed.
        """
        # Create the room
        user_id = self.register_user("alice", "test")
        tok = self.login("alice", "test")
        room_id = self.helper.create_room_as(room_creator=user_id, tok=tok)

        self.get_success(
            self.store.record_event_failed_pull_attempt(
                room_id, "$failed_event_id", "fake cause"
            )
        )

        # Now advance time by 2 hours so we wait long enough for the single failed
        # attempt (2^1 hours).
        self.reactor.advance(datetime.timedelta(hours=2).total_seconds())

        event_ids_with_backoff = self.get_success(
            self.store.get_event_ids_to_not_pull_from_backoff(
                room_id=room_id, event_ids=["$failed_event_id", "$normal_event_id"]
            )
        )
        # Since this function only returns events we should backoff from, time has
        # elapsed past the backoff range so there is no events to backoff from.
        self.assertEqual(event_ids_with_backoff, {})


@attr.s(auto_attribs=True)
class FakeEvent:
    event_id: str
    room_id: str
    auth_events: List[str]

    type = "foo"
    state_key = "foo"

    internal_metadata = EventInternalMetadata({})

    def auth_event_ids(self) -> List[str]:
        return self.auth_events

    def is_state(self) -> bool:
        return True
