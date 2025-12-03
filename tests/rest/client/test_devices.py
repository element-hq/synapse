#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from http import HTTPStatus

from twisted.internet.defer import ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.api.errors import NotFoundError
from synapse.appservice import ApplicationService
from synapse.rest import admin, devices, sync
from synapse.rest.client import keys, login, register
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock

from tests import unittest


class DevicesTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.handler = hs.get_device_handler()

    @unittest.override_config({"delete_stale_devices_after": 72000000})
    def test_delete_stale_devices(self) -> None:
        """Tests that stale devices are automatically removed after a set time of
        inactivity.
        The configuration is set to delete devices that haven't been used in the past 20h.
        """
        # Register a user and creates 2 devices for them.
        user_id = self.register_user("user", "password")
        tok1 = self.login("user", "password", device_id="abc")
        tok2 = self.login("user", "password", device_id="def")

        # Sync them so they have a last_seen value.
        self.make_request("GET", "/sync", access_token=tok1)
        self.make_request("GET", "/sync", access_token=tok2)

        # Advance half a day and sync again with one of the devices, so that the next
        # time the background job runs we don't delete this device (since it will look
        # for devices that haven't been used for over an hour).
        self.reactor.advance(43200)
        self.make_request("GET", "/sync", access_token=tok1)

        # Advance another half a day, and check that the device that has synced still
        # exists but the one that hasn't has been removed.
        self.reactor.advance(43200)
        self.get_success(self.handler.get_device(user_id, "abc"))
        self.get_failure(self.handler.get_device(user_id, "def"), NotFoundError)


class DehydratedDeviceTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        register.register_servlets,
        devices.register_servlets,
        keys.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.registration = hs.get_registration_handler()
        self.message_handler = hs.get_device_message_handler()

    def test_PUT(self) -> None:
        """Sanity-check that we can PUT a dehydrated device.

        Detects https://github.com/matrix-org/synapse/issues/14334.
        """
        alice = self.register_user("alice", "correcthorse")
        token = self.login(alice, "correcthorse")

        # Have alice update their device list
        channel = self.make_request(
            "PUT",
            "_matrix/client/unstable/org.matrix.msc2697.v2/dehydrated_device",
            {
                "device_data": {
                    "algorithm": "org.matrix.msc2697.v1.dehydration.v1.olm",
                    "account": "dehydrated_device",
                },
                "device_keys": {
                    "user_id": "@alice:test",
                    "device_id": "device1",
                    "valid_until_ts": "80",
                    "algorithms": [
                        "m.olm.curve25519-aes-sha2",
                    ],
                    "keys": {
                        "<algorithm>:<device_id>": "<key_base64>",
                    },
                    "signatures": {
                        "<user_id>": {"<algorithm>:<device_id>": "<signature_base64>"}
                    },
                },
            },
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        device_id = channel.json_body.get("device_id")
        self.assertIsInstance(device_id, str)

    @unittest.override_config(
        {"experimental_features": {"msc2697_enabled": False, "msc3814_enabled": True}}
    )
    def test_dehydrate_msc3814(self) -> None:
        user = self.register_user("mikey", "pass")
        token = self.login(user, "pass", device_id="device1")
        content: JsonDict = {
            "device_data": {
                "algorithm": "m.dehydration.v1.olm",
            },
            "device_id": "device1",
            "initial_device_display_name": "foo bar",
            "device_keys": {
                "user_id": "@mikey:test",
                "device_id": "device1",
                "valid_until_ts": "80",
                "algorithms": [
                    "m.olm.curve25519-aes-sha2",
                ],
                "keys": {
                    "<algorithm>:<device_id>": "<key_base64>",
                },
                "signatures": {
                    "<user_id>": {"<algorithm>:<device_id>": "<signature_base64>"}
                },
            },
            "fallback_keys": {
                "alg1:device1": "f4llb4ckk3y",
                "signed_<algorithm>:<device_id>": {
                    "fallback": "true",
                    "key": "f4llb4ckk3y",
                    "signatures": {
                        "<user_id>": {"<algorithm>:<device_id>": "<key_base64>"}
                    },
                },
            },
            "one_time_keys": {"alg1:k1": "0net1m3k3y"},
        }
        channel = self.make_request(
            "PUT",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            content=content,
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        device_id = channel.json_body.get("device_id")
        assert device_id is not None
        self.assertIsInstance(device_id, str)
        self.assertEqual("device1", device_id)

        # test that we can now GET the dehydrated device info
        channel = self.make_request(
            "GET",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        returned_device_id = channel.json_body.get("device_id")
        self.assertEqual(returned_device_id, device_id)
        device_data = channel.json_body.get("device_data")
        expected_device_data = {
            "algorithm": "m.dehydration.v1.olm",
        }
        self.assertEqual(device_data, expected_device_data)

        # test that the keys are correctly uploaded
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    user: ["device1"],
                },
            },
            token,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body["device_keys"][user][device_id]["keys"],
            content["device_keys"]["keys"],
        )
        # first claim should return the onetime key we uploaded
        res = self.get_success(
            self.hs.get_e2e_keys_handler().claim_one_time_keys(
                {user: {device_id: {"alg1": 1}}},
                UserID.from_string(user),
                timeout=None,
                always_include_fallback_keys=False,
            )
        )
        self.assertEqual(
            res,
            {
                "failures": {},
                "one_time_keys": {user: {device_id: {"alg1:k1": "0net1m3k3y"}}},
            },
        )
        # second claim should return fallback key
        res2 = self.get_success(
            self.hs.get_e2e_keys_handler().claim_one_time_keys(
                {user: {device_id: {"alg1": 1}}},
                UserID.from_string(user),
                timeout=None,
                always_include_fallback_keys=False,
            )
        )
        self.assertEqual(
            res2,
            {
                "failures": {},
                "one_time_keys": {user: {device_id: {"alg1:device1": "f4llb4ckk3y"}}},
            },
        )

        # create another device for the user
        (
            new_device_id,
            _,
            _,
            _,
        ) = self.get_success(
            self.registration.register_device(
                user_id=user,
                device_id=None,
                initial_display_name="new device",
            )
        )
        requester = create_requester(user, device_id=new_device_id)

        # Send a message to the dehydrated device
        ensureDeferred(
            self.message_handler.send_device_message(
                requester=requester,
                message_type="test.message",
                messages={user: {device_id: {"body": "test_message"}}},
            )
        )
        self.pump()

        # make sure we can fetch the message with our dehydrated device id
        channel = self.make_request(
            "POST",
            f"_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device/{device_id}/events",
            content={},
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        expected_content = {"body": "test_message"}
        self.assertEqual(channel.json_body["events"][0]["content"], expected_content)

        # fetch messages again and make sure that the message was not deleted
        channel = self.make_request(
            "POST",
            f"_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device/{device_id}/events",
            content={},
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["events"][0]["content"], expected_content)
        next_batch_token = channel.json_body.get("next_batch")

        # make sure fetching messages with next batch token works - there are no unfetched
        # messages so we should receive an empty array
        content = {"next_batch": next_batch_token}
        channel = self.make_request(
            "POST",
            f"_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device/{device_id}/events",
            content=content,
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["events"], [])

        # make sure we can delete the dehydrated device
        channel = self.make_request(
            "DELETE",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)

        # ...and after deleting it is no longer available
        channel = self.make_request(
            "GET",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 401)

    @unittest.override_config(
        {"experimental_features": {"msc2697_enabled": False, "msc3814_enabled": True}}
    )
    def test_msc3814_dehydrated_device_delete_works(self) -> None:
        user = self.register_user("mikey", "pass")
        token = self.login(user, "pass", device_id="device1")
        content: JsonDict = {
            "device_data": {
                "algorithm": "m.dehydration.v1.olm",
            },
            "device_id": "device2",
            "initial_device_display_name": "foo bar",
            "device_keys": {
                "user_id": "@mikey:test",
                "device_id": "device2",
                "valid_until_ts": "80",
                "algorithms": [
                    "m.olm.curve25519-aes-sha2",
                ],
                "keys": {
                    "<algorithm>:<device_id>": "<key_base64>",
                },
                "signatures": {
                    "<user_id>": {"<algorithm>:<device_id>": "<signature_base64>"}
                },
            },
        }
        channel = self.make_request(
            "PUT",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            content=content,
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        device_id = channel.json_body.get("device_id")
        assert device_id is not None
        self.assertIsInstance(device_id, str)
        self.assertEqual("device2", device_id)

        # ensure that keys were uploaded and available
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    user: ["device2"],
                },
            },
            token,
        )
        self.assertEqual(
            channel.json_body["device_keys"][user]["device2"]["keys"],
            {
                "<algorithm>:<device_id>": "<key_base64>",
            },
        )

        # delete the dehydrated device
        channel = self.make_request(
            "DELETE",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)

        # ensure that keys are no longer available for deleted device
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    user: ["device2"],
                },
            },
            token,
        )
        self.assertEqual(channel.json_body["device_keys"], {"@mikey:test": {}})

        # check that an old device is deleted when user PUTs a new device
        # First, create a device
        content["device_id"] = "device3"
        content["device_keys"]["device_id"] = "device3"
        channel = self.make_request(
            "PUT",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            content=content,
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        device_id = channel.json_body.get("device_id")
        assert device_id is not None
        self.assertIsInstance(device_id, str)
        self.assertEqual("device3", device_id)

        # create a second device without deleting first device
        content["device_id"] = "device4"
        content["device_keys"]["device_id"] = "device4"
        channel = self.make_request(
            "PUT",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            content=content,
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        device_id = channel.json_body.get("device_id")
        assert device_id is not None
        self.assertIsInstance(device_id, str)
        self.assertEqual("device4", device_id)

        # check that the second device that was created is what is returned when we GET
        channel = self.make_request(
            "GET",
            "_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device",
            access_token=token,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        returned_device_id = channel.json_body["device_id"]
        self.assertEqual(returned_device_id, "device4")

        # and that if we query the keys for the first device they are not there
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    user: ["device3"],
                },
            },
            token,
        )
        self.assertEqual(channel.json_body["device_keys"], {"@mikey:test": {}})


