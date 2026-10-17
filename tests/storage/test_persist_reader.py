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

"""The Rust persist reads (`rust/src/persist/reader.rs`) against the Python
store methods they port, on the same database. These need the native Rust
Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_persist_reader
"""

from collections import Counter
from typing import Any
from unittest import SkipTest

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership, StickyEventField
from synapse.api.errors import NotFoundError, SynapseError
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.event_federation import EventFederationStore
from synapse.storage.databases.main.events import DeltaState
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.persist import PersistReaderForTest
from synapse.types import StateMap, create_requester
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.test_utils.event_builders import make_test_event
from tests.test_utils.event_injection import inject_event
from tests.utils import RUST_PERSIST_FOR_TESTS


class PersistReaderTestBase(unittest.HomeserverTestCase):
    """The fixture: a room with two members, and the reader on the
    persister of a homeserver at `shadow`."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            "shadow"
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        if RUST_PERSIST_FOR_TESTS == "off":
            raise SkipTest("SYNAPSE_TEST_RUST_PERSIST=off turns the persister off")
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persist_events_store = hs.get_datastores().persist_events
        assert persist_events_store is not None
        self.persist_events_store = persist_events_store
        db_pool = self.store.db_pool._db_pool
        assert isinstance(db_pool, RustConnectionPool)
        assert db_pool._pool is not None
        persister = hs.get_rust_handlers().persister
        assert persister is not None
        self.rust = PersistReaderForTest(db_pool._pool, persister)

        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    def _send(self, body: str) -> str:
        return self.helper.send(self.room_id, body, tok=self.alice_tok)["event_id"]

    def _sql(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        return self.get_success(self.store.db_pool.execute("test", sql, *args))

    def _write(self, sql: str, *args: Any) -> None:
        """Run a statement that returns no rows."""
        self.get_success(
            self.store.db_pool.runInteraction(
                "test", lambda txn: txn.execute(sql, args)
            )
        )

    def _all_event_ids(self) -> list[str]:
        return [
            event_id
            for (event_id,) in self._sql(
                "SELECT event_id FROM events WHERE room_id = ?", self.room_id
            )
        ]

    def _dag_with_bad_events(self) -> list[str]:
        """A chain of events in the room, of which the third is rejected,
        the fourth soft failed, the fifth an outlier and the seventh both
        rejected and an outlier."""
        chain = [self._send(f"m{i}") for i in range(3)]
        chain.append(
            self.get_success(
                inject_event(
                    self.hs,
                    prev_event_ids=[chain[-1]],
                    internal_metadata={"soft_failed": True},
                    room_id=self.room_id,
                    sender=self.alice,
                    type=EventTypes.Message,
                    content={"msgtype": "m.text", "body": "soft failed"},
                )
            ).event_id
        )
        chain.extend(
            self.get_success(
                inject_event(
                    self.hs,
                    prev_event_ids=[chain[-1]],
                    room_id=self.room_id,
                    sender=self.alice,
                    type=EventTypes.Message,
                    content={"msgtype": "m.text", "body": body},
                )
            ).event_id
            for body in ("outlier", "last", "rejected outlier")
        )
        for event_id in (chain[2], chain[6]):
            self._reject(event_id)
        for event_id in (chain[4], chain[6]):
            self._write("UPDATE events SET outlier = TRUE WHERE event_id = ?", event_id)
        return chain

    def _reject(self, event_id: str) -> None:
        self.get_success(
            self.store.db_pool.simple_insert(
                "rejections",
                {"event_id": event_id, "reason": "test", "last_check": "now"},
            )
        )


class PersistReaderTestCase(PersistReaderTestBase):
    """Each test runs a Rust read and the Python store method against the
    same fixture and compares."""

    def test_latest_event_ids_in_room(self) -> None:
        message = self._send("hi")
        # Two children of one parent fork the room.
        for body in ("a", "b"):
            self.get_success(
                inject_event(
                    self.hs,
                    prev_event_ids=[message],
                    room_id=self.room_id,
                    sender=self.alice,
                    type=EventTypes.Message,
                    content={"msgtype": "m.text", "body": body},
                )
            )

        for room_id in (self.room_id, "!unknown:test"):
            # The Python cache was invalidated as the events went in, so both
            # read the table.
            self.store.get_latest_event_ids_in_room.invalidate((room_id,))
            expected = set(
                self.get_success(self.store.get_latest_event_ids_in_room(room_id))
            )
            self.assertEqual(self.rust.latest_event_ids_in_room(room_id), expected)
        self.assertEqual(len(self.rust.latest_event_ids_in_room(self.room_id)), 2)

    def test_state_dag_extremities(self) -> None:
        # The table is only written in MSC4242 rooms, so fill it by hand.
        for event_id in self._all_event_ids()[:2]:
            self.get_success(
                self.store.db_pool.simple_insert(
                    "msc4242_state_dag_forward_extremities",
                    {"room_id": self.room_id, "event_id": event_id},
                )
            )

        for room_id in (self.room_id, "!unknown:test"):
            self.assertEqual(
                self.rust.state_dag_extremities(room_id),
                set(self.get_success(self.store.get_state_dag_extremities(room_id))),
            )
        self.assertEqual(len(self.rust.state_dag_extremities(self.room_id)), 2)

    def test_events_which_are_prevs(self) -> None:
        chain = self._dag_with_bad_events()
        # A second child of "last", so it is a prev event twice.
        self.get_success(
            inject_event(
                self.hs,
                prev_event_ids=[chain[5]],
                room_id=self.room_id,
                sender=self.alice,
                type=EventTypes.Message,
                content={"msgtype": "m.text", "body": "second child"},
            )
        )
        # More than a batch of unknown IDs first, so the real ones are in
        # the second batch.
        ids = [f"$unknown{i}" for i in range(150)] + self._all_event_ids()

        expected = self.get_success(
            self.persist_events_store._get_events_which_are_prevs(ids)
        )
        # Row order is the database's, so compare as multisets.
        self.assertEqual(
            Counter(self.rust.events_which_are_prevs(ids)), Counter(expected)
        )
        # The prevs of the rejected, soft failed and outlier events don't
        # count.
        prevs = set(expected)
        for event_id in chain[1:4]:
            self.assertNotIn(event_id, prevs)
        self.assertIn(chain[0], prevs)
        self.assertIn(chain[4], prevs)
        # The rejected outlier child doesn't count, the other does.
        self.assertEqual(Counter(expected)[chain[5]], 1)
        self.assertEqual(Counter(self.rust.events_which_are_prevs(ids))[chain[5]], 1)

    def test_prevs_before_rejected(self) -> None:
        chain = self._dag_with_bad_events()

        pad = [f"$unknown{i}" for i in range(150)]
        for start in [
            [chain[-1]],
            [chain[3]],
            chain,
            self._all_event_ids(),
            pad + chain,
        ]:
            for include_soft_failed in (True, False):
                self.assertEqual(
                    self.rust.prevs_before_rejected(start, include_soft_failed),
                    self.get_success(
                        self.persist_events_store._get_prevs_before_rejected(
                            start, include_soft_failed
                        )
                    ),
                    (start, include_soft_failed),
                )
        # The walk is from the soft failed event, past the rejected one.
        self.assertEqual(
            self.rust.prevs_before_rejected([chain[3]], True), {chain[1], chain[2]}
        )
        self.assertEqual(self.rust.prevs_before_rejected([chain[3]], False), set())
        self.assertEqual(self.rust.prevs_before_rejected([chain[2]], False), {chain[1]})
        # The rejected outlier's edges are not walked.
        self.assertEqual(self.rust.prevs_before_rejected([chain[6]], False), set())
        # In the second batch, which keeps what the first found.
        self.assertEqual(
            self.rust.prevs_before_rejected(pad + [chain[3]], True),
            {chain[1], chain[2]},
        )

    def test_have_seen_events(self) -> None:
        # More than a batch of unknown IDs first.
        ids = [f"$unknown{i}" for i in range(1001)] + self._all_event_ids()
        self.assertEqual(
            self.rust.have_seen_events(ids),
            self.get_success(self.store.have_seen_events(self.room_id, ids)),
        )

    def test_membership_from_event_ids(self) -> None:
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        ids = [f"$unknown{i}" for i in range(1001)] + self._all_event_ids()
        expected = self.get_success(self.store.get_membership_from_event_ids(ids))
        self.assertEqual(
            self.rust.membership_from_event_ids(ids),
            {
                event_id: (m.user_id, m.membership)
                for event_id, m in expected.items()
                if m is not None
            },
        )
        self.assertIn(
            "leave", {m for _, m in self.rust.membership_from_event_ids(ids).values()}
        )

    def test_is_local_host_in_room_ignoring_users(self) -> None:
        # Local users who are invited, or have left, don't count.
        carol = self.register_user("carol", "pass")
        carol_tok = self.login("carol", "pass")
        self.helper.join(self.room_id, carol, tok=carol_tok)
        self.helper.leave(self.room_id, carol, tok=carol_tok)
        dave = self.register_user("dave", "pass")
        self.helper.invite(self.room_id, self.alice, dave, tok=self.alice_tok)

        for room_id in (self.room_id, "!unknown:test"):
            for ignore in ([], [self.alice], [self.alice, self.bob], ["@x:test"]):
                self.assertEqual(
                    self.rust.is_local_host_in_room_ignoring_users(room_id, ignore),
                    self.get_success(
                        self.store.is_local_host_in_room_ignoring_users(room_id, ignore)
                    ),
                    (room_id, ignore),
                )
        self.assertTrue(
            self.rust.is_local_host_in_room_ignoring_users(self.room_id, [self.alice])
        )
        self.assertFalse(
            self.rust.is_local_host_in_room_ignoring_users(
                self.room_id, [self.alice, self.bob]
            )
        )

    def test_room_version_id(self) -> None:
        self.assertEqual(
            self.rust.room_version_id(self.room_id),
            self.get_success(self.store.get_room_version_id(self.room_id)),
        )
        self.get_failure(self.store.get_room_version_id("!unknown:test"), NotFoundError)
        self.assertIsNone(self.rust.room_version_id("!unknown:test"))

    def test_has_sliding_sync_joined_room(self) -> None:
        for room_id in (self.room_id, "!unknown:test"):
            row = self.get_success(
                self.store.db_pool.simple_select_one_onecol(
                    table="sliding_sync_joined_rooms",
                    keyvalues={"room_id": room_id},
                    retcol="room_id",
                    allow_none=True,
                )
            )
            self.assertEqual(
                self.rust.has_sliding_sync_joined_room(room_id), row is not None
            )
        self.assertTrue(self.rust.has_sliding_sync_joined_room(self.room_id))

    def test_last_event_pos_in_room(self) -> None:
        chain = self._dag_with_bad_events()
        topic = self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "t"}, tok=self.alice_tok
        )["event_id"]
        # Newer events of each type that don't count: a rejected message and
        # an outlier topic.
        rejected = self._send("rejected")
        self._reject(rejected)
        outlier = self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "outlier"}, tok=self.alice_tok
        )["event_id"]
        self._write("UPDATE events SET outlier = TRUE WHERE event_id = ?", outlier)
        # A row from before sharded persisters.
        self._write("UPDATE events SET instance_name = NULL WHERE event_id = ?", topic)

        for room_id in (self.room_id, "!unknown:test"):
            for event_types in (
                None,
                [],
                list(SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES),
                [EventTypes.Topic],
                ["m.nothing"],
            ):
                expected = self.get_success(
                    self.store.get_last_event_pos_in_room(room_id, event_types)
                )
                self.assertEqual(
                    self.rust.last_event_pos_in_room(room_id, event_types),
                    None
                    if expected is None
                    else (expected[0], expected[1].instance_name, expected[1].stream),
                    (room_id, event_types),
                )

        def last(event_types: list[str] | None) -> tuple[str, str] | None:
            pos = self.rust.last_event_pos_in_room(self.room_id, event_types)
            return None if pos is None else (pos[0], pos[1])

        self.assertEqual(last(None), (topic, "master"))
        self.assertEqual(last([EventTypes.Topic]), (topic, "master"))
        self.assertEqual(last([EventTypes.Message]), (chain[5], "master"))

    def test_soft_failed_sticky_events(self) -> None:
        # Sticky events need MSC4354, so fill the table by hand: a soft
        # failed event from each user, and one that is not soft failed, with
        # different expiries.
        start_ms = self.clock.time_msec()
        events: list[str] = []
        for sender, soft_failed, expires_in in (
            (self.alice, True, 2000),
            (self.bob, True, 4000),
            (self.alice, False, 4000),
        ):
            event = self.get_success(
                inject_event(
                    self.hs,
                    internal_metadata={"soft_failed": soft_failed},
                    room_id=self.room_id,
                    sender=sender,
                    type=EventTypes.Message,
                    content={"msgtype": "m.text", "body": f"sticky {len(events)}"},
                )
            )
            events.append(event.event_id)
            self.get_success(
                self.store.db_pool.simple_insert(
                    "sticky_events",
                    {
                        "stream_id": len(events),
                        "instance_name": "master",
                        "event_id": event.event_id,
                        "room_id": self.room_id,
                        "event_stream_ordering": event.internal_metadata.stream_ordering,
                        "sender": sender,
                        "expires_at": start_ms + expires_in,
                    },
                )
            )

        # A soft failed sticky event in another room, which never shows.
        other_room = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        other = self.get_success(
            inject_event(
                self.hs,
                internal_metadata={"soft_failed": True},
                room_id=other_room,
                sender=self.alice,
                type=EventTypes.Message,
                content={"msgtype": "m.text", "body": "elsewhere"},
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                "sticky_events",
                {
                    "stream_id": 100,
                    "instance_name": "master",
                    "event_id": other.event_id,
                    "room_id": other_room,
                    "event_stream_ordering": other.internal_metadata.stream_ordering,
                    "sender": self.alice,
                    "expires_at": start_ms + 10000,
                },
            )
        )

        # Python reads the clock, so move it to each time. An event expires
        # at its `expires_at`.
        expected_all = {
            1000: events[:2],
            2000: events[1:2],
            3000: events[1:2],
            4000: [],
            5000: [],
        }
        for offset_ms, expected_now in expected_all.items():
            self.reactor.advance((start_ms + offset_ms - self.clock.time_msec()) / 1000)
            now_ms = self.clock.time_msec()
            for senders in (None, set(), {self.alice}, {self.bob, "@x:test"}):
                expected = self.get_success(
                    self.store.db_pool.runInteraction(
                        "test",
                        self.store._get_soft_failed_sticky_events_txn,
                        self.room_id,
                        senders=senders,
                    )
                )
                self.assertEqual(
                    sorted(
                        self.rust.soft_failed_sticky_events(
                            self.room_id,
                            None if senders is None else list(senders),
                            now_ms,
                        )
                    ),
                    sorted(expected),
                    (offset_ms, senders),
                )
            self.assertEqual(
                sorted(self.rust.soft_failed_sticky_events(self.room_id, None, now_ms)),
                sorted(expected_now),
                offset_ms,
            )


class ChainCoverReadsTestCase(PersistReaderTestBase):
    """The chain cover reads, against the statements and helpers of
    `_calculate_chain_cover_index`, which are inline in Python."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        # Some state changes by both users, so there are several chains with
        # links between them.
        for i in range(3):
            self.helper.send_state(
                self.room_id, EventTypes.Topic, {"topic": f"{i}"}, tok=self.alice_tok
            )
            self.helper.send_state(
                self.room_id, EventTypes.Name, {"name": f"{i}"}, tok=self.alice_tok
            )
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    def _state_event_ids(self) -> list[str]:
        return [
            event_id
            for (event_id,) in self._sql(
                "SELECT event_id FROM state_events WHERE room_id = ?", self.room_id
            )
        ]

    def _chain_ids(self) -> list[int]:
        return [
            chain_id
            for (chain_id,) in self._sql(
                """SELECT DISTINCT chain_id FROM event_auth_chains
                INNER JOIN events USING (event_id) WHERE room_id = ?""",
                self.room_id,
            )
        ]

    def test_has_auth_chain_index(self) -> None:
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        legacy = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self._write(
            "UPDATE rooms SET has_auth_chain_index = NULL WHERE room_id = ?", other
        )
        self._write(
            "UPDATE rooms SET has_auth_chain_index = FALSE WHERE room_id = ?", legacy
        )

        for room_id in (self.room_id, other, legacy, "!unknown:test"):
            row = self.get_success(
                self.store.db_pool.simple_select_one(
                    table="rooms",
                    keyvalues={"room_id": room_id},
                    retcols=("room_id", "has_auth_chain_index"),
                    allow_none=True,
                )
            )
            # As `calculate_chain_cover_index_for_events_txn` decides.
            expected = not (row is None or row[1] is False)
            self.assertEqual(self.rust.has_auth_chain_index(room_id), expected, room_id)
        self.assertTrue(self.rust.has_auth_chain_index(other))
        self.assertFalse(self.rust.has_auth_chain_index(legacy))

    def test_events_with_chain_ids(self) -> None:
        ids = self._all_event_ids() + ["$unknown"]
        rows = self.get_success(
            self.store.db_pool.simple_select_many_batch(
                table="event_auth_chains",
                column="event_id",
                iterable=ids,
                keyvalues={},
                retcols=("event_id",),
            )
        )
        self.assertEqual(
            self.rust.events_with_chain_ids(ids), {event_id for (event_id,) in rows}
        )
        self.assertEqual(
            self.rust.events_with_chain_ids(ids), set(self._state_event_ids())
        )

    def test_chains_to_calculate(self) -> None:
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        for room_id, event_id in ((self.room_id, "$a"), (other, "$b"), (other, "$c")):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "event_auth_chain_to_calculate",
                    {
                        "event_id": event_id,
                        "room_id": room_id,
                        "type": EventTypes.Member,
                        "state_key": self.bob,
                    },
                )
            )

        for room_ids in ([self.room_id], [self.room_id, other], [], ["!unknown:test"]):
            rows = self.get_success(
                self.store.db_pool.simple_select_many_batch(
                    table="event_auth_chain_to_calculate",
                    keyvalues={},
                    column="room_id",
                    iterable=set(room_ids),
                    retcols=("event_id", "type", "state_key"),
                )
            )
            self.assertEqual(
                sorted(self.rust.chains_to_calculate(room_ids)), sorted(rows), room_ids
            )

    def test_auth_event_ids(self) -> None:
        for event_id in self._all_event_ids() + ["$unknown"]:
            expected = self.get_success(
                self.store.db_pool.simple_select_onecol(
                    "event_auth", keyvalues={"event_id": event_id}, retcol="auth_id"
                )
            )
            self.assertEqual(
                sorted(self.rust.auth_event_ids(event_id)), sorted(expected), event_id
            )

    def test_auth_event_chains(self) -> None:
        # An event without a chain, as an out of band membership has.
        state = self._state_event_ids()
        self._write("DELETE FROM event_auth_chains WHERE event_id = ?", state[0])
        ids = self._all_event_ids() + ["$unknown"]

        # The statement `_calculate_chain_cover_index` runs, through Python.
        rows = self._sql(
            """SELECT event_id, events.type, se.state_key, chain_id, sequence_number
            FROM events
            INNER JOIN state_events AS se USING (event_id)
            LEFT JOIN event_auth_chains USING (event_id)
            WHERE event_id = ANY(?)""",
            ids,
        )
        actual = self.rust.auth_event_chains(ids)
        self.assertEqual(sorted(actual), sorted(rows))
        self.assertEqual(len(actual), len(state))
        self.assertIn((state[0], None, None), {(r[0], r[3], r[4]) for r in actual})

    def test_chain_links(self) -> None:
        chains = self._chain_ids()
        self.assertGreater(len(chains), 1)
        for fetch in ([chains[0]], chains, [], [-1]):

            def python_links(txn: LoggingTransaction) -> list[tuple[int, ...]]:
                return [
                    (origin, origin_seq, target, target_seq)
                    for links in EventFederationStore._get_chain_links(txn, set(fetch))
                    for origin, inner in links.items()
                    for origin_seq, target, target_seq in inner
                ]

            expected = self.get_success(
                self.store.db_pool.runInteraction("test", python_links)
            )
            self.assertEqual(
                Counter(self.rust.chain_links(fetch)), Counter(expected), fetch
            )
        self.assertTrue(self.rust.chain_links(chains))

    def test_max_sequence_numbers(self) -> None:
        chains = self._chain_ids()
        for chain_ids in (chains, chains[:1], [], [-1]):
            rows = (
                self._sql(
                    """SELECT chain_id, MAX(sequence_number) FROM event_auth_chains
                    WHERE chain_id = ANY(?)
                    GROUP BY chain_id""",
                    chain_ids,
                )
                if chain_ids
                else []
            )
            self.assertEqual(
                self.rust.max_sequence_numbers(chain_ids), dict(rows), chain_ids
            )


