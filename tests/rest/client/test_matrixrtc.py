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

"""Tests REST events for /rtc/transports and RTC config parsing and /versions setup."""

import os
import tempfile
import unittest as stdlib_unittest

import yaml
from pydantic import ValidationError

from twisted.internet.testing import MemoryReactor

from synapse.appservice import ApplicationService
from synapse.config._base import ConfigError
from synapse.config.matrixrtc import (
    TransportConfigModel,
    validate_msc4195_stable_endpoints_config,
)
from synapse.rest import admin
from synapse.rest.client import login, matrixrtc, register, room, versions
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import HomeserverTestCase, override_config

PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4143"
STABLE_PATH = "/_matrix/client/v1/rtc/transports"
RTC_ENDPOINT = {"type": "focusA", "required_field": "theField"}
LIVEKIT_TRANSPORT = {
    "type": "livekit",
    "url": "wss://livekit.example.com",
}
BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT = {
    "type": "livekit",
    "url": "wss://livekit.example.com",
    "livekit_service_url": "https://livekit.example.com",
}
LEGACY_LIVEKIT_TRANSPORT = {
    "type": "livekit",
    "livekit_service_url": "https://livekit.example.com",
}
STABLE_LIVEKIT_TRANSPORT = {
    "type": "m.livekit",
    "url": "wss://livekit.example.com",
}


class MatrixRtcTestCase(HomeserverTestCase):
    """Tests the unstable /rtc/transports Client-Server REST API."""

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
            "matrix_rtc": {"transports": [LIVEKIT_TRANSPORT]},
        }
    )
    def test_matrixrtc_endpoint_livekit_transport(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict({"rtc_transports": [LIVEKIT_TRANSPORT]}, channel.json_body)

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {"transports": [BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT]},
        }
    )
    def test_matrixrtc_endpoint_backwards_compatible_livekit_transport(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict(
            {"rtc_transports": [BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT]},
            channel.json_body,
        )

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {"transports": [LEGACY_LIVEKIT_TRANSPORT]},
        }
    )
    def test_matrixrtc_endpoint_legacy_livekit_transport(self) -> None:
        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assert_dict(
            {"rtc_transports": [LEGACY_LIVEKIT_TRANSPORT]}, channel.json_body
        )


class StableMatrixRtcTestCase(HomeserverTestCase):
    """Tests the stable /rtc/transports Client-Server REST API."""

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

    def test_matrixrtc_endpoint_enabled_by_default(self) -> None:
        channel = self.make_request("GET", STABLE_PATH, access_token=self._alice_tok)
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"transports": []}, channel.json_body)

    def test_matrixrtc_endpoint_requires_authentication(self) -> None:
        channel = self.make_request("GET", STABLE_PATH)
        self.assertEqual(401, channel.code, channel.json_body)

    @override_config({"allow_guest_access": True})
    def test_matrixrtc_endpoint_allows_guests(self) -> None:
        channel = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(200, channel.code, channel.json_body)
        guest_tok = channel.json_body["access_token"]

        channel = self.make_request("GET", STABLE_PATH, access_token=guest_tok)
        self.assertEqual(200, channel.code, channel.json_body)

    @override_config({"matrix_rtc": {"transports": [RTC_ENDPOINT]}})
    def test_matrixrtc_endpoint_contains_expected_transport(self) -> None:
        channel = self.make_request("GET", STABLE_PATH, access_token=self._alice_tok)
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"transports": [RTC_ENDPOINT]}, channel.json_body)

    @override_config({"matrix_rtc": {"transports": [STABLE_LIVEKIT_TRANSPORT]}})
    def test_matrixrtc_endpoint_stable_livekit_transport(self) -> None:
        channel = self.make_request("GET", STABLE_PATH, access_token=self._alice_tok)
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"transports": [STABLE_LIVEKIT_TRANSPORT]}, channel.json_body)

    @override_config(
        {"matrix_rtc": {"transports": [BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT]}}
    )
    def test_matrixrtc_endpoint_unstable_livekit_transport(self) -> None:
        channel = self.make_request("GET", STABLE_PATH, access_token=self._alice_tok)
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(
            {"transports": [BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT]},
            channel.json_body,
        )

    @override_config(
        {
            "experimental_features": {"msc4143_enabled": True},
            "matrix_rtc": {
                "transports": [
                    STABLE_LIVEKIT_TRANSPORT,
                    BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT,
                    RTC_ENDPOINT,
                ]
            },
        }
    )
    def test_matrixrtc_stable_and_unstable_endpoints_coexist(self) -> None:
        """Both endpoints return all configured transports."""
        transports = [
            STABLE_LIVEKIT_TRANSPORT,
            BACKWARDS_COMPATIBLE_LIVEKIT_TRANSPORT,
            RTC_ENDPOINT,
        ]

        channel = self.make_request("GET", STABLE_PATH, access_token=self._alice_tok)
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"transports": transports}, channel.json_body)

        channel = self.make_request(
            "GET", f"{PATH_PREFIX}/rtc/transports", access_token=self._alice_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"rtc_transports": transports}, channel.json_body)