class MSC4190AppserviceDevicesTestCase(unittest.HomeserverTestCase):
    servlets = [
        register.register_servlets,
        devices.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()

        # This application service uses the new MSC4190 behaviours
        self.msc4190_service = ApplicationService(
            id="msc4190",
            token="some_token",
            hs_token="some_token",
            sender=UserID.from_string("@as:example.com"),
            namespaces={
                ApplicationService.NS_USERS: [{"regex": "@.*", "exclusive": False}]
            },
            msc4190_device_management=True,
        )
        # This application service doesn't use the new MSC4190 behaviours
        self.pre_msc_service = ApplicationService(
            id="regular",
            token="other_token",
            hs_token="other_token",
            sender=UserID.from_string("@as2:example.com"),
            namespaces={
                ApplicationService.NS_USERS: [{"regex": "@.*", "exclusive": False}]
            },
            msc4190_device_management=False,
        )
        self.hs.get_datastores().main.services_cache.append(self.msc4190_service)
        self.hs.get_datastores().main.services_cache.append(self.pre_msc_service)
        return self.hs

    def test_PUT_device(self) -> None:
        self.register_appservice_user(
            "alice", self.msc4190_service.token, inhibit_login=True
        )
        self.register_appservice_user("bob", self.pre_msc_service.token)

        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {"devices": []})

        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/devices/AABBCCDD?user_id=@alice:test",
            content={"display_name": "Alice's device"},
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 201, channel.json_body)

        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["devices"]), 1)
        self.assertEqual(channel.json_body["devices"][0]["device_id"], "AABBCCDD")

        # Doing a second time should return a 200 instead of a 201
        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/devices/AABBCCDD?user_id=@alice:test",
            content={"display_name": "Alice's device"},
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_DELETE_device(self) -> None:
        self.register_appservice_user(
            "alice", self.msc4190_service.token, inhibit_login=True
        )

        # There should be no device
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {"devices": []})

        # Create a device
        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/devices/AABBCCDD?user_id=@alice:test",
            content={},
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 201, channel.json_body)

        # There should be one device
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["devices"]), 1)

        # Delete the device. UIA should not be required.
        channel = self.make_request(
            "DELETE",
            "/_matrix/client/v3/devices/AABBCCDD?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # There should be no device again
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {"devices": []})

    def test_POST_delete_devices(self) -> None:
        self.register_appservice_user(
            "alice", self.msc4190_service.token, inhibit_login=True
        )

        # There should be no device
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {"devices": []})

        # Create a device
        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/devices/AABBCCDD?user_id=@alice:test",
            content={},
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 201, channel.json_body)

        # There should be one device
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(len(channel.json_body["devices"]), 1)

        # Delete the device with delete_devices
        # UIA should not be required.
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/delete_devices?user_id=@alice:test",
            content={"devices": ["AABBCCDD"]},
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # There should be no device again
        channel = self.make_request(
            "GET",
            "/_matrix/client/v3/devices?user_id=@alice:test",
            access_token=self.msc4190_service.token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {"devices": []})
