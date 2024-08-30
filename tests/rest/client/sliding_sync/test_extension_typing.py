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

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EduTypes
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import StreamKeyType
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncTypingExtensionTestCase(SlidingSyncBase):
    """Tests for the typing notification sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the typing extension works during an intitial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the typing extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "typing": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertIncludes(
            response_body["extensions"]["typing"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling typing extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "typing": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the typing extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIncludes(
            response_body["extensions"]["typing"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_typing_initial_sync(self) -> None:
        """
        On initial sync, we return all typing notifications for rooms that we request
        and are being returned in the Sliding Sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        # Create a room
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        # User1 starts typing in room1
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id1}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User2 starts typing in room1
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id1}/typing/{user2_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create another room
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        self.helper.join(room_id2, user3_id, tok=user3_tok)
        self.helper.join(room_id2, user4_id, tok=user4_tok)
        # User1 starts typing in room2
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id2}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User2 starts typing in room2
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id2}/typing/{user2_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make an initial Sliding Sync request with the typing extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "typing": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["typing"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["typing"]["rooms"][room_id1]["type"],
            EduTypes.TYPING,
        )
        # We can see user1 and user2 typing
        self.assertIncludes(
            set(
                response_body["extensions"]["typing"]["rooms"][room_id1]["content"][
                    "user_ids"
                ]
            ),
            {user1_id, user2_id},
            exact=True,
        )

    def test_typing_incremental_sync(self) -> None:
        """
        On incremental sync, we return all typing notifications in the token range for a
        given room but only for rooms that we request and are being returned in the
        Sliding Sync response.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        # Create room1
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        # User2 starts typing in room1
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id1}/typing/{user2_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create room2
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        # User1 starts typing in room2 (before the `from_token`)
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id2}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create room3
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id3, user1_id, tok=user1_tok)
        self.helper.join(room_id3, user3_id, tok=user3_tok)

        # Create room4
        room_id4 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id4, user1_id, tok=user1_tok)
        self.helper.join(room_id4, user3_id, tok=user3_tok)
        # User1 starts typing in room4 (before the `from_token`)
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id4}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Advance time so all of the typing notifications timeout before we make our
        # Sliding Sync requests. Even though these are sent before the `from_token`, the
        # typing code only keeps track of stream position of the latest typing
        # notification so "old" typing notifications that are still "alive" (haven't
        # timed out) can appear in the response.
        self.reactor.advance(36)

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
                room_id3: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
                room_id4: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
            },
            "extensions": {
                "typing": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2, room_id3, room_id4],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some more typing notifications after the `from_token`
        #
        # User1 starts typing in room1
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id1}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User1 starts typing in room2
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id2}/typing/{user1_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User3 starts typing in room3
        channel = self.make_request(
            "PUT",
            f"/rooms/{room_id3}/typing/{user3_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user3_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # No activity for room4 after the `from_token`

        # Make an incremental Sliding Sync request with the typing extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Even though we requested room2, we only expect rooms to show up if they are
        # already in the Sliding Sync response. room4 doesn't show up because there is
        # no activity after the `from_token`.
        self.assertIncludes(
            response_body["extensions"]["typing"].get("rooms").keys(),
            {room_id1, room_id3},
            exact=True,
        )

        # Check room1:
        #
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["typing"]["rooms"][room_id1]["type"],
            EduTypes.TYPING,
        )
        # We only see that user1 is typing in room1 since the `from_token`
        self.assertIncludes(
            set(
                response_body["extensions"]["typing"]["rooms"][room_id1]["content"][
                    "user_ids"
                ]
            ),
            {user1_id},
            exact=True,
        )

        # Check room3:
        #
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["typing"]["rooms"][room_id3]["type"],
            EduTypes.TYPING,
        )
        # We only see that user3 is typing in room1 since the `from_token`
        self.assertIncludes(
            set(
                response_body["extensions"]["typing"]["rooms"][room_id3]["content"][
                    "user_ids"
                ]
            ),
            {user3_id},
            exact=True,
        )

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
            },
            "extensions": {
                "typing": {
                    "enabled": True,
                    "rooms": [room_id],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the typing extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the typing status to trigger new results
        typing_channel = self.make_request(
            "PUT",
            f"/rooms/{room_id}/typing/{user2_id}",
            b'{"typing": true, "timeout": 30000}',
            access_token=user2_tok,
        )
        self.assertEqual(typing_channel.code, 200, typing_channel.json_body)
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the new typing notification
        self.assertIncludes(
            channel.json_body.get("extensions", {})
            .get("typing", {})
            .get("rooms", {})
            .keys(),
            {room_id},
            exact=True,
            message=str(channel.json_body),
        )
        self.assertIncludes(
            set(
                channel.json_body["extensions"]["typing"]["rooms"][room_id]["content"][
                    "user_ids"
                ]
            ),
            {user2_id},
            exact=True,
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the typing extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "typing": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?timeout=10000&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(
            user1_id, wake_stream_key=StreamKeyType.ACCOUNT_DATA
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        self.assertIncludes(
            channel.json_body["extensions"]["typing"].get("rooms").keys(),
            set(),
            exact=True,
        )