# Event ID to `(chain, sequence_number, links)`, with each chain named as
# `ChainCoverTestCase._normalise` names it.
_Chains = dict[str, tuple[str, int, list[tuple[str, int]]]]


class ChainCoverTestCase(PersistReaderTestBase):
    """`rust/src/persist/chain_cover.rs` against
    `calculate_chain_cover_index_for_events`, over the same database and the
    same new events. Each side allocates its own chain IDs, and Python
    hands them to new chains in no fixed order, so a new chain is compared
    by the lowest event ID on it."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.room_version = self.get_success(self.store.get_room_version(self.room_id))
        state = self.get_success(
            hs.get_storage_controllers().state.get_current_state_ids(self.room_id)
        )
        self.create = state[(EventTypes.Create, "")]
        self.power_levels = state[(EventTypes.PowerLevels, "")]
        self.join_rules = state[(EventTypes.JoinRules, "")]
        self.alice_member = state[(EventTypes.Member, self.alice)]
        self.bob_member = state[(EventTypes.Member, self.bob)]
        self._counter = 0

    def _new(
        self, event_type: str, state_key: str | None, auth_events: list[str]
    ) -> tuple[EventBase, EventContext]:
        """A new event for the chunk, not persisted."""
        self._counter += 1
        if self.room_version.msc4291_room_ids_as_hashes:
            # The create event is implied by the room ID.
            auth_events = [a for a in auth_events if a != self.create]
        fields: dict[str, Any] = {
            "room_id": self.room_id,
            "sender": self.alice,
            "type": event_type,
            "content": {"n": self._counter},
            "prev_events": [],
            "auth_events": auth_events,
            # The event ID hashes the redacted event, which keeps the depth
            # and not the content.
            "depth": 10 + self._counter,
        }
        if state_key is not None:
            fields["state_key"] = state_key
        event = make_test_event(fields, self.room_version)
        return event, EventContext.for_outlier(self.hs.get_storage_controllers())

    def _position(self, event_id: str) -> tuple[str, int]:
        ((chain_id, sequence_number),) = self._sql(
            "SELECT chain_id, sequence_number FROM event_auth_chains WHERE event_id = ?",
            event_id,
        )
        return str(chain_id), sequence_number

    def _to_calculate(self) -> list[tuple[Any, ...]]:
        return sorted(
            self._sql(
                "SELECT event_id, room_id, type, state_key FROM event_auth_chain_to_calculate"
            )
        )

    def _restore_to_calculate(self, rows: list[tuple[Any, ...]]) -> None:
        def restore(txn: LoggingTransaction) -> None:
            txn.execute("DELETE FROM event_auth_chain_to_calculate")
            for row in rows:
                txn.execute(
                    """INSERT INTO event_auth_chain_to_calculate
                    (event_id, room_id, type, state_key) VALUES (?, ?, ?, ?)""",
                    row,
                )

        self.get_success(self.store.db_pool.runInteraction("test", restore))

    def _put_off(self, event_id: str) -> None:
        """Drop the event's chain, as for an out of band membership."""
        self._write("DELETE FROM event_auth_chains WHERE event_id = ?", event_id)

    def _normalise(
        self, chains: dict[str, tuple[int, int, list[tuple[int, int]]]]
    ) -> _Chains:
        existing = {
            chain_id
            for (chain_id,) in self._sql(
                "SELECT DISTINCT chain_id FROM event_auth_chains"
            )
        }
        names: dict[int, str] = {}
        for event_id, (chain_id, _, _) in sorted(chains.items()):
            if chain_id not in existing:
                names.setdefault(chain_id, f"new {event_id}")

        def name(chain_id: int) -> str:
            return names.get(chain_id, str(chain_id))

        return {
            event_id: (
                name(chain_id),
                sequence_number,
                sorted((name(target), target_seq) for target, target_seq in links),
            )
            for event_id, (chain_id, sequence_number, links) in chains.items()
        }

    def _calculate(self, events: list[tuple[EventBase, EventContext]]) -> _Chains:
        """Run Rust and then Python on the same database, and check that they
        agree on the chains and on what they put off."""
        before = self._to_calculate()
        actual = self.rust.calculate_chain_cover_index_for_events(
            self.room_id, [event for event, _ in events]
        )
        put_off = self._to_calculate()
        # Python would otherwise see what Rust put off as left from an
        # earlier chunk.
        self._restore_to_calculate(before)

        expected = self.get_success(
            self.persist_events_store.calculate_chain_cover_index_for_events(
                self.room_id, [event for event, _ in events]
            )
        )
        self.assertEqual(self._to_calculate(), put_off)
        result = self._normalise(actual)
        self.assertEqual(
            result,
            self._normalise(
                {
                    event_id: (links.chain_id, links.sequence_number, links.links)
                    for event_id, links in expected.items()
                }
            ),
        )
        return result

    def test_new_state_events(self) -> None:
        m1, _ = m1_pair = self._new(
            EventTypes.Member,
            self.bob,
            [self.create, self.power_levels, self.join_rules, self.bob_member],
        )
        m2, _ = m2_pair = self._new(
            EventTypes.Member, self.bob, [self.create, self.power_levels, m1.event_id]
        )
        # A fork: m2 and m3 both follow m1.
        m3, _ = m3_pair = self._new(
            EventTypes.Member, self.bob, [self.create, self.power_levels, m1.event_id]
        )
        name, _ = name_pair = self._new(
            EventTypes.Name, "", [self.create, self.power_levels, self.alice_member]
        )
        message = self._new(
            EventTypes.Message,
            None,
            [self.create, self.power_levels, self.alice_member],
        )
        known = (
            self.get_success(self.store.get_event(self.alice_member)),
            EventContext.for_outlier(self.hs.get_storage_controllers()),
        )

        chains = self._calculate(
            [m3_pair, m2_pair, name_pair, message, m1_pair, name_pair, known]
        )
        self.assertEqual(
            set(chains), {m1.event_id, m2.event_id, m3.event_id, name.event_id}
        )
        # m1 extends Bob's join's chain, and one of m2 and m3 extends m1's.
        bob_chain, bob_seq = self._position(self.bob_member)
        self.assertEqual(chains[m1.event_id][:2], (bob_chain, bob_seq + 1))
        self.assertEqual(
            sorted(chains[e.event_id][0] == bob_chain for e in (m2, m3)), [False, True]
        )
        # The power levels reach Alice's join and the create event, so the
        # name links only to them.
        self.assertEqual(chains[name.event_id][2], [self._position(self.power_levels)])

    def test_missing_auth_event(self) -> None:
        carol = "@carol:test"
        oob, _ = oob_pair = self._new(
            EventTypes.Member, carol, [self.create, self.power_levels, "$unknown"]
        )
        after, _ = after_pair = self._new(
            EventTypes.Member, carol, [self.create, self.power_levels, oob.event_id]
        )
        name, _ = name_pair = self._new(
            EventTypes.Name, "", [self.create, self.power_levels, self.alice_member]
        )

        chains = self._calculate([oob_pair, after_pair, name_pair])
        self.assertEqual(set(chains), {name.event_id})
        self.assertEqual(
            {row[0] for row in self._to_calculate()}, {oob.event_id, after.event_id}
        )

    def test_dry_run(self) -> None:
        """A dry run returns what a real one does and writes nothing."""
        carol = "@carol:test"
        oob, _ = self._new(
            EventTypes.Member, carol, [self.create, self.power_levels, "$unknown"]
        )
        name, _ = self._new(
            EventTypes.Name, "", [self.create, self.power_levels, self.alice_member]
        )
        before = self._to_calculate()

        dry = self.rust.calculate_chain_cover_index_for_events(
            self.room_id, [oob, name], dry_run=True
        )
        self.assertEqual(self._to_calculate(), before)

        actual = self.rust.calculate_chain_cover_index_for_events(
            self.room_id, [oob, name]
        )
        self.assertEqual({row[0] for row in self._to_calculate()}, {oob.event_id})
        self.assertEqual(set(dry), {name.event_id})
        self.assertEqual(self._normalise(dry), self._normalise(actual))

    def test_auth_event_without_a_chain(self) -> None:
        self._put_off(self.bob_member)
        member, _ = pair = self._new(
            EventTypes.Member,
            self.bob,
            [self.create, self.power_levels, self.bob_member],
        )

        chains = self._calculate([pair])
        self.assertEqual(set(chains), {self.bob_member, member.event_id})
        self.assertEqual(chains[member.event_id][0], chains[self.bob_member][0])

    def test_put_off_event_is_calculated(self) -> None:
        self._put_off(self.bob_member)
        self.get_success(
            self.store.db_pool.simple_insert(
                "event_auth_chain_to_calculate",
                {
                    "event_id": self.bob_member,
                    "room_id": self.room_id,
                    "type": EventTypes.Member,
                    "state_key": self.bob,
                },
            )
        )
        name, _ = pair = self._new(
            EventTypes.Name, "", [self.create, self.power_levels, self.alice_member]
        )

        chains = self._calculate([pair])
        self.assertEqual(set(chains), {self.bob_member, name.event_id})

    def test_legacy_room(self) -> None:
        self._write(
            "UPDATE rooms SET has_auth_chain_index = FALSE WHERE room_id = ?",
            self.room_id,
        )
        pair = self._new(
            EventTypes.Name, "", [self.create, self.power_levels, self.alice_member]
        )
        self.assertEqual(self._calculate([pair]), {})


