#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026, Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
import json
import sqlite3
from dataclasses import dataclass
from unittest.mock import patch
from urllib.parse import quote

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, EventUnsignedContentFields, StickyEvent
from synapse.rest import admin
from synapse.rest.client import account_data, login, register, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.utils import USE_POSTGRES_FOR_TESTS


class SyncStickyEventsTestCase(unittest.HomeserverTestCase):
    """
    Tests for oldschool (v3) /sync with sticky events (MSC4354)
    """

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        # We need the JSON functionality in SQLite
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    servlets = [
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets,
        sync.register_servlets,
        account_data.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4354_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Register an account
        self.user_id = self.register_user("user1", "pass")
        self.token = self.login(self.user_id, "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.token)

    def test_single_sticky_event_appears_in_initial_sync(self) -> None:
        """
        Test sending a single sticky event and then doing an initial /sync.
        """

        # Send a sticky event
        sticky_event_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=self.token,
        )
        sticky_event_id = sticky_event_response["event_id"]

        # Perform initial sync
        channel = self.make_request(
            "GET",
            "/sync",
            access_token=self.token,
        )

        self.assertEqual(channel.code, 200, channel.result)

        # Get timeline events from the sync response
        timeline_events = channel.json_body["rooms"]["join"][self.room_id]["timeline"][
            "events"
        ]

        # Verify the sticky event is present and has the sticky TTL field
        self.assertEqual(
            timeline_events[-1]["event_id"],
            sticky_event_id,
            f"Sticky event {sticky_event_id} not found in sync timeline",
        )
        self.assertEqual(
            timeline_events[-1]["unsigned"][EventUnsignedContentFields.STICKY_TTL],
            # The other 100 ms is advanced in FakeChannel.await_result.
            59_900,
        )

        self.assertNotIn(
            "sticky",
            channel.json_body["rooms"]["join"][self.room_id],
            "Unexpected sticky section of sync response (sticky event should be deduplicated)",
        )

    def test_sticky_event_beyond_timeline_in_initial_sync(self) -> None:
        """
        Test that when sending a sticky event which is subsequently
        pushed out of the timeline window by other messages,
        the sticky event comes down the dedicated sticky section of the sync response.
        """
        # Send the first sticky event
        first_sticky_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "first sticky", "msgtype": "m.text"},
            tok=self.token,
        )
        first_sticky_event_id = first_sticky_response["event_id"]

        # Send 10 regular timeline events,
        # in order to push the sticky event out of the timeline window
        # that the /sync will get.
        regular_event_ids = []
        for i in range(10):
            # (Note: each one advances time by 100ms)
            response = self.helper.send(
                room_id=self.room_id,
                body=f"regular message {i}",
                tok=self.token,
            )
            regular_event_ids.append(response["event_id"])

        # Send another sticky event
        # (Note: this advances time by 100ms)
        second_sticky_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "second sticky", "msgtype": "m.text"},
            tok=self.token,
        )
        second_sticky_event_id = second_sticky_response["event_id"]

        # Perform initial sync
        channel = self.make_request(
            "GET",
            "/sync",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Get timeline events from the sync response
        timeline_events = channel.json_body["rooms"]["join"][self.room_id]["timeline"][
            "events"
        ]
        timeline_event_ids = [event["event_id"] for event in timeline_events]
        sticky_events = channel.json_body["rooms"]["join"][self.room_id][
            "msc4354_sticky"
        ]["events"]

        # This is canary to check the test setup is valid and that we're actually excluding the sticky event
        # because it's outside the timeline window, not for some other potential reason.
        self.assertNotIn(
            regular_event_ids[0],
            timeline_event_ids,
            f"First regular event {regular_event_ids[0]} unexpectedly found in sync timeline (this means our test is invalid)",
        )

        # Assertions for the first sticky event: should be only in sticky section
        self.assertNotIn(
            first_sticky_event_id,
            timeline_event_ids,
            f"First sticky event {first_sticky_event_id} unexpectedly found in sync timeline (expected it to be outside the timeline window)",
        )
        self.assertEqual(
            len(sticky_events),
            1,
            f"Expected exactly 1 item in sticky events section, got {sticky_events}",
        )
        self.assertEqual(sticky_events[0]["event_id"], first_sticky_event_id)
        self.assertEqual(
            # The 'missing' 1100 ms were elapsed when sending events
            sticky_events[0]["unsigned"][EventUnsignedContentFields.STICKY_TTL],
            58_800,
        )

        # Assertions for the second sticky event: should be only in timeline section
        self.assertEqual(
            timeline_events[-1]["event_id"],
            second_sticky_event_id,
            f"Second sticky event {second_sticky_event_id} not found in sync timeline",
        )
        self.assertEqual(
            timeline_events[-1]["unsigned"][EventUnsignedContentFields.STICKY_TTL],
            # The other 100 ms is advanced in FakeChannel.await_result.
            59_900,
        )
        # (sticky section: we already checked it only has 1 item and
        # that item was the first above)

    def test_sticky_event_filtered_from_timeline_appears_in_sticky_section(
        self,
    ) -> None:
        """
        Test that a sticky event which is excluded from the timeline by a timeline filter
        still appears in the sticky section of the sync response.

        > Interaction with RoomFilter: The RoomFilter does not apply to the sticky.events section,
        > as it is neither timeline nor state. However, the timeline filter MUST be applied before
        > applying the deduplication logic above. In other words, if a sticky event would normally
        > appear in both the timeline.events section and the sticky.events section, but is filtered
        > out by the timeline filter, the sticky event MUST appear in sticky.events.
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/4340903c15e9eab1bfb2f6a31cfa08fd535f7e7c/proposals/4354-sticky-events.md#sync-api-changes
        """
        # A filter that excludes message events
        filter_json = json.dumps(
            {
                "room": {
                    # We only want these io.element.example events
                    "timeline": {"types": ["io.element.example"]},
                }
            }
        )

        # Send a sticky message event (will be filtered by our filter)
        sticky_event_id = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        # Send a non-message event (will pass the filter)
        nonmessage_event_id = self.helper.send_event(
            room_id=self.room_id,
            type="io.element.example",
            content={"membership": "join"},
            tok=self.token,
        )["event_id"]

        # Perform initial sync with our filter
        channel = self.make_request(
            "GET",
            f"/sync?filter={quote(filter_json)}",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Get timeline and sticky events from the sync response
        timeline_events = channel.json_body["rooms"]["join"][self.room_id]["timeline"][
            "events"
        ]
        sticky_events = channel.json_body["rooms"]["join"][self.room_id][
            "msc4354_sticky"
        ]["events"]

        # Extract event IDs from the timeline
        timeline_event_ids = [event["event_id"] for event in timeline_events]

        # The sticky message event should NOT be in the timeline (filtered out)
        self.assertNotIn(
            sticky_event_id,
            timeline_event_ids,
            f"Sticky message event {sticky_event_id} should be filtered from timeline",
        )

        # The member event should be in the timeline
        self.assertIn(
            nonmessage_event_id,
            timeline_event_ids,
            f"Non-message event {nonmessage_event_id} should be in timeline",
        )

        # The sticky message event MUST appear in the sticky section
        # because it was filtered from the timeline
        received_sticky_event_ids = [e["event_id"] for e in sticky_events]
        self.assertEqual(
            received_sticky_event_ids,
            [sticky_event_id],
        )

    def test_ignored_users_sticky_events(self) -> None:
        """
        Test that sticky events from ignored users are not delivered to clients.

        > As with normal events, sticky events sent by ignored users MUST NOT be
        > delivered to clients.
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/4340903c15e9eab1bfb2f6a31cfa08fd535f7e7c/proposals/4354-sticky-events.md#sync-api-changes
        """
        # Register a second user who will be ignored
        user2_id = self.register_user("user2", "pass")
        user2_token = self.login(user2_id, "pass")

        # Join user2 to the room
        self.helper.join(self.room_id, user2_id, tok=user2_token)

        # User1 ignores user2
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{self.user_id}/account_data/m.ignored_user_list",
            {"ignored_users": {user2_id: {}}},
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # User2 sends a sticky event
        sticky_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "sticky from ignored user", "msgtype": "m.text"},
            tok=user2_token,
        )
        sticky_event_id = sticky_response["event_id"]

        # User1 syncs
        channel = self.make_request(
            "GET",
            "/sync",
            access_token=self.token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Check timeline events - should not include the sticky event from ignored user
        timeline_events = channel.json_body["rooms"]["join"][self.room_id]["timeline"][
            "events"
        ]
        timeline_event_ids = [event["event_id"] for event in timeline_events]

        self.assertNotIn(
            sticky_event_id,
            timeline_event_ids,
            f"Sticky event from ignored user {sticky_event_id} should not be in timeline",
        )

        # Check sticky events section - should also not include the event
        sticky_events = (
            channel.json_body["rooms"]["join"][self.room_id]
            .get("msc4354_sticky", {})
            .get("events", [])
        )

        sticky_event_ids = [event["event_id"] for event in sticky_events]

        self.assertNotIn(
            sticky_event_id,
            sticky_event_ids,
            f"Sticky event from ignored user {sticky_event_id} should not be in sticky section",
        )

    def test_history_visibility_bypass_for_sticky_events(self) -> None:
        """
        Test that joined users can see sticky events even when history visibility
        is set to "joined" and they joined after the event was sent.
        This is required by MSC4354: "History visibility checks MUST NOT
        be applied to sticky events. Any joined user is authorised to see
        sticky events for the duration they remain sticky."
        """
        # Create a new room with restricted history visibility
        room_id = self.helper.create_room_as(
            self.user_id,
            tok=self.token,
            extra_content={
                # Anyone can join
                "preset": "public_chat",
                # But you can't see history before you joined
                "initial_state": [
                    {
                        "type": EventTypes.RoomHistoryVisibility,
                        "state_key": "",
                        "content": {"history_visibility": "joined"},
                    }
                ],
            },
            is_public=False,
        )

        # User1 sends a sticky event
        sticky_event_id = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        # User1 also sends a regular event, to verify our test setup
        regular_event_id = self.helper.send(
            room_id=room_id,
            body="regular message",
            tok=self.token,
        )["event_id"]

        # Register and join a second user after the sticky event was sent
        user2_id = self.register_user("user2", "pass")
        user2_token = self.login(user2_id, "pass")
        self.helper.join(room_id, user2_id, tok=user2_token)

        # User2 syncs - they should see the sticky event even though
        # history visibility is "joined" and they joined after it was sent
        channel = self.make_request(
            "GET",
            "/sync",
            access_token=user2_token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Get sticky events from the sync response
        sticky_events = channel.json_body["rooms"]["join"][room_id]["msc4354_sticky"][
            "events"
        ]
        sticky_event_ids = [event["event_id"] for event in sticky_events]

        # The sticky event should be visible to user2
        self.assertEqual(
            [sticky_event_id],
            sticky_event_ids,
            f"Sticky event {sticky_event_id} should be visible despite history visibility",
        )

        # Also check that the regular (non-sticky) event sent at the same time
        # is NOT visible. This is to verify our test setup.
        timeline_events = channel.json_body["rooms"]["join"][room_id]["timeline"][
            "events"
        ]
        timeline_event_ids = [event["event_id"] for event in timeline_events]

        # The regular message should NOT be visible (history visibility = joined)
        self.assertNotIn(
            regular_event_id,
            timeline_event_ids,
            f"Expecting to not see regular event ({regular_event_id}) before user1 joined.",
        )

    @patch.object(StickyEvent, "MAX_EVENTS_IN_SYNC", 3)
    def test_pagination_with_many_sticky_events(self) -> None:
        """
        Test that pagination works correctly when there are more sticky events than
        the intended limit.

        The MSC doesn't define a limit or how to set one.
        See thread: https://github.com/matrix-org/matrix-spec-proposals/pull/4354#discussion_r2885670008

        But Synapse currently emits 100 at a time, controlled by `MAX_EVENTS_IN_SYNC`.
        In this test we patch it to 3 (as sending 100 events is not very efficient).
        """

        # A filter that excludes message events
        # This is needed so that the sticky events come down the sticky section
        # and not the timeline section, which would hamper our test.
        filter_json = json.dumps(
            {
                "room": {
                    # We only want these io.element.example events
                    "timeline": {"types": ["io.element.example"]},
                }
            }
        )

        # Send 8 sticky events: enough for 2 full pages and then a partial page with 2.
        sent_sticky_event_ids = []
        for i in range(8):
            response = self.helper.send_sticky_event(
                self.room_id,
                EventTypes.Message,
                duration=Duration(minutes=1),
                content={"body": f"sticky message {i}", "msgtype": "m.text"},
                tok=self.token,
            )
            sent_sticky_event_ids.append(response["event_id"])

        @dataclass
        class SyncHelperResponse:
            sticky_event_ids: list[str]
            """Event IDs of events returned from the sticky section, in-order."""

            next_batch: str
            """Sync token to pass to `?since=` in order to do an incremental sync."""

        def _do_sync(*, since: str | None) -> SyncHelperResponse:
            """Small helper to do a sync and get the sticky events out."""
            and_since_param = "" if since is None else f"&since={since}"
            channel = self.make_request(
                "GET",
                f"/sync?filter={quote(filter_json)}{and_since_param}",
                access_token=self.token,
            )
            self.assertEqual(channel.code, 200, channel.result)

            # Get sticky events from the sync response
            sticky_events = []
            # In this test, when no sticky events are in the response,
            # we don't have a rooms section at all
            if "rooms" in channel.json_body:
                sticky_events = channel.json_body["rooms"]["join"][self.room_id][
                    "msc4354_sticky"
                ]["events"]

            return SyncHelperResponse(
                sticky_event_ids=[
                    sticky_event["event_id"] for sticky_event in sticky_events
                ],
                next_batch=channel.json_body["next_batch"],
            )

        # Perform initial sync, we should get the first 3 sticky events,
        # in order.
        sync1 = _do_sync(since=None)
        self.assertEqual(sync1.sticky_event_ids, sent_sticky_event_ids[0:3])

        # Now do an incremental sync and expect the next page of 3
        sync2 = _do_sync(since=sync1.next_batch)
        self.assertEqual(sync2.sticky_event_ids, sent_sticky_event_ids[3:6])

        # Now do another incremental sync and expect the last 2
        sync3 = _do_sync(since=sync2.next_batch)
        self.assertEqual(sync3.sticky_event_ids, sent_sticky_event_ids[6:8])

        # Finally, expect an empty incremental sync at the end
        sync4 = _do_sync(since=sync3.next_batch)
        self.assertEqual(sync4.sticky_event_ids, [])
