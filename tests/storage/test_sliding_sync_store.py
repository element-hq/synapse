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

"""The Rust sliding sync store (`rust/src/storage/sliding_sync_store.rs`)
against the Python store methods it ports, on the same database. These need
the native Rust Postgres driver:

    SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.storage.test_sliding_sync_store
"""

import random
from typing import Any
from unittest import SkipTest
from unittest.mock import patch

from immutabledict import immutabledict
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, RoomTypes
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.databases.main.state import ROOM_UNKNOWN_SENTINEL
from synapse.storage.roommember import RoomsForUserSlidingSync
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.sliding_sync import SlidingSyncStoreForTest
from synapse.types import (
    PersistedEventPosition,
    RoomStreamToken,
    StreamKeyType,
    StreamToken,
)
from synapse.util.clock import Clock

from tests import unittest
from tests.sync.fixtures import SEEDS, WRITERS


def _snapshot(room: RoomsForUserSlidingSync) -> tuple[Any, ...]:
    """A Python snapshot in the Rust test form."""
    return (
        room.room_id,
        room.sender,
        room.membership,
        room.event_id,
        room.room_version_id,
        room.event_pos.instance_name,
        room.event_pos.stream,
        room.has_known_state,
        room.room_type,
        bool(room.is_encrypted),
    )


def _position(position: PersistedEventPosition | None) -> tuple[str, int] | None:
    if position is None:
        return None
    # A null instance name reads as "master" on the Rust side.
    return (position.instance_name or "master", position.stream)


