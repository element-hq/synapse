#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import logging
from http import HTTPStatus
from typing import Any, Dict, Iterable, List

from parameterized import parameterized, parameterized_class

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    HistoryVisibility,
    Membership,
    ReceiptTypes,
    RelationTypes,
)
from synapse.events import EventBase
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import (
    devices,
    knock,
    login,
    read_marker,
    receipts,
    room,
    sendtodevice,
    sync,
)
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomStreamToken, StreamKeyType, StreamToken, UserID
from synapse.util import Clock

from tests import unittest
from tests.federation.transport.test_knocking import (
    KnockingStrippedStateEventHelperMixin,
)
from tests.server import FakeChannel, TimedOutException
from tests.test_utils.event_injection import mark_event_as_partial_state
from tests.unittest import skip_unless

logger = logging.getLogger(__name__)


class FilterTestCase(unittest.HomeserverTestCase):
    user_id = "@apple:test"
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def test_sync_argless(self) -> None:
        channel = self.make_request("GET", "/sync")

        self.assertEqual(channel.code, 200)
        self.assertIn("next_batch", channel.json_body)


class SyncFilterTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def test_sync_filter_labels(self) -> None:
        """Test that we can filter by a label."""
        sync_filter = json.dumps(
            {
                "room": {
                    "timeline": {
                        "types": [EventTypes.Message],
                        "org.matrix.labels": ["#fun"],
                    }
                }
            }
        )

        events = self._test_sync_filter_labels(sync_filter)

        self.assertEqual(len(events), 2, [event["content"] for event in events])
        self.assertEqual(events[0]["content"]["body"], "with right label", events[0])
        self.assertEqual(events[1]["content"]["body"], "with right label", events[1])

    def test_sync_filter_not_labels(self) -> None:
        """Test that we can filter by the absence of a label."""
        sync_filter = json.dumps(
            {
                "room": {
                    "timeline": {
                        "types": [EventTypes.Message],
                        "org.matrix.not_labels": ["#fun"],
                    }
                }
            }
        )

        events = self._test_sync_filter_labels(sync_filter)

        self.assertEqual(len(events), 3, [event["content"] for event in events])
        self.assertEqual(events[0]["content"]["body"], "without label", events[0])
        self.assertEqual(events[1]["content"]["body"], "with wrong label", events[1])
        self.assertEqual(
            events[2]["content"]["body"], "with two wrong labels", events[2]
        )

    def test_sync_filter_labels_not_labels(self) -> None:
        """Test that we can filter by both a label and the absence of another label."""
        sync_filter = json.dumps(
            {
                "room": {
                    "timeline": {
                        "types": [EventTypes.Message],
                        "org.matrix.labels": ["#work"],
                        "org.matrix.not_labels": ["#notfun"],
                    }
                }
            }
        )

        events = self._test_sync_filter_labels(sync_filter)

        self.assertEqual(len(events), 1, [event["content"] for event in events])
        self.assertEqual(events[0]["content"]["body"], "with wrong label", events[0])

    def _test_sync_filter_labels(self, sync_filter: str) -> List[JsonDict]:
        user_id = self.register_user("kermit", "test")
        tok = self.login("kermit", "test")

        room_id = self.helper.create_room_as(user_id, tok=tok)

        self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "with right label",
                EventContentFields.LABELS: ["#fun"],
            },
            tok=tok,
        )

        self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "without label"},
            tok=tok,
        )

        self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "with wrong label",
                EventContentFields.LABELS: ["#work"],
            },
            tok=tok,
        )

        self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "with two wrong labels",
                EventContentFields.LABELS: ["#work", "#notfun"],
            },
            tok=tok,
        )

        self.helper.send_event(
            room_id=room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "with right label",
                EventContentFields.LABELS: ["#fun"],
            },
            tok=tok,
        )

        channel = self.make_request(
            "GET", "/sync?filter=%s" % sync_filter, access_token=tok
        )
        self.assertEqual(channel.code, 200, channel.result)

        return channel.json_body["rooms"]["join"][room_id]["timeline"]["events"]


class SyncTypingTests(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]
    user_id = True
    hijack_auth = False

    def test_sync_backwards_typing(self) -> None:
        """
        If the typing serial goes backwards and the typing handler is then reset
        (such as when the master restarts and sets the typing serial to 0), we
        do not incorrectly return typing information that had a serial greater
        than the now-reset serial.
        """
        typing_url = "/rooms/%s/typing/%s?access_token=%s"
        sync_url = "/sync?timeout=3000000&access_token=%s&since=%s"

        # Register the user who gets notified
        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        # Register the user who sends the message
        other_user_id = self.register_user("otheruser", "pass")
        other_access_token = self.login("otheruser", "pass")

        # Create a room
        room = self.helper.create_room_as(user_id, tok=access_token)

        # Invite the other person
        self.helper.invite(room=room, src=user_id, tok=access_token, targ=other_user_id)

        # The other user joins
        self.helper.join(room=room, user=other_user_id, tok=other_access_token)

        # The other user sends some messages
        self.helper.send(room, body="Hi!", tok=other_access_token)
        self.helper.send(room, body="There!", tok=other_access_token)

        # Start typing.
        channel = self.make_request(
            "PUT",
            typing_url % (room, other_user_id, other_access_token),
            b'{"typing": true, "timeout": 30000}',
        )
        self.assertEqual(200, channel.code)

        channel = self.make_request("GET", "/sync?access_token=%s" % (access_token,))
        self.assertEqual(200, channel.code)
        next_batch = channel.json_body["next_batch"]

        # Stop typing.
        channel = self.make_request(
            "PUT",
            typing_url % (room, other_user_id, other_access_token),
            b'{"typing": false}',
        )
        self.assertEqual(200, channel.code)

        # Start typing.
        channel = self.make_request(
            "PUT",
            typing_url % (room, other_user_id, other_access_token),
            b'{"typing": true, "timeout": 30000}',
        )
        self.assertEqual(200, channel.code)

        # Should return immediately
        channel = self.make_request("GET", sync_url % (access_token, next_batch))
        self.assertEqual(200, channel.code)
        next_batch = channel.json_body["next_batch"]

        # Reset typing serial back to 0, as if the master had.
        typing = self.hs.get_typing_handler()
        typing._latest_room_serial = 0

        # Since it checks the state token, we need some state to update to
        # invalidate the stream token.
        self.helper.send(room, body="There!", tok=other_access_token)

        channel = self.make_request("GET", sync_url % (access_token, next_batch))
        self.assertEqual(200, channel.code)
        next_batch = channel.json_body["next_batch"]

        # This should time out! But it does not, because our stream token is
        # ahead, and therefore it's saying the typing (that we've actually
        # already seen) is new, since it's got a token above our new, now-reset
        # stream token.
        channel = self.make_request("GET", sync_url % (access_token, next_batch))
        self.assertEqual(200, channel.code)
        next_batch = channel.json_body["next_batch"]

        # Clear the typing information, so that it doesn't think everything is
        # in the future.
        typing._reset()

        # Now it SHOULD fail as it never completes!
        with self.assertRaises(TimedOutException):
            self.make_request("GET", sync_url % (access_token, next_batch))


