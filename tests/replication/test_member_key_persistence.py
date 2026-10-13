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
"""MSC4430 membership changes persisted on the main process reach a worker's
caches keyed by user ID, although the room's events name member keys."""

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.test_utils.member_key_rooms import (
    Member,
    MemberKeyRoom,
    register_member_key_room_version,
)


class MemberKeyPersistenceWorkerTestCase(BaseMultiWorkerStreamTestCase):
    servlets = [admin.register_servlets]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4430_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        register_member_key_room_version()
        self.store = hs.get_datastores().main
        self.alice = self.register_user("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.alice_member = Member()
        self.room = MemberKeyRoom(hs, self.alice_member)
        self.get_success(self.room.create())
        self.get_success(
            self.store.create_local_member_key(
                self.room.room_id, self.alice, self.alice_member.signing_key
            )
        )

    def test_worker_sees_join_and_leave_by_user_id(self) -> None:
        worker_store = (
            self.make_worker_hs("synapse.app.generic_worker").get_datastores().main
        )
        room_id = self.room.room_id

        # The worker caches Alice as being in no rooms, and nothing having changed.
        self.assertEqual(
            self.get_success(worker_store.get_rooms_for_user(self.alice)), frozenset()
        )
        before = worker_store.get_room_max_token()
        self.assertFalse(
            worker_store._membership_stream_cache.has_entity_changed(
                self.alice, before.stream
            )
        )

        # Alice joins on the main process, under her member key.
        join = self.get_success(self.room.send_member(self.alice_member, "join"))
        self.replicate()

        # The worker's user-keyed caches were invalidated over replication, and
        # the change recorded at the join's position (not at some position of
        # the caches stream, which the membership stream cache would ignore or
        # misplace).
        self.assertEqual(
            self.get_success(worker_store.get_rooms_for_user(self.alice)),
            frozenset({room_id}),
        )
        self.assertTrue(
            worker_store._membership_stream_cache.has_entity_changed(
                self.alice, before.stream
            )
        )
        self.assertEqual(
            worker_store._membership_stream_cache._entity_to_key[self.alice],
            join.internal_metadata.stream_ordering,
        )
        after = worker_store.get_room_max_token()
        changes = self.get_success(
            worker_store.get_membership_changes_for_user(self.alice, before, after)
        )
        self.assertEqual([e.event_id for e in changes], [join.event_id])
        self.assertEqual(
            self.get_success(worker_store.get_users_in_room(room_id)),
            [self.alice_member.key],
        )

        # An out-of-band invite for Bob (an outlier, never in the current state)
        # reaches the worker's invite lookup too.
        bob_member = Member()
        self.get_success(
            self.store.create_local_member_key(
                room_id, self.bob, bob_member.signing_key
            )
        )
        self.assertEqual(
            self.get_success(worker_store.get_invited_rooms_for_local_user(self.bob)),
            [],
        )
        invite = self.get_success(
            self.room.send_out_of_band_member(bob_member, "invite", sender=Member())
        )
        self.replicate()
        self.assertEqual(
            [
                r.event_id
                for r in self.get_success(
                    worker_store.get_invited_rooms_for_local_user(self.bob)
                )
            ],
            [invite.event_id],
        )

        # And a leave.
        self.get_success(self.room.send_member(self.alice_member, "leave"))
        self.replicate()
        self.assertEqual(
            self.get_success(worker_store.get_rooms_for_user(self.alice)), frozenset()
        )
        self.assertEqual(self.get_success(worker_store.get_users_in_room(room_id)), [])
