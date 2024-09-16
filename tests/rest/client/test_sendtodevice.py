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
from parameterized import parameterized_class

from synapse.api.constants import EduTypes
from synapse.rest import admin
from synapse.rest.client import login, sendtodevice, sync
from synapse.types import JsonDict

from tests.unittest import HomeserverTestCase, override_config


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
class SendToDeviceTestCase(HomeserverTestCase):
    """
    Test `/sendToDevice` will deliver messages across to people receiving them over `/sync`.

    Attributes:
        sync_endpoint: The endpoint under test to use for syncing.
        experimental_features: The experimental features homeserver config to use.
    """

    sync_endpoint: str
    experimental_features: JsonDict

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sendtodevice.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = self.experimental_features
        return config

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
        channel = self.make_request("GET", self.sync_endpoint, access_token=user2_tok)
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
        channel = self.make_request("GET", self.sync_endpoint, access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body["to_device"], expected_result)

        # it should *not* appear if we do an incremental sync
        sync_token = channel.json_body["next_batch"]
        channel = self.make_request(
            "GET",
            f"{self.sync_endpoint}?since={sync_token}",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body.get("to_device", {}).get("events", []), [])

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
        channel = self.make_request("GET", self.sync_endpoint, access_token=user2_tok)
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
            f"{self.sync_endpoint}?since={sync_token}",
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
        channel = self.make_request("GET", self.sync_endpoint, access_token=user2_tok)
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
            f"{self.sync_endpoint}?since={sync_token}",
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

    def test_limited_sync(self) -> None:
        """If a limited sync for to-devices happens the next /sync should respond immediately."""

        self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        # Do an initial sync
        channel = self.make_request("GET", self.sync_endpoint, access_token=user2_tok)
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
            f"{self.sync_endpoint}?since={sync_token}&timeout=300000",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        messages = channel.json_body.get("to_device", {}).get("events", [])
        self.assertEqual(len(messages), 100)
        sync_token = channel.json_body["next_batch"]

        channel = self.make_request(
            "GET",
            f"{self.sync_endpoint}?since={sync_token}&timeout=300000",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        messages = channel.json_body.get("to_device", {}).get("events", [])
        self.assertEqual(len(messages), 50)