class MatrixRtcVersionsTestCase(HomeserverTestCase):
    """Tests that org.matrix.msc4143 and org.matrix.msc4195.stable are correctly
    advertised in /versions."""

    servlets = [versions.register_servlets]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Register an application service proxying the LiveKit endpoints, which
        # `matrix_rtc.msc4195_stable_endpoints` requires. This doesn't affect
        # what is advertised unless that option is enabled.
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="as_livekit_config", delete=False
        ) as f:
            self.addCleanup(os.remove, f.name)
            yaml.dump(
                {
                    "id": "livekit",
                    "url": None,
                    "as_token": "as_token",
                    "hs_token": "hs_token",
                    "sender_localpart": "livekit",
                    "namespaces": {},
                    "io.element.msc4512.proxy_prefix": ApplicationService.LIVEKIT_PROXY_PREFIX,
                    "io.element.msc4512.proxy_url": "http://lk-jwt-service.example.com",
                },
                f,
            )
            config["app_service_config_files"] = [f.name]
        config.setdefault("experimental_features", {})["msc4512_enabled"] = True
        return config

    def test_msc4143_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["org.matrix.msc4143"])

    @unittest.override_config({"experimental_features": {"msc4143_enabled": True}})
    def test_msc4143_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["org.matrix.msc4143"])

    def test_msc4143_stable_true_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(
            channel.json_body["unstable_features"]["org.matrix.msc4143.stable"]
        )

    def test_msc4195_stable_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(
            channel.json_body["unstable_features"]["org.matrix.msc4195.stable"]
        )

    @unittest.override_config({"matrix_rtc": {"msc4195_stable_endpoints": True}})
    def test_msc4195_stable_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(
            channel.json_body["unstable_features"]["org.matrix.msc4195.stable"]
        )


