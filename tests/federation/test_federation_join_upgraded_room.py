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

import logging
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomAlias
from synapse.util.clock import Clock

from tests import unittest
from tests.federation._remote_join import RemoteJoinHelper

logger = logging.getLogger(__name__)


def _predecessor(room_id: str) -> JsonDict:
    """`create_content` for a remote room that claims `room_id` as its predecessor."""
    return {
        "predecessor": {
            "room_id": room_id,
            # inert dummy
            "event_id": "$some_tombstone_event:test",
        }
    }


class FederationJoinUpgradedRoomTestCase(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self._federation_http_client = Mock(
            # The problem with using `spec=MatrixFederationHttpClient` here is that it
            # requires everything to be mocked which is a lot of work that I don't want
            # to do when the code only uses a few methods (`get_json` and `put_json`).
        )
        return self.setup_test_homeserver(
            federation_http_client=self._federation_http_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

    def _room_is_public(self, room_id: str) -> bool:
        """`is_public` flag from the `rooms` table (asserts the row exists)."""
        room = self.get_success(self.store.get_room(room_id))
        assert room is not None, f"no rooms row for {room_id}"
        is_public, _ = room
        return is_public

    def test_alias_transferred_on_federation_join_to_upgraded_room(self) -> None:
        """
        Tests that joining an upgraded room over federation,
        where the predecessor has a valid corresponding tombstone,
        should transfer all room aliases from the old room to the new room.

        The old room is set up with a couple of local aliases and a tombstone
        event pointing at the new (remote) room.
        After the federation join, all room aliases should have been
        transferred from the old room to the new room.
        """
        local_user_id = self.register_user("user1", "pass")
        local_user_tok = self.login(local_user_id, "pass")

        # Set up an old room and point 2 room aliases at it
        old_room_id = self.helper.create_room_as(
            room_creator=local_user_id,
            tok=local_user_tok,
        )

        alias1 = RoomAlias.from_string("#old_room:test")
        alias2 = RoomAlias.from_string("#old_room_alt:test")
        for alias in (alias1, alias2):
            self.get_success(
                self.store.create_room_alias_association(
                    alias, old_room_id, [self.hs.hostname]
                )
            )

        # Now set up a replacement room, which we will remote-room-join in a moment
        join_helper = RemoteJoinHelper(
            self,
            self._federation_http_client,
            create_content=_predecessor(old_room_id),
        )

        # Place a tombstone in the old room, essentially authorising
        # the replacement room to replace it
        self.helper.send_state(
            old_room_id,
            EventTypes.Tombstone,
            {"replacement_room": join_helper.room_id},
            tok=local_user_tok,
        )

        # Trigger the remote room join
        join_helper.join(local_user_id, local_user_tok)

        # The new room should have acquired the aliases...
        new_aliases = self.get_success(
            self.store.get_aliases_for_room(join_helper.room_id)
        )
        self.assertCountEqual(new_aliases, [alias1.to_string(), alias2.to_string()])

        # ...and the old room therefore must have given them up
        old_aliases = self.get_success(self.store.get_aliases_for_room(old_room_id))
        self.assertEqual(old_aliases, [])

    def test_room_directory_visibility_transferred(self) -> None:
        """
        On a valid federation join of an upgraded room, the room directory public
        flag should move from the old room to the new room.

        A public old room becomes private (so people don't accidentally join it)
        and the newly-joined room is marked public.
        """
        local_user_id = self.register_user("user1", "pass")
        local_user_tok = self.login(local_user_id, "pass")

        # Set up an old room and mark it as public (for room directory purposes)
        old_room_id = self.helper.create_room_as(
            room_creator=local_user_id,
            tok=local_user_tok,
        )
        self.get_success(self.store.set_room_is_public(old_room_id, True))
        self.assertTrue(self._room_is_public(old_room_id))

        # Now set up a replacement room, which we will remote-room-join in a moment
        join_helper = RemoteJoinHelper(
            self,
            self._federation_http_client,
            create_content=_predecessor(old_room_id),
        )

        # Place a tombstone in the old room, essentially authorising
        # the replacement room to replace it
        self.helper.send_state(
            old_room_id,
            EventTypes.Tombstone,
            {"replacement_room": join_helper.room_id},
            tok=local_user_tok,
        )

        # Trigger the remote room join
        join_helper.join(local_user_id, local_user_tok)

        # The room directory publicity should have shifted to the new room
        # (So users don't accidentally join the old room from the directory)
        self.assertFalse(self._room_is_public(old_room_id))
        self.assertTrue(self._room_is_public(join_helper.room_id))

    def test_room_directory_visibility_not_transferred_for_private_room(self) -> None:
        """
        On a valid federation join of an upgraded room, a private old room should
        leave the room directory visibility untouched for both the old and new
        rooms (both remain private).
        """
        local_user_id = self.register_user("user1", "pass")
        local_user_tok = self.login(local_user_id, "pass")

        # Set up an old room and mark it as public (for room directory purposes)
        old_room_id = self.helper.create_room_as(
            room_creator=local_user_id,
            tok=local_user_tok,
        )
        self.assertFalse(self._room_is_public(old_room_id))

        # Now set up a replacement room, which we will remote-room-join in a moment
        join_helper = RemoteJoinHelper(
            self,
            self._federation_http_client,
            create_content=_predecessor(old_room_id),
        )

        # Place a tombstone in the old room, essentially authorising
        # the replacement room to replace it
        self.helper.send_state(
            old_room_id,
            EventTypes.Tombstone,
            {"replacement_room": join_helper.room_id},
            tok=local_user_tok,
        )

        # Trigger the remote room join
        join_helper.join(local_user_id, local_user_tok)

        self.assertFalse(self._room_is_public(old_room_id))
        self.assertFalse(self._room_is_public(join_helper.room_id))

    def test_no_transfer_when_predecessor_room_has_no_tombstone(self) -> None:
        """
        Tests that when joining a remote room over federation,
        if the room has an illegitimate predecessor (a predecessor pointing
        to a room that does not have a corresponding tombstone to vouch for it
        as the successor), room aliases are not transferred.
        """
        local_user_id = self.register_user("user1", "pass")
        local_user_tok = self.login(local_user_id, "pass")

        # Set up a room with an alias
        old_room_id = self.helper.create_room_as(
            room_creator=local_user_id,
            tok=local_user_tok,
        )

        alias = RoomAlias.from_string("#old_room:test")
        self.get_success(
            self.store.create_room_alias_association(
                alias, old_room_id, [self.hs.hostname]
            )
        )

        join_helper = RemoteJoinHelper(
            self,
            self._federation_http_client,
            # The new room (illegitimately) claims to be the successor
            # of the old room.
            create_content=_predecessor(old_room_id),
        )

        # Notably, we do NOT set up a tombstone in the 'old' room.

        # Do the remote room join dance
        join_helper.join(local_user_id, local_user_tok)

        # Check that the room alias did _not_ get transferred...
        new_aliases = self.get_success(
            self.store.get_aliases_for_room(join_helper.room_id)
        )
        self.assertCountEqual(new_aliases, [])

        # ...and that the old room still has it
        old_aliases = self.get_success(self.store.get_aliases_for_room(old_room_id))
        self.assertCountEqual(old_aliases, [alias.to_string()])

    def test_no_transfer_when_tombstone_does_not_match(self) -> None:
        """
        A predecessor room whose tombstone points to a different room than the
        one being joined must not trigger an alias transfer.

        The tombstone's `replacement_room` must match the joined room for the
        upgrade link to be considered valid.
        """
        local_user_id = self.register_user("user1", "pass")
        local_user_tok = self.login(local_user_id, "pass")

        # Set up a room with an alias
        old_room_id = self.helper.create_room_as(
            room_creator=local_user_id,
            tok=local_user_tok,
        )

        alias = RoomAlias.from_string("#old_room:test")
        self.get_success(
            self.store.create_room_alias_association(
                alias, old_room_id, [self.hs.hostname]
            )
        )

        # Tombstone points at a _different_, room.
        self.helper.send_state(
            old_room_id,
            EventTypes.Tombstone,
            {"replacement_room": "!the_real_replacement_room:example.com"},
            tok=local_user_tok,
        )

        join_helper = RemoteJoinHelper(
            self,
            self._federation_http_client,
            # The new room (illegitimately) claims to be the successor
            # of the old room.
            create_content=_predecessor(old_room_id),
        )
        join_helper.join(local_user_id, local_user_tok)

        # Check that the room alias did _not_ get transferred...
        new_aliases = self.get_success(
            self.store.get_aliases_for_room(join_helper.room_id)
        )
        self.assertCountEqual(new_aliases, [])

        # ...and that the old room still has it
        old_aliases = self.get_success(self.store.get_aliases_for_room(old_room_id))
        self.assertCountEqual(old_aliases, [alias.to_string()])
