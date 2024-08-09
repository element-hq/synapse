#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


class SlidingSyncConnectionTrackingTestCase(SlidingSyncBase):
    """
    Test connection tracking in the Sliding Sync API.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

    def test_rooms_required_state_incremental_sync_LIVE(self) -> None:
        """Test that we only get state updates in incremental sync for rooms
        we've already seen (LIVE).
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 0,
                }
            }
        }

        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )

        # Send a state event
        self.helper.send_state(
            room_id1, EventTypes.Name, body={"name": "foo"}, tok=user2_tok
        )

        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self.assertNotIn("initial", response_body["rooms"][room_id1])
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

    @parameterized.expand([(False,), (True,)])
    def test_rooms_timeline_incremental_sync_PREVIOUSLY(self, limited: bool) -> None:
        """
        Test getting room data where we have previously sent down the room, but
        we missed sending down some timeline events previously and so its status
        is considered PREVIOUSLY.

        There are two versions of this test, one where there are more messages
        than the timeline limit, and one where there isn't.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        timeline_limit = 5
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            },
            "conn_id": "conn_id",
        }

        # The first room gets sent down the initial sync
        response_body, initial_from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        # We now send down some events in room1 (depending on the test param).
        expected_events = []  # The set of events in the timeline
        if limited:
            for _ in range(10):
                resp = self.helper.send(room_id1, "msg1", tok=user1_tok)
                expected_events.append(resp["event_id"])
        else:
            resp = self.helper.send(room_id1, "msg1", tok=user1_tok)
            expected_events.append(resp["event_id"])

        # A second messages happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(
            sync_body, since=initial_from_token, tok=user1_tok
        )

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1, so we should sync all the missing events.
        resp = self.helper.send(room_id1, "msg2", tok=user1_tok)
        expected_events.append(resp["event_id"])

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )
        self.assertNotIn("initial", response_body["rooms"][room_id1])

        self.assertEqual(
            [ev["event_id"] for ev in response_body["rooms"][room_id1]["timeline"]],
            expected_events[-timeline_limit:],
        )
        self.assertEqual(response_body["rooms"][room_id1]["limited"], limited)
        self.assertEqual(response_body["rooms"][room_id1].get("required_state"), None)

    def test_rooms_required_state_incremental_sync_PREVIOUSLY(self) -> None:
        """
        Test getting room data where we have previously sent down the room, but
        we missed sending down some state previously and so its status is
        considered PREVIOUSLY.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 0,
                }
            },
            "conn_id": "conn_id",
        }

        # The first room gets sent down the initial sync
        response_body, initial_from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        # We now send down some state in room1
        resp = self.helper.send_state(
            room_id1, EventTypes.Name, {"name": "foo"}, tok=user1_tok
        )
        name_change_id = resp["event_id"]

        # A second messages happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(
            sync_body, since=initial_from_token, tok=user1_tok
        )

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1, so we should sync all the missing state.
        self.helper.send(room_id1, "msg", tok=user1_tok)

        # This sync should contain the state changes from room1.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )
        self.assertNotIn("initial", response_body["rooms"][room_id1])

        # We should only see the name change.
        self.assertEqual(
            [
                ev["event_id"]
                for ev in response_body["rooms"][room_id1]["required_state"]
            ],
            [name_change_id],
        )

    def test_rooms_required_state_incremental_sync_NEVER(self) -> None:
        """
        Test getting `required_state` where we have NEVER sent down the room before
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        self.helper.send(room_id1, "msg", tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        [EventTypes.Create, ""],
                        [EventTypes.RoomHistoryVisibility, ""],
                        # This one doesn't exist in the room
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }

        # A message happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1, so we should send down the full
        # room.
        self.helper.send(room_id1, "msg2", tok=user1_tok)

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )

    def test_rooms_timeline_incremental_sync_NEVER(self) -> None:
        """
        Test getting timeline room data where we have NEVER sent down the room
        before
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 5,
                }
            },
        }

        expected_events = []
        for _ in range(4):
            resp = self.helper.send(room_id1, "msg", tok=user1_tok)
            expected_events.append(resp["event_id"])

        # A message happens in the other room, so room1 won't get sent down.
        self.helper.send(room_id2, "msg", tok=user1_tok)

        # Only the second room gets sent down sync.
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id2}, response_body["rooms"]
        )

        # We now send another event to room1 so it comes down sync
        resp = self.helper.send(room_id1, "msg2", tok=user1_tok)
        expected_events.append(resp["event_id"])

        # This sync should contain the messages from room1 not yet sent down.
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertCountEqual(
            response_body["rooms"].keys(), {room_id1}, response_body["rooms"]
        )

        self.assertEqual(
            [ev["event_id"] for ev in response_body["rooms"][room_id1]["timeline"]],
            expected_events,
        )
        self.assertEqual(response_body["rooms"][room_id1]["limited"], True)
        self.assertEqual(response_body["rooms"][room_id1]["initial"], True)
