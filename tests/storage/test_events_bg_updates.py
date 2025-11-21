#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#


from twisted.internet.testing import MemoryReactor

from synapse.api.constants import MAX_DEPTH
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class TestFixupMaxDepthCapBgUpdate(HomeserverTestCase):
    """Test the background update that caps topological_ordering at MAX_DEPTH."""

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = self.hs.get_datastores().main
        self.db_pool = self.store.db_pool

        self.room_id = "!testroom:example.com"

        # Reinsert the background update as it was already run at the start of
        # the test.
        self.get_success(
            self.db_pool.simple_insert(
                table="background_updates",
                values={
                    "update_name": "fixup_max_depth_cap",
                    "progress_json": "{}",
                },
            )
        )

    def create_room(self, room_version: RoomVersion) -> dict[str, int]:
        """Create a room with a known room version and insert events.

        Returns the set of event IDs that exceed MAX_DEPTH and
        their depth.
        """

        # Create a room with a specific room version
        self.get_success(
            self.db_pool.simple_insert(
                table="rooms",
                values={
                    "room_id": self.room_id,
                    "room_version": room_version.identifier,
                },
            )
        )

        # Insert events with some depths exceeding MAX_DEPTH
        event_id_to_depth: dict[str, int] = {}
        for depth in range(MAX_DEPTH - 5, MAX_DEPTH + 5):
            event_id = f"$event{depth}:example.com"
            event_id_to_depth[event_id] = depth

            self.get_success(
                self.db_pool.simple_insert(
                    table="events",
                    values={
                        "event_id": event_id,
                        "room_id": self.room_id,
                        "topological_ordering": depth,
                        "depth": depth,
                        "type": "m.test",
                        "sender": "@user:test",
                        "processed": True,
                        "outlier": False,
                    },
                )
            )

        return event_id_to_depth

    def test_fixup_max_depth_cap_bg_update(self) -> None:
        """Test that the background update correctly caps topological_ordering
        at MAX_DEPTH."""

        event_id_to_depth = self.create_room(RoomVersions.V6)

        # Run the background update
        progress = {"room_id": ""}
        batch_size = 10
        num_rooms = self.get_success(
            self.store.fixup_max_depth_cap_bg_update(progress, batch_size)
        )

        # Verify the number of rooms processed
        self.assertEqual(num_rooms, 1)

        # Verify that the topological_ordering of events has been capped at
        # MAX_DEPTH
        rows = self.get_success(
            self.db_pool.simple_select_list(
                table="events",
                keyvalues={"room_id": self.room_id},
                retcols=["event_id", "topological_ordering"],
            )
        )

        for event_id, topological_ordering in rows:
            if event_id_to_depth[event_id] >= MAX_DEPTH:
                # Events with a depth greater than or equal to MAX_DEPTH should
                # be capped at MAX_DEPTH.
                self.assertEqual(topological_ordering, MAX_DEPTH)
            else:
                # Events with a depth less than MAX_DEPTH should remain
                # unchanged.
                self.assertEqual(topological_ordering, event_id_to_depth[event_id])

    def test_fixup_max_depth_cap_bg_update_old_room_version(self) -> None:
        """Test that the background update does not cap topological_ordering for
        rooms with old room versions."""

        event_id_to_depth = self.create_room(RoomVersions.V5)

        # Run the background update
        progress = {"room_id": ""}
        batch_size = 10
        num_rooms = self.get_success(
            self.store.fixup_max_depth_cap_bg_update(progress, batch_size)
        )

        # Verify the number of rooms processed
        self.assertEqual(num_rooms, 0)

        # Verify that the topological_ordering of events has been capped at
        # MAX_DEPTH
        rows = self.get_success(
            self.db_pool.simple_select_list(
                table="events",
                keyvalues={"room_id": self.room_id},
                retcols=["event_id", "topological_ordering"],
            )
        )

        # Assert that the topological_ordering of events has not been changed
        # from their depth.
        self.assertDictEqual(event_id_to_depth, dict(rows))
