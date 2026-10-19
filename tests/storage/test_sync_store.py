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

"""The Rust sync store (`rust/src/storage/sync_store.rs`) against the Python
store methods it ports, on the same database. These need the native Rust
Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_sync_store
"""

import random
from typing import Any
from unittest import SkipTest

from immutabledict import immutabledict
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    AccountDataTypes,
    Direction,
    EventContentFields,
    EventTypes,
    RelationTypes,
)
from synapse.events import EventBase
from synapse.logging.context import LoggingContext
from synapse.rest import admin
from synapse.rest.client import login, receipts, room
from synapse.server import HomeServer
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.sync import SyncStoreForTest
from synapse.types import RoomStreamToken, StreamKeyType, StreamToken
from synapse.types.state import StateFilter
from synapse.util.clock import Clock

from tests import unittest
from tests.sync.fixtures import SEEDS, WRITERS, random_live_token

ONE_DAY_MS = 24 * 60 * 60 * 1000


class SyncStoreTestCase(unittest.HomeserverTestCase):
    """Each test runs a Rust query and the Python store method against the
    same fixture and compares."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["retention"] = {
            "enabled": True,
            "default_policy": {
                "min_lifetime": ONE_DAY_MS,
                "max_lifetime": 3 * ONE_DAY_MS,
            },
            "allowed_lifetime_min": ONE_DAY_MS,
            "allowed_lifetime_max": 30 * ONE_DAY_MS,
        }
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        db_pool = self.store.db_pool._db_pool
        assert isinstance(db_pool, RustConnectionPool)
        assert db_pool._pool is not None
        rust_handler = hs.get_rust_handlers().sync
        assert rust_handler is not None
        self.rust = SyncStoreForTest(db_pool._pool, rust_handler)

        self.alice = self.register_user("alice", "pass", admin=True)
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    def test_transactions_begin_with_their_first_statement(self) -> None:
        """A transaction's `BEGIN` goes out with its first statement, which runs
        inside the transaction."""
        self.assertEqual(
            self.rust.transaction_settings(),
            [("repeatable read", "on"), ("repeatable read", "off")],
        )

    def test_rooms_for_user(self) -> None:
        other = self.helper.create_room_as(self.bob, tok=self.bob_tok)
        self.helper.invite(other, self.bob, self.alice, tok=self.bob_tok)

        for user in (self.alice, self.bob, "@nobody:test"):
            self.assertEqual(
                self.rust.rooms_for_user(user),
                set(self.get_success(self.store.get_rooms_for_user(user))),
            )

        # A membership change invalidates the Python cache, and the Rust cache
        # with it.
        self.helper.join(other, self.alice, tok=self.alice_tok)
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        for user in (self.alice, self.bob):
            self.assertEqual(
                self.rust.rooms_for_user(user),
                set(self.get_success(self.store.get_rooms_for_user(user))),
            )

    def test_events_in_current_state(self) -> None:
        topic = self.helper.send_state(
            self.room_id, EventTypes.Topic, {"topic": "a"}, tok=self.alice_tok
        )["event_id"]
        old_name = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "a"}, tok=self.alice_tok
        )["event_id"]
        self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "b"}, tok=self.alice_tok
        )
        message = self.helper.send(self.room_id, "hi", tok=self.alice_tok)["event_id"]

        ids = [topic, old_name, message, "$unknown"]
        self.assertEqual(
            self.rust.events_in_current_state(ids),
            set(self.get_success(self.store.check_if_events_in_current_state(ids))),
        )
        self.assertEqual(self.rust.events_in_current_state([]), set())

    @parameterized.expand(SEEDS)
    def test_timeline_gap(self, seed: int) -> None:
        """Gaps recorded by several writers, against random tokens with
        instance maps."""
        rng = random.Random(seed)
        for stream in rng.sample(range(1, 40), 12):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "timeline_gaps",
                    {
                        "room_id": self.room_id,
                        "instance_name": rng.choice(WRITERS),
                        "stream_ordering": stream,
                    },
                )
            )
        for _ in range(20):
            from_token = random_live_token(rng) if rng.random() < 0.8 else None
            to_token = random_live_token(rng)
            to_token = RoomStreamToken(
                stream=to_token.stream + 15,
                instance_map=immutabledict(
                    {name: pos + 15 for name, pos in to_token.instance_map.items()}
                ),
            )
            expected = self.get_success(
                self.store.get_timeline_gaps(self.room_id, from_token, to_token)
            )
            self.assertEqual(
                self.rust.timeline_gap(self.room_id, from_token, to_token),
                expected,
                f"from {from_token} to {to_token}",
            )

    def test_partial_state_rooms(self) -> None:
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        join = self.helper.join(other, self.bob, tok=self.bob_tok)["event_id"]
        self.get_success(
            self.store.db_pool.simple_insert(
                "partial_state_rooms",
                {
                    "room_id": other,
                    "device_lists_stream_id": 0,
                    "join_event_id": join,
                    "joined_via": "other",
                },
            )
        )
        # Creating the room read the flag already, so the Python cache is
        # stale, as it would never be in production.
        self.store.is_partial_state_room.invalidate((other,))
        rooms = [self.room_id, other, "!unknown:test"]
        expected = dict(
            self.get_success(self.store.is_partial_state_room_batched(rooms))
        )
        self.assertEqual(self.rust.partial_state_rooms(rooms), expected)
        self.assertTrue(expected[other])

        # Clearing the flag invalidates the Python cache, and the Rust cache
        # with it.
        self.get_success(
            self.store.db_pool.simple_delete(
                "partial_state_rooms", {"room_id": other}, desc="clear"
            )
        )
        self.store.is_partial_state_room.invalidate((other,))
        self.assertEqual(
            self.rust.partial_state_rooms(rooms), dict.fromkeys(rooms, False)
        )

    def test_room_summary(self) -> None:
        carol = self.register_user("carol", "pass")
        carol_tok = self.login("carol", "pass")
        dave = self.register_user("dave", "pass")
        dave_tok = self.login("dave", "pass")
        self.helper.join(self.room_id, carol, tok=carol_tok)
        self.helper.join(self.room_id, dave, tok=dave_tok)
        self.helper.leave(self.room_id, carol, tok=carol_tok)
        self.helper.ban(self.room_id, self.alice, dave, tok=self.alice_tok)
        self.helper.invite(self.room_id, self.alice, "@erin:test", tok=self.alice_tok)
        empty = self.helper.create_room_as(self.bob, tok=self.bob_tok)
        self.helper.leave(empty, self.bob, tok=self.bob_tok)

        for room_id in (self.room_id, empty, "!unknown:test"):
            expected = {
                membership: (list(summary.members), summary.count)
                for membership, summary in self.get_success(
                    self.store.get_room_summary(room_id)
                ).items()
            }
            self.assertEqual(self.rust.room_summary(room_id), expected, room_id)

        # A membership change invalidates the Python cache, and the Rust cache
        # with it.
        self.helper.join(self.room_id, carol, tok=carol_tok)
        expected = {
            membership: (list(summary.members), summary.count)
            for membership, summary in self.get_success(
                self.store.get_room_summary(self.room_id)
            ).items()
        }
        self.assertEqual(self.rust.room_summary(self.room_id), expected)

    def test_tags_for_room(self) -> None:
        self.get_success(
            self.store.add_tag_to_room(
                self.alice, self.room_id, "m.favourite", {"order": 0.5}
            )
        )
        self.get_success(
            self.store.add_tag_to_room(self.alice, self.room_id, "u.work", {})
        )
        for user in (self.alice, self.bob):
            self.assertEqual(
                self.rust.tags_for_room(user, self.room_id),
                dict(
                    self.get_success(self.store.get_tags_for_room(user, self.room_id))
                ),
            )

    def test_global_account_data_by_type(self) -> None:
        content = {"a": [1, 2, {"b": None}], "big": 2**60}
        self.get_success(
            self.store.add_account_data_for_user(
                self.alice, "org.example.type", content
            )
        )
        for user, data_type in (
            (self.alice, "org.example.type"),
            (self.alice, "org.example.other"),
            (self.bob, "org.example.type"),
        ):
            self.assertEqual(
                self.rust.global_account_data_by_type(user, data_type),
                self.get_success(
                    self.store.get_global_account_data_by_type_for_user(user, data_type)
                ),
            )

    def test_ignored_users(self) -> None:
        self.get_success(
            self.store.add_account_data_for_user(
                self.alice,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": {self.bob: {}, "@spam:example.com": {}}},
            )
        )
        for user in (self.alice, self.bob):
            self.assertEqual(
                self.rust.ignored_users(user),
                set(self.get_success(self.store.ignored_users(user))),
            )

    def test_are_users_erased(self) -> None:
        self.get_success(self.store.mark_user_erased(self.bob))
        users = [self.alice, self.bob, "@nobody:test"]
        self.assertEqual(
            self.rust.are_users_erased(users),
            dict(self.get_success(self.store.are_users_erased(users))),
        )
        # Erasure invalidates the Python cache, and the Rust cache with it.
        self.get_success(self.store.mark_user_erased(self.alice))
        self.assertEqual(
            self.rust.are_users_erased(users),
            dict(self.get_success(self.store.are_users_erased(users))),
        )

    def test_retention_policy(self) -> None:
        with_policy = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.send_state(
            with_policy,
            EventTypes.Retention,
            {"max_lifetime": 2 * ONE_DAY_MS},
            tok=self.alice_tok,
        )
        for room_id in (self.room_id, with_policy, "!unknown:test"):
            policy = self.get_success(self.store.get_retention_policy_for_room(room_id))
            self.assertEqual(
                self.rust.retention_policy(room_id),
                (policy.min_lifetime, policy.max_lifetime),
                room_id,
            )
        # A new policy invalidates the Python cache, and the Rust cache with
        # it.
        self.helper.send_state(
            with_policy,
            EventTypes.Retention,
            {"min_lifetime": 2 * ONE_DAY_MS, "max_lifetime": 3 * ONE_DAY_MS},
            tok=self.alice_tok,
        )
        policy = self.get_success(self.store.get_retention_policy_for_room(with_policy))
        self.assertEqual(
            self.rust.retention_policy(with_policy),
            (policy.min_lifetime, policy.max_lifetime),
        )

    def test_is_server_admin(self) -> None:
        for user in (self.alice, self.bob, "@nobody:test"):
            self.assertEqual(
                self.rust.is_server_admin(user),
                self.get_success(self.store.is_server_admin(user)),
            )

    # Events and the timeline.

    def _redact(self, room_id: str, event_id: str, tok: str) -> str:
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id}/redact/{event_id}/{random.random()}",
            content={},
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body["event_id"]

    def _invalidate_event_caches(self, *event_ids: str) -> None:
        """Drop events from both event caches, so that a change made to the
        tables behind their backs is read."""
        for event_id in event_ids:
            self.store._invalidate_local_get_event_cache(event_id)

    def _event_summary(self, event: EventBase) -> tuple[Any, ...]:
        return (
            event.event_id,
            dict(event.content),
            event.rejected_reason,
            event.internal_metadata.is_redacted(),
            event.internal_metadata.redacted_by,
            event.unsigned.get("prev_content"),
            event.unsigned.get("prev_sender"),
        )

    def _assert_events_agree(
        self,
        event_ids: list[str],
        redact_behaviour: EventRedactBehaviour = EventRedactBehaviour.redact,
        allow_rejected: bool = False,
        get_prev_content: bool = False,
    ) -> None:
        expected = self.get_success(
            self.store.get_events_as_list(
                event_ids,
                redact_behaviour=redact_behaviour,
                allow_rejected=allow_rejected,
                get_prev_content=get_prev_content,
            )
        )
        actual = self.rust.events(
            event_ids,
            redact_behaviour=redact_behaviour.name,
            allow_rejected=allow_rejected,
            get_prev_content=get_prev_content,
        )
        self.assertEqual(
            [self._event_summary(e) for e in actual],
            [self._event_summary(e) for e in expected],
            f"{redact_behaviour} allow_rejected={allow_rejected}",
        )

    def test_events(self) -> None:
        """Events with redactions of every kind, under each redact behaviour."""
        create = self.get_success(
            self.store.get_create_event_for_room(self.room_id)
        ).event_id
        plain = self.helper.send(self.room_id, "plain", tok=self.alice_tok)["event_id"]
        redacted = self.helper.send(self.room_id, "gone", tok=self.alice_tok)[
            "event_id"
        ]
        redaction = self._redact(self.room_id, redacted, self.alice_tok)
        rejected = self.helper.send(self.room_id, "rejected", tok=self.bob_tok)[
            "event_id"
        ]
        self.get_success(
            self.store.db_pool.simple_insert(
                "rejections",
                {"event_id": rejected, "reason": "test", "last_check": "x"},
            )
        )
        # A redaction persisted before its target is checked when read.
        rechecked = self.helper.send(self.room_id, "rechecked", tok=self.alice_tok)[
            "event_id"
        ]
        recheck_redaction = self._redact(self.room_id, rechecked, self.alice_tok)
        self.get_success(
            self.store.db_pool.simple_update_one(
                "redactions", {"event_id": recheck_redaction}, {"recheck": True}
            )
        )
        # MSC4293: a ban can redact the banned user's events before it.
        bob_before = [
            self.helper.send(self.room_id, f"bob {i}", tok=self.bob_tok)["event_id"]
            for i in range(3)
        ]
        ban = self.helper.ban(self.room_id, self.alice, self.bob, tok=self.alice_tok)
        bob_end = self.get_success(
            self.store.get_position_for_event(bob_before[2])
        ).stream
        self.get_success(
            self.store.db_pool.simple_insert(
                "room_ban_redactions",
                {
                    "room_id": self.room_id,
                    "user_id": self.bob,
                    "redacting_event_id": ban["event_id"],
                    "redact_end_ordering": bob_end,
                },
            )
        )
        self._invalidate_event_caches(
            rejected, rechecked, recheck_redaction, *bob_before, ban["event_id"]
        )

        ids = [
            create,
            plain,
            redacted,
            redaction,
            rejected,
            rechecked,
            recheck_redaction,
            *bob_before,
            ban["event_id"],
            "$unknown",
            plain,
        ]
        for behaviour in EventRedactBehaviour:
            for allow_rejected in (False, True):
                self._assert_events_agree(ids, behaviour, allow_rejected)

    def test_events_prev_content(self) -> None:
        first = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "a"}, tok=self.alice_tok
        )["event_id"]
        second = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "b"}, tok=self.alice_tok
        )["event_id"]
        third = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "c"}, tok=self.alice_tok
        )["event_id"]
        self._invalidate_event_caches(first, second, third)
        self._assert_events_agree([third, second, first], get_prev_content=False)
        self._assert_events_agree([third, second, first], get_prev_content=True)

    @unittest.override_config({"event_cache_size": 1000})
    def test_events_share_one_cache(self) -> None:
        """Python and the Rust readers read and fill the same event cache."""
        first = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "a"}, tok=self.alice_tok
        )["event_id"]
        second = self.helper.send_state(
            self.room_id, EventTypes.Name, {"name": "b"}, tok=self.alice_tok
        )["event_id"]
        self._invalidate_event_caches(first, second)

        # Python adds `prev_content` to the event it fetched, and the Rust
        # reader sees it on the same cached event, though it did not ask.
        self.get_success(self.store.get_events_as_list([second], get_prev_content=True))
        [rust_event] = self.rust.events(
            [second],
            redact_behaviour="redact",
            allow_rejected=False,
            get_prev_content=False,
        )
        self.assertEqual(rust_event.unsigned.get("prev_content"), {"name": "a"})

        # An event the Rust reader loaded is a cache hit for Python.
        self._invalidate_event_caches(first)
        self.rust.events(
            [first],
            redact_behaviour="redact",
            allow_rejected=False,
            get_prev_content=False,
        )
        with LoggingContext(name="test", server_name=self.hs.hostname) as ctx:
            self.get_success(self.store.get_event(first))
            self.assertEqual(ctx.get_resource_usage().evt_db_fetch_count, 0)

    def _write_events(self, room_id: str, count: int) -> list[str]:
        """Send `count` events of a few kinds, spread over several writers."""
        rng = random.Random(count)
        event_ids = []
        for i in range(count):
            kind = rng.random()
            if kind < 0.2:
                event = self.helper.send_state(
                    room_id, EventTypes.Topic, {"topic": str(i)}, tok=self.alice_tok
                )
            elif kind < 0.3:
                event = self.helper.send_state(
                    room_id,
                    EventTypes.Member,
                    {"membership": "join", "displayname": str(i)},
                    state_key=self.bob,
                    tok=self.bob_tok,
                )
            else:
                tok = self.alice_tok if kind < 0.7 else self.bob_tok
                event = self.helper.send(room_id, str(i), tok=tok)
            event_ids.append(event["event_id"])
        # Pretend some were persisted by other writers.
        for event_id in event_ids:
            self.get_success(
                self.store.db_pool.simple_update_one(
                    "events",
                    {"event_id": event_id},
                    {"instance_name": rng.choice(WRITERS)},
                )
            )
        self._invalidate_event_caches(*event_ids)
        return event_ids

    def _stream_range(self, room_id: str) -> tuple[int, int]:
        rows = self.get_success(
            self.store.db_pool.execute(
                "range",
                "SELECT MIN(stream_ordering), MAX(stream_ordering), MAX(topological_ordering)"
                " FROM events WHERE room_id = ?",
                room_id,
            )
        )
        return rows[0][0], rows[0][1]

    def _random_token(
        self, rng: random.Random, lo: int, hi: int, historic: bool
    ) -> RoomStreamToken:
        stream = rng.randint(lo - 2, hi + 2)
        if historic:
            return RoomStreamToken(topological=rng.randint(1, hi), stream=stream)
        instance_map = {
            name: stream + rng.randint(1, 6)
            for name in rng.sample(WRITERS, rng.randint(0, len(WRITERS)))
        }
        return RoomStreamToken(stream=stream, instance_map=immutabledict(instance_map))

    def test_current_topological_token(self) -> None:
        """Every stream position around the room's, read by the bounded scan
        and by the aggregate it falls back to, against Python's."""
        self._write_events(self.room_id, 15)

        # Orderings that do not rise together: a late event with a low
        # topological ordering, as a late federated event gets, and an
        # outlier. The rows are only read here, so they need not be events
        # anything else knows about.
        def add_row(
            event_id: str, stream: int, topological: int, outlier: bool
        ) -> None:
            self.get_success(
                self.store.db_pool.simple_insert(
                    "events",
                    {
                        "event_id": event_id,
                        "room_id": self.room_id,
                        "type": "m.room.message",
                        "stream_ordering": stream,
                        "topological_ordering": topological,
                        "processed": True,
                        "outlier": outlier,
                    },
                    desc="add_row",
                )
            )

        _, last = self._stream_range(self.room_id)
        add_row("$late", last + 100, 2, False)
        add_row("$outlier", last + 101, 1, True)
        # A backfilled event, with a negative stream ordering.
        add_row("$backfilled", -50, 3, False)
        lo, hi = self._stream_range(self.room_id)
        other_room = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        for stream in range(lo - 2, hi + 3):
            expected = self.get_success(
                self.store.get_current_topological_token(self.room_id, stream)
            )
            for scan in (1, 3, 1001):
                self.assertEqual(
                    self.rust.current_topological_token(self.room_id, stream, scan),
                    expected,
                    (stream, scan),
                )
        # A room without events at all.
        self.assertEqual(self.rust.current_topological_token("!nothing:test", 5), 0)
        self.assertGreater(self.rust.current_topological_token(other_room, hi), 0)

    @parameterized.expand(SEEDS)
    def test_paginate_room_events(self, seed: int) -> None:
        rng = random.Random(seed)
        self._write_events(self.room_id, 25)
        lo, hi = self._stream_range(self.room_id)
        for _ in range(12):
            order = rng.choice(["stream", "topological"])
            historic = order == "topological" and rng.random() < 0.4
            from_key = self._random_token(rng, lo, hi, historic)
            to_key = (
                self._random_token(rng, lo, hi, historic and rng.random() < 0.5)
                if rng.random() < 0.7
                else None
            )
            direction = rng.choice([Direction.FORWARDS, Direction.BACKWARDS])
            limit = rng.choice([1, 2, 5, 10, 40])
            python = (
                self.store.paginate_room_events_by_stream_ordering
                if order == "stream"
                else self.store.paginate_room_events_by_topological_ordering
            )
            expected_events, expected_token, expected_limited = self.get_success(
                python(
                    room_id=self.room_id,
                    from_key=from_key,
                    to_key=to_key,
                    direction=direction,
                    limit=limit,
                )
            )
            events, token, limited = self.rust.paginate_room_events(
                self.room_id, from_key, to_key, direction.value, limit, order
            )
            context = f"{order} {direction} from {from_key} to {to_key} limit {limit}"
            self.assertEqual(
                [self._event_summary(e) for e in events],
                [self._event_summary(e) for e in expected_events],
                context,
            )
            self.assertEqual(token, expected_token, context)
            self.assertEqual(limited, expected_limited, context)

    @parameterized.expand(SEEDS)
    def test_recent_event_ids_and_last_event_before(self, seed: int) -> None:
        rng = random.Random(seed)
        self._write_events(self.room_id, 15)
        lo, hi = self._stream_range(self.room_id)
        for _ in range(12):
            end_token = self._random_token(rng, lo, hi, False)
            limit = rng.choice([0, 1, 3, 20])
            expected_rows, expected_token = self.get_success(
                self.store.get_recent_event_ids_for_room(self.room_id, limit, end_token)
            )
            rows, token = self.rust.recent_event_ids(self.room_id, limit, end_token)
            self.assertEqual(
                rows,
                [
                    (r.event_id, r.topological_ordering, r.stream_ordering)
                    for r in expected_rows
                ],
                f"limit {limit} at {end_token}",
            )
            self.assertEqual(token, expected_token, f"limit {limit} at {end_token}")

            expected_last = self.get_success(
                self.store.get_last_event_pos_in_room_before_stream_ordering(
                    self.room_id, end_token
                )
            )
            last = self.rust.last_event_before(self.room_id, end_token)
            if expected_last is None:
                self.assertIsNone(last, str(end_token))
            else:
                event_id, position = expected_last
                self.assertEqual(
                    last,
                    (event_id, position.instance_name, position.stream),
                    str(end_token),
                )

    @parameterized.expand(SEEDS)
    def test_last_event_of_types_before(self, seed: int) -> None:
        rng = random.Random(seed)
        self._write_events(self.room_id, 15)
        lo, hi = self._stream_range(self.room_id)
        type_choices = [
            EventTypes.Message,
            EventTypes.Topic,
            EventTypes.Member,
            EventTypes.Create,
            "org.example.unknown",
        ]
        for _ in range(12):
            end_token = self._random_token(rng, lo, hi, False)
            event_types = rng.sample(type_choices, rng.randint(0, 3))
            expected = self.get_success(
                self.store.get_last_event_pos_in_room_before_stream_ordering(
                    self.room_id, end_token, event_types=event_types
                )
            )
            last = self.rust.last_event_of_types_before(
                self.room_id, end_token, event_types
            )
            context = f"{event_types} at {end_token}"
            if expected is None:
                self.assertIsNone(last, context)
            else:
                event_id, position = expected
                self.assertEqual(
                    last, (event_id, position.instance_name, position.stream), context
                )

    @parameterized.expand(SEEDS)
    def test_membership_changes(self, seed: int) -> None:
        rng = random.Random(seed)
        rooms = [self.room_id] + [
            self.helper.create_room_as(self.alice, tok=self.alice_tok) for _ in range(2)
        ]
        event_ids = []
        for i in range(12):
            room_id = rng.choice(rooms)
            joined = room_id in self.get_success(
                self.store.get_rooms_for_user(self.bob)
            )
            if joined and rng.random() < 0.4:
                event = self.helper.leave(room_id, self.bob, tok=self.bob_tok)
            elif joined:
                event = self.helper.send_state(
                    room_id,
                    EventTypes.Member,
                    {"membership": "join", "displayname": str(i)},
                    state_key=self.bob,
                    tok=self.bob_tok,
                )
            else:
                event = self.helper.join(room_id, self.bob, tok=self.bob_tok)
            event_ids.append(event["event_id"])
        for event_id in event_ids:
            self.get_success(
                self.store.db_pool.simple_update_one(
                    "events",
                    {"event_id": event_id},
                    {"instance_name": rng.choice(WRITERS)},
                )
            )
        self._invalidate_event_caches(*event_ids)
        lo, hi = self._stream_range(self.room_id)
        for _ in range(10):
            from_key = self._random_token(rng, lo, hi, False)
            to_key = self._random_token(rng, lo, hi, False)
            excluded = (
                rng.sample(rooms, rng.randint(0, 2)) if rng.random() < 0.5 else None
            )
            expected = self.get_success(
                self.store.get_membership_changes_for_user(
                    self.bob, from_key, to_key, excluded_rooms=excluded
                )
            )
            actual = self.rust.membership_changes(self.bob, from_key, to_key, excluded)
            self.assertEqual(
                [self._event_summary(e) for e in actual],
                [self._event_summary(e) for e in expected],
                f"from {from_key} to {to_key} excluding {excluded}",
            )

    # State.

    def _state_filters(self) -> list[StateFilter]:
        return [
            StateFilter.all(),
            StateFilter.none(),
            StateFilter.from_types([(EventTypes.Member, self.bob)]),
            StateFilter.from_types([(EventTypes.Member, None), (EventTypes.Name, "")]),
            StateFilter(
                types=immutabledict({EventTypes.Member: frozenset()}),
                include_others=True,
            ),
            StateFilter(
                types=immutabledict(
                    {EventTypes.Member: frozenset({self.alice}), EventTypes.Topic: None}
                ),
                include_others=True,
            ),
        ]

    def test_current_state_ids(self) -> None:
        self._write_events(self.room_id, 10)
        controller = self.hs.get_storage_controllers().state
        for state_filter in self._state_filters():
            self.assertEqual(
                self.rust.current_state_ids(self.room_id, state_filter),
                dict(
                    self.get_success(
                        controller.get_current_state_ids(
                            self.room_id, state_filter=state_filter
                        )
                    )
                ),
                str(state_filter),
            )

    def test_state_group_for_events(self) -> None:
        event_ids = self._write_events(self.room_id, 6) + ["$unknown"]
        self.assertEqual(
            self.rust.state_group_for_events(event_ids),
            dict(self.get_success(self.store._get_state_group_for_events(event_ids))),
        )
        with self.assertRaises(RuntimeError):
            self.rust.state_ids_for_event("$unknown", StateFilter.all())

    @parameterized.expand(SEEDS[:5])
    def test_state_for_event_and_at(self, seed: int) -> None:
        rng = random.Random(seed)
        event_ids = self._write_events(self.room_id, 12)
        controller = self.hs.get_storage_controllers().state
        lo, hi = self._stream_range(self.room_id)
        for _ in range(6):
            state_filter = rng.choice(self._state_filters())
            event_id = rng.choice(event_ids)
            self.assertEqual(
                self.rust.state_ids_for_event(event_id, state_filter),
                dict(
                    self.get_success(
                        controller.get_state_ids_for_event(
                            event_id, state_filter=state_filter
                        )
                    )
                ),
                f"{event_id} {state_filter}",
            )
            self.assertEqual(
                self.rust.state_after_event(event_id, state_filter),
                dict(
                    self.get_success(
                        controller.get_state_after_event(
                            event_id, state_filter=state_filter
                        )
                    )
                ),
                f"{event_id} {state_filter}",
            )
            token = self._random_token(rng, lo, hi, False)
            self.assertEqual(
                self.rust.state_ids_at(self.room_id, token, state_filter),
                dict(
                    self.get_success(
                        controller.get_state_ids_at(
                            self.room_id,
                            StreamToken.START.copy_and_replace(
                                StreamKeyType.ROOM, token
                            ),
                            state_filter=state_filter,
                        )
                    )
                ),
                f"{token} {state_filter}",
            )

    @parameterized.expand(SEEDS)
    def test_state_deltas(self, seed: int) -> None:
        rng = random.Random(seed)
        event_ids = self._write_events(self.room_id, 15)
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self._write_events(other, 4)
        # The delta rows of the rewritten events move to the same writer.
        for event_id in event_ids:
            rows = self.get_success(
                self.store.db_pool.simple_select_list(
                    "events", {"event_id": event_id}, ["instance_name"]
                )
            )
            self.get_success(
                self.store.db_pool.simple_update(
                    "current_state_delta_stream",
                    {"event_id": event_id},
                    {"instance_name": rows[0][0]},
                    desc="rewrite",
                )
            )
        lo, hi = self._stream_range(self.room_id)
        for _ in range(12):
            from_token = (
                self._random_token(rng, lo, hi, False) if rng.random() < 0.8 else None
            )
            to_token = (
                self._random_token(rng, lo, hi, False) if rng.random() < 0.8 else None
            )
            expected = self.get_success(
                self.store.get_current_state_deltas_for_room_by_event_position(
                    self.room_id, from_token=from_token, to_token=to_token
                )
            )
            self.assertEqual(
                sorted(self.rust.state_deltas(self.room_id, from_token, to_token)),
                sorted(
                    (
                        d.stream_id,
                        d.event_type,
                        d.state_key,
                        d.event_id,
                        d.prev_event_id,
                    )
                    for d in expected
                ),
                f"from {from_token} to {to_token}",
            )

    @parameterized.expand(SEEDS)
    def test_state_deltas_by_stream_id(self, seed: int) -> None:
        rng = random.Random(seed)
        event_ids = self._write_events(self.room_id, 15)
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self._write_events(other, 4)
        # The delta rows of the rewritten events move to the same writer.
        for event_id in event_ids:
            rows = self.get_success(
                self.store.db_pool.simple_select_list(
                    "events", {"event_id": event_id}, ["instance_name"]
                )
            )
            self.get_success(
                self.store.db_pool.simple_update(
                    "current_state_delta_stream",
                    {"event_id": event_id},
                    {"instance_name": rows[0][0]},
                    desc="rewrite",
                )
            )
        lo, hi = self._stream_range(self.room_id)
        for _ in range(12):
            from_token = (
                self._random_token(rng, lo, hi, False) if rng.random() < 0.8 else None
            )
            to_token = (
                self._random_token(rng, lo, hi, False) if rng.random() < 0.8 else None
            )
            expected = self.get_success(
                self.store.get_current_state_deltas_for_room(
                    self.room_id, from_token=from_token, to_token=to_token
                )
            )
            # In `stream_id` order. Deltas of one batch share a `stream_id`, so
            # compare them as a sorted list.
            rust = self.rust.state_deltas_by_stream_id(
                self.room_id, from_token, to_token
            )
            self.assertEqual(
                [delta[0] for delta in rust], sorted(delta[0] for delta in rust)
            )
            self.assertEqual(
                sorted(rust),
                sorted(
                    (
                        d.stream_id,
                        d.event_type,
                        d.state_key,
                        d.event_id,
                        d.prev_event_id,
                    )
                    for d in expected
                ),
                f"from {from_token} to {to_token}",
            )

    def test_member_counts(self) -> None:
        carol = self.register_user("carol", "pass")
        carol_tok = self.login("carol", "pass")
        dave = self.register_user("dave", "pass")
        self.helper.invite(self.room_id, self.alice, carol, tok=self.alice_tok)
        self.helper.invite(self.room_id, self.alice, dave, tok=self.alice_tok)
        self.helper.join(self.room_id, carol, tok=carol_tok)
        self.helper.leave(self.room_id, carol, tok=carol_tok)
        self.helper.ban(self.room_id, self.alice, self.bob, tok=self.alice_tok)
        other = self.helper.create_room_as(self.bob, tok=self.bob_tok)

        for room_id in (self.room_id, other, "!unknown:test"):
            self.assertEqual(
                self.rust.member_counts(room_id),
                dict(self.get_success(self.store.get_member_counts(room_id))),
                room_id,
            )

    def test_latest_bump_stamp(self) -> None:
        self.helper.send(self.room_id, "hi", tok=self.alice_tok)
        # A room whose row has no bump stamp.
        no_stamp = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.get_success(
            self.store.db_pool.simple_update_one(
                "sliding_sync_joined_rooms",
                {"room_id": no_stamp},
                {"bump_stamp": None},
            )
        )
        for room_id in (self.room_id, no_stamp, "!unknown:test"):
            self.assertEqual(
                self.rust.latest_bump_stamp(room_id),
                self.get_success(self.store.get_latest_bump_stamp_for_room(room_id)),
                room_id,
            )
        self.assertIsNotNone(self.rust.latest_bump_stamp(self.room_id))
        self.assertIsNone(self.rust.latest_bump_stamp(no_stamp))

    # Notifications.

    def _unread_counts(
        self, room_id: str, user_id: str
    ) -> tuple[tuple[int, int, int], dict[str, tuple[int, int, int]]]:
        counts = self.get_success(
            self.store.get_unread_event_push_actions_by_room_for_user(room_id, user_id)
        )
        return (
            (
                counts.main_timeline.notify_count,
                counts.main_timeline.unread_count,
                counts.main_timeline.highlight_count,
            ),
            {
                thread_id: (c.notify_count, c.unread_count, c.highlight_count)
                for thread_id, c in counts.threads.items()
            },
        )

    def test_unread_counts(self) -> None:
        """Counts through messages, highlights, threads, receipts and
        rotations of the summary."""

        def send(body: str, highlight: bool = False, thread: str | None = None) -> str:
            content: dict[str, Any] = {"msgtype": "m.text", "body": body}
            if highlight:
                content[EventContentFields.MENTIONS] = {"user_ids": [self.alice]}
            if thread is not None:
                content["m.relates_to"] = {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread,
                }
            return self.helper.send_event(
                self.room_id, "m.room.message", content, tok=self.bob_tok
            )["event_id"]

        def read(event_id: str, thread: str | None = None) -> None:
            channel = self.make_request(
                "POST",
                f"/rooms/{self.room_id}/receipt/m.read/{event_id}",
                {"thread_id": thread} if thread else {},
                access_token=self.alice_tok,
            )
            self.assertEqual(channel.code, 200, channel.json_body)

        def check(context: str) -> None:
            self.assertEqual(
                self.rust.unread_counts(self.room_id, self.alice),
                self._unread_counts(self.room_id, self.alice),
                context,
            )

        check("nothing yet")
        root = send("1")
        check("one message")
        send("2", highlight=True)
        check("a highlight")
        self.get_success(self.store._rotate_notifs())
        check("rotated")
        first_reply = send("t1", thread=root)
        send("t2", thread=root, highlight=True)
        check("thread replies")
        self.get_success(self.store._rotate_notifs())
        check("rotated with a thread")
        send("3")
        check("after rotation")
        read(root)
        check("read the first message")
        read(first_reply, thread=root)
        check("read the first reply")
        send("t3", thread=root)
        send("4", highlight=True)
        self.get_success(self.store._rotate_notifs())
        check("rotated again")
        read(send("5"))
        check("read everything in the main timeline")