class StableLivekitEndpointsConfigTestCase(HomeserverTestCase):
    """Tests the validation of `matrix_rtc.msc4195_stable_endpoints`
    against the rest of the config and the registered application
    services."""

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["enable_registration_without_verification"] = True
        return config

    def _make_appservice(self, proxy_prefix: str | None) -> ApplicationService:
        return ApplicationService(
            token="as_token",
            id="livekit",
            sender=UserID("livekit", self.hs.hostname),
            proxy_prefix=proxy_prefix,
            proxy_url=(
                "http://lk-jwt-service.example.com"
                if proxy_prefix is not None
                else None
            ),
        )

    def test_disabled_requires_nothing(self) -> None:
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = False
        self.hs.config.experimental.msc4512_enabled = False
        validate_msc4195_stable_endpoints_config(self.hs.config, [])

    def test_requires_msc4512(self) -> None:
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = True
        self.hs.config.experimental.msc4512_enabled = False
        with self.assertRaises(ConfigError):
            validate_msc4195_stable_endpoints_config(
                self.hs.config,
                [self._make_appservice(ApplicationService.LIVEKIT_PROXY_PREFIX)],
            )

    def test_requires_livekit_appservice(self) -> None:
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = True
        self.hs.config.experimental.msc4512_enabled = True
        with self.assertRaises(ConfigError):
            validate_msc4195_stable_endpoints_config(self.hs.config, [])
        with self.assertRaises(ConfigError):
            validate_msc4195_stable_endpoints_config(
                self.hs.config, [self._make_appservice(None)]
            )
        with self.assertRaises(ConfigError):
            validate_msc4195_stable_endpoints_config(
                self.hs.config,
                [self._make_appservice("rtc/livekit/get_token")],
            )

    def test_passes_with_livekit_appservice(self) -> None:
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = True
        self.hs.config.experimental.msc4512_enabled = True
        validate_msc4195_stable_endpoints_config(
            self.hs.config,
            [
                self._make_appservice(None),
                self._make_appservice(ApplicationService.LIVEKIT_PROXY_PREFIX),
            ],
        )

    def _write_appservice_file(self, proxy_prefix: str | None) -> str:
        """Writes an application service registration file and returns its path."""
        registration: JsonDict = {
            "id": "livekit",
            "url": None,
            "as_token": "as_token",
            "hs_token": "hs_token",
            "sender_localpart": "livekit",
            "namespaces": {},
        }
        if proxy_prefix is not None:
            registration["io.element.msc4512.proxy_prefix"] = proxy_prefix
            registration["io.element.msc4512.proxy_url"] = (
                "http://lk-jwt-service.example.com"
            )
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="as_livekit_config", delete=False
        ) as f:
            self.addCleanup(os.remove, f.name)
            yaml.dump(registration, f)
            return f.name

    def test_validate_config_loads_appservices(self) -> None:
        """`HomeServerConfig.validate_config` should load the application service
        registrations and run the check against them."""
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = True
        self.hs.config.experimental.msc4512_enabled = True

        self.hs.config.appservice.app_service_config_files = []
        with self.assertRaises(ConfigError):
            self.hs.config.validate_config()

        self.hs.config.appservice.app_service_config_files = [
            self._write_appservice_file(None)
        ]
        with self.assertRaises(ConfigError):
            self.hs.config.validate_config()

        self.hs.config.appservice.app_service_config_files = [
            self._write_appservice_file(ApplicationService.LIVEKIT_PROXY_PREFIX)
        ]
        self.hs.config.validate_config()

    def test_validate_config_skips_appservices_if_disabled(self) -> None:
        """Application service registrations shouldn't be loaded (and thus can't
        fail validation) if the option is disabled."""
        self.hs.config.matrix_rtc.msc4195_stable_endpoints = False
        self.hs.config.appservice.app_service_config_files = ["/does/not/exist"]
        self.hs.config.validate_config()


class TransportConfigModelTestCase(stdlib_unittest.TestCase):
    """Tests validation of the `TransportConfigModel` pydantic model."""

    def test_livekit_transport_requires_url_or_livekit_service_url(self) -> None:
        with self.assertRaises(ValidationError):
            TransportConfigModel(type="livekit")

    def test_livekit_transport_with_only_url(self) -> None:
        TransportConfigModel(type="livekit", url="wss://livekit.example.com")

    def test_livekit_transport_with_only_livekit_service_url(self) -> None:
        TransportConfigModel(
            type="livekit", livekit_service_url="https://livekit.example.com"
        )

    def test_invalid_field_type(self) -> None:
        with self.assertRaises(ValidationError):
            TransportConfigModel(type="livekit", url=1234)  # type: ignore[arg-type]

    def test_stable_livekit_transport_requires_url(self) -> None:
        with self.assertRaises(ValidationError):
            TransportConfigModel(type="m.livekit")

    def test_stable_livekit_transport_with_url(self) -> None:
        TransportConfigModel(type="m.livekit", url="wss://livekit.example.com")

    def test_stable_livekit_transport_with_livekit_service_url(self) -> None:
        TransportConfigModel(
            type="m.livekit",
            url="wss://livekit.example.com",
            livekit_service_url="https://livekit.example.com",
        )

    def test_stable_livekit_transport_with_only_livekit_service_url(self) -> None:
        with self.assertRaises(ValidationError):
            TransportConfigModel(
                type="m.livekit", livekit_service_url="https://livekit.example.com"
            )
