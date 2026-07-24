#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.rest.client import capabilities, login
from synapse.server import HomeServer
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.unittest import override_config, skip_unless

try:
    import lxml
except ImportError:
    lxml = None  # type: ignore[assignment]


class CapabilitiesTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        capabilities.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.url = b"/capabilities"
        hs = self.setup_test_homeserver()
        self.config = hs.config
        self.auth_handler = hs.get_auth_handler()
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.localpart = "user"
        self.password = "pass"
        self.user = self.register_user(self.localpart, self.password)

    def test_check_auth_required(self) -> None:
        channel = self.make_request("GET", self.url)

        self.assertEqual(channel.code, 401)

    def test_get_room_version_capabilities(self) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, 200)
        for room_version in capabilities["m.room_versions"]["available"].keys():
            self.assertTrue(room_version in KNOWN_ROOM_VERSIONS, "" + room_version)

        self.assertEqual(
            self.config.server.default_room_version.identifier,
            capabilities["m.room_versions"]["default"],
        )

    def test_get_change_password_capabilities_password_login(self) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, 200)
        self.assertTrue(capabilities["m.change_password"]["enabled"])

    @override_config({"password_config": {"localdb_enabled": False}})
    def test_get_change_password_capabilities_localdb_disabled(self) -> None:
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, 200)
        self.assertFalse(capabilities["m.change_password"]["enabled"])

    @override_config({"password_config": {"enabled": False}})
    def test_get_change_password_capabilities_password_disabled(self) -> None:
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, 200)
        self.assertFalse(capabilities["m.change_password"]["enabled"])

    def test_get_change_users_attributes_capabilities(self) -> None:
        """Test that server returns capabilities by default."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertTrue(capabilities["m.change_password"]["enabled"])
        self.assertTrue(capabilities["m.set_displayname"]["enabled"])
        self.assertTrue(capabilities["m.set_avatar_url"]["enabled"])
        self.assertTrue(capabilities["m.3pid_changes"]["enabled"])

    @override_config({"enable_set_displayname": False})
    def test_get_set_displayname_capabilities_displayname_disabled(self) -> None:
        """Test if set displayname is disabled that the server responds it."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.set_displayname"]["enabled"])
        self.assertTrue(capabilities["m.profile_fields"]["enabled"])
        self.assertEqual(
            capabilities["m.profile_fields"]["disallowed"], ["displayname"]
        )

    @override_config({"enable_set_avatar_url": False})
    def test_get_set_avatar_url_capabilities_avatar_url_disabled(self) -> None:
        """Test if set avatar_url is disabled that the server responds it."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.set_avatar_url"]["enabled"])
        self.assertTrue(capabilities["m.profile_fields"]["enabled"])
        self.assertEqual(capabilities["m.profile_fields"]["disallowed"], ["avatar_url"])

    @override_config(
        {
            "enable_set_displayname": False,
            "experimental_features": {"msc4133_enabled": True},
        }
    )
    def test_get_set_displayname_capabilities_displayname_disabled_msc4133(
        self,
    ) -> None:
        """Test if set displayname is disabled that the server responds it."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.set_displayname"]["enabled"])
        self.assertTrue(capabilities["m.profile_fields"]["enabled"])
        self.assertEqual(
            capabilities["m.profile_fields"]["disallowed"], ["displayname"]
        )
        self.assertTrue(capabilities["uk.tcpip.msc4133.profile_fields"]["enabled"])
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["disallowed"],
            ["displayname"],
        )

    @override_config(
        {
            "enable_set_avatar_url": False,
            "experimental_features": {"msc4133_enabled": True},
        }
    )
    def test_get_set_avatar_url_capabilities_avatar_url_disabled_msc4133(self) -> None:
        """Test if set avatar_url is disabled that the server responds it."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.set_avatar_url"]["enabled"])
        self.assertTrue(capabilities["m.profile_fields"]["enabled"])
        self.assertEqual(capabilities["m.profile_fields"]["disallowed"], ["avatar_url"])
        self.assertTrue(capabilities["uk.tcpip.msc4133.profile_fields"]["enabled"])
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["disallowed"],
            ["avatar_url"],
        )

    @override_config(
        {"experimental_features": {"msc4133_key_allowlist": ["allowed_field"]}}
    )
    def test_get_profile_fields_capabilities_allowlist_requires_msc4133_enabled(
        self,
    ) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(capabilities["m.profile_fields"], {"enabled": True})
        self.assertNotIn("uk.tcpip.msc4133.profile_fields", capabilities)

    @override_config(
        {
            "enable_set_displayname": False,
            "experimental_features": {
                "msc4133_enabled": True,
                "msc4133_key_allowlist": ["allowed_field"],
            },
        }
    )
    def test_get_profile_fields_capabilities_allowlist_displayname_disabled(
        self,
    ) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            capabilities["m.profile_fields"]["disallowed"], ["displayname"]
        )
        self.assertNotIn("allowed", capabilities["m.profile_fields"])
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["allowed"],
            ["allowed_field", "avatar_url"],
        )
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["disallowed"],
            ["displayname"],
        )

    @override_config(
        {
            "experimental_features": {
                "msc4133_enabled": True,
                "msc4133_key_allowlist": ["allowed_field"],
                "msc4133_key_denylist": ["denied_field"],
            }
        }
    )
    def test_get_profile_fields_capabilities_allowlist_and_denylist(
        self,
    ) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["allowed"],
            ["allowed_field", "displayname", "avatar_url"],
        )
        self.assertEqual(
            capabilities["uk.tcpip.msc4133.profile_fields"]["disallowed"],
            ["denied_field"],
        )

    def test_get_delayed_events_capabilities_default_config_msc4140(self) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            capabilities["org.matrix.msc4140.delayed_events"]["max_delay_ms"], 0
        )
        self.assertEqual(
            capabilities["org.matrix.msc4140.delayed_events"]["max_scheduled"], 100
        )

    @override_config(
        {
            "max_event_delay_duration": "24h",
            "experimental_features": {
                "msc4140_max_delayed_events_per_user": 50,
            },
        }
    )
    def test_get_delayed_events_capabilities_custom_config_msc4140(self) -> None:
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            capabilities["org.matrix.msc4140.delayed_events"]["max_delay_ms"],
            Duration(days=1).as_millis(),
        )
        self.assertEqual(
            capabilities["org.matrix.msc4140.delayed_events"]["max_scheduled"], 50
        )

    @override_config({"enable_3pid_changes": False})
    def test_get_change_3pid_capabilities_3pid_disabled(self) -> None:
        """Test if change 3pid is disabled that the server responds it."""
        access_token = self.login(self.localpart, self.password)

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.3pid_changes"]["enabled"])

    def test_get_get_token_login_fields_when_disabled(self) -> None:
        """By default login via an existing session is disabled."""
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["m.get_login_token"]["enabled"])

    @override_config({"login_via_existing_session": {"enabled": True}})
    def test_get_get_token_login_fields_when_enabled(self) -> None:
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )

        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertTrue(capabilities["m.get_login_token"]["enabled"])

    @override_config(
        {
            "experimental_features": {"msc4267_enabled": True},
            "forget_rooms_on_leave": True,
        }
    )
    def test_get_forget_forced_upon_leave_with_auto_forget(self) -> None:
        # Server auto-forgets on /leave, expect enabled client capability
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )
        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertTrue(
            capabilities["org.matrix.msc4267.forget_forced_upon_leave"]["enabled"]
        )

    @override_config(
        {
            "experimental_features": {"msc4267_enabled": True},
            "forget_rooms_on_leave": False,
        }
    )
    def test_get_forget_forced_upon_leave_without_auto_forget(self) -> None:
        # Server doesn't auto-forget on /leave, expect disabled client capability
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )
        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(
            capabilities["org.matrix.msc4267.forget_forced_upon_leave"]["enabled"]
        )

    @override_config(
        {
            "url_preview_enabled": False,
            "experimental_features": {"msc4452_enabled": True},
        }
    )
    def test_url_previews_disabled(self) -> None:
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )
        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertFalse(capabilities["io.element.msc4452.preview_url"]["enabled"])

    @skip_unless(lxml is not None, "Requires lxml")
    @override_config(
        {
            "url_preview_enabled": True,
            "url_preview_ip_range_blacklist": ["127.0.0.1"],
            "experimental_features": {"msc4452_enabled": True},
        },
    )
    def test_url_previews_enabled(self) -> None:
        access_token = self.get_success(
            self.auth_handler.create_access_token_for_user_id(
                self.user, device_id=None, valid_until_ms=None
            )
        )
        channel = self.make_request("GET", self.url, access_token=access_token)
        capabilities = channel.json_body["capabilities"]
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertTrue(capabilities["io.element.msc4452.preview_url"]["enabled"])
