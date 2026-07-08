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

from parameterized import parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import StrSequence
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


# FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
# foreground update for
# `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
# https://github.com/element-hq/synapse/issues/17623)
@parameterized_class(
    ("use_new_tables",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'new' if params_dict['use_new_tables'] else 'fallback'}",
)
class SlidingSyncRoomsTimelineTestCase(SlidingSyncBase):
    """
    Test `rooms.timeline` in the Sliding Sync API.
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

        super().prepare(reactor, clock, hs)

    def _assertListEqual(
        self,
        actual_items: StrSequence,
        expected_items: StrSequence,
        message: str | None = None,
    ) -> None:
        """
        Like `self.assertListEqual(...)` but with an actually understandable diff message.
        """

        if actual_items == expected_items:
            return

        expected_lines: list[str] = []
        for expected_item in expected_items:
            is_expected_in_actual = expected_item in actual_items
            expected_lines.append(
                "{}  {}".format(" " if is_expected_in_actual else "?", expected_item)
            )

        actual_lines: list[str] = []
        for actual_item in actual_items:
            is_actual_in_expected = actual_item in expected_items
            actual_lines.append(
                "{}  {}".format("+" if is_actual_in_expected else " ", actual_item)
            )

        newline = "\n"
        expected_string = f"Expected items to be in actual ('?' = missing expected items):\n [\n{newline.join(expected_lines)}\n ]"
        actual_string = f"Actual ('+' = found expected items):\n [\n{newline.join(actual_lines)}\n ]"
        first_message = "Items must"
        diff_message = f"{first_message}\n{expected_string}\n{actual_string}"

        self.fail(f"{diff_message}\n{message}")

    def _assertTimelineEqual(
        self,
        *,
        room_id: str,
        actual_event_ids: list[str],
        expected_event_ids: list[str],
        message: str | None = None,
    ) -> None:
        """
        Like `self.assertListEqual(...)` for event IDs in a room but will give a nicer
        output with context for what each event_id is (type, stream_ordering, content,
        etc).
        """
        if actual_event_ids == expected_event_ids:
            return

        event_id_set = set(actual_event_ids + expected_event_ids)
        events = self.get_success(self.store.get_events(event_id_set))

        def event_id_to_string(event_id: str) -> str:
            event = events.get(event_id)
            if event:
                state_key = event.get_state_key()
                state_key_piece = f", {state_key}" if state_key is not None else ""
                return (
                    f"({event.internal_metadata.stream_ordering: >2}, {event.internal_metadata.instance_name}) "
                    + f"{event.event_id} ({event.type}{state_key_piece}) {event.content.get('membership', '')}{event.content.get('body', '')}"
                )

            return f"{event_id} <event not found in room_id={room_id}>"

        self._assertListEqual(
            actual_items=[
                event_id_to_string(event_id) for event_id in actual_event_ids
            ],
            expected_items=[
                event_id_to_string(event_id) for event_id in expected_event_ids
            ],
            message=message,
        )

    def test_rooms_limited_initial_sync(self) -> None:
        """
        Test that we mark `rooms` as `limited=True` when we saturate the `timeline_limit`
        on initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        event_response1 = self.helper.send(room_id1, "activity1", tok=user2_tok)
        event_response2 = self.helper.send(room_id1, "activity2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity4", tok=user2_tok)
        event_response5 = self.helper.send(room_id1, "activity5", tok=user2_tok)
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We expect to saturate the `timeline_limit` (there are more than 3 messages in the room)
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            response_body["rooms"][room_id1],
        )
        # Check to make sure the latest events are returned
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            expected_event_ids=[
                event_response4["event_id"],
                event_response5["event_id"],
                user1_join_response["event_id"],
            ],
            message=str(response_body["rooms"][room_id1]["timeline"]),
        )

        # Check to make sure the `prev_batch` points at the right place
        prev_batch_token = response_body["rooms"][room_id1]["prev_batch"]

        # If we use the `prev_batch` token to look backwards we should see
        # `event3` and older next.
        channel = self.make_request(
            "GET",
            f"/rooms/{room_id1}/messages?from={prev_batch_token}&dir=b&limit=3",
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertListEqual(
            [
                event_response3["event_id"],
                event_response2["event_id"],
                event_response1["event_id"],
            ],
            [ev["event_id"] for ev in channel.json_body["chunk"]],
        )

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" range
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )

    def test_rooms_not_limited_initial_sync(self) -> None:
        """
        Test that we mark `rooms` as `limited=False` when there are no more events to
        paginate to.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity1", tok=user2_tok)
        self.helper.send(room_id1, "activity2", tok=user2_tok)
        self.helper.send(room_id1, "activity3", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        timeline_limit = 100
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # The timeline should be `limited=False` because we have all of the events (no
        # more to paginate to)
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            response_body["rooms"][room_id1],
        )
        expected_number_of_events = 9
        # We're just looking to make sure we got all of the events before hitting the `timeline_limit`
        self.assertEqual(
            len(response_body["rooms"][room_id1]["timeline"]),
            expected_number_of_events,
            response_body["rooms"][room_id1]["timeline"],
        )
        self.assertLessEqual(expected_number_of_events, timeline_limit)

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" token range.
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            0,
            response_body["rooms"][room_id1],
        )

    def test_rooms_incremental_sync(self) -> None:
        """
        Test `rooms` data during an incremental sync after an initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.send(room_id1, "activity before initial sync1", tok=user2_tok)

        # Make an initial Sliding Sync request to grab a token. This is also a sanity
        # check that we can go from initial to incremental sync.
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Send some events but don't send enough to saturate the `timeline_limit`.
        # We want to later test that we only get the new events since the `next_pos`
        event_response2 = self.helper.send(room_id1, "activity after2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)

        # Make an incremental Sliding Sync request (what we're trying to test)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We only expect to see the new events since the last sync which isn't enough to
        # fill up the `timeline_limit`.
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            f"Our `timeline_limit` was {sync_body['lists']['foo-list']['timeline_limit']} "
            + f"and {len(response_body['rooms'][room_id1]['timeline'])} events were returned in the timeline. "
            + str(response_body["rooms"][room_id1]),
        )
        # Check to make sure the latest events are returned
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            expected_event_ids=[
                event_response2["event_id"],
                event_response3["event_id"],
            ],
            message=str(response_body["rooms"][room_id1]["timeline"]),
        )

        # All events are "live"
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            2,
            response_body["rooms"][room_id1],
        )

    def test_rooms_newly_joined_incremental_sync(self) -> None:
        """
        Test that when we make an incremental sync with a `newly_joined` `rooms`, we are
        able to see some historical events before the `from_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before token1", tok=user2_tok)
        event_response2 = self.helper.send(
            room_id1, "activity before token2", tok=user2_tok
        )

        # The `timeline_limit` is set to 4 so we can at least see one historical event
        # before the `from_token`. We should see historical events because this is a
        # `newly_joined` room.
        timeline_limit = 4
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": timeline_limit,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Join the room after the `from_token` which will make us consider this room as
        # `newly_joined`.
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Send some events but don't send enough to saturate the `timeline_limit`.
        # We want to later test that we only get the new events since the `next_pos`
        event_response3 = self.helper.send(
            room_id1, "activity after token3", tok=user2_tok
        )
        event_response4 = self.helper.send(
            room_id1, "activity after token4", tok=user2_tok
        )

        # Make an incremental Sliding Sync request (what we're trying to test)
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should see the new events and the rest should be filled with historical
        # events which will make us `limited=True` since there are more to paginate to.
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            True,
            f"Our `timeline_limit` was {timeline_limit} "
            + f"and {len(response_body['rooms'][room_id1]['timeline'])} events were returned in the timeline. "
            + str(response_body["rooms"][room_id1]),
        )
        # Check to make sure that the "live" and historical events are returned
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            expected_event_ids=[
                event_response2["event_id"],
                user1_join_response["event_id"],
                event_response3["event_id"],
                event_response4["event_id"],
            ],
            message=str(response_body["rooms"][room_id1]["timeline"]),
        )

        # Only events after the `from_token` are "live" (join, event3, event4)
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            3,
            response_body["rooms"][room_id1],
        )

    def test_rooms_ban_initial_sync(self) -> None:
        """
        Test that `rooms` we are banned from in an intial sync only allows us to see
        timeline events up to the ban event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity after4", tok=user2_tok)
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        self.helper.send(room_id1, "activity after5", tok=user2_tok)
        self.helper.send(room_id1, "activity after6", tok=user2_tok)

        # Make the Sliding Sync request
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # We should see events before the ban but not after
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            expected_event_ids=[
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            message=str(response_body["rooms"][room_id1]["timeline"]),
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

    def test_rooms_ban_incremental_sync1(self) -> None:
        """
        Test that `rooms` we are banned from during the next incremental sync only
        allows us to see timeline events up to the ban event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 4,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity after4", tok=user2_tok)
        # The ban is within the token range (between the `from_token` and the sliding
        # sync request)
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        self.helper.send(room_id1, "activity after5", tok=user2_tok)
        self.helper.send(room_id1, "activity after6", tok=user2_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # We should see events before the ban but not after
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[
                event["event_id"]
                for event in response_body["rooms"][room_id1]["timeline"]
            ],
            expected_event_ids=[
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            message=str(response_body["rooms"][room_id1]["timeline"]),
        )
        # All live events in the incremental sync
        self.assertEqual(
            response_body["rooms"][room_id1]["num_live"],
            3,
            response_body["rooms"][room_id1],
        )
        # There aren't anymore events to paginate to in this range
        self.assertEqual(
            response_body["rooms"][room_id1]["limited"],
            False,
            response_body["rooms"][room_id1],
        )

    def test_rooms_ban_incremental_sync2(self) -> None:
        """
        Test that `rooms` we are banned from before the incremental sync don't return
        any events in the timeline.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send(room_id1, "activity after2", tok=user2_tok)
        # The ban is before we get our `from_token`
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        self.helper.send(room_id1, "activity after3", tok=user2_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 4,
                }
            }
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        # Nothing to see for this banned user in the room in the token range
        self.assertIsNone(response_body["rooms"].get(room_id1))

    def test_increasing_timeline_range_sends_more_messages(self) -> None:
        """
        Test that increasing the timeline limit via room subscriptions sends the
        room down with more messages in a limited sync.
        """

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [[EventTypes.Create, ""]],
                    "timeline_limit": 1,
                }
            }
        }

        message_events = []
        for _ in range(10):
            resp = self.helper.send(room_id1, "msg", tok=user1_tok)
            message_events.append(resp["event_id"])

        # Make the first Sliding Sync request
        response_body, from_token = self.do_sync(sync_body, tok=user1_tok)
        room_response = response_body["rooms"][room_id1]

        self.assertEqual(room_response["initial"], True)
        self.assertNotIn("unstable_expanded_timeline", room_response)
        self.assertEqual(room_response["limited"], True)

        # We only expect the last message at first
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[event["event_id"] for event in room_response["timeline"]],
            expected_event_ids=message_events[-1:],
            message=str(room_response["timeline"]),
        )

        # We also expect to get the create event state.
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )
        self._assertRequiredStateIncludes(
            room_response["required_state"],
            {state_map[(EventTypes.Create, "")]},
            exact=True,
        )

        # Now do another request with a room subscription with an increased timeline limit
        sync_body["room_subscriptions"] = {
            room_id1: {
                "required_state": [],
                "timeline_limit": 10,
            }
        }

        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        room_response = response_body["rooms"][room_id1]

        self.assertNotIn("initial", room_response)
        self.assertEqual(room_response["unstable_expanded_timeline"], True)
        self.assertEqual(room_response["limited"], True)

        # Now we expect all the messages
        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[event["event_id"] for event in room_response["timeline"]],
            expected_event_ids=message_events,
            message=str(room_response["timeline"]),
        )

        # We don't expect to get the room create down, as nothing has changed.
        self.assertNotIn("required_state", room_response)

        # Decreasing the timeline limit shouldn't resend any events
        sync_body["room_subscriptions"] = {
            room_id1: {
                "required_state": [],
                "timeline_limit": 5,
            }
        }

        event_response = self.helper.send(room_id1, "msg", tok=user1_tok)
        latest_event_id = event_response["event_id"]

        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        room_response = response_body["rooms"][room_id1]

        self.assertNotIn("initial", room_response)
        self.assertNotIn("unstable_expanded_timeline", room_response)
        self.assertEqual(room_response["limited"], False)

        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[event["event_id"] for event in room_response["timeline"]],
            expected_event_ids=[latest_event_id],
            message=str(room_response["timeline"]),
        )

        # Increasing the limit to what it was before also should not resend any
        # events
        sync_body["room_subscriptions"] = {
            room_id1: {
                "required_state": [],
                "timeline_limit": 10,
            }
        }

        event_response = self.helper.send(room_id1, "msg", tok=user1_tok)
        latest_event_id = event_response["event_id"]

        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=user1_tok
        )
        room_response = response_body["rooms"][room_id1]

        self.assertNotIn("initial", room_response)
        self.assertNotIn("unstable_expanded_timeline", room_response)
        self.assertEqual(room_response["limited"], False)

        self._assertTimelineEqual(
            room_id=room_id1,
            actual_event_ids=[event["event_id"] for event in room_response["timeline"]],
            expected_event_ids=[latest_event_id],
            message=str(room_response["timeline"]),
        )
