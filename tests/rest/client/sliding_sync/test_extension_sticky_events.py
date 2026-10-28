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
        # Include (up to) the first 11 rooms in the window, but don't return any
        # timeline events for them.
        "ranges": [[0, 10]],
        "required_state": [],
        "timeline_limit": 0,
    }
}
"""
Subscription lists that can be used in the Sliding Sync request `lists` field,
which sets up a list whose window covers all of the (few) rooms used in these
tests, but does not return any timelines.

As per the common per-room extension semantics of MSC4508 (which MSC4480 follows),
only rooms within the range of a list (or room subscriptions) are in scope for
the sticky events extension.
"""

STICKY_EVENTS_EXT = "org.matrix.msc4354.sticky_events"

ROOM_SUBSCRIPTION = {"required_state": [], "timeline_limit": 0}
"""A room subscription that doesn't return any timeline events."""


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
    ) -> None:
        """Assert the sliding sync response was successful and has the expected
        sticky events.

        Args:
            response_body: Sliding Sync response body
            expected_events_by_room:
                map of room ID to list of event IDs to expect (in the order we expect them),
                or None if we expect an empty sticky events extension response
        """
        extensions = response_body["extensions"]
        sticky_events = extensions.get(STICKY_EVENTS_EXT)

        # If there are no expected events, we shouldn't get anything in the response
        if expected_events_by_room is None:
            self.assertIsNone(sticky_events)
            return

        self.assertIsNotNone(sticky_events)

        actual_rooms = sticky_events["rooms"]
        # Check that we have the expected rooms
        self.assertIncludes(
            set(actual_rooms.keys()), set(expected_events_by_room.keys()), exact=True
        )

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

    def _send_sticky_event(
        self,
        room_id: str,
        tok: str,
        *,
        duration: Duration = Duration(minutes=5),
        body: str = "sticky message",
        event_type: str = EventTypes.Message,
    ) -> str:
        """Send a sticky event and return its event ID."""
        return self.helper.send_sticky_event(
            room_id,
            event_type,
            duration=duration,
            content={"body": body, "msgtype": "m.text"},
            tok=tok,
        )["event_id"]

    def test_empty_sync(self) -> None:
        """Test that enabling sticky events extension works on initial and incremental sync,
        even if there is no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                STICKY_EVENTS_EXT: {
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
        """Test that we get all unexpired sticky events on an initial sync (no
        `pos`), and only new ones on subsequent incremental syncs.
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
                STICKY_EVENTS_EXT: {
                    "enabled": True,
                }
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # Do an incremental sync immediately again
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

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
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
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
                STICKY_EVENTS_EXT: {
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
                STICKY_EVENTS_EXT: {
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
                STICKY_EVENTS_EXT: {
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
                STICKY_EVENTS_EXT: {
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
                STICKY_EVENTS_EXT: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user2_tok)

        timeline_events = response_body["rooms"][room_id]["timeline"]
        timeline_event_ids = [e["event_id"] for e in timeline_events]

        # The sticky event is recent enough to be included in timeline instead of in the sticky section.
        self.assertIn(sticky_event_id, timeline_event_ids)
        self._assert_sticky_events_response(response_body, {room_id: []})

        # Sanity check: the regular (non-sticky) event is NOT history-visible.
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
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 2}},
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # We expect to see the first 2 sticky events by stream order
        # and they should be in that stream order
        self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[0:2]}
        )

        # Incremental sync to get remaining sticky events
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                STICKY_EVENTS_EXT: {
                    "enabled": True,
                    "limit": 3,
                }
            },
        }
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )

        # Should get remaining events, in stream order again
        self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[2:4]}
        )

        # And nothing more after that
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # A new sticky event is delivered as normal afterwards
        sticky_event_id = self._send_sticky_event(room_id, user2_tok)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

    def test_sticky_event_limit_zero_does_not_advance(self) -> None:
        """
        Tests that sending a limit of 0 in the Sticky Events request extension returns no
        sticky events and omits the extension from the response, rather than erroring,
        and that doing so doesn't cause any sticky events to be skipped.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Send 2 sticky events (more than our limit of 1)
        sticky_event_ids = [
            self._send_sticky_event(room_id, user2_tok, body=f"sticky message {i}")
            for i in range(2)
        ]

        # Initial sync
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 1}},
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)

        # We expect to see the first sticky event
        self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[0:1]}
        )

        # Incremental sync with limit=0
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 0}},
        }
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # The second sticky event is still delivered once the limit allows it
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 1}},
        }
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(
            response_body, {room_id: sticky_event_ids[1:2]}
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
                STICKY_EVENTS_EXT: {
                    "enabled": True,
                    "limit": 2,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        events_in_sticky_section = response_body["extensions"][STICKY_EVENTS_EXT][
            "rooms"
        ][room_id]["events"]
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

    def test_retry_with_same_pos_redelivers(self) -> None:
        """
        Test that retrying a request with the same `pos` (e.g. because the
        response was lost) delivers the same sticky events again.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True}},
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        sticky_event_id = self._send_sticky_event(room_id, user2_tok)

        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # Retry with the same `pos`
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

    def test_enable_extension_partway_through_connection(self) -> None:
        """
        Test that enabling the extension for the first time partway through a
        connection returns all unexpired sticky events for the rooms in scope,
        including ones sent before the request's `pos`.

        This allows clients to defer enabling the extension past their initial
        request, as required by MSC4508.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sticky_event_id = self._send_sticky_event(room_id, user2_tok)

        # Initial sync and an incremental sync, both without the extension
        sync_body: JsonDict = {"lists": DUMMY_LISTS}
        _, from_token = self.do_sync(sync_body, tok=user1_tok)
        _, from_token = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Now enable the extension: we should get the sticky event sent before
        # the connection even started.
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True}},
        }
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # But not again on the next request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

    def test_reenable_extension_after_disabling(self) -> None:
        """
        Test that re-enabling the extension after having disabled it (or omitted
        it) on the same connection returns the unexpired sticky events that were
        sent while it was disabled, as well as those sent after the request's
        `pos`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        enabled_sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True}},
        }
        disabled_sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": False}},
        }
        omitted_sync_body: JsonDict = {"lists": DUMMY_LISTS}

        _, from_token = self.do_sync(enabled_sync_body, tok=user1_tok)

        # Disable the extension and send a sticky event while it is disabled
        _, from_token = self.do_sync(
            disabled_sync_body, since=from_token, tok=user1_tok
        )
        sticky_event_id1 = self._send_sticky_event(
            room_id, user2_tok, body="while disabled"
        )
        response_body, from_token = self.do_sync(
            disabled_sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Omit the extension altogether and send another sticky event, plus one
        # that will have expired by the time the extension is re-enabled.
        self._send_sticky_event(
            room_id, user2_tok, duration=Duration(seconds=2), body="expires soon"
        )
        sticky_event_id2 = self._send_sticky_event(
            room_id, user2_tok, body="while omitted"
        )
        response_body, from_token = self.do_sync(
            omitted_sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)
        self.reactor.advance(3)

        # Re-enable: the unexpired sticky events sent in the meantime are replayed
        response_body, from_token = self.do_sync(
            enabled_sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id1, sticky_event_id2]}
        )

        # But not again afterwards, and new sticky events are delivered as normal
        response_body, from_token = self.do_sync(
            enabled_sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)
        sticky_event_id3 = self._send_sticky_event(room_id, user2_tok)
        response_body, _ = self.do_sync(
            enabled_sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id3]}
        )

    def test_newly_joined_room_gets_all_unexpired_sticky_events(self) -> None:
        """
        Test that when the user joins a room on an incremental sync, all unexpired
        sticky events in the room are returned, including ones sent before `pos`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        sticky_event_id1 = self._send_sticky_event(room_id, user2_tok, body="one")

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True}},
        }
        # user1 isn't in the room yet, so sees nothing
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

        # user1 joins and sees the historical sticky event
        self.helper.join(room_id, user1_id, tok=user1_tok)
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id1]}
        )

        # user1 leaves, another sticky event is sent, and user1 re-joins: they
        # should get all unexpired sticky events in the room again.
        self.helper.leave(room_id, user1_id, tok=user1_tok)
        _, from_token = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        sticky_event_id2 = self._send_sticky_event(room_id, user2_tok, body="two")
        self.helper.join(room_id, user1_id, tok=user1_tok)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_id1, sticky_event_id2]}
        )

    def test_scoping_by_lists_and_rooms(self) -> None:
        """
        Test that the common per-room extension fields `lists` and `rooms`
        (MSC4508) control which rooms sticky events are returned for.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)
        sticky_event_id = self._send_sticky_event(room_id, user2_tok)

        # No lists in scope, no room subscriptions: nothing is in scope
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                STICKY_EVENTS_EXT: {"enabled": True, "lists": [], "rooms": []}
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

        # A list key that doesn't exist in the request is ignored
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {
                STICKY_EVENTS_EXT: {"enabled": True, "lists": ["other"], "rooms": []}
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

        # Naming the list brings the room into scope
        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "lists": ["main"]}},
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # A room subscription brings the room into scope, even with no lists
        sync_body = {
            "room_subscriptions": {room_id: ROOM_SUBSCRIPTION},
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "lists": []}},
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, {room_id: [sticky_event_id]})

        # ...unless the subscription isn't named in `rooms`
        sync_body = {
            "room_subscriptions": {room_id: ROOM_SUBSCRIPTION},
            "extensions": {
                STICKY_EVENTS_EXT: {"enabled": True, "lists": [], "rooms": []}
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

    def test_room_outside_list_range_is_not_in_scope(self) -> None:
        """
        Test that a room that matches a list but is outside the list's range is
        not in scope for the extension, and that it gets all of its unexpired
        sticky events when it comes into range.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id_a = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_a, user1_id, tok=user1_tok)
        room_id_b = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_b, user1_id, tok=user1_tok)

        # A sticky event in room B, and then a regular message in room A so that
        # room A is the most recent room (and therefore the only one in a range
        # of [0, 0]).
        sticky_event_id = self._send_sticky_event(room_id_b, user2_tok)
        self.helper.send(room_id_a, body="bump A", tok=user2_tok)

        sync_body = {
            "lists": {
                "main": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True}},
        }
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self.assertEqual(
            response_body["lists"]["main"]["ops"][0]["room_ids"], [room_id_a]
        )
        # Room B is out of range, so its sticky event is not returned
        self._assert_sticky_events_response(response_body, None)

        # Bump room B into range: we now get its (old) sticky event
        self.helper.send(room_id_b, body="bump B", tok=user2_tok)
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self.assertEqual(
            response_body["lists"]["main"]["ops"][0]["room_ids"], [room_id_b]
        )
        self._assert_sticky_events_response(
            response_body, {room_id_b: [sticky_event_id]}
        )

        # And not again afterwards
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

    def test_room_leaving_and_reentering_scope(self) -> None:
        """
        Test that when a room drops out of scope and later comes back into scope,
        the sticky events sent in the meantime are returned (unless they have
        expired by then).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id_a = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_a, user1_id, tok=user1_tok)
        room_id_b = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_b, user1_id, tok=user1_tok)

        def sync_body_with_scope(room_ids: list[str]) -> JsonDict:
            return {
                "room_subscriptions": {
                    room_id_a: ROOM_SUBSCRIPTION,
                    room_id_b: ROOM_SUBSCRIPTION,
                },
                "extensions": {
                    STICKY_EVENTS_EXT: {
                        "enabled": True,
                        "lists": [],
                        "rooms": room_ids,
                    }
                },
            }

        # Room A is in scope to begin with
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_a]), tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Room A drops out of scope
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_b]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Sticky events are sent in room A while it is out of scope
        expiring_event_id = self._send_sticky_event(
            room_id_a,
            user2_tok,
            duration=Duration(seconds=2),
            body="expires soon",
        )
        sticky_event_id = self._send_sticky_event(
            room_id_a, user2_tok, body="still sticky"
        )
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_b]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Let the short-lived sticky event expire
        self.reactor.advance(3)

        # Room A comes back into scope: we get the sticky event sent while it was
        # out of scope, but not the one that has expired since.
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_a]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id_a: [sticky_event_id]}
        )
        self.assertNotIn(
            expiring_event_id,
            [
                e["event_id"]
                for e in response_body["extensions"][STICKY_EVENTS_EXT]["rooms"][
                    room_id_a
                ]["events"]
            ],
        )

        # And not again afterwards
        response_body, _ = self.do_sync(
            sync_body_with_scope([room_id_a]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

    def test_pagination_across_rooms_entering_scope(self) -> None:
        """
        Test that the `limit` bounds the response even when a room with many
        unexpired sticky events comes into scope, with the remainder following on
        subsequent requests in stream order, and that this doesn't lose track of
        sticky events in other rooms that are out of scope in the meantime.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id_a = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_a, user1_id, tok=user1_tok)
        room_id_b = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id_b, user1_id, tok=user1_tok)

        def sync_body_with_scope(room_ids: list[str]) -> JsonDict:
            return {
                "room_subscriptions": {
                    room_id_a: ROOM_SUBSCRIPTION,
                    room_id_b: ROOM_SUBSCRIPTION,
                },
                "extensions": {
                    STICKY_EVENTS_EXT: {
                        "enabled": True,
                        "limit": 2,
                        "lists": [],
                        "rooms": room_ids,
                    }
                },
            }

        # 3 sticky events in room B, which is out of scope to begin with
        sticky_event_ids_b = [
            self._send_sticky_event(room_id_b, user2_tok, body=f"B {i}")
            for i in range(3)
        ]
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_a]), tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Room B comes into scope and room A drops out of scope, and a new sticky
        # event arrives in room A (which must not be returned while out of scope).
        sticky_event_id_a = self._send_sticky_event(room_id_a, user2_tok, body="A")

        # First page: the oldest 2 of room B's sticky events
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_b]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id_b: sticky_event_ids_b[0:2]}
        )

        # Second page: the remaining one
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_b]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id_b: sticky_event_ids_b[2:3]}
        )

        # Nothing more after that
        response_body, from_token = self.do_sync(
            sync_body_with_scope([room_id_b]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(response_body, None)

        # Room A comes back into scope and we get the sticky event that was sent
        # while it was out of scope.
        response_body, _ = self.do_sync(
            sync_body_with_scope([room_id_a]), since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id_a: [sticky_event_id_a]}
        )

    def test_live_room_pagination(self) -> None:
        """
        Test that `limit` also bounds the sticky events of rooms that are already
        live on the connection, with the remainder following on subsequent
        requests in stream order and nothing being lost or duplicated.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 2}},
        }
        # The room is now live on the connection
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

        sticky_event_ids = [
            self._send_sticky_event(room_id, user2_tok, body=f"sticky {i}")
            for i in range(5)
        ]

        # The sticky events arrive in pages of 2, in stream order
        for page in (sticky_event_ids[0:2], sticky_event_ids[2:4]):
            response_body, from_token = self.do_sync(
                sync_body, since=from_token, tok=user1_tok
            )
            self._assert_sticky_events_response(response_body, {room_id: page})

        # A new sticky event sent in the meantime is picked up after the backlog
        sticky_event_id = self._send_sticky_event(room_id, user2_tok, body="newest")
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self._assert_sticky_events_response(
            response_body, {room_id: [sticky_event_ids[4], sticky_event_id]}
        )

        # And then nothing more
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)
        self._assert_sticky_events_response(response_body, None)

    def test_per_room_state_is_persisted(self) -> None:
        """
        Test that the per-room sticky events position is persisted in the
        per-connection state in the database (rather than only held in memory),
        so that it survives restarts and is shared between workers.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        def get_persisted_sticky_events_state(pos: str) -> list[tuple[str, str, str]]:
            """Returns (room_id, room_status, last_token) rows for the given `pos`."""
            connection_position = int(pos.split("/", 1)[0])
            return self.get_success(
                self.store.db_pool.simple_select_list(
                    table="sliding_sync_connection_streams",
                    keyvalues={
                        "connection_position": connection_position,
                        "stream": "sticky_events",
                    },
                    retcols=("room_id", "room_status", "last_token"),
                    desc="get_persisted_sticky_events_state",
                )
            )

        for _ in range(3):
            self._send_sticky_event(room_id, user2_tok)

        sync_body = {
            "lists": DUMMY_LISTS,
            "extensions": {STICKY_EVENTS_EXT: {"enabled": True, "limit": 2}},
        }

        # The first page is cut short, so the room is recorded as partially sent,
        # along with the position we got up to.
        _, from_token = self.do_sync(sync_body, tok=user1_tok)
        rows = get_persisted_sticky_events_state(from_token)
        self.assertEqual(len(rows), 1, rows)
        persisted_room_id, room_status, last_token = rows[0]
        self.assertEqual(persisted_room_id, room_id)
        self.assertEqual(room_status, "previously")
        self.assertIsNotNone(last_token)

        # After the remainder is sent, the room is recorded as live (and the
        # persisted position was correctly read back to continue from).
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        self.assertEqual(
            len(
                response_body["extensions"][STICKY_EVENTS_EXT]["rooms"][room_id][
                    "events"
                ]
            ),
            1,
        )
        rows = get_persisted_sticky_events_state(from_token)
        self.assertEqual(rows, [(room_id, "live", None)])
