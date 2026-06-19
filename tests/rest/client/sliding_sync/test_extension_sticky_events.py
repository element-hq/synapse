#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
import logging
import sqlite3

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
import synapse.rest.client.account_data
from synapse.api.constants import EventTypes, EventUnsignedContentFields
from synapse.rest.client import account_data, login, register, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, StreamKeyType
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.server import TimedOutException
from tests.utils import USE_POSTGRES_FOR_TESTS

logger = logging.getLogger(__name__)


DUMMY_LISTS = {
    "main": {
        # Don't include any rooms in the top-N window
        "ranges": [[0, 0]],
        "required_state": [],
        "timeline_limit": 0,
    }
}
"""
Subscription lists that can be used in the Sliding Sync request `lists` field,
which sets up a subscription that is interested in all rooms but does not let any rooms into the window,
thus does not return any timelines.

Sufficient to get sticky event updates as per MSC4354:

> The server MUST include sticky events across all rooms that would be matched by at least one subscription list
> (i.e. all rooms that the client is interested in), even if the room does not appear in top-N window for that
> subscription list at this time.
> Rooms that would not be matched by a list are not included, as this means the client is not interested
> in those rooms.
>
> — https://github.com/matrix-org/matrix-spec-proposals/pull/4354/changes#diff-d76bc1a1d612c6da37d024f5b57f7b8352939b8db8a7ee9c6b71c1a848359afdR213-R217
"""