class SlidingSyncStoreTestCase(unittest.HomeserverTestCase):
    """Each test runs a Rust query and the Python store method against the
    same fixture and compares."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

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
        rust_handler = hs.get_rust_handlers().sliding_sync
        assert rust_handler is not None
        self.rust = SlidingSyncStoreForTest(db_pool._pool, rust_handler)

        self.alice = self.register_user("alice", "pass")
        self.alice_tok = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    # Fixtures.

    def _forget(self, room_id: str, tok: str) -> None:
        channel = self.make_request(
            "POST", f"/_matrix/client/r0/rooms/{room_id}/forget", {}, access_token=tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _bob_memberships(self) -> list[str]:
        """Rooms with every kind of membership snapshot for Bob: joined,
        invited, left himself, kicked, banned, forgotten, a space, an
        encrypted room. Returns them."""
        rooms = [self.room_id]
        invited = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.invite(invited, self.alice, self.bob, tok=self.alice_tok)
        rooms.append(invited)
        left = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(left, self.bob, tok=self.bob_tok)
        self.helper.leave(left, self.bob, tok=self.bob_tok)
        rooms.append(left)
        kicked = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(kicked, self.bob, tok=self.bob_tok)
        self.helper.change_membership(
            kicked, self.alice, self.bob, "leave", tok=self.alice_tok
        )
        rooms.append(kicked)
        banned = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(banned, self.bob, tok=self.bob_tok)
        self.helper.ban(banned, self.alice, self.bob, tok=self.alice_tok)
        rooms.append(banned)
        forgotten = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(forgotten, self.bob, tok=self.bob_tok)
        self.helper.leave(forgotten, self.bob, tok=self.bob_tok)
        self._forget(forgotten, self.bob_tok)
        rooms.append(forgotten)
        space = self.helper.create_room_as(
            self.alice,
            tok=self.alice_tok,
            extra_content={"creation_content": {"type": RoomTypes.SPACE}},
        )
        self.helper.join(space, self.bob, tok=self.bob_tok)
        rooms.append(space)
        encrypted = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.send_state(
            encrypted,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=self.alice_tok,
        )
        self.helper.join(encrypted, self.bob, tok=self.bob_tok)
        rooms.append(encrypted)
        return rooms

    def _scatter_writers(self, rng: random.Random, user_id: str) -> None:
        """Pretend the user's memberships were persisted by random writers,
        in every table the queries take an instance name from."""
        rows = self.get_success(
            self.store.db_pool.execute(
                "snapshots",
                "SELECT room_id FROM sliding_sync_membership_snapshots WHERE user_id = ?",
                user_id,
            )
        )
        for (room_id,) in rows:
            self.get_success(
                self.store.db_pool.simple_update_one(
                    "sliding_sync_membership_snapshots",
                    {"room_id": room_id, "user_id": user_id},
                    {"event_instance_name": rng.choice(WRITERS)},
                )
            )
        rows = self.get_success(
            self.store.db_pool.execute(
                "deltas",
                "SELECT stream_id, room_id, type, state_key"
                " FROM current_state_delta_stream WHERE state_key = ?",
                user_id,
            )
        )
        for stream_id, room_id, event_type, state_key in rows:
            self.get_success(
                self.store.db_pool.simple_update(
                    "current_state_delta_stream",
                    {
                        "stream_id": stream_id,
                        "room_id": room_id,
                        "type": event_type,
                        "state_key": state_key,
                    },
                    {"instance_name": rng.choice(WRITERS + [None])},
                    desc="scatter",
                )
            )
        rows = self.get_success(
            self.store.db_pool.execute(
                "events",
                "SELECT event_id FROM room_memberships WHERE user_id = ?",
                user_id,
            )
        )
        for (event_id,) in rows:
            self.get_success(
                self.store.db_pool.simple_update_one(
                    "events",
                    {"event_id": event_id},
                    {"instance_name": rng.choice(WRITERS)},
                )
            )
        # The changes behind the caches' backs.
        self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots.invalidate_all()
        self.store._get_event_cache.clear()

    def _stream_range(self) -> tuple[int, int]:
        rows = self.get_success(
            self.store.db_pool.execute(
                "range", "SELECT MIN(stream_ordering), MAX(stream_ordering) FROM events"
            )
        )
        return rows[0][0], rows[0][1]

    def _random_token(self, rng: random.Random, lo: int, hi: int) -> RoomStreamToken:
        stream = rng.randint(lo - 2, hi + 2)
        instance_map = {
            name: stream + rng.randint(1, 6)
            for name in rng.sample(WRITERS, rng.randint(0, len(WRITERS)))
        }
        return RoomStreamToken(stream=stream, instance_map=immutabledict(instance_map))

    # Membership snapshots.

    def test_rooms_from_snapshots(self) -> None:
        self._bob_memberships()
        for user in (self.alice, self.bob, "@nobody:test"):
            expected = self.get_success(
                self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots(
                    user
                )
            )
            self.assertEqual(
                self.rust.rooms_from_snapshots(user),
                {room_id: _snapshot(room) for room_id, room in expected.items()},
                user,
            )

        # A membership change invalidates the Python cache, and the Rust one
        # with it.
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(other, self.bob, tok=self.bob_tok)
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        expected = self.get_success(
            self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots(
                self.bob
            )
        )
        self.assertIn(other, expected)
        self.assertNotIn(self.room_id, expected)
        self.assertEqual(
            self.rust.rooms_from_snapshots(self.bob),
            {room_id: _snapshot(room) for room_id, room in expected.items()},
        )

    def test_rooms_from_snapshots_skips_unknown_versions(self) -> None:
        other = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        self.helper.join(other, self.bob, tok=self.bob_tok)
        self.get_success(
            self.store.db_pool.simple_update_one(
                "rooms", {"room_id": other}, {"room_version": "unknown-version"}
            )
        )
        self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots.invalidate_all()
        expected = self.get_success(
            self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots(
                self.bob
            )
        )
        self.assertNotIn(other, expected)
        self.assertEqual(
            self.rust.rooms_from_snapshots(self.bob),
            {room_id: _snapshot(room) for room_id, room in expected.items()},
        )
        # The single and batch reads keep it.
        kept = self.get_success(
            self.store.get_sliding_sync_room_for_user(self.bob, other)
        )
        assert kept is not None
        self.assertEqual(
            self.rust.snapshot_room(self.bob, other),
            _snapshot(kept),
        )

    @parameterized.expand(SEEDS)
    def test_self_leave_rooms_after(self, seed: int) -> None:
        rng = random.Random(seed)
        self._bob_memberships()
        for _ in range(3):
            room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
            self.helper.join(room_id, self.bob, tok=self.bob_tok)
            self.helper.leave(room_id, self.bob, tok=self.bob_tok)
        self._scatter_writers(rng, self.bob)
        lo, hi = self._stream_range()
        for _ in range(10):
            to_token = self._random_token(rng, lo, hi)
            expected = self.get_success(
                self.store.get_sliding_sync_self_leave_rooms_after_to_token(
                    self.bob,
                    StreamToken.START.copy_and_replace(StreamKeyType.ROOM, to_token),
                )
            )
            self.assertEqual(
                self.rust.self_leave_rooms_after(self.bob, to_token),
                {room_id: _snapshot(room) for room_id, room in expected.items()},
                to_token,
            )

    def test_snapshot_rooms(self) -> None:
        rooms = self._bob_memberships() + ["!unknown:test"]
        for room_id in rooms:
            expected = self.get_success(
                self.store.get_sliding_sync_room_for_user(self.bob, room_id)
            )
            self.assertEqual(
                self.rust.snapshot_room(self.bob, room_id),
                None if expected is None else _snapshot(expected),
                room_id,
            )
        expected_batch = self.get_success(
            self.store.get_sliding_sync_room_for_user_batch(self.bob, rooms)
        )
        self.assertEqual(
            self.rust.snapshot_rooms(self.bob, rooms),
            {room_id: _snapshot(room) for room_id, room in expected_batch.items()},
        )
        self.assertEqual(self.rust.snapshot_rooms(self.bob, []), {})

    # Membership changes.

    def _random_memberships(self, rng: random.Random) -> list[str]:
        """Random joins, leaves, display name changes, kicks and invites of
        Bob in a few rooms."""
        rooms = [self.room_id] + [
            self.helper.create_room_as(self.alice, tok=self.alice_tok) for _ in range(3)
        ]
        for i in range(14):
            room_id = rng.choice(rooms)
            membership = self.get_success(
                self.store.get_local_current_membership_for_user_in_room(
                    self.bob, room_id
                )
            )[0]
            roll = rng.random()
            if membership == "join" and roll < 0.3:
                self.helper.leave(room_id, self.bob, tok=self.bob_tok)
            elif membership == "join" and roll < 0.45:
                self.helper.change_membership(
                    room_id, self.alice, self.bob, "leave", tok=self.alice_tok
                )
            elif membership == "join":
                self.helper.send_state(
                    room_id,
                    EventTypes.Member,
                    {"membership": "join", "displayname": str(i)},
                    state_key=self.bob,
                    tok=self.bob_tok,
                )
            elif membership != "invite" and roll < 0.3:
                self.helper.invite(room_id, self.alice, self.bob, tok=self.alice_tok)
            else:
                self.helper.join(room_id, self.bob, tok=self.bob_tok)
        return rooms

    @parameterized.expand(SEEDS)
    def test_snapshot_membership_changes(self, seed: int) -> None:
        rng = random.Random(seed)
        rooms = self._random_memberships(rng)
        self._scatter_writers(rng, self.bob)
        lo, hi = self._stream_range()
        for _ in range(10):
            from_token = self._random_token(rng, lo, hi)
            to_token = self._random_token(rng, lo, hi)
            excluded = set(rng.sample(rooms, rng.randint(0, 2)))
            may_have_changed = self.store._membership_stream_cache.has_entity_changed(
                self.bob, from_token.stream
            )
            expected = self.get_success(
                self.store.get_sliding_sync_membership_changes(
                    self.bob, from_token, to_token, excluded_room_ids=excluded
                )
            )
            self.assertEqual(
                self.rust.snapshot_membership_changes(
                    self.bob, from_token, to_token, list(excluded), may_have_changed
                ),
                [
                    (
                        change.room_id,
                        change.sender,
                        change.membership,
                        change.event_id,
                        _position(change.event_pos),
                        change.room_version_id,
                    )
                    for change in expected.values()
                ],
                f"from {from_token} to {to_token} excluding {excluded}",
            )

    @parameterized.expand(SEEDS)
    def test_membership_delta_changes(self, seed: int) -> None:
        rng = random.Random(seed)
        rooms = self._random_memberships(rng)
        self._scatter_writers(rng, self.bob)
        lo, hi = self._stream_range()
        for _ in range(10):
            from_token = self._random_token(rng, lo, hi)
            to_token = self._random_token(rng, lo, hi)
            excluded = rng.sample(rooms, rng.randint(0, 2))
            may_have_changed = self.store._membership_stream_cache.has_entity_changed(
                self.bob, from_token.stream
            )
            expected = self.get_success(
                self.store.get_current_state_delta_membership_changes_for_user(
                    self.bob, from_token, to_token, excluded_room_ids=excluded
                )
            )
            self.assertEqual(
                self.rust.membership_delta_changes(
                    self.bob, from_token, to_token, excluded, may_have_changed
                ),
                [
                    (
                        change.room_id,
                        change.event_id,
                        _position(change.event_pos),
                        change.membership,
                        change.sender,
                        change.prev_event_id,
                        _position(change.prev_event_pos),
                        change.prev_membership,
                        change.prev_sender,
                    )
                    for change in expected
                ],
                f"from {from_token} to {to_token} excluding {excluded}",
            )

    def test_membership_changes_without_a_change(self) -> None:
        """Without a change since the token, as the membership stream cache
        says, there is nothing to read."""
        token = self.store.get_room_max_token()
        self.helper.leave(self.room_id, self.bob, tok=self.bob_tok)
        later = self.store.get_room_max_token()
        self.assertEqual(
            self.rust.snapshot_membership_changes(self.bob, token, later, [], False),
            [],
        )
        self.assertEqual(
            len(
                self.rust.snapshot_membership_changes(self.bob, token, later, [], True)
            ),
            1,
        )
        self.assertEqual(
            self.rust.membership_delta_changes(self.bob, token, token, [], True), []
        )

    # Stream positions.

    def _rooms_with_events(self, rng: random.Random) -> list[str]:
        rooms = [self.room_id] + [
            self.helper.create_room_as(self.alice, tok=self.alice_tok) for _ in range(4)
        ]
        for _ in range(12):
            self.helper.send(rng.choice(rooms), "hi", tok=self.alice_tok)
        # A room the server has left.
        left = self.helper.create_room_as(self.bob, tok=self.bob_tok)
        self.helper.leave(left, self.bob, tok=self.bob_tok)
        return rooms + [left, "!unknown:test"]

    @parameterized.expand(SEEDS)
    def test_rooms_with_updates_since(self, seed: int) -> None:
        rng = random.Random(seed)
        rooms = self._rooms_with_events(rng)
        lo, hi = self._stream_range()
        for _ in range(8):
            from_token = RoomStreamToken(stream=rng.randint(lo - 2, hi + 2))
            subset = rng.sample(rooms, rng.randint(0, len(rooms)))

            # The table, with the stream cache out of reach. Python checks
            # the background updates without awaiting the check, so the
            # coroutine is always true. Only the finished path is ported.
            cache = self.store._events_stream_cache
            with (
                patch.object(
                    cache, "get_earliest_known_position", return_value=hi + 10
                ),
                patch.object(
                    self.store,
                    "have_finished_sliding_sync_background_jobs",
                    new=lambda: True,
                ),
            ):
                expected = self.get_success(
                    self.store.get_rooms_that_have_updates_since_sliding_sync_table(
                        subset, from_token
                    )
                )
            self.assertEqual(
                self.rust.rooms_with_updates_since(subset, from_token, None),
                set(expected),
                from_token,
            )

            # The stream cache's answer, as Python passes it.
            changed = cache.get_all_entities_changed(from_token.stream)
            if changed.hit:
                expected = self.get_success(
                    self.store.get_rooms_that_have_updates_since_sliding_sync_table(
                        subset, from_token
                    )
                )
                self.assertEqual(
                    self.rust.rooms_with_updates_since(
                        subset, from_token, set(changed.entities)
                    ),
                    set(expected),
                    from_token,
                )

    @parameterized.expand(SEEDS)
    def test_max_and_last_event_pos(self, seed: int) -> None:
        rng = random.Random(seed)
        rooms = self._rooms_with_events(rng)
        lo, hi = self._stream_range()
        cache = self.store._events_stream_cache
        for _ in range(8):
            # A "current" token behind some rooms' latest events, so both
            # sides recheck them.
            now_token = self._random_token(rng, (lo + hi) // 2, hi)
            end_token = self._random_token(rng, lo, hi)
            subset = rng.sample(rooms, rng.randint(0, len(rooms)))
            # Start each round cold, which the mirror carries over.
            self.store._get_max_event_pos.invalidate_all()
            with (
                patch.object(cache, "get_max_pos_of_last_change", return_value=None),
                patch.object(self.store, "get_room_max_token", return_value=now_token),
            ):
                expected_max = self.get_success(
                    self.store._bulk_get_max_event_pos(subset)
                )
                self.assertEqual(
                    self.rust.max_event_pos(subset, now_token),
                    dict(expected_max),
                    now_token,
                )
                self.store._get_max_event_pos.invalidate_all()
                expected_last = self.get_success(
                    self.store.bulk_get_last_event_pos_in_room_before_stream_ordering(
                        subset, end_token
                    )
                )
                self.assertEqual(
                    self.rust.last_event_pos_before(subset, end_token, now_token),
                    expected_last,
                    f"{end_token} now {now_token}",
                )

    def test_max_event_pos_invalidated_by_new_events(self) -> None:
        now = self.store.get_room_max_token()
        before = self.rust.max_event_pos([self.room_id], now)[self.room_id]
        self.helper.send(self.room_id, "hi", tok=self.alice_tok)
        now = self.store.get_room_max_token()
        after = self.rust.max_event_pos([self.room_id], now)[self.room_id]
        assert before is not None and after is not None
        self.assertGreater(after, before)
        self.assertEqual(after, now.stream)

    # Room metadata.

    def test_room_metadata(self) -> None:
        rooms = self._bob_memberships() + ["!unknown:test"]
        for room_id in rooms:
            self.store.get_room_type.invalidate((room_id,))
            room_type = self.get_success(self.store.get_room_type(room_id))
            self.assertEqual(
                self.rust.room_type(room_id),
                (False, None)
                if room_type is ROOM_UNKNOWN_SENTINEL
                else (True, room_type),
                room_id,
            )
            try:
                version: str | None = self.get_success(
                    self.store.get_room_version_id(room_id)
                )
            except AssertionError:
                version = None
            self.assertEqual(self.rust.room_version_id(room_id), version, room_id)
            self.assertEqual(
                self.rust.cached_member_counts(room_id),
                dict(self.get_success(self.store.get_member_counts(room_id))),
                room_id,
            )

        # A join invalidates the counts.
        self.helper.join(self.room_id, self.alice, tok=self.alice_tok)
        carol = self.register_user("carol", "pass")
        self.helper.join(self.room_id, carol, tok=self.login("carol", "pass"))
        self.assertEqual(
            self.rust.cached_member_counts(self.room_id),
            dict(self.get_success(self.store.get_member_counts(self.room_id))),
        )

    def test_partial_rooms(self) -> None:
        self.assertEqual(
            self.rust.partial_rooms(),
            set(self.get_success(self.store.get_partial_rooms())),
        )
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
        # Behind the cache's back. Python invalidates the whole cache when
        # it changes the table, and the mirror with it.
        self.store.get_partial_rooms.invalidate_all()
        self.assertEqual(self.rust.partial_rooms(), {other})
        self.assertEqual(
            self.rust.partial_rooms(),
            set(self.get_success(self.store.get_partial_rooms())),
        )

    def test_tags_and_account_data(self) -> None:
        other = self.helper.create_room_as(self.bob, tok=self.bob_tok)
        self.assertEqual(
            self.rust.tags_for_user(self.bob),
            self.get_success(self.store.get_tags_for_user(self.bob)),
        )
        self.get_success(
            self.store.add_tag_to_room(self.bob, self.room_id, "m.favourite", {})
        )
        self.get_success(
            self.store.add_tag_to_room(
                self.bob, other, "u.work", {"order": 0.5, "é": "ü"}
            )
        )
        self.get_success(self.store.add_tag_to_room(self.bob, other, "u.home", {}))
        expected = self.get_success(self.store.get_tags_for_user(self.bob))
        self.assertEqual(len(expected), 2)
        self.assertEqual(self.rust.tags_for_user(self.bob), expected)
        self.get_success(self.store.remove_tag_from_room(self.bob, other, "u.home"))
        self.assertEqual(
            self.rust.tags_for_user(self.bob),
            self.get_success(self.store.get_tags_for_user(self.bob)),
        )

    @parameterized.expand(SEEDS[:5])
    def test_is_encrypted_at(self, seed: int) -> None:
        rng = random.Random(seed)
        room_lists = self.hs.get_sliding_sync_handler().room_lists
        room_id = self.helper.create_room_as(self.alice, tok=self.alice_tok)
        for _ in range(4):
            self.helper.send(room_id, "hi", tok=self.alice_tok)
            if rng.random() < 0.5:
                self.helper.send_state(
                    room_id,
                    EventTypes.RoomEncryption,
                    rng.choice(
                        [
                            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1"},
                            {EventContentFields.ENCRYPTION_ALGORITHM: None},
                            {},
                        ]
                    ),
                    tok=self.alice_tok,
                )
        lo, hi = self._stream_range()
        now_token = self.store.get_room_max_token()
        for stream in range(lo - 1, hi + 1):
            to_token = RoomStreamToken(stream=stream)
            self.assertEqual(
                self.rust.is_encrypted_at(room_id, to_token, now_token),
                self.get_success(
                    room_lists.get_is_encrypted_for_room_at_token(room_id, to_token)
                ),
                stream,
            )
