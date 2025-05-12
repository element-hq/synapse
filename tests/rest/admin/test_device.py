#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Dirk Klimpel
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
import urllib.parse

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes
from synapse.handlers.device import DeviceHandler
from synapse.rest.client import devices, login
from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest


class DeviceRestTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        handler = hs.get_device_handler()
        assert isinstance(handler, DeviceHandler)
        self.handler = handler

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")
        self.other_user_token = self.login("user", "pass")
        res = self.get_success(self.handler.get_devices_by_user(self.other_user))
        self.other_user_device_id = res[0]["device_id"]

        self.url = "/_synapse/admin/v2/users/%s/devices/%s" % (
            urllib.parse.quote(self.other_user),
            self.other_user_device_id,
        )

    @parameterized.expand(["GET", "PUT", "DELETE"])
    def test_no_auth(self, method: str) -> None:
        """
        Try to get a device of an user without authentication.
        """
        channel = self.make_request(method, self.url, b"{}")

        self.assertEqual(
            401,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    @parameterized.expand(["GET", "PUT", "DELETE"])
    def test_requester_is_no_admin(self, method: str) -> None:
        """
        If the user is not a server admin, an error is returned.
        """
        channel = self.make_request(
            method,
            self.url,
            access_token=self.other_user_token,
        )

        self.assertEqual(
            403,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    @parameterized.expand(["GET", "PUT", "DELETE"])
    def test_user_does_not_exist(self, method: str) -> None:
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = (
            "/_synapse/admin/v2/users/@unknown_person:test/devices/%s"
            % self.other_user_device_id
        )

        channel = self.make_request(
            method,
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    @parameterized.expand(["GET", "PUT", "DELETE"])
    def test_user_is_not_local(self, method: str) -> None:
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = (
            "/_synapse/admin/v2/users/@unknown_person:unknown_domain/devices/%s"
            % self.other_user_device_id
        )

        channel = self.make_request(
            method,
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_unknown_device(self) -> None:
        """
        Tests that a lookup for a device that does not exist returns either 404 or 200.
        """
        url = "/_synapse/admin/v2/users/%s/devices/unknown_device" % urllib.parse.quote(
            self.other_user
        )

        channel = self.make_request(
            "GET",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

        channel = self.make_request(
            "PUT",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)

        channel = self.make_request(
            "DELETE",
            url,
            access_token=self.admin_user_tok,
        )

        # Delete unknown device returns status 200
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def test_update_device_too_long_display_name(self) -> None:
        """
        Update a device with a display name that is invalid (too long).
        """
        # Set iniital display name.
        update = {"display_name": "new display"}
        self.get_success(
            self.handler.update_device(
                self.other_user, self.other_user_device_id, update
            )
        )

        # Request to update a device display name with a new value that is longer than allowed.
        update = {
            "display_name": "a"
            * (synapse.handlers.device.MAX_DEVICE_DISPLAY_NAME_LEN + 1)
        }

        channel = self.make_request(
            "PUT",
            self.url,
            access_token=self.admin_user_tok,
            content=update,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.TOO_LARGE, channel.json_body["errcode"])

        # Ensure the display name was not updated.
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual("new display", channel.json_body["display_name"])

    def test_update_no_display_name(self) -> None:
        """
        Tests that a update for a device without JSON returns a 200
        """
        # Set iniital display name.
        update = {"display_name": "new display"}
        self.get_success(
            self.handler.update_device(
                self.other_user, self.other_user_device_id, update
            )
        )

        channel = self.make_request(
            "PUT",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)

        # Ensure the display name was not updated.
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual("new display", channel.json_body["display_name"])

    def test_update_display_name(self) -> None:
        """
        Tests a normal successful update of display name
        """
        # Set new display_name
        channel = self.make_request(
            "PUT",
            self.url,
            access_token=self.admin_user_tok,
            content={"display_name": "new displayname"},
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)

        # Check new display_name
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual("new displayname", channel.json_body["display_name"])

    def test_get_device(self) -> None:
        """
        Tests that a normal lookup for a device is successfully
        """
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(self.other_user, channel.json_body["user_id"])
        # Check that all fields are available
        self.assertIn("user_id", channel.json_body)
        self.assertIn("device_id", channel.json_body)
        self.assertIn("display_name", channel.json_body)
        self.assertIn("last_seen_ip", channel.json_body)
        self.assertIn("last_seen_ts", channel.json_body)

    def test_delete_device(self) -> None:
        """
        Tests that a remove of a device is successfully
        """
        # Count number of devies of an user.
        res = self.get_success(self.handler.get_devices_by_user(self.other_user))
        number_devices = len(res)
        self.assertEqual(1, number_devices)

        # Delete device
        channel = self.make_request(
            "DELETE",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)

        # Ensure that the number of devices is decreased
        res = self.get_success(self.handler.get_devices_by_user(self.other_user))
        self.assertEqual(number_devices - 1, len(res))


class DevicesRestTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        devices.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")

        self.url = "/_synapse/admin/v2/users/%s/devices" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self) -> None:
        """
        Try to list devices of an user without authentication.
        """
        channel = self.make_request("GET", self.url, b"{}")

        self.assertEqual(
            401,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error is returned.
        """
        other_user_token = self.login("user", "pass")

        channel = self.make_request(
            "GET",
            self.url,
            access_token=other_user_token,
        )

        self.assertEqual(
            403,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_does_not_exist(self) -> None:
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = "/_synapse/admin/v2/users/@unknown_person:test/devices"
        channel = self.make_request(
            "GET",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_user_is_not_local(self) -> None:
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = "/_synapse/admin/v2/users/@unknown_person:unknown_domain/devices"

        channel = self.make_request(
            "GET",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_user_has_no_devices(self) -> None:
        """
        Tests that a normal lookup for devices is successfully
        if user has no devices
        """

        # Get devices
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(0, channel.json_body["total"])
        self.assertEqual(0, len(channel.json_body["devices"]))

    @unittest.override_config(
        {"experimental_features": {"msc2697_enabled": False, "msc3814_enabled": True}}
    )
    def test_get_devices(self) -> None:
        """
        Tests that a normal lookup for devices is successfully
        """
        # Create devices
        number_devices = 5
        # we create 2 fewer devices in the loop, because we will create another
        # login after the loop, and we will create a dehydrated device
        for _ in range(number_devices - 2):
            self.login("user", "pass")

        other_user_token = self.login("user", "pass")
        dehydrated_device_url = (
            "/_matrix/client/unstable/org.matrix.msc3814.v1/dehydrated_device"
        )
        content = {
            "device_data": {
                "algorithm": "m.dehydration.v1.olm",
            },
            "device_id": "dehydrated_device",
            "initial_device_display_name": "foo bar",
            "device_keys": {
                "user_id": "@user:test",
                "device_id": "dehydrated_device",
                "valid_until_ts": "80",
                "algorithms": [
                    "m.olm.curve25519-aes-sha2",
                ],
                "keys": {
                    "<algorithm>:<device_id>": "<key_base64>",
                },
                "signatures": {
                    "@user:test": {"<algorithm>:<device_id>": "<signature_base64>"}
                },
            },
            "fallback_keys": {
                "alg1:device1": "f4llb4ckk3y",
                "signed_<algorithm>:<device_id>": {
                    "fallback": "true",
                    "key": "f4llb4ckk3y",
                    "signatures": {
                        "@user:test": {"<algorithm>:<device_id>": "<key_base64>"}
                    },
                },
            },
            "one_time_keys": {"alg1:k1": "0net1m3k3y"},
        }
        self.make_request(
            "PUT",
            dehydrated_device_url,
            access_token=other_user_token,
            content=content,
        )

        # Get devices
        channel = self.make_request(
            "GET",
            self.url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(number_devices, channel.json_body["total"])
        self.assertEqual(number_devices, len(channel.json_body["devices"]))
        self.assertEqual(self.other_user, channel.json_body["devices"][0]["user_id"])
        # Check that all fields are available, and that the dehydrated device is marked as dehydrated
        found_dehydrated = False
        for d in channel.json_body["devices"]:
            self.assertIn("user_id", d)
            self.assertIn("device_id", d)
            self.assertIn("display_name", d)
            self.assertIn("last_seen_ip", d)
            self.assertIn("last_seen_ts", d)
            if d["device_id"] == "dehydrated_device":
                self.assertTrue(d.get("dehydrated"))
                found_dehydrated = True
            else:
                # Either the field is not present, or set to False
                self.assertFalse(d.get("dehydrated"))

        self.assertTrue(found_dehydrated)


class DeleteDevicesRestTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.handler = hs.get_device_handler()

        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        self.other_user = self.register_user("user", "pass")

        self.url = "/_synapse/admin/v2/users/%s/delete_devices" % urllib.parse.quote(
            self.other_user
        )

    def test_no_auth(self) -> None:
        """
        Try to delete devices of an user without authentication.
        """
        channel = self.make_request("POST", self.url, b"{}")

        self.assertEqual(
            401,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.MISSING_TOKEN, channel.json_body["errcode"])

    def test_requester_is_no_admin(self) -> None:
        """
        If the user is not a server admin, an error is returned.
        """
        other_user_token = self.login("user", "pass")

        channel = self.make_request(
            "POST",
            self.url,
            access_token=other_user_token,
        )

        self.assertEqual(
            403,
            channel.code,
            msg=channel.json_body,
        )
        self.assertEqual(Codes.FORBIDDEN, channel.json_body["errcode"])

    def test_user_does_not_exist(self) -> None:
        """
        Tests that a lookup for a user that does not exist returns a 404
        """
        url = "/_synapse/admin/v2/users/@unknown_person:test/delete_devices"
        channel = self.make_request(
            "POST",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(Codes.NOT_FOUND, channel.json_body["errcode"])

    def test_user_is_not_local(self) -> None:
        """
        Tests that a lookup for a user that is not a local returns a 400
        """
        url = "/_synapse/admin/v2/users/@unknown_person:unknown_domain/delete_devices"

        channel = self.make_request(
            "POST",
            url,
            access_token=self.admin_user_tok,
        )

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual("Can only lookup local users", channel.json_body["error"])

    def test_unknown_devices(self) -> None:
        """
        Tests that a remove of a device that does not exist returns 200.
        """
        channel = self.make_request(
            "POST",
            self.url,
            access_token=self.admin_user_tok,
            content={"devices": ["unknown_device1", "unknown_device2"]},
        )

        # Delete unknown devices returns status 200
        self.assertEqual(200, channel.code, msg=channel.json_body)

    def test_delete_devices(self) -> None:
        """
        Tests that a remove of devices is successfully
        """

        # Create devices
        number_devices = 5
        for _ in range(number_devices):
            self.login("user", "pass")

        # Get devices
        res = self.get_success(self.handler.get_devices_by_user(self.other_user))
        self.assertEqual(number_devices, len(res))

        # Create list of device IDs
        device_ids = []
        for d in res:
            device_ids.append(str(d["device_id"]))

        # Delete devices
        channel = self.make_request(
            "POST",
            self.url,
            access_token=self.admin_user_tok,
            content={"devices": device_ids},
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)

        res = self.get_success(self.handler.get_devices_by_user(self.other_user))
        self.assertEqual(0, len(res))
