#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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

from twisted.internet.testing import MemoryReactor

from synapse.api.room_versions import RoomVersions
from synapse.server import HomeServer
from synapse.storage.databases.main.room import _BackgroundUpdates
from synapse.storage.types import Cursor
from synapse.types import RoomAlias, RoomID, UserID
from synapse.util.clock import Clock

from tests.rest.admin.test_media import _AdminMediaTests
from tests.unittest import HomeserverTestCase


class RoomStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # We can't test RoomStore on its own without the DirectoryStore, for
        # management of the 'room_aliases' table
        self.store = hs.get_datastores().main

        self.room = RoomID.from_string("!abcde:test")
        self.alias = RoomAlias.from_string("#a-room-name:test")
        self.u_creator = UserID.from_string("@creator:test")

        self.get_success(
            self.store.store_room(
                self.room.to_string(),
                room_creator_user_id=self.u_creator.to_string(),
                is_public=True,
                room_version=RoomVersions.V1,
            )
        )

    def test_get_room(self) -> None:
        room = self.get_success(self.store.get_room(self.room.to_string()))
        assert room is not None
        self.assertTrue(room[0])

    def test_get_room_unknown_room(self) -> None:
        self.assertIsNone(self.get_success(self.store.get_room("!uknown:test")))

    def test_get_room_with_stats(self) -> None:
        res = self.get_success(self.store.get_room_with_stats(self.room.to_string()))
        assert res is not None
        self.assertEqual(res.room_id, self.room.to_string())
        self.assertEqual(res.creator, self.u_creator.to_string())
        self.assertTrue(res.public)

    def test_get_room_with_stats_unknown_room(self) -> None:
        self.assertIsNone(
            self.get_success(self.store.get_room_with_stats("!uknown:test"))
        )


class FlagExistingQuarantinedMediaBackgroundUpdatesTestCase(_AdminMediaTests):
    """
    Test the `flag_existing_quarantined_media` background update.
    """

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_populates_quarantined_only(self) -> None:
        self.register_user("admin", "pass", admin=True)
        admin_user_tok = self.login("admin", "pass")

        # Upload two distinct media items so we can quarantine one. If they shared content,
        # then the quarantine-by-hash code would hit both.
        _unaffected_media_upload_response = self.helper.upload_media(
            b"first content", tok=admin_user_tok, expect_code=200
        )
        # Upload the media we're going to quarantine
        media_upload_response = self.helper.upload_media(
            b"second content", tok=admin_user_tok, expect_code=200
        )
        # Extract media ID from the response
        quarantined_media_origin_and_media_id = media_upload_response["content_uri"][
            6:
        ]  # cut off 'mxc://'
        quarantined_media_origin, quarantined_media_id = (
            quarantined_media_origin_and_media_id.split("/")
        )

        # Ideally we'd also upload remote media to ensure that `remote_media_cache` gets picked up, but that's
        # a little tricky to set up in the test here. We hope that local and remote media
        # are treated similarly during the background update.

        # Quarantine the media like an admin would. Because the quarantine API also inserts
        # a record into the database for us, we'll clear out the `quarantined_media_changes`
        # table before running the background update. This will simulate already-quarantined
        # media being in the database prior to the background update.
        channel = self.make_request(
            "POST",
            "/_synapse/admin/v1/media/quarantine/%s/%s"
            % (
                quarantined_media_origin,
                quarantined_media_id,
            ),
            access_token=admin_user_tok,
        )
        self.assertEqual(200, channel.code, msg=channel.json_body)

        # Do that table clear we mentioned above
        def _wipe_table(txn: Cursor) -> None:
            txn.execute("DELETE FROM quarantined_media_changes")

        self.get_success(
            self.store.db_pool.runInteraction(
                "test_populates_quarantined_only._wipe_table", _wipe_table
            )
        )

        # Insert and run the background update
        self.get_success(
            self.store.db_pool.simple_insert(
                table="background_updates",
                values={
                    "update_name": _BackgroundUpdates.FLAG_EXISTING_QUARANTINED_MEDIA,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Check that the changes table is now populated, and has exactly 1 quarantined
        # media object in it (the one we quarantined).
        changes: list[tuple[str | None, str, bool]] = self.get_success(
            self.store.db_pool.simple_select_list(
                "quarantined_media_changes",
                None,
                retcols=("origin", "media_id", "quarantined"),
            )
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], None)  # origin (local media)
        self.assertEqual(changes[0][1], quarantined_media_id)  # media_id
        self.assertEqual(changes[0][2], True)  # quarantined flag