class SlidingSyncStickyEventsExtensionTestCase(SlidingSyncBase):
    """Tests for the sticky events sliding sync extension"""

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        # We need the JSON functionality in SQLite
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        account_data.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync and sticky events MSCs
        config["experimental_features"] = {
            "msc3575_enabled": True,
            "msc4354_enabled": True,
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        super().prepare(reactor, clock, hs)

    def _assert_sticky_events_response(
        self,
        response_body: JsonDict,
        expected_events_by_room: dict[str, list[str]] | None,
    ) -> str | None:
        """Assert the sliding sync response was successful and has the expected
        sticky events.

        Args:
            response_body: Sliding Sync response body
            expected_events_by_room:
                map of room ID to list of event IDs to expect (in the order we expect them),
                or None if we expect an empty sticky events extension response

        Returns the next_batch token from the sticky events section,
        unless we're expecting an empty response.
        """
        extensions = response_body["extensions"]
        sticky_events = extensions.get("org.matrix.msc4354.sticky_events")

        # If there are no expected events, we shouldn't get anything in the response
        if expected_events_by_room is None:
            self.assertIsNone(sticky_events)
            return None

        self.assertIsNotNone(sticky_events)
        self.assertIsInstance(sticky_events["next_batch"], str)

        actual_rooms = sticky_events["rooms"]
        # Check that we have the expected rooms
        self.assertIncludes(set(actual_rooms.keys()), set(expected_events_by_room.keys()), exact=True)

        # Check the events in each room
        for room_id, expected_events in expected_events_by_room.items():
            actual_events = actual_rooms[room_id]["events"]
            actual_event_ids = [e["event_id"] for e in actual_events]
            self.assertEqual(actual_event_ids, expected_events)
            for actual_event in actual_events:
                # Check the sticky TTL is sent
                self.assertIn("unsigned", actual_event)
                ttl = actual_event["unsigned"][EventUnsignedContentFields.STICKY_TTL]
                self.assertIsInstance(ttl, int)

        self.assertIn("next_batch", sticky_events)
        return sticky_events["next_batch"]

    def test_empty_sync(self) -> None:
        """Test that enabling sticky events extension works on initial and incremental sync,
        even if there is no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        # No sticky events in initial sync.
        self._assert_sticky_events_response(response_body, None)

        # Incremental sync should also have no sticky events
        response_body, _ = self.do_sync(
            sync_body, since=response_body["pos"], tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

    def test_initial_sync(self) -> None:
        """Test that we get sticky events when we don't specify a since token
        (initial sync).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room and join both users
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Send a sticky event from user2
        sticky_event_id: str = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=user2_tok,
        )["event_id"]

        # Initial sync should return the sticky event
        sync_body: JsonDict = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert the response and then get the next_batch for the next sliding sync request
        next_batch = self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id]}
        )
        assert next_batch is not None

        # Do an incremental sync immediately again
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                    "since": next_batch,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Check we don't get that event again
        self._assert_sticky_events_response(response_body, None)

        # Send another sticky event
        sticky_event_id2: str = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": "another sticky message", "msgtype": "m.text"},
            tok=user1_tok,
        )["event_id"]

        # Now the incremental sync should give us that event
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id2]}
        )

    def test_expired_events_not_returned(self) -> None:
        """Test that expired sticky events are not returned."""
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Send a sticky event with a short duration
        sticky_event_id = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(seconds=2),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=user2_tok,
        )["event_id"]

        # Initial sync should return the sticky event
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should still get the event for now
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # Advance time past the sticky duration
        self.reactor.advance(3)

        # A second initial sync should not return the expired sticky event
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

    def test_wait_for_new_data(self) -> None:
        """Test that the sliding sync request waits for new sticky events to arrive.
        (Only applies to incremental syncs with a `timeout` specified).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Initial sync with no sticky events
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the sliding sync request with a timeout
        channel = self.make_request(
            "POST",
            self.sync_endpoint + "?timeout=10000" + f"&pos={from_token}",
            content=sync_body,
            access_token=user1_tok,
            await_result=False,
        )

        # Block for 5 seconds to make sure we are in `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)

        # Send a sticky event to trigger new results
        sticky_event_id = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=user2_tok,
        )["event_id"]

        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=100)
        self.assertEqual(channel.code, 200, channel.json_body)

        self._assert_sticky_events_response(
            channel.json_body,
            {room_id: [sticky_event_id]},
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test that the sliding sync request waits for new sticky events to arrive
        and times out when no data arrives before the deadline.
        (Only applies to incremental syncs with a `timeout` specified).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Initial sync with no sticky events
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make the sliding sync request with a timeout
        channel = self.make_request(
            "POST",
            self.sync_endpoint + "?timeout=10000" + f"&pos={from_token}",
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
            # wake key is intentionally unrelated to sticky events
            user1_id,
            wake_stream_key=StreamKeyType.ACCOUNT_DATA,
        )
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        self._assert_sticky_events_response(
            channel.json_body,
            None,
        )

    def test_ignored_users_sticky_events(self) -> None:
        """
        Test that sticky events from ignored users are not delivered to clients.

        > As with normal events, sticky events sent by ignored users MUST NOT be
        > delivered to clients.
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/4340903c15e9eab1bfb2f6a31cfa08fd535f7e7c/proposals/4354-sticky-events.md#sync-api-changes
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # User1 ignores user2
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/user/{user1_id}/account_data/m.ignored_user_list",
            {"ignored_users": {user2_id: {}}},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # User2 sends a sticky event
        sticky_event_id = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": "sticky from ignored user", "msgtype": "m.text"},
            tok=user2_tok,
        )["event_id"]

        # Initial sync for user1
        sync_body = {
            "lists": {
                "main": {
                    "ranges": [[0, 10]],
                    "required_state": [],
                    # In this test we ask for 10 events of timeline.
                    "timeline_limit": 10,
                }
            },
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Timeline events should not include sticky event from ignored user
        timeline_events = response_body["rooms"][room_id]["timeline"]
        timeline_event_ids = [e["event_id"] for e in timeline_events]

        self.assertNotIn(
            sticky_event_id,
            timeline_event_ids,
            "Sticky event from ignored user should not be in timeline",
        )

        # Sticky events section should also not include the event from ignored user
        self._assert_sticky_events_response(response_body, None)

    def test_history_visibility_bypass_for_sticky_events(self) -> None:
        """
        Test that joined users can see sticky events even when history visibility
        is set to "joined" and they joined after the event was sent.

        > History visibility checks MUST NOT be applied to sticky events.
        > Any joined user is authorised to see sticky events for the duration they remain sticky.
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/4340903c15e9eab1bfb2f6a31cfa08fd535f7e7c/proposals/4354-sticky-events.md#proposal
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create a room with restrictive history visibility
        room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
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
            tok=user1_tok,
        )["event_id"]

        # User1 also sends a regular event, to verify our test setup
        regular_event_id = self.helper.send(
            room_id=room_id,
            body="regular message",
            tok=user1_tok,
        )["event_id"]

        # Register and join a second user after the sticky event was sent
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # User2 syncs - they should see sticky event even though
        # history visibility is "joined" and they joined after it was sent
        sync_body = {
            "lists": {
                "main": {
                    "ranges": [[0, 10]],
                    "required_state": [],
                    # In this test, we ask for 10 events of timeline.
                    "timeline_limit": 10,
                }
            },
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user2_tok)

        # The sticky event is fully visible in its own right,
        # but AFAICT the timeline only includes events since we join the room
        # (regardless of history visibility),
        # so this comes down in the sticky extension
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # Instead the sticky event is in the timeline
        timeline_events = response_body["rooms"][room_id]["timeline"]
        timeline_event_ids = [e["event_id"] for e in timeline_events]
        self.assertNotIn(
            regular_event_id,
            timeline_event_ids,
            f"Expecting to not see regular event ({regular_event_id}) before user1 joined.",
        )

    def test_sticky_event_pagination(self) -> None:
        """
        Test that pagination works correctly when there are many sticky events.
        Also check they are delivered in stream order.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Send 4 sticky events (more than our limit of 2)
        sticky_event_ids: list[str] = []
        for i in range(4):
            event_id = self.helper.send_sticky_event(
                room_id,
                EventTypes.Message,
                duration=Duration(minutes=5),
                content={"body": f"sticky message {i}", "msgtype": "m.text"},
                tok=user2_tok,
            )["event_id"]
            sticky_event_ids.append(event_id)

        # Initial sync
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {"enabled": True, "limit": 2}
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We expect to see the first 2 sticky events by stream order
        # and they should be in that stream order
        next_batch = self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[0:2]}
        )

        # Incremental sync to get remaining sticky events
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                    # This makes it incremental
                    "since": next_batch,
                    "limit": 3,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Should get remaining events, in stream order again
        self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[2:4]}
        )

    def test_deduplication_with_timeline(self) -> None:
        """
        Test that sticky events are not included in the sticky event extension of sliding sync
        if they are included in the main timeline section.

        Send 3 events:
            1. sticky
            2. sticky
            3. regular

        We then will sync with a timeline limit of 2 and a sticky event limit of 2.
        We should then see (2) and (3) included in the timeline
        and (1) in the sticky event response (but not (2) because it's already
        included in the timeline.)

            1. sticky            [in sticky section]

                ------------->>> Timeline section
            2. sticky
            3. regular
                -------------<<<
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        sticky_event_ids: list[str] = []
        for i in range(2):
            event_id = self.helper.send_sticky_event(
                room_id,
                EventTypes.Message,
                duration=Duration(minutes=5),
                content={"body": f"sticky message {i}", "msgtype": "m.text"},
                tok=user1_tok,
            )["event_id"]
            sticky_event_ids.append(event_id)

        non_sticky_event_id = self.helper.send_event(
            room_id,
            EventTypes.Message,
            content={"body": "regular message", "msgtype": "m.text"},
            tok=user1_tok,
        )["event_id"]

        # Sync
        sync_body = {
            "lists": {
                "main": {
                    "ranges": [[0, 10]],
                    "required_state": [],
                    # In this test, we want a timeline window of the 2 latest messages
                    "timeline_limit": 2,
                }
            },
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                    "limit": 2,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        events_in_sticky_section = response_body["extensions"][
            "org.matrix.msc4354.sticky_events"
        ]["rooms"][room_id]["events"]
        event_ids_in_sticky_section = [e["event_id"] for e in events_in_sticky_section]

        events_in_timeline_section = response_body["rooms"][room_id]["timeline"]
        event_ids_in_timeline_section = [
            e["event_id"] for e in events_in_timeline_section
        ]

        self.assertEqual(
            event_ids_in_sticky_section,
            [sticky_event_ids[0]],
        )
        self.assertEqual(
            event_ids_in_timeline_section, [sticky_event_ids[1], non_sticky_event_id]
        )
