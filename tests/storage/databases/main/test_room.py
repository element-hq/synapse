#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

import json

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import RoomTypes
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.databases.main.room import _BackgroundUpdates
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class RoomBackgroundUpdateStoreTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = self.register_user("foo", "pass")
        self.token = self.login("foo", "pass")

    def _generate_room(self) -> str:
        """Create a room and return the room ID."""
        return self.helper.create_room_as(self.user_id, tok=self.token)

    def run_background_updates(self, update_name: str) -> None:
        """Insert and run the background update."""
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {"update_name": update_name, "progress_json": "{}"},
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.store.db_pool.updates._all_done = False

        # Now let's actually drive the updates to completion
        self.wait_for_background_updates()

    def test_background_populate_rooms_creator_column(self) -> None:
        """Test that the background update to populate the rooms creator column
        works properly.
        """

        # Insert a room without the creator
        room_id = self._generate_room()
        self.get_success(
            self.store.db_pool.simple_update(
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"creator": None},
                desc="test",
            )
        )

        # Make sure the test is starting out with a room without a creator
        room_creator_before = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="rooms",
                keyvalues={"room_id": room_id},
                retcol="creator",
                allow_none=True,
            )
        )
        self.assertEqual(room_creator_before, None)

        self.run_background_updates(_BackgroundUpdates.POPULATE_ROOMS_CREATOR_COLUMN)

        # Make sure the background update filled in the room creator
        room_creator_after = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="rooms",
                keyvalues={"room_id": room_id},
                retcol="creator",
                allow_none=True,
            )
        )
        self.assertEqual(room_creator_after, self.user_id)

    def test_background_add_room_type_column(self) -> None:
        """Test that the background update to populate the `room_type` column in
        `room_stats_state` works properly.
        """

        # Create a room without a type
        room_id = self._generate_room()

        # Get event_id of the m.room.create event
        event_id = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="current_state_events",
                keyvalues={
                    "room_id": room_id,
                    "type": "m.room.create",
                },
                retcol="event_id",
            )
        )

        # Fake a room creation event with a room type
        event = {
            "content": {
                "creator": "@user:server.org",
                "room_version": "9",
                "type": RoomTypes.SPACE,
            },
            "type": "m.room.create",
        }
        self.get_success(
            self.store.db_pool.simple_update(
                table="event_json",
                keyvalues={"event_id": event_id},
                updatevalues={"json": json.dumps(event)},
                desc="test",
            )
        )

        self.run_background_updates(_BackgroundUpdates.ADD_ROOM_TYPE_COLUMN)

        # Make sure the background update filled in the room type
        room_type_after = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="room_stats_state",
                keyvalues={"room_id": room_id},
                retcol="room_type",
                allow_none=True,
            )
        )
        self.assertEqual(room_type_after, RoomTypes.SPACE)

    def test_populate_stats_broken_rooms(self) -> None:
        """Ensure that re-populating room stats skips broken rooms."""

        # Create a good room.
        good_room_id = self._generate_room()

        # Create a room and then break it by having no room version.
        room_id = self._generate_room()
        self.get_success(
            self.store.db_pool.simple_update(
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"room_version": None},
                desc="test",
            )
        )

        # Nuke any current stats in the database.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="room_stats_state", keyvalues={"1": 1}, desc="test"
            )
        )

        self.run_background_updates("populate_stats_process_rooms")

        # Only the good room appears in the stats tables.
        results = self.get_success(
            self.store.db_pool.simple_select_onecol(
                table="room_stats_state",
                keyvalues={},
                retcol="room_id",
            )
        )
        self.assertEqual(results, [good_room_id])
