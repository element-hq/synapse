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

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    JoinRules,
    ReceiptTypes,
    RelationTypes,
)
from synapse.events import EventBase
from synapse.replication.tcp.resource import _batch_updates
from synapse.replication.tcp.streams.events import EventsStream
from synapse.rest.admin.experimental_features import ExperimentalFeature
from synapse.rest.client import devices, knock, login, read_marker, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomStreamToken, StreamKeyType, StreamToken
from synapse.util.clock import Clock

from tests import unittest
from tests.federation.transport.test_knocking import (
    KnockingStrippedStateEventHelperMixin,
)
from tests.rest.client.test_rooms import make_request_with_cancellation_test
from tests.server import TimedOutException
from tests.test_utils.event_injection import create_event, inject_event

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

    def _test_sync_filter_labels(self, sync_filter: str) -> list[JsonDict]:
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

        # Clear the typing information, so that it doesn't think everything is
        # in the future. This happens automatically when the typing stream
        # resets.
        typing._reset()

        # Nothing new, so we time out.
        with self.assertRaises(TimedOutException):
            self.make_request("GET", sync_url % (access_token, next_batch))

        # Sync and start typing again.
        sync_channel = self.make_request(
            "GET", sync_url % (access_token, next_batch), await_result=False
        )
        self.assertFalse(sync_channel.is_finished())

        channel = self.make_request(
            "PUT",
            typing_url % (room, other_user_id, other_access_token),
            b'{"typing": true, "timeout": 30000}',
        )
        self.assertEqual(200, channel.code)

        # Sync should now return.
        sync_channel.await_result()
        self.assertEqual(200, sync_channel.code)
        next_batch = sync_channel.json_body["next_batch"]


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


class SyncCreateEventInPrejoinStateTestCase(unittest.HomeserverTestCase):
    """MSC4311: Tests that m.room.create is present in invite_state and knock_state"""

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        knock.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        return config

    def test_create_event_present_in_invite_state(self) -> None:
        """m.room.create must appear in invite_state."""
        inviter = self.register_user("inviter", "pass")
        inviter_tok = self.login("inviter", "pass")
        invitee = self.register_user("invitee", "pass")
        invitee_tok = self.login("invitee", "pass")

        room_id = self.helper.create_room_as(inviter, tok=inviter_tok)
        self.helper.invite(room=room_id, src=inviter, targ=invitee, tok=inviter_tok)

        channel = self.make_request("GET", "/sync", access_token=invitee_tok)
        self.assertEqual(channel.code, 200, channel.json_body)

        invite_state_events = channel.json_body["rooms"]["invite"][room_id][
            "invite_state"
        ]["events"]
        event_types = {stripped_event["type"] for stripped_event in invite_state_events}
        self.assertIn(EventTypes.Create, event_types)

    def test_create_event_present_in_knock_state(self) -> None:
        """m.room.create must appear in knock_state."""
        host = self.register_user("host", "pass")
        host_tok = self.login("host", "pass")
        knocker = self.register_user("knocker", "pass")
        knocker_tok = self.login("knocker", "pass")

        room_id = self.helper.create_room_as(
            host, is_public=False, room_version="7", tok=host_tok
        )
        self.helper.send_state(
            room_id,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=host_tok,
        )

        self.helper.knock(room_id, knocker, tok=knocker_tok)

        channel = self.make_request("GET", "/sync", access_token=knocker_tok)
        self.assertEqual(channel.code, 200, channel.json_body)

        knock_state_events = channel.json_body["rooms"]["knock"][room_id][
            "knock_state"
        ]["events"]
        event_types = {stripped_event["type"] for stripped_event in knock_state_events}
        self.assertIn(EventTypes.Create, event_types)


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


class DeviceListSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device list (`device_lists`) changes.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

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
            "/sync",
            access_token=bob_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch_token = channel.json_body["next_batch"]

        # ...and then an incremental sync. This should block until the sync stream is woken up,
        # which we hope will happen as a result of Alice updating their device list.
        bob_sync_channel = self.make_request(
            "GET",
            f"/sync?since={next_batch_token}&timeout=30000",
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
            "/sync",
            access_token=bob_access_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch_token = channel.json_body["next_batch"]

        # ...and then an incremental sync. This should block until the sync stream is woken up,
        # which we hope will happen as a result of Alice updating their device list.
        bob_sync_channel = self.make_request(
            "GET",
            f"/sync?since={next_batch_token}&timeout=1000",
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
        channel = self.make_request("GET", "/sync", access_token=alice_access_token)
        self.assertEqual(channel.code, 200, channel.json_body)
        next_batch = channel.json_body["next_batch"]

        # Now, make an incremental sync request.
        # It won't return until something has happened
        incremental_sync_channel = self.make_request(
            "GET",
            f"/sync?since={next_batch}&timeout=30000",
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


class DeviceOneTimeKeysSyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device one time keys (`device_one_time_keys_count`) changes.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

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
        channel = self.make_request("GET", "/sync", access_token=alice_access_token)
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
        channel = self.make_request("GET", "/sync", access_token=alice_access_token)
        self.assertEqual(channel.code, 200, channel.json_body)

        # Check for those one time key counts
        self.assertDictEqual(
            channel.json_body["device_one_time_keys_count"],
            {"alg1": 1, "alg2": 2, "signed_curve25519": 0},
            channel.json_body["device_one_time_keys_count"],
        )


class DeviceUnusedFallbackKeySyncTestCase(unittest.HomeserverTestCase):
    """
    Tests regarding device one time keys (`device_unused_fallback_key_types`) changes.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
    ]

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
        channel = self.make_request("GET", "/sync", access_token=alice_access_token)
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
        channel = self.make_request("GET", "/sync", access_token=alice_access_token)
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


class SyncCancellationTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
        room.register_servlets,
    ]

    def test_initial_sync(self) -> None:
        """Tests that an initial sync request can be cancelled."""
        user_id = self.register_user("user", "password")
        tok = self.login("user", "password")

        # Populate the account with a few rooms
        for _ in range(5):
            room_id = self.helper.create_room_as(user_id, tok=tok)
            self.helper.send(room_id, tok=tok)

        channel = make_request_with_cancellation_test(
            "test_initial_sync",
            self.reactor,
            self.site,
            "GET",
            "/_matrix/client/v3/sync",
            token=tok,
        )

        self.assertEqual(200, channel.code, msg=channel.result["body"])

    def test_incremental_sync(self) -> None:
        """Tests that an incremental sync request can be cancelled."""
        user_id = self.register_user("user", "password")
        tok = self.login("user", "password")

        # Populate the account with a few rooms
        room_ids = []
        for _ in range(5):
            room_id = self.helper.create_room_as(user_id, tok=tok)
            self.helper.send(room_id, tok=tok)
            room_ids.append(room_id)

        # Do an initial sync to get a since token.
        channel = self.make_request("GET", "/sync", access_token=tok)
        self.assertEqual(200, channel.code, msg=channel.result)
        since = channel.json_body["next_batch"]

        # Send some more messages to generate activity in the rooms.
        for room_id in room_ids:
            self.helper.send(room_id, tok=tok)

        channel = make_request_with_cancellation_test(
            "test_incremental_sync",
            self.reactor,
            self.site,
            "GET",
            f"/_matrix/client/v3/sync?since={since}&timeout=10000",
            token=tok,
        )

        self.assertEqual(200, channel.code, msg=channel.result["body"])


class SyncStateAfterTimelineStateTestCase(unittest.HomeserverTestCase):
    """Tests for the MSC4222 invariant that any state event served in a sync
    response's *timeline* is reflected in `state_after` (unless it lost state
    resolution): `state at since` + `state_after` must equal the state at the
    end of the timeline. See
    https://github.com/matrix-org/matrix-spec-proposals/pull/4222.

    This breaks when the `since` token falls inside a persist batch: the
    `current_state_delta_stream` row for a state event persisted in a batch is
    stamped with the *minimum* stream ordering of the batch, so such a token
    selects the event for the timeline but not its delta. A worker reading the
    events stream from replication routinely observes such a token, which is
    why this is seen on worker deployments and not on a single process.

    The remaining tests guard that whatever repairs this reports the *resolved*
    state at the end of the timeline rather than replaying the timeline.

    The first two tests are storage- and replication-level and would normally
    live under `tests/storage/` / `tests/replication/`; they are kept here
    deliberately, as the documented preconditions of the end-to-end tests
    beside them (with which they share the `_persist_batch` helper).
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.persistence = hs.get_storage_controllers().persistence
        assert self.persistence is not None

        self.alice = self.register_user("alice", "password")
        self.alice_tok = self.login("alice", "password")
        self.bob = self.register_user("bob", "password")
        self.bob_tok = self.login("bob", "password")

        self.get_success(
            self.store.set_features_for_user(
                self.alice, {ExperimentalFeature.MSC4222: True}
            )
        )

        # Named room, so that hero calculation doesn't inject current
        # membership state into the response and confuse the assertions.
        self.room_id = self.helper.create_room_as(
            self.alice, tok=self.alice_tok, extra_content={"name": "test room"}
        )
        self.helper.join(self.room_id, self.bob, tok=self.bob_tok)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _sync_url(self, lazy_load_members: bool, timeline_limit: int = 10) -> str:
        # The default `timeline_limit` of 10 is arbitrary: comfortably more
        # events than any test here produces between syncs, so the timeline is
        # never truncated unless a test passes a smaller limit on purpose.
        sync_filter: JsonDict = {"room": {"timeline": {"limit": timeline_limit}}}
        if lazy_load_members:
            sync_filter["room"]["state"] = {"lazy_load_members": True}
        return f"/sync?filter={json.dumps(sync_filter)}&org.matrix.msc4222.use_state_after=true"

    def _sync(self, sync_url: str, since: str | None = None) -> JsonDict:
        url = sync_url if since is None else f"{sync_url}&since={since}"
        channel = self.make_request("GET", url, access_token=self.alice_tok)
        self.assertEqual(channel.code, 200, channel.result)
        return channel.json_body

    def _joined_room(self, response: JsonDict) -> JsonDict:
        rooms = response["rooms"].get("join", {})
        self.assertIn(self.room_id, rooms, f"room missing from sync: {response}")
        return rooms[self.room_id]

    def _timeline_ids(self, room: JsonDict) -> list[str]:
        return [e["event_id"] for e in room["timeline"]["events"]]

    def _state_after_ids(self, room: JsonDict) -> list[str]:
        return [e["event_id"] for e in room["org.matrix.msc4222.state_after"]["events"]]

    def _persist_batch(self) -> tuple[EventBase, EventBase]:
        """Persist a message and a state event in a *single* persist batch
        (a single `_persist_events_and_state_updates` call), with the message
        first, so that the message's stream ordering is the batch minimum."""
        assert self.persistence is not None
        prev_event_ids = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )

        message, message_ctx = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.room.message",
                sender=self.alice,
                content={"msgtype": "m.text", "body": "batched message"},
                prev_event_ids=prev_event_ids,
            )
        )
        state_event, state_ctx = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.call.member",
                state_key=self.alice,
                sender=self.alice,
                content={"memberships": [{"device_id": "BATCHED"}]},
                prev_event_ids=prev_event_ids,
            )
        )

        self.get_success(
            self.persistence.persist_events(
                [(message, message_ctx), (state_event, state_ctx)]
            )
        )
        return message, state_event

    def _assert_state_after_is_current_state(self, room: JsonDict) -> None:
        """For a joined room `end_token` is the global `now_token`, so every
        entry in `state_after` must be the room's current state for that key.

        This is the guard against "fix" shapes that seed `state_after` straight
        from the timeline: a state event can be in the timeline and *not* be the
        state at the end of it.
        """
        current_state = self.get_success(
            self.store.get_partial_current_state_ids(self.room_id)
        )
        current_ids = set(current_state.values())
        for event in room["org.matrix.msc4222.state_after"]["events"]:
            self.assertIn(
                event["event_id"],
                current_ids,
                f"state_after reports {event['type']}/{event['state_key']} = "
                f"{event['event_id']}, which is not the current state "
                f"({current_state.get((event['type'], event['state_key']))})",
            )

    # ------------------------------------------------------------------
    # The persist-batch window
    # ------------------------------------------------------------------

    def test_state_delta_stream_id_of_batched_state_event(self) -> None:
        """Documents that `current_state_delta_stream.stream_id` for a state
        event persisted in a batch is the *minimum* stream ordering of the batch
        (see `_persist_events_txn`: `min_stream_order`), not the state event's
        own stream ordering.

        This is the storage-level precondition for the "timeline has it,
        state_after doesn't" symptom: any sync token that falls strictly between
        the batch minimum and the state event's stream ordering will put the
        state event in the timeline while excluding its delta.
        """
        message, state_event = self._persist_batch()

        message_pos = message.internal_metadata.stream_ordering
        state_pos = state_event.internal_metadata.stream_ordering
        assert message_pos is not None and state_pos is not None
        self.assertLess(message_pos, state_pos)

        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="current_state_delta_stream",
                keyvalues={"room_id": self.room_id, "type": "m.call.member"},
                retcols=("stream_id", "event_id", "instance_name"),
                desc="test_state_delta_stream_id",
            )
        )
        self.assertEqual(len(rows), 1, rows)
        delta_stream_id, delta_event_id, _instance_name = rows[0]
        self.assertEqual(delta_event_id, state_event.event_id)

        self.assertEqual(
            delta_stream_id,
            message_pos,
            "expected the delta to be recorded at the batch minimum",
        )

        deltas = self.get_success(
            self.store.get_current_state_deltas_for_room(
                self.room_id,
                from_token=RoomStreamToken(stream=message_pos),
                to_token=RoomStreamToken(stream=state_pos),
            )
        )
        self.assertEqual(
            [d.event_id for d in deltas],
            [],
            "state delta unexpectedly visible in (message_pos, state_pos]",
        )

    def test_state_after_with_token_inside_persist_batch(self) -> None:
        """The end-to-end consequence of the above: if a client's `since` token
        lands strictly inside a persist batch (which a *reader* worker can
        observe, because it advances its events-stream position from replication
        RDATA batches that may split a persist batch), the state event is in the
        timeline of the next sync and must also be in `state_after`.
        """
        sync_url = self._sync_url(lazy_load_members=False)
        base = self._sync(sync_url)["next_batch"]
        base_token = self.get_success(StreamToken.from_string(self.store, base))

        message, state_event = self._persist_batch()
        message_pos = message.internal_metadata.stream_ordering
        assert message_pos is not None

        # A token positioned just after the message but before the state event
        # -- i.e. in the middle of the persist batch.
        split_token = base_token.copy_and_replace(
            StreamKeyType.ROOM, RoomStreamToken(stream=message_pos)
        )

        deltas_up_to_split = self.get_success(
            self.store.get_current_state_deltas_for_room(
                self.room_id,
                from_token=base_token.room_key,
                to_token=RoomStreamToken(stream=message_pos),
            )
        )
        self.assertIn(
            state_event.event_id,
            [d.event_id for d in deltas_up_to_split],
            "expected the delta to be visible *before* its event",
        )

        split_since = self.get_success(split_token.to_string(self.store))
        room = self._joined_room(self._sync(sync_url, split_since))
        self.assertIn(state_event.event_id, self._timeline_ids(room))
        self.assertIn(
            state_event.event_id,
            self._state_after_ids(room),
            f"state event in timeline but missing from state_after when the "
            f"since token splits a persist batch: {room}",
        )

    def test_events_replication_stream_splits_persist_batches(self) -> None:
        """Shows that the `since` token used by
        `test_state_after_with_token_inside_persist_batch` is one a real
        deployment hands out.

        The events replication stream emits a distinct token for every event
        stream ordering (`_batch_updates` only collapses rows that share a
        token), and `_process_rdata` calls `on_rdata` -- and hence
        `process_replication_position` -- once per token. So a reader worker
        (e.g. a sync worker) advances its events-stream position *through* the
        middle of a persist batch, one event at a time, while reading
        `current_state_delta_stream` straight from the database.
        """
        before = self.store.get_room_max_token().stream
        message, state_event = self._persist_batch()
        after = self.store.get_room_max_token().stream

        message_pos = message.internal_metadata.stream_ordering
        state_pos = state_event.internal_metadata.stream_ordering
        assert message_pos is not None and state_pos is not None

        stream = EventsStream(self.hs)
        updates, _upto, _limited = self.get_success(
            # A limit of 100 is comfortably more than the handful of rows this
            # test persists, so the update batch is never truncated.
            stream._update_function("master", before, after, 100)
        )

        # The tokens a reader will actually advance to, in order.
        delivered = [token for token, _row in _batch_updates(updates) if token]

        self.assertIn(
            message_pos,
            delivered,
            "a reader worker advances to the batch minimum before it sees the "
            "state event",
        )
        self.assertIn(state_pos, delivered)
        self.assertLess(delivered.index(message_pos), delivered.index(state_pos))

    # ------------------------------------------------------------------
    # Guards: `state_after` must be the *resolved* state, not a replay of
    # the timeline
    # ------------------------------------------------------------------

    def test_state_res_loser_alone_in_timeline(self) -> None:
        """A state event that appears in the timeline but *loses* state
        resolution must not be reported in `state_after` -- `state_after` is
        the resolved state at the end of the timeline, not a replay of the
        timeline.

        Here the client has already synced past the first of two conflicting
        events, so the second one arrives alone in the timeline with no delta of
        its own.
        """
        sync_url = self._sync_url(lazy_load_members=False)

        fork_point = self.get_success(self.store.get_prev_events_for_room(self.room_id))

        # Pin the state resolution outcome: conflicted events at the same
        # mainline position are applied in `(origin_server_ts, event_id)`
        # order with the last application winning, so giving the first event
        # the later timestamp makes it deterministically beat the second one.
        now = self.clock.time_msec()

        first = self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                type="m.call.member",
                state_key=self.alice,
                sender=self.alice,
                content={"memberships": [{"device_id": "FIRST"}]},
                prev_event_ids=fork_point,
                origin_server_ts=now + 1000,
            )
        )

        # The client syncs past the first event.
        since = self._sync(sync_url)["next_batch"]

        # A conflicting event forked off the same point arrives afterwards.
        second = self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                type="m.call.member",
                state_key=self.alice,
                sender=self.alice,
                content={"memberships": [{"device_id": "SECOND"}]},
                prev_event_ids=fork_point,
                origin_server_ts=now,
            )
        )

        current_state = self.get_success(
            self.store.get_partial_current_state_ids(self.room_id)
        )
        self.assertEqual(
            current_state[("m.call.member", self.alice)],
            first.event_id,
            "test setup: expected the first event to win state resolution",
        )

        room = self._joined_room(self._sync(sync_url, since))
        self.assertIn(second.event_id, self._timeline_ids(room))
        self._assert_state_after_is_current_state(room)
        self.assertNotIn(
            second.event_id,
            self._state_after_ids(room),
            "a state-res loser leaked into state_after",
        )

    def test_state_after_is_current_state_with_split_token(self) -> None:
        """Whatever mechanism repairs the persist-batch window must not report
        anything other than the state at the end of the timeline."""
        sync_url = self._sync_url(lazy_load_members=False)
        base = self._sync(sync_url)["next_batch"]
        base_token = self.get_success(StreamToken.from_string(self.store, base))

        message, state_event = self._persist_batch()
        message_pos = message.internal_metadata.stream_ordering
        assert message_pos is not None

        split_token = base_token.copy_and_replace(
            StreamKeyType.ROOM, RoomStreamToken(stream=message_pos)
        )
        since = self.get_success(split_token.to_string(self.store))

        room = self._joined_room(self._sync(sync_url, since))
        self._assert_state_after_is_current_state(room)
        self.assertIn(state_event.event_id, self._state_after_ids(room))

    def test_gappy_timeline_with_split_token(self) -> None:
        """The gappy variant of the persist-batch window: the `since` token
        splits a persist batch *and* the timeline is truncated so that the
        state event falls outside the window. The state event is then in
        neither the timeline nor the deltas, but it is a state change since
        `since`, so `state_after` must still report it -- with `limited: true`
        the client relies entirely on `state_after` to bridge the gap.
        """
        sync_url = self._sync_url(lazy_load_members=False, timeline_limit=3)
        base = self._sync(sync_url)["next_batch"]
        base_token = self.get_success(StreamToken.from_string(self.store, base))

        message, state_event = self._persist_batch()
        message_pos = message.internal_metadata.stream_ordering
        assert message_pos is not None

        # Push the state event out of the timeline window.
        for i in range(10):
            self.helper.send(self.room_id, body=f"filler {i}", tok=self.bob_tok)

        split_token = base_token.copy_and_replace(
            StreamKeyType.ROOM, RoomStreamToken(stream=message_pos)
        )
        since = self.get_success(split_token.to_string(self.store))

        room = self._joined_room(self._sync(sync_url, since))
        self.assertTrue(room["timeline"].get("limited"), room)
        self.assertNotIn(state_event.event_id, self._timeline_ids(room))
        self.assertIn(
            state_event.event_id,
            self._state_after_ids(room),
            f"state event outside a truncated timeline is missing from "
            f"state_after when the since token splits a persist batch: {room}",
        )