class SyncKnockTestCase(KnockingStrippedStateEventHelperMixin):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        knock.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.url = "/sync?since=%s"
        self.next_batch = "s0"

        # Register the first user (used to create the room to knock on).
        self.user_id = self.register_user("kermit", "monkey")
        self.tok = self.login("kermit", "monkey")

        # Create the room we'll knock on.
        self.room_id = self.helper.create_room_as(
            self.user_id,
            is_public=False,
            room_version="7",
            tok=self.tok,
        )

        # Register the second user (used to knock on the room).
        self.knocker = self.register_user("knocker", "monkey")
        self.knocker_tok = self.login("knocker", "monkey")

        # Perform an initial sync for the knocking user.
        channel = self.make_request(
            "GET",
            self.url % self.next_batch,
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Store the next batch for the next request.
        self.next_batch = channel.json_body["next_batch"]

        # Set up some room state to test with.
        self.expected_room_state = self.send_example_state_events_to_room(
            hs, self.room_id, self.user_id
        )

    def test_knock_room_state(self) -> None:
        """Tests that /sync returns state from a room after knocking on it."""
        # Knock on a room
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/knock/{self.room_id}",
            b"{}",
            self.knocker_tok,
        )
        self.assertEqual(200, channel.code, channel.result)

        # We expect to see the knock event in the stripped room state later
        self.expected_room_state[EventTypes.Member] = {
            "content": {"membership": "knock", "displayname": "knocker"},
            "state_key": "@knocker:test",
        }

        # Check that /sync includes stripped state from the room
        channel = self.make_request(
            "GET",
            self.url % self.next_batch,
            access_token=self.knocker_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Extract the stripped room state events from /sync
        knock_entry = channel.json_body["rooms"]["knock"]
        room_state_events = knock_entry[self.room_id]["knock_state"]["events"]

        # Validate that the knock membership event came last
        self.assertEqual(room_state_events[-1]["type"], EventTypes.Member)

        # Validate the stripped room state events
        self.check_knock_room_state_against_room_state(
            room_state_events, self.expected_room_state
        )


class UnreadMessagesTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        read_marker.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        receipts.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {
            "msc2654_enabled": True,
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.url = "/sync?since=%s"
        self.next_batch = "s0"

        # Register the first user (used to check the unread counts).
        self.user_id = self.register_user("kermit", "monkey")
        self.tok = self.login("kermit", "monkey")

        # Create the room we'll check unread counts for.
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.tok)

        # Register the second user (used to send events to the room).
        self.user2 = self.register_user("kermit2", "monkey")
        self.tok2 = self.login("kermit2", "monkey")

        # Change the power levels of the room so that the second user can send state
        # events.
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            {
                "users": {self.user_id: 100, self.user2: 100},
                "users_default": 0,
                "events": {
                    "m.room.name": 50,
                    "m.room.power_levels": 100,
                    "m.room.history_visibility": 100,
                    "m.room.canonical_alias": 50,
                    "m.room.avatar": 50,
                    "m.room.tombstone": 100,
                    "m.room.server_acl": 100,
                    "m.room.encryption": 100,
                },
                "events_default": 0,
                "state_default": 50,
                "ban": 50,
                "kick": 50,
                "redact": 50,
                "invite": 0,
            },
            tok=self.tok,
        )

    def test_unread_counts(self) -> None:
        """Tests that /sync returns the right value for the unread count (MSC2654)."""

        # Check that our own messages don't increase the unread count.
        self.helper.send(self.room_id, "hello", tok=self.tok)
        self._check_unread_count(0)

        # Join the new user and check that this doesn't increase the unread count.
        self.helper.join(room=self.room_id, user=self.user2, tok=self.tok2)
        self._check_unread_count(0)

        # Check that the new user sending a message increases our unread count.
        res = self.helper.send(self.room_id, "hello", tok=self.tok2)
        self._check_unread_count(1)

        # Send a read receipt to tell the server we've read the latest event.
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/read_markers",
            {ReceiptTypes.READ: res["event_id"]},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check that the unread counter is back to 0.
        self._check_unread_count(0)

        # Check that private read receipts don't break unread counts
        res = self.helper.send(self.room_id, "hello", tok=self.tok2)
        self._check_unread_count(1)

        # Send a read receipt to tell the server we've read the latest event.
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/{ReceiptTypes.READ_PRIVATE}/{res['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check that the unread counter is back to 0.
        self._check_unread_count(0)

        # Check that room name changes increase the unread counter.
        self.helper.send_state(
            self.room_id,
            "m.room.name",
            {"name": "my super room"},
            tok=self.tok2,
        )
        self._check_unread_count(1)

        # Check that room topic changes increase the unread counter.
        self.helper.send_state(
            self.room_id,
            "m.room.topic",
            {"topic": "welcome!!!"},
            tok=self.tok2,
        )
        self._check_unread_count(2)

        # Check that encrypted messages increase the unread counter.
        self.helper.send_event(self.room_id, EventTypes.Encrypted, {}, tok=self.tok2)
        self._check_unread_count(3)

        # Check that custom events with a body increase the unread counter.
        result = self.helper.send_event(
            self.room_id,
            "org.matrix.custom_type",
            {"body": "hello"},
            tok=self.tok2,
        )
        event_id = result["event_id"]
        self._check_unread_count(4)

        # Check that edits don't increase the unread counter.
        self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "body": "hello",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": RelationTypes.REPLACE,
                    "event_id": event_id,
                },
            },
            tok=self.tok2,
        )
        self._check_unread_count(4)

        # Check that notices don't increase the unread counter.
        self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={"body": "hello", "msgtype": "m.notice"},
            tok=self.tok2,
        )
        self._check_unread_count(4)

        # Check that tombstone events changes increase the unread counter.
        res1 = self.helper.send_state(
            self.room_id,
            EventTypes.Tombstone,
            {"replacement_room": "!someroom:test"},
            tok=self.tok2,
        )
        self._check_unread_count(5)
        res2 = self.helper.send(self.room_id, "hello", tok=self.tok2)

        # Make sure both m.read and m.read.private advance
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/m.read/{res1['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._check_unread_count(1)

        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/{ReceiptTypes.READ_PRIVATE}/{res2['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._check_unread_count(0)

    # We test for all three receipt types that influence notification counts
    @parameterized.expand(
        [
            ReceiptTypes.READ,
            ReceiptTypes.READ_PRIVATE,
        ]
    )
    def test_read_receipts_only_go_down(self, receipt_type: str) -> None:
        # Join the new user
        self.helper.join(room=self.room_id, user=self.user2, tok=self.tok2)

        # Send messages
        res1 = self.helper.send(self.room_id, "hello", tok=self.tok2)
        res2 = self.helper.send(self.room_id, "hello", tok=self.tok2)

        # Read last event
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/{ReceiptTypes.READ_PRIVATE}/{res2['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._check_unread_count(0)

        # Make sure neither m.read nor m.read.private make the
        # read receipt go up to an older event
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/{ReceiptTypes.READ_PRIVATE}/{res1['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._check_unread_count(0)

        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/m.read/{res1['event_id']}",
            {},
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._check_unread_count(0)

    def _check_unread_count(self, expected_count: int) -> None:
        """Syncs and compares the unread count with the expected value."""

        channel = self.make_request(
            "GET",
            self.url % self.next_batch,
            access_token=self.tok,
        )

        self.assertEqual(channel.code, 200, channel.json_body)

        room_entry = (
            channel.json_body.get("rooms", {}).get("join", {}).get(self.room_id, {})
        )
        self.assertEqual(
            room_entry.get("org.matrix.msc2654.unread_count", 0),
            expected_count,
            room_entry,
        )

        # Store the next batch for the next request.
        self.next_batch = channel.json_body["next_batch"]


class SyncCacheTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def test_noop_sync_does_not_tightloop(self) -> None:
        """If the sync times out, we shouldn't cache the result

        Essentially a regression test for https://github.com/matrix-org/synapse/issues/8518.
        """
        self.user_id = self.register_user("kermit", "monkey")
        self.tok = self.login("kermit", "monkey")

        # we should immediately get an initial sync response
        channel = self.make_request("GET", "/sync", access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.json_body)

        # now, make an incremental sync request, with a timeout
        next_batch = channel.json_body["next_batch"]
        channel = self.make_request(
            "GET",
            f"/sync?since={next_batch}&timeout=10000",
            access_token=self.tok,
            await_result=False,
        )
        # that should block for 10 seconds
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=9900)
        channel.await_result(timeout_ms=200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # we expect the next_batch in the result to be the same as before
        self.assertEqual(channel.json_body["next_batch"], next_batch)

        # another incremental sync should also block.
        channel = self.make_request(
            "GET",
            f"/sync?since={next_batch}&timeout=10000",
            access_token=self.tok,
            await_result=False,
        )
        # that should block for 10 seconds
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=9900)
        channel.await_result(timeout_ms=200)
        self.assertEqual(channel.code, 200, channel.json_body)


@parameterized_class(
    ("sync_endpoint", "experimental_features"),
    [
        ("/sync", {}),
        (
            "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee",
            # Enable sliding sync
            {"msc3575_enabled": True},
        ),
    ],
)
class DeviceListSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device list (`device_lists`) changes.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    sync_endpoint: str
    experimental_features: JsonDict

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = self.experimental_features
        return config

    def test_receiving_local_device_list_changes(self) -> None:
        """Tests that a local users that share a room receive each other's device list
        changes.
        """
        # Register two users
        test_device_id = "TESTDEVICE"
        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        bob_user_id = self.register_user("bob", "ponyponypony")
        bob_access_token = self.login(bob_user_id, "ponyponypony")

        # Create a room for them to coexist peacefully in
        new_room_id = self.helper.create_room_as(
            alice_user_id, is_public=True, tok=alice_access_token
        )
        self.assertIsNotNone(new_room_id)

        # Have Bob join the room
        self.helper.invite(
            new_room_id, alice_user_id, bob_user_id, tok=alice_access_token
        )
        self.helper.join(new_room_id, bob_user_id, tok=bob_access_token)

        # Now have Bob initiate an initial sync (in order to get a since token)
        channel = self.make_request(
            "GET",
            self.sync_endpoint,
            access_token=bob_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch_token = channel.json_body["next_batch"]

        # ...and then an incremental sync. This should block until the sync stream is woken up,
        # which we hope will happen as a result of Alice updating their device list.
        bob_sync_channel = self.make_request(
            "GET",
            f"{self.sync_endpoint}?since={next_batch_token}&timeout=30000",
            access_token=bob_access_token,
            # Start the request, then continue on.
            await_result=False,
        )

        # Have alice update their device list
        channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=alice_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check that bob's incremental sync contains the updated device list.
        # If not, the client would only receive the device list update on the
        # *next* sync.
        bob_sync_channel.await_result()
        self.assertEqual(bob_sync_channel.code, 200, bob_sync_channel.json_body)

        changed_device_lists = bob_sync_channel.json_body.get("device_lists", {}).get(
            "changed", []
        )
        self.assertIn(alice_user_id, changed_device_lists, bob_sync_channel.json_body)

    def test_not_receiving_local_device_list_changes(self) -> None:
        """Tests a local users DO NOT receive device updates from each other if they do not
        share a room.
        """
        # Register two users
        test_device_id = "TESTDEVICE"
        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        bob_user_id = self.register_user("bob", "ponyponypony")
        bob_access_token = self.login(bob_user_id, "ponyponypony")

        # These users do not share a room. They are lonely.

        # Have Bob initiate an initial sync (in order to get a since token)
        channel = self.make_request(
            "GET",
            self.sync_endpoint,
            access_token=bob_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch_token = channel.json_body["next_batch"]

        # ...and then an incremental sync. This should block until the sync stream is woken up,
        # which we hope will happen as a result of Alice updating their device list.
        bob_sync_channel = self.make_request(
            "GET",
            f"{self.sync_endpoint}?since={next_batch_token}&timeout=1000",
            access_token=bob_access_token,
            # Start the request, then continue on.
            await_result=False,
        )

        # Have alice update their device list
        channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=alice_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check that bob's incremental sync does not contain the updated device list.
        bob_sync_channel.await_result()
        self.assertEqual(bob_sync_channel.code, 200, bob_sync_channel.json_body)

        changed_device_lists = bob_sync_channel.json_body.get("device_lists", {}).get(
            "changed", []
        )
        self.assertNotIn(
            alice_user_id, changed_device_lists, bob_sync_channel.json_body
        )

    def test_user_with_no_rooms_receives_self_device_list_updates(self) -> None:
        """Tests that a user with no rooms still receives their own device list updates"""
        test_device_id = "TESTDEVICE"

        # Register a user and login, creating a device
        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        # Request an initial sync
        channel = self.make_request(
            "GET", self.sync_endpoint, access_token=alice_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch = channel.json_body["next_batch"]

        # Now, make an incremental sync request.
        # It won't return until something has happened
        incremental_sync_channel = self.make_request(
            "GET",
            f"{self.sync_endpoint}?since={next_batch}&timeout=30000",
            access_token=alice_access_token,
            await_result=False,
        )

        # Change our device's display name
        channel = self.make_request(
            "PUT",
            f"devices/{test_device_id}",
            {
                "display_name": "freeze ray",
            },
            access_token=alice_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # The sync should now have returned
        incremental_sync_channel.await_result(timeout_ms=20000)
        self.assertEqual(incremental_sync_channel.code, 200, channel.json_body)

        # We should have received notification that the (user's) device has changed
        device_list_changes = incremental_sync_channel.json_body.get(
            "device_lists", {}
        ).get("changed", [])

        self.assertIn(
            alice_user_id, device_list_changes, incremental_sync_channel.json_body
        )


@parameterized_class(
    ("sync_endpoint", "experimental_features"),
    [
        ("/sync", {}),
        (
            "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee",
            # Enable sliding sync
            {"msc3575_enabled": True},
        ),
    ],
)
class DeviceOneTimeKeysSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device one time keys (`device_one_time_keys_count`) changes.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    sync_endpoint: str
    experimental_features: JsonDict

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = self.experimental_features
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    def test_no_device_one_time_keys(self) -> None:
        """
        Tests when no one time keys set, it still has the default `signed_curve25519` in
        `device_one_time_keys_count`
        """
        test_device_id = "TESTDEVICE"

        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        # Request an initial sync
        channel = self.make_request(
            "GET", self.sync_endpoint, access_token=alice_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for those one time key counts
        self.assertDictEqual(
            channel.json_body["device_one_time_keys_count"],
            # Note that "signed_curve25519" is always returned in key count responses
            # regardless of whether we uploaded any keys for it. This is necessary until
            # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
            {"signed_curve25519": 0},
            channel.json_body["device_one_time_keys_count"],
        )

    def test_returns_device_one_time_keys(self) -> None:
        """
        Tests that one time keys for the device/user are counted correctly in the `/sync`
        response
        """
        test_device_id = "TESTDEVICE"

        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        # Upload one time keys for the user/device
        keys: JsonDict = {
            "alg1:k1": "key1",
            "alg2:k2": {"key": "key2", "signatures": {"k1": "sig1"}},
            "alg2:k3": {"key": "key3"},
        }
        res = self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                alice_user_id, test_device_id, {"one_time_keys": keys}
            )
        )
        # Note that "signed_curve25519" is always returned in key count responses
        # regardless of whether we uploaded any keys for it. This is necessary until
        # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
        self.assertDictEqual(
            res,
            {"one_time_key_counts": {"alg1": 1, "alg2": 2, "signed_curve25519": 0}},
        )

        # Request an initial sync
        channel = self.make_request(
            "GET", self.sync_endpoint, access_token=alice_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for those one time key counts
        self.assertDictEqual(
            channel.json_body["device_one_time_keys_count"],
            {"alg1": 1, "alg2": 2, "signed_curve25519": 0},
            channel.json_body["device_one_time_keys_count"],
        )


@parameterized_class(
    ("sync_endpoint", "experimental_features"),
    [
        ("/sync", {}),
        (
            "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee",
            # Enable sliding sync
            {"msc3575_enabled": True},
        ),
    ],
)
class DeviceUnusedFallbackKeySyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device one time keys (`device_unused_fallback_key_types`) changes.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    sync_endpoint: str
    experimental_features: JsonDict

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = self.experimental_features
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    def test_no_device_unused_fallback_key(self) -> None:
        """
        Test when no unused fallback key is set, it just returns an empty list. The MSC
        says "The device_unused_fallback_key_types parameter must be present if the
        server supports fallback keys.",
        https://github.com/matrix-org/matrix-spec-proposals/blob/54255851f642f84a4f1aaf7bc063eebe3d76752b/proposals/2732-olm-fallback-keys.md
        """
        test_device_id = "TESTDEVICE"

        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        # Request an initial sync
        channel = self.make_request(
            "GET", self.sync_endpoint, access_token=alice_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for those one time key counts
        self.assertListEqual(
            channel.json_body["device_unused_fallback_key_types"],
            [],
            channel.json_body["device_unused_fallback_key_types"],
        )

    def test_returns_device_one_time_keys(self) -> None:
        """
        Tests that device unused fallback key type is returned correctly in the `/sync`
        """
        test_device_id = "TESTDEVICE"

        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(
            alice_user_id, "correcthorse", device_id=test_device_id
        )

        # We shouldn't have any unused fallback keys yet
        res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(alice_user_id, test_device_id)
        )
        self.assertEqual(res, [])

        # Upload a fallback key for the user/device
        self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                alice_user_id,
                test_device_id,
                {"fallback_keys": {"alg1:k1": "fallback_key1"}},
            )
        )
        # We should now have an unused alg1 key
        fallback_res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(alice_user_id, test_device_id)
        )
        self.assertEqual(fallback_res, ["alg1"], fallback_res)

        # Request an initial sync
        channel = self.make_request(
            "GET", self.sync_endpoint, access_token=alice_access_token
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for the unused fallback key types
        self.assertListEqual(
            channel.json_body["device_unused_fallback_key_types"],
            ["alg1"],
            channel.json_body["device_unused_fallback_key_types"],
        )


class ExcludeRoomTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        room.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.user_id = self.register_user("user", "password")
        self.tok = self.login("user", "password")

        self.excluded_room_id = self.helper.create_room_as(self.user_id, tok=self.tok)
        self.included_room_id = self.helper.create_room_as(self.user_id, tok=self.tok)

        # We need to manually append the room ID, because we can't know the ID before
        # creating the room, and we can't set the config after starting the homeserver.
        self.hs.get_sync_handler().rooms_to_exclude_globally.append(
            self.excluded_room_id
        )

    def test_join_leave(self) -> None:
        """Tests that rooms are correctly excluded from the 'join' and 'leave' sections of
        sync responses.
        """
        channel = self.make_request("GET", "/sync", access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.result)

        self.assertNotIn(self.excluded_room_id, channel.json_body["rooms"]["join"])
        self.assertIn(self.included_room_id, channel.json_body["rooms"]["join"])

        self.helper.leave(self.excluded_room_id, self.user_id, tok=self.tok)
        self.helper.leave(self.included_room_id, self.user_id, tok=self.tok)

        channel = self.make_request(
            "GET",
            "/sync?since=" + channel.json_body["next_batch"],
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.assertNotIn(self.excluded_room_id, channel.json_body["rooms"]["leave"])
        self.assertIn(self.included_room_id, channel.json_body["rooms"]["leave"])

    def test_invite(self) -> None:
        """Tests that rooms are correctly excluded from the 'invite' section of sync
        responses.
        """
        invitee = self.register_user("invitee", "password")
        invitee_tok = self.login("invitee", "password")

        self.helper.invite(self.excluded_room_id, self.user_id, invitee, tok=self.tok)
        self.helper.invite(self.included_room_id, self.user_id, invitee, tok=self.tok)

        channel = self.make_request("GET", "/sync", access_token=invitee_tok)
        self.assertEqual(channel.code, 200, channel.result)

        self.assertNotIn(self.excluded_room_id, channel.json_body["rooms"]["invite"])
        self.assertIn(self.included_room_id, channel.json_body["rooms"]["invite"])

    def test_incremental_sync(self) -> None:
        """Tests that activity in the room is properly filtered out of incremental
        syncs.
        """
        channel = self.make_request("GET", "/sync", access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.result)
        next_batch = channel.json_body["next_batch"]

        self.helper.send(self.excluded_room_id, tok=self.tok)
        self.helper.send(self.included_room_id, tok=self.tok)

        channel = self.make_request(
            "GET",
            f"/sync?since={next_batch}",
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.assertNotIn(self.excluded_room_id, channel.json_body["rooms"]["join"])
        self.assertIn(self.included_room_id, channel.json_body["rooms"]["join"])


class SlidingSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding MSC3575 Sliding Sync `/sync` endpoint.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.sync_endpoint = (
            "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"
        )
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.storage_controllers = hs.get_storage_controllers()
        self.account_data_handler = hs.get_account_data_handler()
        self.notifier = hs.get_notifier()

    def _assertRequiredStateIncludes(
        self,
        actual_required_state: Any,
        expected_state_events: Iterable[EventBase],
        exact: bool = False,
    ) -> None:
        """
        Wrapper around `assertIncludes` to give slightly better looking diff error
        messages that include some context "$event_id (type, state_key)".

        Args:
            actual_required_state: The "required_state" of a room from a Sliding Sync
                request response.
            expected_state_events: The expected state events to be included in the
                `actual_required_state`.
            exact: Whether the actual state should be exactly equal to the expected
                state (no extras).
        """

        assert isinstance(actual_required_state, list)
        for event in actual_required_state:
            assert isinstance(event, dict)

        self.assertIncludes(
            {
                f'{event["event_id"]} ("{event["type"]}", "{event["state_key"]}")'
                for event in actual_required_state
            },
            {
                f'{event.event_id} ("{event.type}", "{event.state_key}")'
                for event in expected_state_events
            },
            exact=exact,
            # Message to help understand the diff in context
            message=str(actual_required_state),
        )

    def _add_new_dm_to_global_account_data(
        self, source_user_id: str, target_user_id: str, target_room_id: str
    ) -> None:
        """
        Helper to handle inserting a new DM for the source user into global account data
        (handles all of the list merging).

        Args:
            source_user_id: The user ID of the DM mapping we're going to update
            target_user_id: User ID of the person the DM is with
            target_room_id: Room ID of the DM
        """

        # Get the current DM map
        existing_dm_map = self.get_success(
            self.store.get_global_account_data_by_type_for_user(
                source_user_id, AccountDataTypes.DIRECT
            )
        )
        # Scrutinize the account data since it has no concrete type. We're just copying
        # everything into a known type. It should be a mapping from user ID to a list of
        # room IDs. Ignore anything else.
        new_dm_map: Dict[str, List[str]] = {}
        if isinstance(existing_dm_map, dict):
            for user_id, room_ids in existing_dm_map.items():
                if isinstance(user_id, str) and isinstance(room_ids, list):
                    for room_id in room_ids:
                        if isinstance(room_id, str):
                            new_dm_map[user_id] = new_dm_map.get(user_id, []) + [
                                room_id
                            ]

        # Add the new DM to the map
        new_dm_map[target_user_id] = new_dm_map.get(target_user_id, []) + [
            target_room_id
        ]
        # Save the DM map to global account data
        self.get_success(
            self.store.add_account_data_for_user(
                source_user_id,
                AccountDataTypes.DIRECT,
                new_dm_map,
            )
        )

    def _create_dm_room(
        self,
        inviter_user_id: str,
        inviter_tok: str,
        invitee_user_id: str,
        invitee_tok: str,
        should_join_room: bool = True,
    ) -> str:
        """
        Helper to create a DM room as the "inviter" and invite the "invitee" user to the
        room. The "invitee" user also will join the room. The `m.direct` account data
        will be set for both users.
        """

        # Create a room and send an invite the other user
        room_id = self.helper.create_room_as(
            inviter_user_id,
            is_public=False,
            tok=inviter_tok,
        )
        self.helper.invite(
            room_id,
            src=inviter_user_id,
            targ=invitee_user_id,
            tok=inviter_tok,
            extra_data={"is_direct": True},
        )
        if should_join_room:
            # Person that was invited joins the room
            self.helper.join(room_id, invitee_user_id, tok=invitee_tok)

        # Mimic the client setting the room as a direct message in the global account
        # data for both users.
        self._add_new_dm_to_global_account_data(
            invitee_user_id, inviter_user_id, room_id
        )
        self._add_new_dm_to_global_account_data(
            inviter_user_id, invitee_user_id, room_id
        )

        return room_id

    def _bump_notifier_wait_for_events(self, user_id: str) -> None:
        """
        Wake-up a `notifier.wait_for_events(user_id)` call without affecting the Sliding
        Sync results.
        """
        # We're expecting some new activity from this point onwards
        from_token = self.event_sources.get_current_token()

        triggered_notifier_wait_for_events = False

        async def _on_new_acivity(
            before_token: StreamToken, after_token: StreamToken
        ) -> bool:
            nonlocal triggered_notifier_wait_for_events
            triggered_notifier_wait_for_events = True
            return True

        # Listen for some new activity for the user. We're just trying to confirm that
        # our bump below actually does what we think it does (triggers new activity for
        # the user).
        result_awaitable = self.notifier.wait_for_events(
            user_id,
            1000,
            _on_new_acivity,
            from_token=from_token,
        )

        # Update the account data so that `notifier.wait_for_events(...)` wakes up.
        # We're bumping account data because it won't show up in the Sliding Sync
        # response so it won't affect whether we have results.
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id,
                "org.matrix.foobarbaz",
                {"foo": "bar"},
            )
        )

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )

    def test_sync_list(self) -> None:
        """
        Test that room IDs show up in the Sliding Sync `lists`
        """
        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(alice_user_id, "correcthorse")

        room_id = self.helper.create_room_as(
            alice_user_id, tok=alice_access_token, is_public=True
        )

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 99]],
                        "required_state": [
                            ["m.room.join_rules", ""],
                            ["m.room.history_visibility", ""],
                            ["m.space.child", "*"],
                        ],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=alice_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["foo-list"],
            channel.json_body["lists"].keys(),
        )

        # Make sure the list includes the room we are joined to
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [room_id],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

    def test_wait_for_sync_token(self) -> None:
        """
        Test that worker will wait until it catches up to the given token
        """
        alice_user_id = self.register_user("alice", "correcthorse")
        alice_access_token = self.login(alice_user_id, "correcthorse")

        # Create a future token that will cause us to wait. Since we never send a new
        # event to reach that future stream_ordering, the worker will wait until the
        # full timeout.
        stream_id_gen = self.store.get_events_stream_id_generator()
        stream_id = self.get_success(stream_id_gen.get_next().__aenter__())
        current_token = self.event_sources.get_current_token()
        future_position_token = current_token.copy_and_replace(
            StreamKeyType.ROOM,
            RoomStreamToken(stream=stream_id),
        )

        future_position_token_serialized = self.get_success(
            future_position_token.to_string(self.store)
        )

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?pos={future_position_token_serialized}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 99]],
                        "required_state": [
                            ["m.room.join_rules", ""],
                            ["m.room.history_visibility", ""],
                            ["m.space.child", "*"],
                        ],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=alice_access_token,
            await_result=False,
        )
        # Block for 10 seconds to make `notifier.wait_for_stream_token(from_token)`
        # timeout
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=9900)
        channel.await_result(timeout_ms=200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We expect the next `pos` in the result to be the same as what we requested
        # with because we weren't able to find anything new yet.
        self.assertEqual(channel.json_body["pos"], future_position_token_serialized)

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

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 0]],
                        "required_state": [],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the room with new events to trigger new results
        event_response1 = self.helper.send(
            room_id, "new activity in room", tok=user1_tok
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check to make sure the new event is returned
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id]["timeline"]
            ],
            [
                event_response1["event_id"],
            ],
            channel.json_body["rooms"][room_id]["timeline"],
        )

    # TODO: Once we remove `ops`, we should be able to add a `RoomResult.__bool__` to
    # check if there are any updates since the `from_token`.
    @skip_unless(
        False,
        "Once we remove ops from the Sliding Sync response, this test should pass",
    )
    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 0]],
                        "required_state": [],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(user1_id)
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We still see rooms because that's how Sliding Sync lists work but we reached
        # the timeout before seeing them
        self.assertEqual(
            [event["event_id"] for event in channel.json_body["rooms"].keys()],
            [room_id],
        )

    def test_filter_list(self) -> None:
        """
        Test that filters apply to `lists`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create a DM room
        joined_dm_room_id = self._create_dm_room(
            inviter_user_id=user1_id,
            inviter_tok=user1_tok,
            invitee_user_id=user2_id,
            invitee_tok=user2_tok,
            should_join_room=True,
        )
        invited_dm_room_id = self._create_dm_room(
            inviter_user_id=user1_id,
            inviter_tok=user1_tok,
            invitee_user_id=user2_id,
            invitee_tok=user2_tok,
            should_join_room=False,
        )

        # Create a normal room
        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)

        # Create a room that user1 is invited to
        invite_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.invite(invite_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    # Absense of filters does not imply "False" values
                    "all": {
                        "ranges": [[0, 99]],
                        "required_state": [],
                        "timeline_limit": 1,
                        "filters": {},
                    },
                    # Test single truthy filter
                    "dms": {
                        "ranges": [[0, 99]],
                        "required_state": [],
                        "timeline_limit": 1,
                        "filters": {"is_dm": True},
                    },
                    # Test single falsy filter
                    "non-dms": {
                        "ranges": [[0, 99]],
                        "required_state": [],
                        "timeline_limit": 1,
                        "filters": {"is_dm": False},
                    },
                    # Test how multiple filters should stack (AND'd together)
                    "room-invites": {
                        "ranges": [[0, 99]],
                        "required_state": [],
                        "timeline_limit": 1,
                        "filters": {"is_dm": False, "is_invite": True},
                    },
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["all", "dms", "non-dms", "room-invites"],
            channel.json_body["lists"].keys(),
        )

        # Make sure the lists have the correct rooms
        self.assertListEqual(
            list(channel.json_body["lists"]["all"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [
                        invite_room_id,
                        room_id,
                        invited_dm_room_id,
                        joined_dm_room_id,
                    ],
                }
            ],
            list(channel.json_body["lists"]["all"]),
        )
        self.assertListEqual(
            list(channel.json_body["lists"]["dms"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invited_dm_room_id, joined_dm_room_id],
                }
            ],
            list(channel.json_body["lists"]["dms"]),
        )
        self.assertListEqual(
            list(channel.json_body["lists"]["non-dms"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invite_room_id, room_id],
                }
            ],
            list(channel.json_body["lists"]["non-dms"]),
        )
        self.assertListEqual(
            list(channel.json_body["lists"]["room-invites"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [invite_room_id],
                }
            ],
            list(channel.json_body["lists"]["room-invites"]),
        )

        # Ensure DM's are correctly marked
        self.assertDictEqual(
            {
                room_id: room.get("is_dm")
                for room_id, room in channel.json_body["rooms"].items()
            },
            {
                invite_room_id: None,
                room_id: None,
                invited_dm_room_id: True,
                joined_dm_room_id: True,
            },
        )

    def test_sort_list(self) -> None:
        """
        Test that the `lists` are sorted by `stream_ordering`
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        # Activity that will order the rooms
        self.helper.send(room_id3, "activity in room3", tok=user1_tok)
        self.helper.send(room_id1, "activity in room1", tok=user1_tok)
        self.helper.send(room_id2, "activity in room2", tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 99]],
                        "required_state": [
                            ["m.room.join_rules", ""],
                            ["m.room.history_visibility", ""],
                            ["m.space.child", "*"],
                        ],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["foo-list"],
            channel.json_body["lists"].keys(),
        )

        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 99],
                    "room_ids": [room_id2, room_id1, room_id3],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

    def test_sliced_windows(self) -> None:
        """
        Test that the `lists` `ranges` are sliced correctly. Both sides of each range
        are inclusive.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        _room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        room_id3 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        # Make the Sliding Sync request for a single room
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 0]],
                        "required_state": [
                            ["m.room.join_rules", ""],
                            ["m.room.history_visibility", ""],
                            ["m.space.child", "*"],
                        ],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["foo-list"],
            channel.json_body["lists"].keys(),
        )
        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 0],
                    "room_ids": [room_id3],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

        # Make the Sliding Sync request for the first two rooms
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            ["m.room.join_rules", ""],
                            ["m.room.history_visibility", ""],
                            ["m.space.child", "*"],
                        ],
                        "timeline_limit": 1,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["foo-list"],
            channel.json_body["lists"].keys(),
        )
        # Make sure the list is sorted in the way we expect
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id3, room_id2],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

    def test_rooms_meta_when_joined(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` are included in the response and
        reflect the current state of the room when the user is joined to the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Reflect the current state of the room
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["name"],
            "my super room",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["avatar"],
            "mxc://DUMMY_MEDIA_ID",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            2,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            0,
        )
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_when_invited(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` are included in the response and
        reflect the current state of the room when the user is invited to the room.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # User1 is invited to the room
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Update the room name after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Update the room avatar URL after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://UPDATED_DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # This should still reflect the current state of the room even when the user is
        # invited.
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["name"],
            "my super duper room",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["avatar"],
            "mxc://UPDATED_DUMMY_MEDIA_ID",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            1,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            1,
        )
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_when_banned(self) -> None:
        """
        Test that the `rooms` `name` and `avatar` reflect the state of the room when the
        user was banned (do not leak current state).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        # Set the room avatar URL
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # Update the room name after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Update the room avatar URL after user1 has left
        self.helper.send_state(
            room_id1,
            EventTypes.RoomAvatar,
            {"url": "mxc://UPDATED_DUMMY_MEDIA_ID"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Reflect the state of the room at the time of leaving
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["name"],
            "my super room",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["avatar"],
            "mxc://DUMMY_MEDIA_ID",
            channel.json_body["rooms"][room_id1],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            # FIXME: The actual number should be "1" (user2) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            0,
        )
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("is_dm"),
        )

    def test_rooms_meta_heroes(self) -> None:
        """
        Test that the `rooms` `heroes` are included in the response when the room
        doesn't have a room name set.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "name": "my super room",
            },
        )
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id1, src=user2_id, targ=user3_id, tok=user2_tok)

        room_id2 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room2",
            },
        )
        self.helper.join(room_id2, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id2, src=user2_id, targ=user3_id, tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Room1 has a name so we shouldn't see any `heroes` which the client would use
        # the calculate the room name themselves.
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["name"],
            "my super room",
            channel.json_body["rooms"][room_id1],
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("heroes"))
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            2,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            1,
        )

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(channel.json_body["rooms"][room_id2].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in channel.json_body["rooms"][room_id2].get("heroes", [])
            ],
            # Heroes shouldn't include the user themselves (we shouldn't see user1)
            [user2_id, user3_id],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id2]["joined_count"],
            2,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id2]["invited_count"],
            1,
        )

        # We didn't request any state so we shouldn't see any `required_state`
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("required_state"))
        self.assertIsNone(channel.json_body["rooms"][room_id2].get("required_state"))

    def test_rooms_meta_heroes_max(self) -> None:
        """
        Test that the `rooms` `heroes` only includes the first 5 users (not including
        yourself).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        user5_tok = self.login(user5_id, "pass")
        user6_id = self.register_user("user6", "pass")
        user6_tok = self.login(user6_id, "pass")
        user7_id = self.register_user("user7", "pass")
        user7_tok = self.login(user7_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room",
            },
        )
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        self.helper.join(room_id1, user5_id, tok=user5_tok)
        self.helper.join(room_id1, user6_id, tok=user6_tok)
        self.helper.join(room_id1, user7_id, tok=user7_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in channel.json_body["rooms"][room_id1].get("heroes", [])
            ],
            # Heroes should be the first 5 users in the room (excluding the user
            # themselves, we shouldn't see `user1`)
            [user2_id, user3_id, user4_id, user5_id, user6_id],
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            7,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            0,
        )

        # We didn't request any state so we shouldn't see any `required_state`
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("required_state"))

    def test_rooms_meta_heroes_when_banned(self) -> None:
        """
        Test that the `rooms` `heroes` are included in the response when the room
        doesn't have a room name set but doesn't leak information past their ban.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")
        user5_id = self.register_user("user5", "pass")
        _user5_tok = self.login(user5_id, "pass")

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                # No room name set so that `heroes` is populated
                #
                # "name": "my super room",
            },
        )
        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # User3 is invited
        self.helper.invite(room_id1, src=user2_id, targ=user3_id, tok=user2_tok)

        # User1 is banned from the room
        self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        # User4 joins the room after user1 is banned
        self.helper.join(room_id1, user4_id, tok=user4_tok)
        # User5 is invited after user1 is banned
        self.helper.invite(room_id1, src=user2_id, targ=user5_id, tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Room2 doesn't have a name so we should see `heroes` populated
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("name"))
        self.assertCountEqual(
            [
                hero["user_id"]
                for hero in channel.json_body["rooms"][room_id1].get("heroes", [])
            ],
            # Heroes shouldn't include the user themselves (we shouldn't see user1). We
            # also shouldn't see user4 since they joined after user1 was banned.
            #
            # FIXME: The actual result should be `[user2_id, user3_id]` but we currently
            # don't support this for rooms where the user has left/been banned.
            [],
        )

        self.assertEqual(
            channel.json_body["rooms"][room_id1]["joined_count"],
            # FIXME: The actual number should be "1" (user2) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
        )
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["invited_count"],
            # We shouldn't see user5 since they were invited after user1 was banned.
            #
            # FIXME: The actual number should be "1" (user3) but we currently don't
            # support this for rooms where the user has left/been banned.
            0,
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
        self.helper.send(room_id1, "activity1", tok=user2_tok)
        self.helper.send(room_id1, "activity2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity3", tok=user2_tok)
        event_pos3 = self.get_success(
            self.store.get_position_for_event(event_response3["event_id"])
        )
        event_response4 = self.helper.send(room_id1, "activity4", tok=user2_tok)
        event_pos4 = self.get_success(
            self.store.get_position_for_event(event_response4["event_id"])
        )
        event_response5 = self.helper.send(room_id1, "activity5", tok=user2_tok)
        user1_join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We expect to saturate the `timeline_limit` (there are more than 3 messages in the room)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            True,
            channel.json_body["rooms"][room_id1],
        )
        # Check to make sure the latest events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response4["event_id"],
                event_response5["event_id"],
                user1_join_response["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )

        # Check to make sure the `prev_batch` points at the right place
        prev_batch_token = self.get_success(
            StreamToken.from_string(
                self.store, channel.json_body["rooms"][room_id1]["prev_batch"]
            )
        )
        prev_batch_room_stream_token_serialized = self.get_success(
            prev_batch_token.room_key.to_string(self.store)
        )
        # If we use the `prev_batch` token to look backwards, we should see `event3`
        # next so make sure the token encompasses it
        self.assertEqual(
            event_pos3.persisted_after(prev_batch_token.room_key),
            False,
            f"`prev_batch` token {prev_batch_room_stream_token_serialized} should be >= event_pos3={self.get_success(event_pos3.to_room_stream_token().to_string(self.store))}",
        )
        # If we use the `prev_batch` token to look backwards, we shouldn't see `event4`
        # anymore since it was just returned in this response.
        self.assertEqual(
            event_pos4.persisted_after(prev_batch_token.room_key),
            True,
            f"`prev_batch` token {prev_batch_room_stream_token_serialized} should be < event_pos4={self.get_success(event_pos4.to_room_stream_token().to_string(self.store))}",
        )

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" range
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": timeline_limit,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # The timeline should be `limited=False` because we have all of the events (no
        # more to paginate to)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            False,
            channel.json_body["rooms"][room_id1],
        )
        expected_number_of_events = 9
        # We're just looking to make sure we got all of the events before hitting the `timeline_limit`
        self.assertEqual(
            len(channel.json_body["rooms"][room_id1]["timeline"]),
            expected_number_of_events,
            channel.json_body["rooms"][room_id1]["timeline"],
        )
        self.assertLessEqual(expected_number_of_events, timeline_limit)

        # With no `from_token` (initial sync), it's all historical since there is no
        # "live" token range.
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
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
        sync_params = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 3,
                }
            }
        }
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            sync_params,
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_pos = channel.json_body["pos"]

        # Send some events but don't send enough to saturate the `timeline_limit`.
        # We want to later test that we only get the new events since the `next_pos`
        event_response2 = self.helper.send(room_id1, "activity after2", tok=user2_tok)
        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)

        # Make an incremental Sliding Sync request (what we're trying to test)
        channel = self.make_request(
            "POST",
            self.sync_endpoint + f"?pos={next_pos}",
            sync_params,
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We only expect to see the new events since the last sync which isn't enough to
        # fill up the `timeline_limit`.
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            False,
            f'Our `timeline_limit` was {sync_params["lists"]["foo-list"]["timeline_limit"]} '
            + f'and {len(channel.json_body["rooms"][room_id1]["timeline"])} events were returned in the timeline. '
            + str(channel.json_body["rooms"][room_id1]),
        )
        # Check to make sure the latest events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response2["event_id"],
                event_response3["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )

        # All events are "live"
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            2,
            channel.json_body["rooms"][room_id1],
        )

    def test_rooms_bump_stamp(self) -> None:
        """
        Test that `bump_stamp` is present and pointing to relevant events.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        event_response1 = message_response = self.helper.send(
            room_id1, "message in room1", tok=user1_tok
        )
        event_pos1 = self.get_success(
            self.store.get_position_for_event(event_response1["event_id"])
        )
        room_id2 = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
        )
        send_response2 = self.helper.send(room_id2, "message in room2", tok=user1_tok)
        event_pos2 = self.get_success(
            self.store.get_position_for_event(send_response2["event_id"])
        )

        # Send a reaction in room1 but it shouldn't affect the `bump_stamp`
        # because reactions are not part of the `DEFAULT_BUMP_EVENT_TYPES`
        self.helper.send_event(
            room_id1,
            type=EventTypes.Reaction,
            content={
                "m.relates_to": {
                    "event_id": message_response["event_id"],
                    "key": "👍",
                    "rel_type": "m.annotation",
                }
            },
            tok=user1_tok,
        )

        # Make the Sliding Sync request
        timeline_limit = 100
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": timeline_limit,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure it has the foo-list we requested
        self.assertListEqual(
            list(channel.json_body["lists"].keys()),
            ["foo-list"],
            channel.json_body["lists"].keys(),
        )

        # Make sure the list includes the rooms in the right order
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    # room1 sorts before room2 because it has the latest event (the
                    # reaction)
                    "room_ids": [room_id1, room_id2],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

        # The `bump_stamp` for room1 should point at the latest message (not the
        # reaction since it's not one of the `DEFAULT_BUMP_EVENT_TYPES`)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["bump_stamp"],
            event_pos1.stream,
            channel.json_body["rooms"][room_id1],
        )

        # The `bump_stamp` for room2 should point at the latest message
        self.assertEqual(
            channel.json_body["rooms"][room_id2]["bump_stamp"],
            event_pos2.stream,
            channel.json_body["rooms"][room_id2],
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

        from_token = self.event_sources.get_current_token()

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

        # The `timeline_limit` is set to 4 so we can at least see one historical event
        # before the `from_token`. We should see historical events because this is a
        # `newly_joined` room.
        timeline_limit = 4
        # Make an incremental Sliding Sync request (what we're trying to test)
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": timeline_limit,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the new events and the rest should be filled with historical
        # events which will make us `limited=True` since there are more to paginate to.
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            True,
            f"Our `timeline_limit` was {timeline_limit} "
            + f'and {len(channel.json_body["rooms"][room_id1]["timeline"])} events were returned in the timeline. '
            + str(channel.json_body["rooms"][room_id1]),
        )
        # Check to make sure that the "live" and historical events are returned
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response2["event_id"],
                user1_join_response["event_id"],
                event_response3["event_id"],
                event_response4["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )

        # Only events after the `from_token` are "live" (join, event3, event4)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            3,
            channel.json_body["rooms"][room_id1],
        )

    def test_rooms_invite_shared_history_initial_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        initial sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but we also shouldn't see any timeline events because the history visiblity is
        `shared` and we haven't joined the room yet.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Ensure we're testing with a room with `shared` history visibility which means
        # history visible until you actually join the room.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.SHARED,
        )

        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after3", tok=user2_tok)
        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("timeline"),
            channel.json_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("num_live"),
            channel.json_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("limited"),
            channel.json_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("prev_batch"),
            channel.json_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("required_state"),
            channel.json_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            channel.json_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            channel.json_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_shared_history_incremental_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        incremental sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but we also shouldn't see any timeline events because the history visiblity is
        `shared` and we haven't joined the room yet.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Ensure we're testing with a room with `shared` history visibility which means
        # history visible until you actually join the room.
        history_visibility_response = self.helper.get_state(
            room_id1, EventTypes.RoomHistoryVisibility, tok=user2_tok
        )
        self.assertEqual(
            history_visibility_response.get("history_visibility"),
            HistoryVisibility.SHARED,
        )

        self.helper.send(room_id1, "activity before invite1", tok=user2_tok)
        self.helper.send(room_id1, "activity before invite2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after invite3", tok=user2_tok)
        self.helper.send(room_id1, "activity after invite4", tok=user2_tok)

        from_token = self.event_sources.get_current_token()

        self.helper.send(room_id1, "activity after token5", tok=user2_tok)
        self.helper.send(room_id1, "activity after toekn6", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("timeline"),
            channel.json_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("num_live"),
            channel.json_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("limited"),
            channel.json_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("prev_batch"),
            channel.json_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("required_state"),
            channel.json_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            channel.json_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            channel.json_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_world_readable_history_initial_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        initial sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but depending on the semantics we decide, we could potentially see some
        historical events before/after the `from_token` because the history is
        `world_readable`. Same situation for events after the `from_token` if the
        history visibility was set to `invited`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

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

        self.helper.send(room_id1, "activity before1", tok=user2_tok)
        self.helper.send(room_id1, "activity before2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after3", tok=user2_tok)
        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        # Large enough to see the latest events and before the invite
                        "timeline_limit": 4,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("timeline"),
            channel.json_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("num_live"),
            channel.json_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("limited"),
            channel.json_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("prev_batch"),
            channel.json_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("required_state"),
            channel.json_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            channel.json_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            channel.json_body["rooms"][room_id1]["invite_state"],
        )

    def test_rooms_invite_world_readable_history_incremental_sync(self) -> None:
        """
        Test that `rooms` we are invited to have some stripped `invite_state` during an
        incremental sync.

        This is an `invite` room so we should only have `stripped_state` (no `timeline`)
        but depending on the semantics we decide, we could potentially see some
        historical events before/after the `from_token` because the history is
        `world_readable`. Same situation for events after the `from_token` if the
        history visibility was set to `invited`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user1 = UserID.from_string(user1_id)
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user2 = UserID.from_string(user2_id)

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

        self.helper.send(room_id1, "activity before invite1", tok=user2_tok)
        self.helper.send(room_id1, "activity before invite2", tok=user2_tok)
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.send(room_id1, "activity after invite3", tok=user2_tok)
        self.helper.send(room_id1, "activity after invite4", tok=user2_tok)

        from_token = self.event_sources.get_current_token()

        self.helper.send(room_id1, "activity after token5", tok=user2_tok)
        self.helper.send(room_id1, "activity after toekn6", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        # Large enough to see the latest events and before the invite
                        "timeline_limit": 4,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # `timeline` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("timeline"),
            channel.json_body["rooms"][room_id1],
        )
        # `num_live` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("num_live"),
            channel.json_body["rooms"][room_id1],
        )
        # `limited` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("limited"),
            channel.json_body["rooms"][room_id1],
        )
        # `prev_batch` is omitted for `invite` rooms with `stripped_state` (no timeline anyway)
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("prev_batch"),
            channel.json_body["rooms"][room_id1],
        )
        # `required_state` is omitted for `invite` rooms with `stripped_state`
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("required_state"),
            channel.json_body["rooms"][room_id1],
        )
        # We should have some `stripped_state` so the potential joiner can identify the
        # room (we don't care about the order).
        self.assertCountEqual(
            channel.json_body["rooms"][room_id1]["invite_state"],
            [
                {
                    "content": {"creator": user2_id, "room_version": "10"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.create",
                },
                {
                    "content": {"join_rule": "public"},
                    "sender": user2_id,
                    "state_key": "",
                    "type": "m.room.join_rules",
                },
                {
                    "content": {"displayname": user2.localpart, "membership": "join"},
                    "sender": user2_id,
                    "state_key": user2_id,
                    "type": "m.room.member",
                },
                {
                    "content": {"displayname": user1.localpart, "membership": "invite"},
                    "sender": user2_id,
                    "state_key": user1_id,
                    "type": "m.room.member",
                },
            ],
            channel.json_body["rooms"][room_id1]["invite_state"],
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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see events before the ban but not after
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            True,
            channel.json_body["rooms"][room_id1],
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

        from_token = self.event_sources.get_current_token()

        event_response3 = self.helper.send(room_id1, "activity after3", tok=user2_tok)
        event_response4 = self.helper.send(room_id1, "activity after4", tok=user2_tok)
        # The ban is within the token range (between the `from_token` and the sliding
        # sync request)
        user1_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )

        self.helper.send(room_id1, "activity after5", tok=user2_tok)
        self.helper.send(room_id1, "activity after6", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 4,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see events before the ban but not after
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                event_response3["event_id"],
                event_response4["event_id"],
                user1_ban_response["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )
        # All live events in the incremental sync
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            3,
            channel.json_body["rooms"][room_id1],
        )
        # There aren't anymore events to paginate to in this range
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            False,
            channel.json_body["rooms"][room_id1],
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

        from_token = self.event_sources.get_current_token()

        self.helper.send(room_id1, "activity after4", tok=user2_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [],
                        "timeline_limit": 4,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Nothing to see for this banned user in the room in the token range
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("timeline"))
        # No events returned in the timeline so nothing is "live"
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
        )
        # There aren't anymore events to paginate to in this range
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            False,
            channel.json_body["rooms"][room_id1],
        )

    def test_rooms_no_required_state(self) -> None:
        """
        Empty `rooms.required_state` should not return any state events in the room
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        # Empty `required_state`
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # No `required_state` in response
        self.assertIsNone(
            channel.json_body["rooms"][room_id1].get("required_state"),
            channel.json_body["rooms"][room_id1],
        )

    def test_rooms_required_state_initial_sync(self) -> None:
        """
        Test `rooms.required_state` returns requested state events in the room during an
        initial sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.RoomHistoryVisibility, ""],
                            # This one doesn't exist in the room
                            [EventTypes.Tombstone, ""],
                        ],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_incremental_sync(self) -> None:
        """
        Test `rooms.required_state` returns requested state events in the room during an
        incremental sync.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(after_room_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.RoomHistoryVisibility, ""],
                            # This one doesn't exist in the room
                            [EventTypes.Tombstone, ""],
                        ],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # The returned state doesn't change from initial to incremental sync. In the
        # future, we will only return updates but only if we've sent the room down the
        # connection before.
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.RoomHistoryVisibility, "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_wildcard(self) -> None:
        """
        Test `rooms.required_state` returns all state events when using wildcard `["*", "*"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="namespaced",
            body={"foo": "bar"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `event_type` and `state_key`
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [StateValues.WILDCARD, StateValues.WILDCARD],
                        ],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            # We should see all the state events in the room
            state_map.values(),
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_wildcard_event_type(self) -> None:
        """
        Test `rooms.required_state` returns relevant state events when using wildcard in
        the event_type `["*", "foobarbaz"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key=user2_id,
            body={"foo": "bar"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `event_type`
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [StateValues.WILDCARD, user2_id],
                        ],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We expect at-least any state event with the `user2_id` as the `state_key`
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user2_id)],
                state_map[("org.matrix.foo_state", user2_id)],
            },
            # Ideally, this would be exact but we're currently returning all state
            # events when the `event_type` is a wildcard.
            exact=False,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_wildcard_state_key(self) -> None:
        """
        Test `rooms.required_state` returns relevant state events when using wildcard in
        the state_key `["foobarbaz","*"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # Make the Sliding Sync request with wildcards for the `state_key`
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Member, StateValues.WILDCARD],
                        ],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_lazy_loading_room_members(self) -> None:
        """
        Test `rooms.required_state` returns people relevant to the timeline when
        lazy-loading room members, `["m.room.member","$LAZY"]`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)
        self.helper.send(room_id1, "2", tok=user3_tok)
        self.helper.send(room_id1, "3", tok=user2_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.Member, StateValues.LAZY],
                        ],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user3_id)],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_me(self) -> None:
        """
        Test `rooms.required_state` correctly handles $ME.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send(room_id1, "1", tok=user2_tok)

        # Also send normal state events with state keys of the users, first
        # change the power levels to allow this.
        self.helper.send_state(
            room_id1,
            event_type=EventTypes.PowerLevels,
            body={"users": {user1_id: 50, user2_id: 100}},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo",
            state_key=user1_id,
            body={},
            tok=user1_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo",
            state_key=user2_id,
            body={},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with a request for '$ME'.
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.Member, StateValues.ME],
                            ["org.matrix.foo", StateValues.ME],
                        ],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[("org.matrix.foo", user1_id)],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    @parameterized.expand([(Membership.LEAVE,), (Membership.BAN,)])
    def test_rooms_required_state_leave_ban(self, stop_membership: str) -> None:
        """
        Test `rooms.required_state` should not return state past a leave/ban event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        from_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user3_id, tok=user3_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )

        if stop_membership == Membership.LEAVE:
            # User 1 leaves
            self.helper.leave(room_id1, user1_id, tok=user1_tok)
        elif stop_membership == Membership.BAN:
            # User 1 is banned
            self.helper.ban(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # Change the state after user 1 leaves
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "qux"},
            tok=user2_tok,
        )
        self.helper.leave(room_id1, user3_id, tok=user3_tok)

        # Make the Sliding Sync request with lazy loading for the room members
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.Member, "*"],
                            ["org.matrix.foo_state", ""],
                        ],
                        "timeline_limit": 3,
                    }
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Only user2 and user3 sent events in the 3 events we see in the `timeline`
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
                state_map[(EventTypes.Member, user3_id)],
                state_map[("org.matrix.foo_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_combine_superset(self) -> None:
        """
        Test `rooms.required_state` is combined across lists and room subscriptions.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        self.helper.send_state(
            room_id1,
            event_type="org.matrix.foo_state",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        self.helper.send_state(
            room_id1,
            event_type="org.matrix.bar_state",
            state_key="",
            body={"bar": "qux"},
            tok=user2_tok,
        )

        # Make the Sliding Sync request with wildcards for the `state_key`
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            [EventTypes.Member, user1_id],
                        ],
                        "timeline_limit": 0,
                    },
                    "bar-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Member, StateValues.WILDCARD],
                            ["org.matrix.foo_state", ""],
                        ],
                        "timeline_limit": 0,
                    },
                },
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [["org.matrix.bar_state", ""]],
                        "timeline_limit": 0,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
                state_map[(EventTypes.Member, user1_id)],
                state_map[(EventTypes.Member, user2_id)],
                state_map[("org.matrix.foo_state", "")],
                state_map[("org.matrix.bar_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

    def test_rooms_required_state_partial_state(self) -> None:
        """
        Test partially-stated room are excluded unless `rooms.required_state` is
        lazy-loading room members.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        _join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)

        # Mark room2 as partial state
        self.get_success(
            mark_event_as_partial_state(self.hs, join_response2["event_id"], room_id2)
        )

        # Make the Sliding Sync request (NOT lazy-loading room members)
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                        ],
                        "timeline_limit": 0,
                    },
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Make sure the list includes room1 but room2 is excluded because it's still
        # partially-stated
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id1],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

        # Make the Sliding Sync request (with lazy-loading room members)
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {
                    "foo-list": {
                        "ranges": [[0, 1]],
                        "required_state": [
                            [EventTypes.Create, ""],
                            # Lazy-load room members
                            [EventTypes.Member, StateValues.LAZY],
                        ],
                        "timeline_limit": 0,
                    },
                }
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # The list should include both rooms now because we're lazy-loading room members
        self.assertListEqual(
            list(channel.json_body["lists"]["foo-list"]["ops"]),
            [
                {
                    "op": "SYNC",
                    "range": [0, 1],
                    "room_ids": [room_id2, room_id1],
                }
            ],
            channel.json_body["lists"]["foo-list"],
        )

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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [
                            [EventTypes.Create, ""],
                        ],
                        "timeline_limit": 1,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(room_id1)
        )

        # We should see some state
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[(EventTypes.Create, "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

        # We should see some events
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            True,
            channel.json_body["rooms"][room_id1],
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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [
                            ["org.matrix.foo_state", ""],
                        ],
                        "timeline_limit": 2,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the state at the time of the leave
        self._assertRequiredStateIncludes(
            channel.json_body["rooms"][room_id1]["required_state"],
            {
                state_map[("org.matrix.foo_state", "")],
            },
            exact=True,
        )
        self.assertIsNone(channel.json_body["rooms"][room_id1].get("invite_state"))

        # We should see some before we left (nothing after)
        self.assertEqual(
            [
                event["event_id"]
                for event in channel.json_body["rooms"][room_id1]["timeline"]
            ],
            [
                join_response["event_id"],
                leave_response["event_id"],
            ],
            channel.json_body["rooms"][room_id1]["timeline"],
        )
        # No "live" events in an initial sync (no `from_token` to define the "live"
        # range)
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["num_live"],
            0,
            channel.json_body["rooms"][room_id1],
        )
        # There are more events to paginate to
        self.assertEqual(
            channel.json_body["rooms"][room_id1]["limited"],
            True,
            channel.json_body["rooms"][room_id1],
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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [
                            [EventTypes.Create, ""],
                        ],
                        "timeline_limit": 1,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should not see the room at all (we're not in it)
        self.assertIsNone(
            channel.json_body["rooms"].get(room_id1), channel.json_body["rooms"]
        )

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
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "room_subscriptions": {
                    room_id1: {
                        "required_state": [
                            [EventTypes.Create, ""],
                        ],
                        "timeline_limit": 1,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # FIXME: In the future, we should be able to see the room because it's
        # `world_readable` but currently we don't support this.
        self.assertIsNone(
            channel.json_body["rooms"].get(room_id1), channel.json_body["rooms"]
        )


class SlidingSyncToDeviceExtensionTestCase(unittest.HomeserverTestCase):
    """Tests for the to-device sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        sendtodevice.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.account_data_handler = hs.get_account_data_handler()
        self.notifier = hs.get_notifier()
        self.sync_endpoint = (
            "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"
        )

    def _bump_notifier_wait_for_events(self, user_id: str) -> None:
        """
        Wake-up a `notifier.wait_for_events(user_id)` call without affecting the Sliding
        Sync results.
        """
        # We're expecting some new activity from this point onwards
        from_token = self.event_sources.get_current_token()

        triggered_notifier_wait_for_events = False

        async def _on_new_acivity(
            before_token: StreamToken, after_token: StreamToken
        ) -> bool:
            nonlocal triggered_notifier_wait_for_events
            triggered_notifier_wait_for_events = True
            return True

        # Listen for some new activity for the user. We're just trying to confirm that
        # our bump below actually does what we think it does (triggers new activity for
        # the user).
        result_awaitable = self.notifier.wait_for_events(
            user_id,
            1000,
            _on_new_acivity,
            from_token=from_token,
        )

        # Update the account data so that `notifier.wait_for_events(...)` wakes up.
        # We're bumping account data because it won't show up in the Sliding Sync
        # response so it won't affect whether we have results.
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id,
                "org.matrix.foobarbaz",
                {"foo": "bar"},
            )
        )

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )

    def _assert_to_device_response(
        self, channel: FakeChannel, expected_messages: List[JsonDict]
    ) -> str:
        """Assert the sliding sync response was successful and has the expected
        to-device messages.

        Returns the next_batch token from the to-device section.
        """
        self.assertEqual(channel.code, 200, channel.json_body)
        extensions = channel.json_body["extensions"]
        to_device = extensions["to_device"]
        self.assertIsInstance(to_device["next_batch"], str)
        self.assertEqual(to_device["events"], expected_messages)

        return to_device["next_batch"]

    def test_no_data(self) -> None:
        """Test that enabling to-device extension works, even if there is
        no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )

        # We expect no to-device messages
        self._assert_to_device_response(channel, [])

    def test_data_initial_sync(self) -> None:
        """Test that we get to-device messages when we don't specify a since
        token"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        # Send the to-device message
        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self._assert_to_device_response(
            channel,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

    def test_data_incremental_sync(self) -> None:
        """Test that we get to-device messages over incremental syncs"""

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        # No to-device messages yet.
        next_batch = self._assert_to_device_response(channel, [])

        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                        "since": next_batch,
                    }
                },
            },
            access_token=user1_tok,
        )
        next_batch = self._assert_to_device_response(
            channel,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

        # The next sliding sync request should not include the to-device
        # message.
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                        "since": next_batch,
                    }
                },
            },
            access_token=user1_tok,
        )
        self._assert_to_device_response(channel, [])

        # An initial sliding sync request should not include the to-device
        # message, as it should have been deleted
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self._assert_to_device_response(channel, [])

    def test_wait_for_new_data(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive.

        (Only applies to incremental syncs with a `timeout` specified)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", "d1")
        user2_id = self.register_user("u2", "pass")
        user2_tok = self.login(user2_id, "pass", "d2")

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the to-device messages to trigger new results
        test_msg = {"foo": "bar"}
        send_to_device_channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user1_id: {"d1": test_msg}}},
            access_token=user2_tok,
        )
        self.assertEqual(
            send_to_device_channel.code, 200, send_to_device_channel.result
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        self._assert_to_device_response(
            channel,
            [{"content": test_msg, "sender": user2_id, "type": "m.test"}],
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the To-Device extension doesn't trigger a false-positive for new data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "to_device": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(user1_id)
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        self._assert_to_device_response(channel, [])


class SlidingSyncE2eeExtensionTestCase(unittest.HomeserverTestCase):
    """Tests for the e2ee sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()
        self.account_data_handler = hs.get_account_data_handler()
        self.notifier = hs.get_notifier()
        self.sync_endpoint = (
            "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"
        )

    def _bump_notifier_wait_for_events(self, user_id: str) -> None:
        """
        Wake-up a `notifier.wait_for_events(user_id)` call without affecting the Sliding
        Sync results.
        """
        # We're expecting some new activity from this point onwards
        from_token = self.event_sources.get_current_token()

        triggered_notifier_wait_for_events = False

        async def _on_new_acivity(
            before_token: StreamToken, after_token: StreamToken
        ) -> bool:
            nonlocal triggered_notifier_wait_for_events
            triggered_notifier_wait_for_events = True
            return True

        # Listen for some new activity for the user. We're just trying to confirm that
        # our bump below actually does what we think it does (triggers new activity for
        # the user).
        result_awaitable = self.notifier.wait_for_events(
            user_id,
            1000,
            _on_new_acivity,
            from_token=from_token,
        )

        # Update the account data so that `notifier.wait_for_events(...)` wakes up.
        # We're bumping account data because it won't show up in the Sliding Sync
        # response so it won't affect whether we have results.
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                user_id,
                "org.matrix.foobarbaz",
                {"foo": "bar"},
            )
        )

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )

    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling e2ee extension works during an intitial sync, even if there
        is no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the e2ee extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Device list updates are only present for incremental syncs
        self.assertIsNone(channel.json_body["extensions"]["e2ee"].get("device_lists"))

        # Both of these should be present even when empty
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # This is always present because of
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
        )

    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling e2ee extension works during an incremental sync, even if
        there is no-data
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        from_token = self.event_sources.get_current_token()

        # Make an incremental Sliding Sync request with the e2ee extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Device list shows up for incremental syncs
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

        # Both of these should be present even when empty
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
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
        test_device_id = "TESTDEVICE"
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass", device_id=test_device_id)

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)
        self.helper.join(room_id, user3_id, tok=user3_tok)

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Bump the device lists to trigger new results
        # Have user3 update their device list
        device_update_channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=user3_tok,
        )
        self.assertEqual(
            device_update_channel.code, 200, device_update_channel.json_body
        )
        # Should respond before the 10 second timeout
        channel.await_result(timeout_ms=3000)
        self.assertEqual(channel.code, 200, channel.json_body)

        # We should see the device list update
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [user3_id],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

    def test_wait_for_new_data_timeout(self) -> None:
        """
        Test to make sure that the Sliding Sync request waits for new data to arrive but
        no data ever arrives so we timeout. We're also making sure that the default data
        from the E2EE extension doesn't trigger a false-positive for new data (see
        `device_one_time_keys_count.signed_curve25519`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        from_token = self.event_sources.get_current_token()

        # Make the Sliding Sync request
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + "?timeout=10000"
            + f"&pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
            await_result=False,
        )
        # Block for 5 seconds to make sure we are `notifier.wait_for_events(...)`
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=5000)
        # Wake-up `notifier.wait_for_events(...)` that will cause us test
        # `SlidingSyncResult.__bool__` for new results.
        self._bump_notifier_wait_for_events(user1_id)
        # Block for a little bit more to ensure we don't see any new results.
        with self.assertRaises(TimedOutException):
            channel.await_result(timeout_ms=4000)
        # Wait for the sync to complete (wait for the rest of the 10 second timeout,
        # 5000 + 4000 + 1200 > 10000)
        channel.await_result(timeout_ms=1200)
        self.assertEqual(channel.code, 200, channel.json_body)

        # Device lists are present for incremental syncs but empty because no device changes
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [],
        )

        # Both of these should be present even when empty
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_one_time_keys_count"],
            {
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0
            },
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]["device_unused_fallback_key_types"],
            [],
        )

    def test_device_lists(self) -> None:
        """
        Test that device list updates are included in the response
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        test_device_id = "TESTDEVICE"
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass", device_id=test_device_id)

        user4_id = self.register_user("user4", "pass")
        user4_tok = self.login(user4_id, "pass")

        room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id, user1_id, tok=user1_tok)
        self.helper.join(room_id, user3_id, tok=user3_tok)
        self.helper.join(room_id, user4_id, tok=user4_tok)

        from_token = self.event_sources.get_current_token()

        # Have user3 update their device list
        channel = self.make_request(
            "PUT",
            f"/devices/{test_device_id}",
            {
                "display_name": "New Device Name",
            },
            access_token=user3_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # User4 leaves the room
        self.helper.leave(room_id, user4_id, tok=user4_tok)

        # Make an incremental Sliding Sync request with the e2ee extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint
            + f"?pos={self.get_success(from_token.to_string(self.store))}",
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Device list updates show up
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed"),
            [user3_id],
        )
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_lists", {}).get("left"),
            [user4_id],
        )

    def test_device_one_time_keys_count(self) -> None:
        """
        Test that `device_one_time_keys_count` are included in the response
        """
        test_device_id = "TESTDEVICE"
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", device_id=test_device_id)

        # Upload one time keys for the user/device
        keys: JsonDict = {
            "alg1:k1": "key1",
            "alg2:k2": {"key": "key2", "signatures": {"k1": "sig1"}},
            "alg2:k3": {"key": "key3"},
        }
        upload_keys_response = self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                user1_id, test_device_id, {"one_time_keys": keys}
            )
        )
        self.assertDictEqual(
            upload_keys_response,
            {
                "one_time_key_counts": {
                    "alg1": 1,
                    "alg2": 2,
                    # Note that "signed_curve25519" is always returned in key count responses
                    # regardless of whether we uploaded any keys for it. This is necessary until
                    # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                    #
                    # Also related:
                    # https://github.com/element-hq/element-android/issues/3725 and
                    # https://github.com/matrix-org/synapse/issues/10456
                    "signed_curve25519": 0,
                }
            },
        )

        # Make a Sliding Sync request with the e2ee extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for those one time key counts
        self.assertEqual(
            channel.json_body["extensions"]["e2ee"].get("device_one_time_keys_count"),
            {
                "alg1": 1,
                "alg2": 2,
                # Note that "signed_curve25519" is always returned in key count responses
                # regardless of whether we uploaded any keys for it. This is necessary until
                # https://github.com/matrix-org/matrix-doc/issues/3298 is fixed.
                #
                # Also related:
                # https://github.com/element-hq/element-android/issues/3725 and
                # https://github.com/matrix-org/synapse/issues/10456
                "signed_curve25519": 0,
            },
        )

    def test_device_unused_fallback_key_types(self) -> None:
        """
        Test that `device_unused_fallback_key_types` are included in the response
        """
        test_device_id = "TESTDEVICE"
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass", device_id=test_device_id)

        # We shouldn't have any unused fallback keys yet
        res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(user1_id, test_device_id)
        )
        self.assertEqual(res, [])

        # Upload a fallback key for the user/device
        self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                user1_id,
                test_device_id,
                {"fallback_keys": {"alg1:k1": "fallback_key1"}},
            )
        )
        # We should now have an unused alg1 key
        fallback_res = self.get_success(
            self.store.get_e2e_unused_fallback_key_types(user1_id, test_device_id)
        )
        self.assertEqual(fallback_res, ["alg1"], fallback_res)

        # Make a Sliding Sync request with the e2ee extension enabled
        channel = self.make_request(
            "POST",
            self.sync_endpoint,
            {
                "lists": {},
                "extensions": {
                    "e2ee": {
                        "enabled": True,
                    }
                },
            },
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for the unused fallback key types
        self.assertListEqual(
            channel.json_body["extensions"]["e2ee"].get(
                "device_unused_fallback_key_types"
            ),
            ["alg1"],
        )
