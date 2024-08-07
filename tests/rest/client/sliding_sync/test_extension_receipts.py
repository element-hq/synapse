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
from synapse.api.constants import EduTypes, ReceiptTypes
from synapse.rest.client import login, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import StreamKeyType
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException

logger = logging.getLogger(__name__)


class SlidingSyncReceiptsExtensionTestCase(SlidingSyncBase):
    """Tests for the receipts sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the receipts extension works during an intitial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the receipts extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "receipts": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        self.assertIncludes(
            response_body["extensions"]["receipts"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling receipts extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "receipts": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the receipts extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIncludes(
            response_body["extensions"]["receipts"].get("rooms").keys(),
            set(),
            exact=True,
        )

    def test_receipts_initial_sync_with_timeline(self) -> None:
        """
        On initial sync, we only return receipts for events in a given room's timeline.

        We also make sure that we only return receipts for rooms that we request and are
        already being returned in the Sliding Sync response.
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
        room1_event_response1 = self.helper.send(
            room_id1, body="new event1", tok=user2_tok
        )
        room1_event_response2 = self.helper.send(
            room_id1, body="new event2", tok=user2_tok
        )
        # User1 reads the last event
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response2['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User2 reads the last event
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response2['event_id']}",
            {},
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User3 reads the first event
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response1['event_id']}",
            {},
            access_token=user3_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User4 privately reads the last event (make sure this doesn't leak to the other users)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ_PRIVATE}/{room1_event_response2['event_id']}",
            {},
            access_token=user4_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create another room
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        self.helper.join(room_id2, user3_id, tok=user3_tok)
        self.helper.join(room_id2, user4_id, tok=user4_tok)
        room2_event_response1 = self.helper.send(
            room_id2, body="new event2", tok=user2_tok
        )
        # User1 reads the last event
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id2}/receipt/{ReceiptTypes.READ}/{room2_event_response1['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User2 reads the last event
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id2}/receipt/{ReceiptTypes.READ}/{room2_event_response1['event_id']}",
            {},
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User4 privately reads the last event (make sure this doesn't leak to the other users)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id2}/receipt/{ReceiptTypes.READ_PRIVATE}/{room2_event_response1['event_id']}",
            {},
            access_token=user4_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make an initial Sliding Sync request with the receipts extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    # On initial sync, we only have receipts for events in the timeline
                    "timeline_limit": 1,
                }
            },
            "extensions": {
                "receipts": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2],
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Only the latest event in the room is in the timelie because the `timeline_limit` is 1
        self.assertIncludes(
            {
                event["event_id"]
                for event in response_body["rooms"][room_id1].get("timeline", [])
            },
            {room1_event_response2["event_id"]},
            exact=True,
            message=str(response_body["rooms"][room_id1]),
        )

        # Even though we requested room2, we only expect room1 to show up because that's
        # the only room in the Sliding Sync response (room2 is not one of our room
        # subscriptions or in a sliding window list).
        self.assertIncludes(
            response_body["extensions"]["receipts"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["type"],
            EduTypes.RECEIPT,
        )
        # We can see user1 and user2 read receipts
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response2["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user1_id, user2_id},
            exact=True,
        )
        # User1 did not have a private read receipt and we shouldn't leak others'
        # private read receipts
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response2["event_id"]
            ]
            .get(ReceiptTypes.READ_PRIVATE, {})
            .keys(),
            set(),
            exact=True,
        )

        # We shouldn't see receipts for event2 since it wasn't in the timeline and this is an initial sync
        self.assertIsNone(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"].get(
                room1_event_response1["event_id"]
            )
        )

    def test_receipts_incremental_sync(self) -> None:
        """
        On incremental sync, we return all receipts in the token range for a given room
        but only for rooms that we request and are being returned in the Sliding Sync
        response.
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
        room1_event_response1 = self.helper.send(
            room_id1, body="new event2", tok=user2_tok
        )
        # User2 reads the last event (before the `from_token`)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response1['event_id']}",
            {},
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create room2
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        room2_event_response1 = self.helper.send(
            room_id2, body="new event2", tok=user2_tok
        )
        # User1 reads the last event (before the `from_token`)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id2}/receipt/{ReceiptTypes.READ}/{room2_event_response1['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Create room3
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id3, user1_id, tok=user1_tok)
        self.helper.join(room_id3, user3_id, tok=user3_tok)
        room3_event_response1 = self.helper.send(
            room_id3, body="new event", tok=user2_tok
        )

        # Create room4
        room_id4 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id4, user1_id, tok=user1_tok)
        self.helper.join(room_id4, user3_id, tok=user3_tok)
        event_response4 = self.helper.send(room_id4, body="new event", tok=user2_tok)
        # User1 reads the last event (before the `from_token`)
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id4}/receipt/{ReceiptTypes.READ}/{event_response4['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

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
                "receipts": {
                    "enabled": True,
                    "rooms": [room_id1, room_id2, room_id3, room_id4],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Add some more read receipts after the `from_token`
        #
        # User1 reads room1
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response1['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User1 privately reads room2
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id2}/receipt/{ReceiptTypes.READ_PRIVATE}/{room2_event_response1['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User3 reads room3
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id3}/receipt/{ReceiptTypes.READ}/{room3_event_response1['event_id']}",
            {},
            access_token=user3_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # No activity for room4 after the `from_token`

        # Make an incremental Sliding Sync request with the receipts extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Even though we requested room2, we only expect rooms to show up if they are
        # already in the Sliding Sync response. room4 doesn't show up because there is
        # no activity after the `from_token`.
        self.assertIncludes(
            response_body["extensions"]["receipts"].get("rooms").keys(),
            {room_id1, room_id3},
            exact=True,
        )

        # Check room1:
        #
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["type"],
            EduTypes.RECEIPT,
        )
        # We only see that user1 has read something in room1 since the `from_token`
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response1["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user1_id},
            exact=True,
        )
        # User1 did not send a private read receipt in this room and we shouldn't leak
        # others' private read receipts
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response1["event_id"]
            ]
            .get(ReceiptTypes.READ_PRIVATE, {})
            .keys(),
            set(),
            exact=True,
        )
        # No events in the timeline since they were sent before the `from_token`
        self.assertNotIn(room_id1, response_body["rooms"])

        # Check room3:
        #
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["receipts"]["rooms"][room_id3]["type"],
            EduTypes.RECEIPT,
        )
        # We only see that user3 has read something in room1 since the `from_token`
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id3]["content"][
                room3_event_response1["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user3_id},
            exact=True,
        )
        # User1 did not send a private read receipt in this room and we shouldn't leak
        # others' private read receipts
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id3]["content"][
                room3_event_response1["event_id"]
            ]
            .get(ReceiptTypes.READ_PRIVATE, {})
            .keys(),
            set(),
            exact=True,
        )
        # No events in the timeline since they were sent before the `from_token`
        self.assertNotIn(room_id3, response_body["rooms"])

    def test_receipts_incremental_sync_all_live_receipts(self) -> None:
        """
        On incremental sync, we return all receipts in the token range for a given room
        even if they are not in the timeline.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create room1
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id1: {
                    "required_state": [],
                    # The timeline will only include event2
                    "timeline_limit": 1,
                },
            },
            "extensions": {
                "receipts": {
                    "enabled": True,
                    "rooms": [room_id1],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        room1_event_response1 = self.helper.send(
            room_id1, body="new event1", tok=user2_tok
        )
        room1_event_response2 = self.helper.send(
            room_id1, body="new event2", tok=user2_tok
        )

        # User1 reads event1
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response1['event_id']}",
            {},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        # User2 reads event2
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id1}/receipt/{ReceiptTypes.READ}/{room1_event_response2['event_id']}",
            {},
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make an incremental Sliding Sync request with the receipts extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should see room1 because it has receipts in the token range
        self.assertIncludes(
            response_body["extensions"]["receipts"].get("rooms").keys(),
            {room_id1},
            exact=True,
        )
        # Sanity check that it's the correct ephemeral event type
        self.assertEqual(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["type"],
            EduTypes.RECEIPT,
        )
        # We should see all receipts in the token range regardless of whether the events
        # are in the timeline
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response1["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user1_id},
            exact=True,
        )
        self.assertIncludes(
            response_body["extensions"]["receipts"]["rooms"][room_id1]["content"][
                room1_event_response2["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user2_id},
            exact=True,
        )
        # Only the latest event in the timeline because the `timeline_limit` is 1
        self.assertIncludes(
            {
                event["event_id"]
                for event in response_body["rooms"][room_id1].get("timeline", [])
            },
            {room1_event_response2["event_id"]},
            exact=True,
            message=str(response_body["rooms"][room_id1]),
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
        event_response = self.helper.send(room_id, body="new event", tok=user2_tok)

        sync_body = {
            "lists": {},
            "room_subscriptions": {
                room_id: {
                    "required_state": [],
                    "timeline_limit": 0,
                },
            },
            "extensions": {
                "receipts": {
                    "enabled": True,
                    "rooms": [room_id],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the receipts extension enabled
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
        # Bump the receipts to trigger new results
        receipt_channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/receipt/{ReceiptTypes.READ}/{event_response['event_id']}",
            {},
            access_token=user2_tok,
        )
        self.assertEqual(receipt_channel.code, 200, receipt_channel.json_body)
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the new receipt
        self.assertIncludes(
            channel.json_body.get("extensions", {})
            .get("receipts", {})
            .get("rooms", {})
            .keys(),
            {room_id},
            exact=True,
            message=str(channel.json_body),
        )
        self.assertIncludes(
            channel.json_body["extensions"]["receipts"]["rooms"][room_id]["content"][
                event_response["event_id"]
            ][ReceiptTypes.READ].keys(),
            {user2_id},
            exact=True,
        )
        # User1 did not send a private read receipt in this room and we shouldn't leak
        # others' private read receipts
        self.assertIncludes(
            channel.json_body["extensions"]["receipts"]["rooms"][room_id]["content"][
                event_response["event_id"]
            ]
            .get(ReceiptTypes.READ_PRIVATE, {})
            .keys(),
            set(),
            exact=True,
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the receipts extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "receipts": {
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
            channel.json_body["extensions"]["receipts"].get("rooms").keys(),
            set(),
            exact=True,
        )