class ExtremitiesTestCase(PersistReaderTestBase):
    """The extremity calculations in `rust/src/persist/extremities.rs`
    against the controller's Python methods, over the same database and
    the same new events."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.room_version = self.get_success(self.store.get_room_version(self.room_id))
        self._counter = 0

    def _new(
        self,
        prev_event_ids: list[str],
        *,
        sender: str | None = None,
        depth: int = 10,
        prev_state_events: list[str] | None = None,
        rejected: bool = False,
        soft_failed: bool = False,
        outlier: bool = False,
    ) -> tuple[EventBase, EventContext]:
        """A new event for the chunk, not persisted. With
        `prev_state_events` it is an MSC4242 state event."""
        self._counter += 1
        fields: dict[str, Any] = {
            "room_id": self.room_id,
            "sender": sender or self.alice,
            "type": EventTypes.Message,
            "content": {"msgtype": "m.text", "body": f"new {self._counter}"},
            "prev_events": prev_event_ids,
            "depth": depth,
        }
        room_version = self.room_version
        if prev_state_events is not None:
            room_version = RoomVersions.MSC4242v12
            fields["type"] = "m.test.state"
            fields["state_key"] = str(self._counter)
            fields["prev_state_events"] = prev_state_events
        event = make_test_event(fields, room_version)
        event.internal_metadata.outlier = outlier
        if soft_failed:
            event.internal_metadata.soft_failed = True
        context = EventContext.for_outlier(self.hs.get_storage_controllers())
        if rejected:
            context.rejected = "test"
        return event, context

    def _new_extremities(
        self,
        events: list[tuple[EventBase, EventContext]],
        latest: set[str],
    ) -> tuple[set[str], dict[str, Any]]:
        expected = self.get_success(
            self.persistence._calculate_new_extremities(self.room_id, events, latest)
        )
        result, counts = self.rust.calculate_new_extremities(events, list(latest))
        self.assertEqual(result, expected)
        return result, counts

    def test_calculate_new_extremities(self) -> None:
        chain = self._dag_with_bad_events()
        # The fixture flags its events after persisting them, so the stored
        # extremities are out of date. Use the one they would be.
        latest = {chain[5]}

        # A message on top, with new events that cannot be extremities.
        new = self._new([chain[5]])
        result, counts = self._new_extremities(
            [
                new,
                self._new([new[0].event_id], rejected=True),
                self._new([new[0].event_id], soft_failed=True),
                self._new([new[0].event_id], outlier=True),
            ],
            latest,
        )
        self.assertEqual(result, {new[0].event_id})
        self.assertEqual(counts["forward_extremities"], {1: 1})
        self.assertEqual(counts["stale_forward_extremities"], {0: 1})

        # Nothing changes, so nothing is counted.
        result, counts = self._new_extremities(
            [self._new([chain[5]], outlier=True)], latest
        )
        self.assertEqual(result, latest)
        self.assertEqual(counts["forward_extremities"], {})

        # Stale extremities with persisted children go. The new event's prev
        # is soft failed, and behind it a rejected event: everything up to
        # chain[1] goes, while chain[0], whose child chain[1] is persisted,
        # goes anyway.
        on_soft_failed = self._new([chain[3]])
        result, counts = self._new_extremities(
            [on_soft_failed], {chain[0], chain[1], chain[5]}
        )
        self.assertEqual(result, {chain[5], on_soft_failed[0].event_id})
        self.assertEqual(counts["forward_extremities"], {2: 1})
        self.assertEqual(counts["stale_forward_extremities"], {1: 1})

    def _new_extremities_state_dag(
        self,
        existing: set[str],
        events: list[tuple[EventBase, EventContext]],
    ) -> set[str]:
        expected = self.get_success(
            self.persistence._calculate_new_state_dag_extremities(
                self.room_id,
                frozenset(existing),
                events,  # type: ignore[arg-type]
            )
        )
        result, _ = self.rust.calculate_new_state_dag_extremities(
            self.room_id, list(existing), events
        )
        self.assertEqual(result, expected)
        return result

    def test_calculate_new_state_dag_extremities(self) -> None:
        # The function reads the room DAG tables, so the room's messages
        # stand in for persisted state events.
        chain = self._dag_with_bad_events()

        # No state events: unchanged.
        self.assertEqual(
            self._new_extremities_state_dag({chain[5]}, [self._new([chain[5]])]),
            {chain[5]},
        )

        # A rejected new event is known to the next one, and a soft failed
        # one can be an extremity.
        rejected = self._new([], prev_state_events=[chain[5]], rejected=True)
        soft_failed = self._new(
            [], prev_state_events=[rejected[0].event_id], soft_failed=True
        )
        fork = self._new([], prev_state_events=[chain[1]])
        # A rejected event nothing points at is never an extremity.
        rejected_leaf = self._new([], prev_state_events=[chain[5]], rejected=True)
        self.assertEqual(
            self._new_extremities_state_dag(
                {chain[5], chain[1]}, [rejected, soft_failed, fork, rejected_leaf]
            ),
            {chain[5], soft_failed[0].event_id, fork[0].event_id},
        )

        # A prev state event that is rejected takes its ancestors with it:
        # chain[2] is rejected, so chain[1] goes.
        on_rejected = self._new([], prev_state_events=[chain[2]])
        self.assertEqual(
            self._new_extremities_state_dag({chain[1], chain[5]}, [on_rejected]),
            {chain[5], on_rejected[0].event_id},
        )

        # Soft failed events do not count on the state DAG: chain[3] is soft
        # failed, so the walk stops there and chain[1] stays.
        on_soft_failed = self._new([], prev_state_events=[chain[3]])
        self.assertEqual(
            self._new_extremities_state_dag({chain[1], chain[5]}, [on_soft_failed]),
            {chain[1], chain[5], on_soft_failed[0].event_id},
        )

    def test_missing_prev_state_events(self) -> None:
        message = self._send("known")
        events = [
            self._new([], prev_state_events=[message, "$gone1"]),
            self._new([], prev_state_events=["$gone2"]),
        ]
        failure = self.get_failure(
            self.persistence._calculate_new_state_dag_extremities(
                self.room_id,
                frozenset({message}),
                events,  # type: ignore[arg-type]
            ),
            SynapseError,
        )
        with self.assertRaises(SynapseError) as cm:
            self.rust.calculate_new_state_dag_extremities(
                self.room_id, [message], events
            )
        self.assertEqual(cm.exception.code, 500)
        self.assertEqual(cm.exception.msg, failure.value.msg)
        self.assertEqual(cm.exception.errcode, failure.value.errcode)

    def _inject(
        self, sender: str, prev: str, event_type: str = EventTypes.Message
    ) -> str:
        content: dict[str, Any] = (
            {} if event_type == EventTypes.Dummy else {"msgtype": "m.text", "body": "x"}
        )
        self._counter += 1
        content["n"] = self._counter
        return self.get_success(
            inject_event(
                self.hs,
                prev_event_ids=[prev],
                room_id=self.room_id,
                sender=sender,
                type=event_type,
                content=content,
            )
        ).event_id

    def _prune(
        self,
        dropped: str,
        events: list[tuple[EventBase, EventContext]],
        keep: str,
    ) -> set[str]:
        """Prune with `keep` in the resolved state group and `dropped` in
        another."""
        latest = {keep, dropped}
        groups = {keep: 1, dropped: 2}
        expected = self.get_success(
            self.persistence._prune_extremities(
                self.room_id, set(latest), 1, groups, events
            )
        )
        result, counts = self.rust.prune_extremities(
            self.room_id,
            self.hs.hostname,
            list(latest),
            1,
            groups,
            events,
            self.clock.time_msec(),
        )
        self.assertEqual(result, expected)
        self.assertEqual(counts["potential_times_prune_extremities"], 1)
        self.assertEqual(
            counts["times_pruned_extremities"], 1 if result == {keep} else 0
        )
        return result

    def test_prune_extremities(self) -> None:
        keep = self._send("keep")
        old_remote = self._inject("@x:remote", keep)
        old_local = self._inject(self.alice, keep)
        old_dummy_to_remote = self._inject(self.alice, old_remote, EventTypes.Dummy)
        old_dummy_to_local = self._inject(
            self.alice,
            self._inject(self.alice, old_local, EventTypes.Dummy),
            EventTypes.Dummy,
        )
        # Make the events above more than a day old.
        self.reactor.advance(2 * 24 * 60 * 60)
        new_remote = self._inject("@x:other", keep)
        depth = self.get_success(self.store.get_event(new_remote)).depth

        def chunk(
            depth_ahead: int, sender: str = "@y:elsewhere"
        ) -> list[tuple[EventBase, EventContext]]:
            return [self._new([keep], sender=sender, depth=depth + depth_ahead)]

        # Old and more than 100 deep: goes.
        self.assertEqual(self._prune(old_remote, chunk(101), keep), {keep})
        self.assertEqual(self._prune(old_remote, chunk(100), keep), {keep, old_remote})
        # Local events stay, unless dummy events that lead only to remote
        # events.
        self.assertEqual(self._prune(old_local, chunk(500), keep), {keep, old_local})
        self.assertEqual(self._prune(old_dummy_to_remote, chunk(500), keep), {keep})
        self.assertEqual(
            self._prune(old_dummy_to_local, chunk(500), keep),
            {keep, old_dummy_to_local},
        )
        # Recent: goes only if its server sent an event in the chunk, more
        # than 20 deep.
        self.assertEqual(self._prune(new_remote, chunk(500), keep), {keep, new_remote})
        self.assertEqual(self._prune(new_remote, chunk(21, "@z:other"), keep), {keep})
        self.assertEqual(
            self._prune(new_remote, chunk(20, "@z:other"), keep), {keep, new_remote}
        )
        # Never an extremity being persisted.
        being_persisted = self._new([keep], depth=depth + 500)
        self.assertEqual(
            self._prune(
                being_persisted[0].event_id, chunk(500) + [being_persisted], keep
            ),
            {keep, being_persisted[0].event_id},
        )


_Delta = tuple[set[tuple[str, str]], dict[tuple[str, str], str], bool]


def _python_delta(delta: DeltaState | None) -> _Delta | None:
    if delta is None:
        return None
    return (set(delta.to_delete), dict(delta.to_insert), delta.no_longer_in_room)


def _rust_delta(
    delta: tuple[list[tuple[str, str]], dict[tuple[str, str], str], bool] | None,
) -> _Delta | None:
    if delta is None:
        return None
    to_delete, to_insert, no_longer_in_room = delta
    return (set(to_delete), to_insert, no_longer_in_room)


class StateDeltaTestCase(PersistReaderTestBase):
    """The state delta in `rust/src/persist/state_delta.rs` against the
    controller's Python methods, over the same database and the same new
    events, which are built with real contexts but not persisted."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["experimental_features"]["msc4242_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence

    def _create(
        self,
        event_type: str,
        content: dict[str, Any],
        *,
        state_key: str | None = None,
        sender: str | None = None,
        prev_event_ids: list[str] | None = None,
        room_id: str | None = None,
    ) -> tuple[EventBase, EventContext]:
        """An event as the event creation handler builds it, with its state
        group stored but the event not persisted."""
        event_dict: dict[str, Any] = {
            "type": event_type,
            "room_id": room_id or self.room_id,
            "sender": sender or self.alice,
            "content": content,
        }
        if state_key is not None:
            event_dict["state_key"] = state_key
        event, unpersisted = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                create_requester(sender or self.alice),
                event_dict,
                prev_event_ids=prev_event_ids,
            )
        )
        context = self.get_success(unpersisted.persist(event))
        return event, context

    def _topic(
        self, topic: str, prev_event_ids: list[str] | None = None
    ) -> tuple[EventBase, EventContext]:
        return self._create(
            EventTypes.Topic,
            {"topic": topic},
            state_key="",
            prev_event_ids=prev_event_ids,
        )

    def _leave(
        self, user: str, prev_event_ids: list[str] | None = None
    ) -> tuple[EventBase, EventContext]:
        return self._create(
            EventTypes.Member,
            {"membership": Membership.LEAVE},
            state_key=user,
            sender=user,
            prev_event_ids=prev_event_ids,
        )

    def _persist(self, event_and_context: tuple[EventBase, EventContext]) -> str:
        event, context = event_and_context
        self.get_success(self.persistence.persist_event(event, context))
        return event.event_id

    def _latest(self) -> set[str]:
        return set(
            self.get_success(self.store.get_latest_event_ids_in_room(self.room_id))
        )

    def _new_state(
        self,
        events: list[tuple[EventBase, EventContext]],
        old: set[str],
        new: set[str],
    ) -> tuple[StateMap[str] | None, StateMap[str] | None, set[str], dict[str, Any]]:
        current_state, delta_ids, new_latest = self.get_success(
            self.persistence._get_new_state_after_events(
                self.room_id, events, old, set(new)
            )
        )
        rust_state, rust_delta, rust_latest, counts = (
            self.rust.get_new_state_after_events(
                self.room_id,
                self.hs.hostname,
                events,
                list(old),
                list(new),
                True,
                self.clock.time_msec(),
            )
        )
        self.assertEqual(
            rust_state, None if current_state is None else dict(current_state)
        )
        self.assertEqual(rust_delta, None if delta_ids is None else dict(delta_ids))
        self.assertEqual(rust_latest, new_latest)
        return rust_state, rust_delta, rust_latest, counts

    def test_get_new_state_after_events(self) -> None:
        latest = self._latest()

        # A message: the state groups do not change.
        message = self._create(EventTypes.Message, {"msgtype": "m.text", "body": "x"})
        current_state, delta, _, _ = self._new_state(
            [message], latest, {message[0].event_id}
        )
        self.assertIsNone(current_state)
        self.assertIsNone(delta)

        # A state event: its context knows the delta.
        topic = self._topic("one")
        current_state, delta, _, _ = self._new_state(
            [topic], latest, {topic[0].event_id}
        )
        self.assertIsNone(current_state)
        self.assertEqual(delta, {(EventTypes.Topic, ""): topic[0].event_id})

        # Without the delta, the new group's state.
        topic[1].state_group_deltas = {}
        current_state, delta, _, counts = self._new_state(
            [topic], latest, {topic[0].event_id}
        )
        assert current_state is not None
        self.assertIsNone(delta)
        self.assertEqual(current_state[(EventTypes.Topic, "")], topic[0].event_id)
        self.assertEqual(counts["state_resolutions"], 0)

    def test_resolution_of_a_fork(self) -> None:
        (parent,) = self._latest()
        persisted = self._persist(self._topic("persisted", [parent]))
        fork = self._topic("fork", [parent])
        current_state, delta, latest, counts = self._new_state(
            [fork], {persisted}, {persisted, fork[0].event_id}
        )
        assert current_state is not None
        self.assertIsNone(delta)
        self.assertIn(
            current_state[(EventTypes.Topic, "")], {persisted, fork[0].event_id}
        )
        self.assertEqual(latest, {persisted, fork[0].event_id})
        self.assertEqual(counts["state_resolutions"], 1)

    def _pending_deletions(self, groups: list[int]) -> dict[int, tuple[int, int]]:
        rows = self.get_success(
            self.hs.get_datastores().state_deletion.db_pool.simple_select_many_batch(
                table="state_groups_pending_deletion",
                column="state_group",
                iterable=groups,
                retcols=("state_group", "sequence_number", "insertion_ts"),
            )
        )
        return {group: (sequence, ts) for group, sequence, ts in rows}

    def test_resolution_bumps_pending_deletions(self) -> None:
        (parent,) = self._latest()
        persisted_topic = self._topic("persisted", [parent])
        persisted = self._persist(persisted_topic)
        fork = self._topic("fork", [parent])
        persisted_group = persisted_topic[1].state_group
        fork_group = fork[1].state_group
        assert persisted_group is not None and fork_group is not None
        groups = [persisted_group, fork_group]

        self.get_success(
            self.hs.get_datastores().state_deletion.mark_state_groups_as_pending_deletion(
                groups
            )
        )
        before = self._pending_deletions(groups)
        self.assertEqual(before.keys(), set(groups))

        # Only Rust runs, so the bump is its own.
        self.reactor.advance(60)
        self.rust.get_new_state_after_events(
            self.room_id,
            self.hs.hostname,
            [fork],
            [persisted],
            [persisted, fork[0].event_id],
            True,
            self.clock.time_msec(),
        )
        after = self._pending_deletions(groups)
        for group in groups:
            self.assertGreater(after[group][0], before[group][0])
            self.assertEqual(after[group][1], self.clock.time_msec())

    def test_resolution_of_a_deleted_group(self) -> None:
        (parent,) = self._latest()
        persisted = self._persist(self._topic("persisted", [parent]))
        fork = self._topic("fork", [parent])
        fork[1]._state_group = 1_000_000_000
        new = {persisted, fork[0].event_id}

        failure = self.get_failure(
            self.persistence._get_new_state_after_events(
                self.room_id, [fork], {persisted}, set(new)
            ),
            Exception,
        )
        self.assertIn("deleted", str(failure.value))
        with self.assertRaisesRegex(Exception, "State groups have been deleted"):
            self.rust.get_new_state_after_events(
                self.room_id,
                self.hs.hostname,
                [fork],
                [persisted],
                list(new),
                True,
                self.clock.time_msec(),
            )

    def test_calculate_state_delta(self) -> None:
        current = dict(
            self.get_success(self.store.get_partial_current_state_ids(self.room_id))
        )
        new_state = dict(current)
        del new_state[(EventTypes.JoinRules, "")]
        new_state[(EventTypes.Name, "")] = "$changed"
        new_state[(EventTypes.Topic, "")] = "$added"

        expected = self.get_success(
            self.persistence._calculate_state_delta(self.room_id, new_state)
        )
        to_delete, to_insert = self.rust.calculate_state_delta(self.room_id, new_state)
        self.assertEqual(set(to_delete), set(expected.to_delete))
        self.assertEqual(to_insert, expected.to_insert)
        self.assertEqual(set(to_delete), {(EventTypes.JoinRules, "")})
        self.assertEqual(
            to_insert,
            {(EventTypes.Name, ""): "$changed", (EventTypes.Topic, ""): "$added"},
        )

    def _still_joined(
        self,
        events: list[tuple[EventBase, EventContext]],
        to_delete: list[tuple[str, str]],
        to_insert: dict[tuple[str, str], str],
    ) -> bool:
        expected = self.get_success(
            self.persistence._is_server_still_joined(
                self.room_id, events, DeltaState(to_delete, to_insert)
            )
        )
        result = self.rust.is_server_still_joined(
            self.room_id, self.hs.hostname, events, to_delete, to_insert
        )
        self.assertEqual(result, expected)
        return result

    def test_is_server_still_joined(self) -> None:
        alice_key = (EventTypes.Member, self.alice)
        bob_key = (EventTypes.Member, self.bob)
        alice_leave = self._leave(self.alice)
        bob_leave = self._leave(self.bob)

        # No local membership changes.
        self.assertTrue(
            self._still_joined(
                [], [(EventTypes.Member, "@x:remote")], {(EventTypes.Topic, ""): "$t"}
            )
        )
        # Alice leaves, Bob is still joined.
        self.assertTrue(
            self._still_joined([alice_leave], [], {alice_key: alice_leave[0].event_id})
        )
        # Both leave, or one leaves and the other's membership goes.
        self.assertFalse(
            self._still_joined(
                [alice_leave, bob_leave],
                [],
                {
                    alice_key: alice_leave[0].event_id,
                    bob_key: bob_leave[0].event_id,
                },
            )
        )
        self.assertFalse(
            self._still_joined(
                [alice_leave], [bob_key], {alice_key: alice_leave[0].event_id}
            )
        )
        # A join in the database, which is Alice's current membership.
        alice_join = self.get_success(
            self.store.get_partial_current_state_ids(self.room_id)
        )[alice_key]
        self.assertTrue(
            self._still_joined([alice_leave], [bob_key], {alice_key: alice_join})
        )

    def test_still_joined_by_a_join_in_the_chunk(self) -> None:
        alice_key = (EventTypes.Member, self.alice)
        bob_key = (EventTypes.Member, self.bob)
        # Alice changes her display name, a join. Bob's membership goes, so
        # only the join in the chunk keeps the server in the room.
        rejoin = self._create(
            EventTypes.Member,
            {"membership": Membership.JOIN, "displayname": "Alice"},
            state_key=self.alice,
        )
        self.assertTrue(
            self._still_joined([rejoin], [bob_key], {alice_key: rejoin[0].event_id})
        )
        # Without the event in the chunk, the server reads the database,
        # where the join is not stored.
        self.assertFalse(
            self._still_joined([], [bob_key], {alice_key: rejoin[0].event_id})
        )

    def _orchestrate(
        self, events: list[tuple[EventBase, EventContext]]
    ) -> tuple[set[str] | None, _Delta | None, dict[str, Any]]:
        forward, delta = self.get_success(
            self.persistence._calculate_new_forward_extremities_and_state_delta(
                self.room_id, events
            )
        )
        rust_forward, rust_delta, counts = (
            self.rust.calculate_new_forward_extremities_and_state_delta(
                self.room_id, self.hs.hostname, events, self.clock.time_msec()
            )
        )
        self.assertEqual(rust_forward, forward)
        self.assertEqual(_rust_delta(rust_delta), _python_delta(delta))
        return rust_forward, _rust_delta(rust_delta), counts

    def test_calculate_new_forward_extremities_and_state_delta(self) -> None:
        # A message only moves the extremity.
        message = self._create(EventTypes.Message, {"msgtype": "m.text", "body": "x"})
        forward, delta, counts = self._orchestrate([message])
        self.assertEqual(forward, {message[0].event_id})
        self.assertIsNone(delta)
        self.assertEqual(counts["state_delta"], 0)

        # A state event, whose delta the context knows.
        topic = self._topic("one")
        forward, delta, counts = self._orchestrate([topic])
        self.assertEqual(
            delta, (set(), {(EventTypes.Topic, ""): topic[0].event_id}, False)
        )
        self.assertEqual(counts["state_delta"], 1)
        self.assertEqual(counts["state_delta_single_event"], 1)
        self.assertEqual(counts["state_delta_reuse_delta"], 1)

        # The last local user leaves, so the server leaves the room.
        self._persist(topic)
        self._persist(self._leave(self.alice))
        bob_leave = self._leave(self.bob)
        forward, delta, counts = self._orchestrate([bob_leave])
        self.assertEqual(
            delta,
            (set(), {(EventTypes.Member, self.bob): bob_leave[0].event_id}, True),
        )

    def test_orchestration_resolves_a_fork(self) -> None:
        (parent,) = self._latest()
        persisted = self._persist(self._topic("persisted", [parent]))
        fork = self._topic("fork", [parent])
        forward, delta, counts = self._orchestrate([fork])
        self.assertEqual(forward, {persisted, fork[0].event_id})
        assert delta is not None
        self.assertEqual(counts["state_resolutions"], 1)
        self.assertEqual(counts["state_delta"], 1)
        self.assertEqual(counts["state_delta_single_event"], 0)

    def test_state_dag_orchestration(self) -> None:
        room_id = self.helper.create_room_as(
            self.alice,
            room_version=RoomVersions.MSC4242v12.identifier,
            tok=self.alice_tok,
        )

        def compare(
            events: list[tuple[EventBase, EventContext]],
        ) -> tuple[set[str] | None, _Delta | None, set[str] | None, dict[str, Any]]:
            forward, delta, state_dag = self.get_success(
                self.persistence._process_state_dag_forward_extremities_and_state_delta(
                    room_id,
                    events,  # type: ignore[arg-type]
                )
            )
            rust_forward, rust_delta, rust_state_dag, counts = (
                self.rust.process_state_dag_forward_extremities_and_state_delta(
                    room_id, self.hs.hostname, events, self.clock.time_msec()
                )
            )
            self.assertEqual(rust_forward, forward)
            self.assertEqual(_rust_delta(rust_delta), _python_delta(delta))
            self.assertEqual(rust_state_dag, state_dag)
            return rust_forward, _rust_delta(rust_delta), rust_state_dag, counts

        # A message moves only the room DAG.
        message = self._create(
            EventTypes.Message, {"msgtype": "m.text", "body": "x"}, room_id=room_id
        )
        forward, delta, state_dag, _ = compare([message])
        self.assertEqual(forward, {message[0].event_id})
        self.assertIsNone(delta)

        # A state event moves both, and the state.
        name = self._create(
            EventTypes.Name, {"name": "dag"}, state_key="", room_id=room_id
        )
        forward, delta, state_dag, counts = compare([name])
        self.assertEqual(state_dag, {name[0].event_id})
        self.assertEqual(
            delta, (set(), {(EventTypes.Name, ""): name[0].event_id}, False)
        )
        self.assertEqual(counts["state_dag_forward_extremities"], {1: 1})


