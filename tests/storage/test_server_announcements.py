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

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.rest.client import room
from synapse.server import HomeServer
from synapse.storage.databases.main.server_announcements import ServerAnnouncementsStore
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class ServerAnnouncementsStoreTestCase(HomeserverTestCase):
    user_id = "@announcer:test"
    servlets = [room.register_servlets]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.config_id = "news"
        self.room_id = "!announcements:test"
        self.avatar = ("/avatars/news.png", "a" * 64, "mxc://test/avatar")

    def _new_store(self) -> ServerAnnouncementsStore:
        """Create a fresh store over the same database, without announcement config."""
        return self.get_success(
            self.store.db_pool.runWithConnection(
                lambda conn: ServerAnnouncementsStore(self.store.db_pool, conn, self.hs)
            )
        )

    def _store_room(self) -> None:
        """Create a server announcement room mapping."""
        self.get_success(
            self.store.store_server_announcement_room(self.config_id, self.room_id)
        )
        self.get_success(
            self.store.store_server_announcement_avatar(self.config_id, *self.avatar)
        )

    @parameterized.expand([("standard", False), ("force", True)])
    def test_room_purge_deletes_mapping_and_avatar(
        self, _name: str, force: bool
    ) -> None:
        # Set up an announcement room.
        self.room_id = self.helper.create_room_as(self.user_id)
        self._store_room()

        # Read before purging to check that another store sees deletions too.
        reader = self._new_store()
        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.config_id)),
            self.room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(self.room_id)),
            self.config_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_avatar(self.config_id)),
            self.avatar,
        )

        # A standard purge requires all local users to leave; a forced purge
        # must also remove the mapping while the user is still joined.
        if not force:
            self.helper.leave(self.room_id, user=self.user_id)

        self.get_success(
            self.hs.get_pagination_handler().purge_room(self.room_id, force=force)
        )

        # Check that the room no longer exists.
        self.assertIsNone(
            self.get_success(
                self.store.db_pool.simple_select_one_onecol(
                    table="rooms",
                    keyvalues={"room_id": self.room_id},
                    retcol="room_id",
                    allow_none=True,
                )
            )
        )

        # Check that the announcement room exists.
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_room(self.config_id))
        )
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_id(self.room_id))
        )
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_avatar(self.config_id))
        )

        # Check that the same announcement room `id` can then be re-used.
        replacement_room_id = self.helper.create_room_as(self.user_id)
        self.get_success(
            self.store.store_server_announcement_room(
                self.config_id, replacement_room_id
            )
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.config_id)),
            replacement_room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(replacement_room_id)),
            self.config_id,
        )
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_avatar(self.config_id))
        )
