#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# [This file includes modifications made by New Vector Limited]
#
#

"""Tests REST events for /rtc/endpoints path."""

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, matrixrtc, register, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, override_config

PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4143"
RTC_ENDPOINT = {"type": "focusA", "required_field": "theField"}
LIVEKIT_ENDPOINT = {
    "type": "livekit",
    "livekit_service_url": "https://livekit.example.com",
}


class MatrixRtcTestCase(HomeserverTestCase):
    """Tests /rtc/transports Client-Server REST API."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        matrixrtc.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.register_user("alice", "password")
        self._alice_tok = self.login("alice", "password")

    def test_matrixrtc_endpoint_not_enabled(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(404, channel.code, channel.json_body)
        self.assertEqual(
            "M_UNRECOGNIZED", channel.json_body["errcode"], channel.json_body
        )

    @override_config({"experimental_features": {"msc4143_enabled": True}})
    def test_matrixrtc_endpoint_requires_authentication(self) -> None:
        channel = self.make_request("GET", f"{PATH_PREFIX}/rtc/transports")
        self.assertEqual(401, channel.code, channel.json_body)

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {"transports": [RTC_ENDPOINT]},
        }
    )
    def test_matrixrtc_endpoint_contains_expected_transport(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict({"rtc_transports": [RTC_ENDPOINT]}, channel.json_body)

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {"transports": []},
        }
    )
    def test_matrixrtc_endpoint_no_transports_configured(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict({}, channel.json_body)

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {"transports": [LIVEKIT_ENDPOINT]},
        }
    )
    def test_matrixrtc_endpoint_livekit_transport(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict({"rtc_transports": [LIVEKIT_ENDPOINT]}, channel.json_body)