_Snapshot = tuple[str, str, str, str]


class SlidingSyncTestCase(PersistReaderTestBase):
    """`rust/src/persist/sliding_sync.rs` against
    `_calculate_sliding_sync_table_changes`, over the same database, chunk
    and delta. Rust sorts the snapshots to insert by user ID."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.room_version = self.get_success(self.store.get_room_version(self.room_id))
        self._counter = 0

    def _new(
        self,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
        sender: str | None = None,
    ) -> tuple[EventBase, EventContext]:
        """A new state event for the chunk, not persisted."""
        self._counter += 1
        fields: dict[str, Any] = {
            "room_id": self.room_id,
            "sender": sender or self.alice,
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "prev_events": [],
            "auth_events": [],
            # The event ID hashes the redacted event, which keeps the depth
            # and not most of the content.
            "depth": 100 + self._counter,
        }
        event = make_test_event(fields, self.room_version)
        return event, EventContext.for_outlier(self.hs.get_storage_controllers())

    def _state(self) -> dict[tuple[str, str], str]:
        return dict(
            self.get_success(self.store.get_partial_current_state_ids(self.room_id))
        )

    def _send_state(self, event_type: str, content: dict[str, Any]) -> str:
        return self.helper.send_state(
            self.room_id, event_type, content, tok=self.alice_tok
        )["event_id"]

    def _compare(
        self,
        events: list[tuple[EventBase, EventContext]],
        to_insert: dict[tuple[str, str], str],
        to_delete: list[tuple[str, str]] | None = None,
        no_longer_in_room: bool = False,
    ) -> tuple[int | None, dict[str, Any], dict[str, Any], list[_Snapshot], list[str]]:
        delta = DeltaState(
            to_delete=to_delete or [],
            to_insert=to_insert,
            no_longer_in_room=no_longer_in_room,
        )
        expected = self.get_success(
            self.persist_events_store._calculate_sliding_sync_table_changes(
                self.room_id,
                events,
                delta,
            )
        )
        result = self.rust.calculate_sliding_sync_table_changes(
            self.room_id,
            self.hs.hostname,
            [event for event, _ in events],
            list(delta.to_delete),
            dict(delta.to_insert),
            no_longer_in_room,
        )
        bump, joined, shared, inserts, deletes = result
        self.assertEqual(bump, expected.joined_room_bump_stamp_to_fully_insert)
        self.assertEqual(joined, expected.joined_room_updates)
        self.assertEqual(shared, expected.membership_snapshot_shared_insert_values)
        self.assertEqual(
            inserts,
            sorted(
                (i.user_id, i.sender, i.membership_event_id, i.membership)
                for i in expected.to_insert_membership_snapshots
            ),
        )
        self.assertEqual(deletes, expected.to_delete_membership_snapshots)
        return result

    def _both_fail(
        self,
        events: list[tuple[EventBase, EventContext]],
        to_insert: dict[tuple[str, str], str],
        reason: str,
        no_longer_in_room: bool = False,
    ) -> None:
        delta = DeltaState(
            to_delete=[], to_insert=to_insert, no_longer_in_room=no_longer_in_room
        )
        self.get_failure(
            self.persist_events_store._calculate_sliding_sync_table_changes(
                self.room_id,
                events,
                delta,
            ),
            AssertionError,
        )
        with self.assertRaisesRegex(RuntimeError, reason):
            self.rust.calculate_sliding_sync_table_changes(
                self.room_id,
                self.hs.hostname,
                [event for event, _ in events],
                [],
                to_insert,
                no_longer_in_room,
            )

    def test_empty_delta(self) -> None:
        self.assertEqual(self._compare([], {}), (None, {}, {}, [], []))

    def test_local_joins_and_state_in_the_chunk(self) -> None:
        carol = self.register_user("carol", "pass")
        dave = self.register_user("dave", "pass")
        carol_join = self._new(
            EventTypes.Member, carol, {"membership": "join"}, sender=carol
        )
        dave_invite = self._new(EventTypes.Member, dave, {"membership": "invite"})
        name = self._new(EventTypes.Name, "", {"name": "chunk"})
        encryption = self._new(
            EventTypes.RoomEncryption, "", {"algorithm": "m.megolm.v1.aes-sha2"}
        )
        # The remote member and the topic are not loaded. They would fail
        # the step if they were.
        _, joined, shared, inserts, _ = self._compare(
            [dave_invite, carol_join, name, encryption],
            {
                (EventTypes.Member, dave): dave_invite[0].event_id,
                (EventTypes.Member, carol): carol_join[0].event_id,
                (EventTypes.Member, "@remote:other"): "$unknown",
                (EventTypes.Name, ""): name[0].event_id,
                (EventTypes.RoomEncryption, ""): encryption[0].event_id,
                (EventTypes.Topic, ""): "$also_unknown",
            },
        )
        self.assertEqual(
            inserts,
            [
                (carol, carol, carol_join[0].event_id, "join"),
                (dave, self.alice, dave_invite[0].event_id, "invite"),
            ],
        )
        # The joined room's row exists, so it takes only the delta.
        self.assertEqual(joined, {"room_name": "chunk", "is_encrypted": True})
        self.assertEqual(shared, {**joined, "room_type": None, "has_known_state": True})

    def test_missing_joined_room_row(self) -> None:
        self._send_state(EventTypes.Name, {"name": "stored"})
        message = self._send("bump")
        self._send_state(EventTypes.Topic, {"topic": "no bump"})
        self._write(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = ?", self.room_id
        )
        topic = self._new(EventTypes.Topic, "", {"topic": "chunk"})
        bump, joined, shared, inserts, _ = self._compare(
            [topic], {(EventTypes.Topic, ""): topic[0].event_id}
        )
        (stream,) = self._sql(
            "SELECT stream_ordering FROM events WHERE event_id = ?", message
        )[0]
        self.assertEqual(bump, stream)
        self.assertEqual(joined, {"room_type": None, "room_name": "stored"})
        self.assertEqual((shared, inserts), ({}, []))

    def test_state_reset_to_stored_events(self) -> None:
        state = self._state()
        old_name = self._send_state(EventTypes.Name, {"name": "old"})
        self._send_state(EventTypes.Name, {"name": "new"})
        self._send_state(
            EventTypes.RoomEncryption, {"algorithm": "m.megolm.v1.aes-sha2"}
        )
        # Reset Bob back to his join, out of the chunk, and the name back to
        # an older event. Alice is reset out of the room.
        _, joined, shared, inserts, deletes = self._compare(
            [],
            {
                (EventTypes.Member, self.bob): state[(EventTypes.Member, self.bob)],
                (EventTypes.Name, ""): old_name,
            },
            to_delete=[
                (EventTypes.Member, self.alice),
                (EventTypes.Member, "@remote:other"),
                (EventTypes.RoomEncryption, ""),
                (EventTypes.Create, ""),
            ],
        )
        self.assertEqual(
            inserts,
            [(self.bob, self.bob, state[(EventTypes.Member, self.bob)], "join")],
        )
        self.assertEqual(deletes, [self.alice])
        # The deletes clear the joined room's columns. The snapshot drops the
        # deleted state.
        self.assertEqual(
            joined, {"room_name": "old", "room_type": None, "is_encrypted": False}
        )
        self.assertEqual(shared, {"room_name": "old", "has_known_state": True})

    def test_redacted_state(self) -> None:
        name = self._send_state(EventTypes.Name, {"name": "redact me"})
        channel = self.make_request(
            "PUT",
            f"/rooms/{self.room_id}/redact/{name}/txn1",
            {},
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._write(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = ?", self.room_id
        )
        topic = self._new(EventTypes.Topic, "", {"topic": "chunk"})
        _, joined, _, _, _ = self._compare(
            [topic], {(EventTypes.Topic, ""): topic[0].event_id}
        )
        self.assertEqual(joined, {"room_type": None, "room_name": None})

    def test_values_postgres_cannot_store(self) -> None:
        carol = self.register_user("carol", "pass")
        carol_join = self._new(
            EventTypes.Member, carol, {"membership": "join"}, sender=carol
        )
        name = self._new(EventTypes.Name, "", {"name": "a\0b"})
        tombstone = self._new(EventTypes.Tombstone, "", {"replacement_room": 5})
        encryption = self._new(EventTypes.RoomEncryption, "", {"algorithm": None})
        _, joined, shared, _, _ = self._compare(
            [carol_join, name, tombstone, encryption],
            {
                (EventTypes.Member, carol): carol_join[0].event_id,
                (EventTypes.Name, ""): name[0].event_id,
                (EventTypes.Tombstone, ""): tombstone[0].event_id,
                (EventTypes.RoomEncryption, ""): encryption[0].event_id,
            },
        )
        self.assertEqual(joined, {"is_encrypted": False})
        self.assertEqual(shared, {**joined, "room_type": None, "has_known_state": True})

    def test_leave_after_the_server_left(self) -> None:
        bob_leave = self._new(
            EventTypes.Member, self.bob, {"membership": "leave"}, sender=self.bob
        )
        to_insert = {(EventTypes.Member, self.bob): bob_leave[0].event_id}
        self._write("DELETE FROM current_state_events WHERE room_id = ?", self.room_id)
        self.assertEqual(
            self._compare([bob_leave], to_insert, no_longer_in_room=True),
            (None, {}, {}, [(self.bob, self.bob, bob_leave[0].event_id, "leave")], []),
        )
        # Without state to work from, the server must have left.
        self._both_fail([bob_leave], to_insert, "no current state")

    def test_missing_membership_event(self) -> None:
        self._both_fail(
            [], {(EventTypes.Member, self.bob): "$unknown"}, "events not found"
        )

    def test_rejected_stored_state(self) -> None:
        old_name = self._send_state(EventTypes.Name, {"name": "old"})
        self._send_state(EventTypes.Name, {"name": "new"})
        self._reject(old_name)
        self._both_fail([], {(EventTypes.Name, ""): old_name}, "events not found")

    def test_backfilled_bump_events(self) -> None:
        # Only backfilled events, with negative stream orderings, can bump.
        self._send("bump")

        def negate(txn: LoggingTransaction) -> None:
            # Other tables refer to the stream orderings. Skip their foreign
            # key checks for this transaction.
            txn.execute("SET LOCAL session_replication_role = replica")
            txn.execute(
                "UPDATE events SET stream_ordering = -stream_ordering"
                " WHERE room_id = ? AND type = ANY(?)",
                (self.room_id, list(SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES)),
            )

        self.get_success(self.store.db_pool.runInteraction("negate", negate))
        self._write(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = ?", self.room_id
        )
        topic = self._new(EventTypes.Topic, "", {"topic": "chunk"})
        bump, _, _, _, _ = self._compare(
            [topic], {(EventTypes.Topic, ""): topic[0].event_id}
        )
        self.assertIsNone(bump)

    def test_stored_tombstone(self) -> None:
        self._send_state(EventTypes.Tombstone, {"replacement_room": "!next:test"})
        self._write(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = ?", self.room_id
        )
        topic = self._new(EventTypes.Topic, "", {"topic": "chunk"})
        _, joined, _, _, _ = self._compare(
            [topic], {(EventTypes.Topic, ""): topic[0].event_id}
        )
        self.assertEqual(
            joined, {"room_type": None, "tombstone_successor_room_id": "!next:test"}
        )

    def test_space_with_a_redacted_create_event(self) -> None:
        # A redaction would drop the create event's `type` up to room
        # version 10, but neither side serves a redacted create event.
        self.room_id = self.helper.create_room_as(
            self.alice,
            room_version=RoomVersions.V10.identifier,
            extra_content={"creation_content": {"type": "m.space"}},
            tok=self.alice_tok,
        )
        self._write(
            "DELETE FROM sliding_sync_joined_rooms WHERE room_id = ?", self.room_id
        )
        topic = self._new(EventTypes.Topic, "", {"topic": "chunk"})
        to_insert = {(EventTypes.Topic, ""): topic[0].event_id}
        _, joined, _, _, _ = self._compare([topic], to_insert)
        self.assertEqual(joined, {"room_type": "m.space"})

        # The client API refuses to redact a create event.
        self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                type=EventTypes.Redaction,
                sender=self.alice,
                redacts=self._state()[(EventTypes.Create, "")],
                content={},
            )
        )
        _, joined, _, _, _ = self._compare([topic], to_insert)
        self.assertEqual(joined, {"room_type": "m.space"})


class StickyTestCase(PersistReaderTestBase):
    """`rust/src/persist/sticky.rs` against
    `compute_sticky_events_to_un_soft_fail`, over the same database, chunk
    and delta."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["experimental_features"]["msc4354_enabled"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.room_version = self.get_success(self.store.get_room_version(self.room_id))
        self.carol = self.register_user("carol", "pass")
        self.carol_tok = self.login("carol", "pass")
        self._counter = 0

    def _sticky(self, sender: str, minutes: int = 10) -> str:
        """A soft-failed sticky event, as federation would persist one."""
        self._counter += 1
        return self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                sender=sender,
                type=EventTypes.Message,
                content={"msgtype": "m.text", "body": f"sticky {self._counter}"},
                internal_metadata={"soft_failed": True},
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=minutes).as_millis()
                ),
            )
        ).event_id

    def _new(
        self,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
        sender: str | None = None,
    ) -> tuple[EventBase, EventContext]:
        """A new state event for the chunk, not persisted."""
        self._counter += 1
        fields: dict[str, Any] = {
            "room_id": self.room_id,
            "sender": sender or self.alice,
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "prev_events": [],
            "auth_events": [],
            "depth": 100 + self._counter,
        }
        event = make_test_event(fields, self.room_version)
        return event, EventContext.for_outlier(self.hs.get_storage_controllers())

    def _power_levels(self, users: dict[str, Any]) -> dict[str, Any]:
        # The creator has unlimited power in room version 12.
        return {"users": users, "events_default": 0}

    def _compare(
        self,
        events: list[tuple[EventBase, EventContext]],
        to_insert: dict[tuple[str, str], str],
        to_delete: list[tuple[str, str]] | None = None,
        no_longer_in_room: bool = False,
    ) -> set[str]:
        delta = DeltaState(
            to_delete=to_delete or [],
            to_insert=to_insert,
            no_longer_in_room=no_longer_in_room,
        )
        expected = self.get_success(
            self.store.compute_sticky_events_to_un_soft_fail(
                self.room_id, events, delta
            )
        )
        result = self.rust.compute_sticky_events_to_un_soft_fail(
            self.room_id,
            [event for event, _ in events],
            list(delta.to_delete),
            dict(delta.to_insert),
            no_longer_in_room,
            self.clock.time_msec(),
        )
        self.assertEqual(result, expected)
        return result

    def _devoiced_bob(self) -> str:
        """Bob can't send messages, and has a soft-failed sticky event."""
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            self._power_levels({self.bob: -1}),
            tok=self.alice_tok,
        )
        return self._sticky(self.bob)

    def _pl_delta(
        self, users: dict[str, Any]
    ) -> tuple[list[tuple[EventBase, EventContext]], dict[tuple[str, str], str]]:
        pl = self._new(EventTypes.PowerLevels, "", self._power_levels(users))
        return [pl], {(EventTypes.PowerLevels, ""): pl[0].event_id}

    def test_power_levels_from_the_chunk(self) -> None:
        sticky = self._devoiced_bob()
        self.assertEqual(self._compare(*self._pl_delta({self.bob: 0})), {sticky})
        self.assertEqual(self._compare(*self._pl_delta({self.bob: -5})), set())

    def test_stored_power_levels(self) -> None:
        # The state rolls back to power levels that are already stored.
        old_pl = self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            self._power_levels({}),
            tok=self.alice_tok,
        )["event_id"]
        sticky = self._devoiced_bob()
        self.assertEqual(
            self._compare([], {(EventTypes.PowerLevels, ""): old_pl}), {sticky}
        )

    def test_member_changes_recheck_only_those_senders(self) -> None:
        # Alice's event would pass, but her membership doesn't change.
        self._sticky(self.alice)
        carol_sticky = self._sticky(self.carol)
        carol_join = self._new(
            EventTypes.Member, self.carol, {"membership": "join"}, sender=self.carol
        )
        self.assertEqual(
            self._compare(
                [carol_join], {(EventTypes.Member, self.carol): carol_join[0].event_id}
            ),
            {carol_sticky},
        )

    def test_stored_membership(self) -> None:
        # Carol joined and left, and the state rolls back to her join.
        carol_join = self.helper.join(self.room_id, self.carol, tok=self.carol_tok)[
            "event_id"
        ]
        self.helper.leave(self.room_id, self.carol, tok=self.carol_tok)
        carol_sticky = self._sticky(self.carol)
        self.assertEqual(
            self._compare([], {(EventTypes.Member, self.carol): carol_join}),
            {carol_sticky},
        )

    def test_join_rules_recheck_everyone(self) -> None:
        alice_sticky = self._sticky(self.alice)
        self._sticky(self.carol)
        join_rules = self._new(EventTypes.JoinRules, "", {"join_rule": "public"})
        self.assertEqual(
            self._compare(
                [join_rules], {(EventTypes.JoinRules, ""): join_rules[0].event_id}
            ),
            {alice_sticky},
        )

    def test_non_critical_changes(self) -> None:
        self._sticky(self.alice)
        topic = self._new(EventTypes.Topic, "", {"topic": "new"})
        self.assertEqual(
            self._compare([topic], {(EventTypes.Topic, ""): topic[0].event_id}),
            set(),
        )

    def test_no_longer_in_room(self) -> None:
        self._sticky(self.alice)
        events, to_insert = self._pl_delta({})
        self.assertEqual(
            self._compare(events, to_insert, no_longer_in_room=True), set()
        )

    def test_deleted_membership(self) -> None:
        self._sticky(self.alice)
        events, to_insert = self._pl_delta({})
        self.assertEqual(
            self._compare(
                events, to_insert, to_delete=[(EventTypes.Member, self.alice)]
            ),
            set(),
        )

    def test_malformed_power_levels(self) -> None:
        """Both fail on power levels the auth rules can't read."""
        self._devoiced_bob()
        events, to_insert = self._pl_delta({self.bob: "abc"})
        delta = DeltaState(to_delete=[], to_insert=to_insert)
        python = self.get_failure(
            self.store.compute_sticky_events_to_un_soft_fail(
                self.room_id, events, delta
            ),
            Exception,
        )
        with self.assertRaises(Exception) as rust:
            self.rust.compute_sticky_events_to_un_soft_fail(
                self.room_id,
                [event for event, _ in events],
                [],
                dict(to_insert),
                False,
                self.clock.time_msec(),
            )
        self.assertEqual(type(rust.exception), python.type)

    def test_redacted_power_levels(self) -> None:
        """Both read stored auth state redacted. Redaction keeps every power
        level field the rules read, so Bob stays devoiced."""
        sticky = self._devoiced_bob()
        pl_id = self.get_success(
            self.store.get_partial_filtered_current_state_ids(self.room_id)
        )[(EventTypes.PowerLevels, "")]
        channel = self.make_request(
            "PUT",
            f"/rooms/{self.room_id}/redact/{pl_id}/txn1",
            {},
            access_token=self.alice_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        member = self._new(
            EventTypes.Member, self.bob, {"membership": "join"}, sender=self.bob
        )
        self.assertEqual(
            self._compare(
                [member], {(EventTypes.Member, self.bob): member[0].event_id}
            ),
            set(),
        )
        # A soft-failed event of Bob's is still there to recheck.
        self.assertIn(
            sticky,
            self.rust.soft_failed_sticky_events(
                self.room_id, None, self.clock.time_msec()
            ),
        )

    def test_unneeded_insert(self) -> None:
        # A change to Carol's membership doesn't touch Alice's auth state.
        alice_sticky = self._sticky(self.alice)
        events, to_insert = self._pl_delta({})
        carol_join = self._new(
            EventTypes.Member, self.carol, {"membership": "join"}, sender=self.carol
        )
        to_insert[(EventTypes.Member, self.carol)] = carol_join[0].event_id
        self.assertEqual(
            self._compare(events + [carol_join], to_insert), {alice_sticky}
        )

    def test_events_in_the_chunk(self) -> None:
        alice_sticky = self._sticky(self.alice)
        events, to_insert = self._pl_delta({})
        sticky_event = self.get_success(self.store.get_event(alice_sticky))
        self.assertEqual(
            self._compare(
                events
                + [
                    (
                        sticky_event,
                        EventContext.for_outlier(self.hs.get_storage_controllers()),
                    )
                ],
                to_insert,
            ),
            set(),
        )

    def test_expired(self) -> None:
        self._sticky(self.alice, minutes=1)
        alice_sticky = self._sticky(self.alice, minutes=5)
        self.reactor.advance(120)
        self.assertEqual(self._compare(*self._pl_delta({})), {alice_sticky})

    def test_rejected(self) -> None:
        self._reject(self._sticky(self.alice))
        alice_sticky = self._sticky(self.alice)
        self.assertEqual(self._compare(*self._pl_delta({})), {alice_sticky})

    def test_not_soft_failed(self) -> None:
        alice_sticky = self._sticky(self.alice)
        self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "fine", "msgtype": "m.text"},
            tok=self.alice_tok,
        )
        self.assertEqual(self._compare(*self._pl_delta({})), {alice_sticky})
