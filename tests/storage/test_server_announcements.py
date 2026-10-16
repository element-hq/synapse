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

from synapse.api.errors import StoreError
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
        self.announcement_id = "news"
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
        self.get_success(
            self.store.store_server_announcement_room(
                self.announcement_id, self.room_id
            )
        )

    def test_mapping_roundtrip(self) -> None:
        self.assertIsNone(
            self.get_success(
                self.store.get_server_announcement_room(self.announcement_id)
            )
        )
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_id(self.room_id))
        )

        self._store_room()

        self.assertEqual(
            self.get_success(
                self.store.get_server_announcement_room(self.announcement_id)
            ),
            self.room_id,
        )
        self.assertEqual(
            self.get_success(self.store.get_server_announcement_id(self.room_id)),
            self.announcement_id,
        )
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_room("unknown"))
        )
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_id("!unknown:test"))
        )

    def test_announcement_id_is_unique(self) -> None:
        self._store_room()
        self.get_failure(
            self.store.store_server_announcement_room(
                self.announcement_id, "!other:test"
            ),
            self.store.database_engine.module.IntegrityError,
        )
        self.assertEqual(
            self.get_success(
                self.store.get_server_announcement_room(self.announcement_id)
            ),
            self.room_id,
        )
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_id("!other:test"))
        )

    def test_room_id_is_unique(self) -> None:
        self._store_room()
        self.get_failure(
            self.store.store_server_announcement_room("other", self.room_id),
            self.store.database_engine.module.IntegrityError,
        )
        self.assertEqual(
            self.get_success(self.store.get_server_announcement_id(self.room_id)),
            self.announcement_id,
        )
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_room("other"))
        )

    def test_mapping_misses_are_not_cached(self) -> None:
        reader = self._new_store()
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_room(self.announcement_id))
        )
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_id(self.room_id))
        )

        self._store_room()

        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.announcement_id)),
            self.room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(self.room_id)),
            self.announcement_id,
        )

    def test_mapping_and_avatar_persist_across_store_instances(self) -> None:
        self._store_room()
        self.get_success(
            self.store.store_server_announcement_avatar(
                self.announcement_id, *self.avatar
            )
        )

        # No announcement configuration is needed to recover either the mapping or
        # the uploaded avatar: removing configuration must not hide stored data.
        reader = self._new_store()
        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.announcement_id)),
            self.room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(self.room_id)),
            self.announcement_id,
        )
        self.assertEqual(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            ),
            self.avatar,
        )

    def test_avatar_roundtrip_and_update(self) -> None:
        reader = self._new_store()
        self.assertIsNone(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            )
        )
        self._store_room()
        self.assertIsNone(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            )
        )

        for avatar in (
            self.avatar,
            ("/avatars/updated.png", "b" * 64, "mxc://test/updated"),
        ):
            self.get_success(
                self.store.store_server_announcement_avatar(
                    self.announcement_id, *avatar
                )
            )
            self.assertEqual(
                self.get_success(
                    reader.get_server_announcement_avatar(self.announcement_id)
                ),
                avatar,
            )

        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.announcement_id)),
            self.room_id,
        )

    @parameterized.expand([("avatar_path",), ("avatar_sha256",), ("avatar_mxc",)])
    def test_incomplete_avatar_is_not_returned(self, missing_column: str) -> None:
        self._store_room()
        self.get_success(
            self.store.store_server_announcement_avatar(
                self.announcement_id, *self.avatar
            )
        )
        self.get_success(
            self.store.db_pool.simple_update_one(
                table="server_announcement_rooms",
                keyvalues={"id": self.announcement_id},
                updatevalues={missing_column: None},
            )
        )
        self.assertIsNone(
            self.get_success(
                self.store.get_server_announcement_avatar(self.announcement_id)
            )
        )

    def test_avatar_requires_mapping(self) -> None:
        failure = self.get_failure(
            self.store.store_server_announcement_avatar(
                self.announcement_id, *self.avatar
            ),
            StoreError,
        )
        self.assertEqual(failure.value.code, 404)

    @parameterized.expand([("standard", False), ("force", True)])
    def test_room_purge_deletes_mapping_and_avatar(
        self, _name: str, force: bool
    ) -> None:
        self.room_id = self.helper.create_room_as(self.user_id)
        self._store_room()
        self.get_success(
            self.store.store_server_announcement_avatar(
                self.announcement_id, *self.avatar
            )
        )

        # Read before purging to check that another store sees deletions too.
        reader = self._new_store()
        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.announcement_id)),
            self.room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(self.room_id)),
            self.announcement_id,
        )
        self.assertEqual(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            ),
            self.avatar,
        )

        # A standard purge requires all local users to leave; a forced purge
        # must also remove the mapping while the user is still joined.
        if not force:
            self.helper.leave(self.room_id, user=self.user_id)
        self.get_success(
            self.hs.get_pagination_handler().purge_room(self.room_id, force=force)
        )

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
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_room(self.announcement_id))
        )
        self.assertIsNone(
            self.get_success(reader.get_server_announcement_id(self.room_id))
        )
        self.assertIsNone(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            )
        )

        replacement_room_id = self.helper.create_room_as(self.user_id)
        self.get_success(
            self.store.store_server_announcement_room(
                self.announcement_id, replacement_room_id
            )
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_room(self.announcement_id)),
            replacement_room_id,
        )
        self.assertEqual(
            self.get_success(reader.get_server_announcement_id(replacement_room_id)),
            self.announcement_id,
        )
        self.assertIsNone(
            self.get_success(
                reader.get_server_announcement_avatar(self.announcement_id)
            )
        )
