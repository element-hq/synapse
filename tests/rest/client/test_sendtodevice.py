#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from unittest.mock import AsyncMock, Mock

from canonicaljson import encode_canonical_json

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import (
    MAX_EDUS_PER_TRANSACTION,
    SOFT_MAX_EDU_SIZE,
    EduTypes,
)
from synapse.api.errors import Codes
from synapse.handlers.devicemessage import (
    _create_new_to_device_edu,
)
from synapse.rest import admin
from synapse.rest.client import login, sendtodevice, sync
from synapse.rest.client.sendtodevice import UNKNOWN_RECIPIENT_SET, recipient_set_hash
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.server import FakeChannel
from tests.unittest import HomeserverTestCase, override_config


class SendToDeviceTestCase(HomeserverTestCase):
    """
    Test `/sendToDevice` will deliver messages across to people receiving them over `/sync`.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sendtodevice.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["federation_sender_instances"] = None
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_transport_client = Mock(spec=["send_transaction"])
        self.federation_transport_client.send_transaction = AsyncMock()
        hs = self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )

        return hs

    def test_user_to_user(self) -> None:
        """A to-device message from one user to another should get delivered"""

        user1 = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        # send the message
        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user2: {"d2": test_msg}}},
            access_token=user1_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        # check it appears
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        expected_result = {
            "events": [
                {
                    "sender": user1,
                    "type": "m.test",
                    "content": test_msg,
                }
            ]
        }
        self.assertEqual(channel.json_body["to_device"], expected_result)

        # it should re-appear if we do another sync because the to-device message is not
        # deleted until we acknowledge it by sending a `?since=...` parameter in the
        # next sync request corresponding to the `next_batch` value from the response.
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body["to_device"], expected_result)

        # it should *not* appear if we do an incremental sync
        sync_token = channel.json_body["next_batch"]
        channel = self.make_request(
            "GET",
            f"/sync?since={sync_token}",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body.get("to_device", {}).get("events", []), [])

    def test_large_remote_todevice(self) -> None:
        """A to-device message needs to fit in the EDU size limit"""
        _ = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        # Create a message that is over the `SOFT_MAX_EDU_SIZE`
        test_msg = {"foo": "a" * SOFT_MAX_EDU_SIZE}
        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/12345",
            content={"messages": {"@remote_user:secondserver": {"device": test_msg}}},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 413, channel.result)
        self.assertEqual(Codes.TOO_LARGE, channel.json_body["errcode"])

    def test_edu_large_messages_splitting(self) -> None:
        """
        Test that a bunch of to-device messages are split over multiple EDUs if they are
        collectively too large to fit into a single EDU
        """
        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        _ = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")
        destination = "secondserver"
        messages = {}

        # 2 messages, each just big enough to fit into their own EDU
        for i in range(2):
            messages[f"@remote_user{i}:" + destination] = {
                "device": {"foo": "a" * (SOFT_MAX_EDU_SIZE - 1000)}
            }

        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234567",
            content={"messages": messages},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.get_success(sender.send_device_messages([destination]))

        number_of_edus_sent = 0
        for call in mock_send_transaction.call_args_list:
            number_of_edus_sent += len(call[0][1]()["edus"])

        self.assertEqual(number_of_edus_sent, 2)

    def test_edu_large_messages_splitting_one_user(self) -> None:
        """
        Test that a bunch of to-device messages for the same user are split over multiple EDUs if they are
        collectively too large to fit into a single EDU
        """
        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        _ = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")
        destination = "secondserver"
        messages = {}

        # 2 messages, each just big enough to fit into their own EDU
        messages["@remote_user:" + destination] = {
            "device1": {"foo": "a" * (SOFT_MAX_EDU_SIZE - 1000)},
            "device2": {"foo": "a" * (SOFT_MAX_EDU_SIZE - 1000)},
        }

        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/12345678",
            content={"messages": messages},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.get_success(sender.send_device_messages([destination]))

        number_of_edus_sent = 0
        for call in mock_send_transaction.call_args_list:
            number_of_edus_sent += len(call[0][1]()["edus"])

        self.assertEqual(number_of_edus_sent, 2)

    def test_edu_large_messages_not_splitting_one_user(self) -> None:
        """Test that a couple of to-device messages for the same user that just
        collectively fit in a single EDU do not get split into multiple EDUs.

        This tests that we don't over-split messages into multiple EDUs when we
        don't need to.
        """

        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        user_id = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")
        destination = "secondserver"
        remote_user = "@remote_user:" + destination
        messages = {}

        # We create two messages such that the combined size of the messages is
        # just under the `SOFT_MAX_EDU_SIZE` limit, so they should be able to
        # fit in a single EDU together, but not separately.
        wrapper_size = len(
            encode_canonical_json(_create_new_to_device_edu(user_id, "m.test", {}))
        )
        max_size_of_message = (
            SOFT_MAX_EDU_SIZE
            - wrapper_size
            # Size of the device content wrapper, `{"remote_user": …}`
            - (len(remote_user) + 5)
        )
        messages[remote_user] = {
            # The constants are the size of each individual message
            "d1": {"a": "a" * (max_size_of_message - 13 - 16)},
            "d2": {"b": "b"},
        }

        # The full EDU size should now be just shy of the `SOFT_MAX_EDU_SIZE` limit
        full_edu = encode_canonical_json(
            _create_new_to_device_edu(user_id, "m.test", messages)
        )
        self.assertEqual(len(full_edu), SOFT_MAX_EDU_SIZE - 1)

        # Now send the messages, and check they are sent in a single EDU.
        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/12345678",
            content={"messages": messages},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.get_success(sender.send_device_messages([destination]))

        number_of_edus_sent = 0
        for call in mock_send_transaction.call_args_list:
            number_of_edus_sent += len(call[0][1]()["edus"])

        self.assertEqual(number_of_edus_sent, 1)

    def test_edu_small_messages_not_splitting(self) -> None:
        """
        Test that a couple of small messages do not get split into multiple EDUs
        """
        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        _ = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")
        destination = "secondserver"
        messages = {}

        # 2 small messages that should fit in a single EDU
        for i in range(2):
            messages[f"@remote_user{i}:" + destination] = {"device": {"foo": "a" * 100}}

        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/123456",
            content={"messages": messages},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.get_success(sender.send_device_messages([destination]))

        number_of_edus_sent = 0
        for call in mock_send_transaction.call_args_list:
            number_of_edus_sent += len(call[0][1]()["edus"])

        self.assertEqual(number_of_edus_sent, 1)

    def test_transaction_splitting(self) -> None:
        """Test that a bunch of to-device messages are split into multiple transactions if there are too many EDUs"""
        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        _ = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")
        destination = "secondserver"
        messages = {}

        number_of_edus_to_send = MAX_EDUS_PER_TRANSACTION + 1

        for i in range(number_of_edus_to_send):
            messages[f"@remote_user{i}:" + destination] = {
                "device": {"foo": "a" * (SOFT_MAX_EDU_SIZE - 1000)}
            }

        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/12345678",
            content={"messages": messages},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        self.get_success(sender.send_device_messages([destination]))

        # At least 2 transactions should be sent since we are over the EDU limit per transaction
        self.assertGreaterEqual(mock_send_transaction.call_count, 2)

        number_of_edus_sent = 0
        for call in mock_send_transaction.call_args_list:
            number_of_edus_sent += len(call[0][1]()["edus"])

        self.assertEqual(number_of_edus_sent, number_of_edus_to_send)

    @override_config({"rc_key_requests": {"per_second": 10, "burst_count": 2}})
    def test_local_room_key_request(self) -> None:
        """m.room_key_request has special-casing; test from local user"""
        user1 = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        # send three messages
        for i in range(3):
            chan = self.make_request(
                "PUT",
                f"/_matrix/client/r0/sendToDevice/m.room_key_request/{i}",
                content={"messages": {user2: {"d2": {"idx": i}}}},
                access_token=user1_tok,
            )
            self.assertEqual(chan.code, 200, chan.result)

        # now sync: we should get two of the three (because burst_count=2)
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        msgs = channel.json_body["to_device"]["events"]
        self.assertEqual(len(msgs), 2)
        for i in range(2):
            self.assertEqual(
                msgs[i],
                {
                    "sender": user1,
                    "type": "m.room_key_request",
                    "content": {"idx": i},
                },
            )
        sync_token = channel.json_body["next_batch"]

        # ... time passes
        self.reactor.advance(1)

        # and we can send more messages
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.room_key_request/3",
            content={"messages": {user2: {"d2": {"idx": 3}}}},
            access_token=user1_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        # ... which should arrive
        channel = self.make_request(
            "GET",
            f"/sync?since={sync_token}",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        msgs = channel.json_body["to_device"]["events"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(
            msgs[0],
            {"sender": user1, "type": "m.room_key_request", "content": {"idx": 3}},
        )

    @override_config({"rc_key_requests": {"per_second": 10, "burst_count": 2}})
    def test_remote_room_key_request(self) -> None:
        """m.room_key_request has special-casing; test from remote user"""
        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        federation_registry = self.hs.get_federation_registry()

        # send three messages
        for i in range(3):
            self.get_success(
                federation_registry.on_edu(
                    EduTypes.DIRECT_TO_DEVICE,
                    "remote_server",
                    {
                        "sender": "@user:remote_server",
                        "type": "m.room_key_request",
                        "messages": {user2: {"d2": {"idx": i}}},
                        "message_id": f"{i}",
                    },
                )
            )

        # now sync: we should get two of the three
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        msgs = channel.json_body["to_device"]["events"]
        self.assertEqual(len(msgs), 2)
        for i in range(2):
            self.assertEqual(
                msgs[i],
                {
                    "sender": "@user:remote_server",
                    "type": "m.room_key_request",
                    "content": {"idx": i},
                },
            )
        sync_token = channel.json_body["next_batch"]

        # ... time passes
        self.reactor.advance(1)

        # and we can send more messages
        self.get_success(
            federation_registry.on_edu(
                EduTypes.DIRECT_TO_DEVICE,
                "remote_server",
                {
                    "sender": "@user:remote_server",
                    "type": "m.room_key_request",
                    "messages": {user2: {"d2": {"idx": 3}}},
                    "message_id": "3",
                },
            )
        )

        # ... which should arrive
        channel = self.make_request(
            "GET",
            f"/sync?since={sync_token}",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        msgs = channel.json_body["to_device"]["events"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(
            msgs[0],
            {
                "sender": "@user:remote_server",
                "type": "m.room_key_request",
                "content": {"idx": 3},
            },
        )

    def test_remote_spoofed_sender(self) -> None:
        """
        Tests that a to-device message whose sender domain does not match the origin
        server is dropped.
        """
        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        federation_registry = self.hs.get_federation_registry()

        # Send a spoofed to-device message EDU
        self.get_success(
            federation_registry.on_edu(
                EduTypes.DIRECT_TO_DEVICE,
                "example.org",
                {
                    "sender": "@user:not.the.same.example.org",
                    "type": "org.example.test",
                    "messages": {user2: {"d2": {"foo": "bar"}}},
                    "message_id": "1",
                },
            )
        )
        # Also send a valid one as a sentinel, to make sure our test setup
        # is working as we'd expect
        self.get_success(
            federation_registry.on_edu(
                EduTypes.DIRECT_TO_DEVICE,
                "example.org",
                {
                    "sender": "@user:example.org",
                    "type": "org.example.test",
                    "messages": {user2: {"d2": {"hiss": "meow"}}},
                    "message_id": "1",
                },
            )
        )

        # Then do a /sync and be sure it didn't come down
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        messages = channel.json_body.get("to_device", {}).get("events", [])
        self.assertEqual(
            messages,
            [
                # Only the sentinel (valid) to-device message came down, not the spoofed one
                {
                    "content": {"hiss": "meow"},
                    "sender": "@user:example.org",
                    "type": "org.example.test",
                }
            ],
        )

    def test_limited_sync(self) -> None:
        """If a limited sync for to-devices happens the next /sync should respond immediately."""

        self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        # Do an initial sync
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        sync_token = channel.json_body["next_batch"]

        # Send 150 to-device messages. We limit to 100 in `/sync`
        for i in range(150):
            test_msg = {"foo": "bar"}
            chan = self.make_request(
                "PUT",
                f"/_matrix/client/r0/sendToDevice/m.test/1234-{i}",
                content={"messages": {user2: {"d2": test_msg}}},
                access_token=user1_tok,
            )
            self.assertEqual(chan.code, 200, chan.result)

        channel = self.make_request(
            "GET",
            f"/sync?since={sync_token}&timeout=300000",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        messages = channel.json_body.get("to_device", {}).get("events", [])
        self.assertEqual(len(messages), 100)
        sync_token = channel.json_body["next_batch"]

        channel = self.make_request(
            "GET",
            f"/sync?since={sync_token}&timeout=300000",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        messages = channel.json_body.get("to_device", {}).get("events", [])
        self.assertEqual(len(messages), 50)


class SharedToDeviceTestCase(HomeserverTestCase):
    """`/sendToDevice` with the content once and a cached recipient set."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sendtodevice.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault(
            "experimental_features", {"photon_shared_to_device_enabled": True}
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sender = self.register_user("sender", "pass")
        self.sender_tok = self.login("sender", "pass", "S")
        self.tokens = {}
        for name in ("a", "b", "c", "d"):
            user = self.register_user(name, "pass")
            self.tokens[(user, name.upper())] = self.login(name, "pass", name.upper())
        self.txn = 0

    def send(self, recipients: JsonDict, body: str) -> FakeChannel:
        self.txn += 1
        return self.make_request(
            "PUT",
            f"/_matrix/client/v3/sendToDevice/m.test/{self.txn}",
            content={"content": {"body": body}, "recipients": recipients},
            access_token=self.sender_tok,
        )

    def received(self) -> set[tuple[str, str]]:
        """Which devices have a message they haven't seen yet, consuming it."""
        got = set()
        for (user, device), tok in self.tokens.items():
            since = getattr(self, f"since_{device}", None)
            path = "/sync" + (f"?since={since}" if since else "")
            channel = self.make_request("GET", path, access_token=tok)
            setattr(self, f"since_{device}", channel.json_body["next_batch"])
            if channel.json_body.get("to_device", {}).get("events"):
                got.add((user, device))
        return got

    def pair(self, name: str) -> tuple[str, str]:
        return (f"@{name}:test", name.upper())

    def test_base_plus_add_minus_remove(self) -> None:
        a, b, c, d = (self.pair(n) for n in "abcd")
        abc = recipient_set_hash(frozenset([a, b, c]))

        # Full list seeds hash(a b c).
        self.assertEqual(self.send({"add": [a, b, c]}, "1").code, 200)
        self.assertEqual(self.received(), {a, b, c})

        # hash(a b c) + d, which seeds hash(a b c d).
        self.assertEqual(self.send({"base": abc, "add": [d]}, "2").code, 200)
        self.assertEqual(self.received(), {a, b, c, d})
        abcd = recipient_set_hash(frozenset([a, b, c, d]))

        # hash(a b c d) - b.
        self.assertEqual(self.send({"base": abcd, "remove": [b]}, "3").code, 200)
        self.assertEqual(self.received(), {a, c, d})

        # The older base is still usable.
        self.assertEqual(self.send({"base": abc}, "4").code, 200)
        self.assertEqual(self.received(), {a, b, c})

    def test_unknown_base(self) -> None:
        channel = self.send({"base": "nope", "add": [self.pair("a")]}, "x")
        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], UNKNOWN_RECIPIENT_SET)
        self.assertEqual(self.received(), set())

    def test_sets_are_per_sending_device(self) -> None:
        a = self.pair("a")
        self.assertEqual(self.send({"add": [a]}, "1").code, 200)
        other_tok = self.login("sender", "pass", "S2")
        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/sendToDevice/m.test/other",
            content={
                "content": {},
                "recipients": {"base": recipient_set_hash(frozenset([a]))},
            },
            access_token=other_tok,
        )
        self.assertEqual(channel.json_body["errcode"], UNKNOWN_RECIPIENT_SET)

    @override_config(
        {"experimental_features": {"photon_shared_to_device_enabled": False}}
    )
    def test_disabled(self) -> None:
        channel = self.send({"add": [self.pair("a")]}, "x")
        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], Codes.MISSING_PARAM)
