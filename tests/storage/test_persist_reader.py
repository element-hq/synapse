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

from synapse.api.constants import EventTypes
from synapse.api.errors import NotFoundError
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.event_federation import EventFederationStore
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.persist import PersistReaderForTest
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.util.clock import Clock

from tests import unittest
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
