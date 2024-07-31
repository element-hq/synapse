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
from http import HTTPStatus

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, HistoryVisibility
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


class SlidingSyncRoomSubscriptionsTestCase(SlidingSyncBase):
    """
    Test `room_subscriptions` in the Sliding Sync API.
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

    def test_room_subscriptions_with_join_membership(self) -> None:
        """
        Test `room_subscriptions` with a joined room should give us timeline and current
        state events.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We should see some state
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

        # We should see some events
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )

    def test_room_subscriptions_with_leave_membership(self) -> None:
        """
        Test `room_subscriptions` with a leave room should give us timeline and state
        events up to the leave event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )

        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Send some events after user1 leaves
        self.helper.send(room_id1, "activity after leave", tok=user2_tok)
        # Update state after user1 leaves
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "qux"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        ["org.matrix.foo_state", ""],
                    ],
                    "timeline_limit": 2,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should see the state at the time of the leave
        self._assertRequiredStateIncludes(
            response_body["rooms"][room_id1]["required_state"],
            {
                state_map[("org.matrix.foo_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(response_body["rooms"][room_id1].get("invite_state"))

        # We should see some before we left (nothing after)
        self.assertEqual(
            [
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
                leave_response["event_id"],
            ],
            response_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )

    def test_room_subscriptions_no_leak_private_room(self) -> None:
        """
        Test `room_subscriptions` with a private room we have never been in should not
        leak any data to the user.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=False)

        # We should not be able to join the private room
        self.helper.join(
            room_id1, user1_id, tok=user1_tok, expect_code=HTTPStatus.FORBIDDEN
        )

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should not see the room at all (we're not in it)
        self.assertIsNone(response_body["rooms"].get(room_id1), response_body["rooms"])

    def test_room_subscriptions_world_readable(self) -> None:
        """
        Test `room_subscriptions` with a room that has `world_readable` history visibility

        FIXME: We should be able to see the room timeline and state
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room with `world_readable` history visibility
        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "preset": "public_chat",
                "initial_state": [
                    {
                        "content": {
                            "history_visibility": HistoryVisibility.WORLD_READABLE
                        },
                        "state_key": "",
                        "type": EventTypes.RoomHistoryVisibility,
                    }
                ],
            },
        )
        # Ensure we're testing with a room with `world_readable` history visibility
        # which means events are visible to anyone even without membership.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.WORLD_READABLE,
        )

        # Note: We never join the room

        # Make the Sliding Sync request with just the room subscription
        sync_body = {
            "room_subscriptions": {
                room_id1: {
                    "required_state": [
                        [EventTypes.Create, ""],
                    ],
                    "timeline_limit": 1,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # FIXME: In the future, we should be able to see the room because it's
        # `world_readable` but currently we don't support this.
        self.assertIsNone(response_body["rooms"].get(room_id1), response_body["rooms"])
