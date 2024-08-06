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
from typing import List

from parameterized import parameterized, parameterized_class

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    ReceiptTypes,
    RelationTypes,
)
from synapse.rest.client import devices, knock, login, read_marker, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.federation.transport.test_knocking import (
    KnockingStrippedStateEventHelperMixin,
)
from tests.server import TimedOutException

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
