#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Tests the default implementation of the MSC4195 endpoints."""

import os
import tempfile
from unittest.mock import AsyncMock, Mock

import yaml

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.api.errors import HttpResponseException, RequestSendFailed
from synapse.rest import admin
from synapse.rest.client import appservice_proxy, login, matrixrtc, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils import FakeResponse
from tests.test_utils.event_injection import inject_member_event

STABLE_PATH = "/_matrix/client/v1/rtc/livekit/get_token"
UNSTABLE_PATH = "/_matrix/client/unstable/io.element.msc4195/rtc/livekit/get_token"
FEDERATION_STABLE_PATH = "/_matrix/federation/v1/rtc/livekit/get_token"
FEDERATION_UNSTABLE_PATH = (
    "/_matrix/federation/unstable/io.element.msc4195/rtc/livekit/get_token"
)
SFU_URL = "wss://livekit.other.example.com"


def _error(code: int, errcode: str | None) -> HttpResponseException:
    body = (
        b'{"errcode": "%s", "error": "nope"}' % (errcode.encode(),) if errcode else b""
    )
    return HttpResponseException(code, "error", body)


class MatrixRtcLiveKitTestCase(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        room.register_servlets,
        matrixrtc.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["allow_guest_access"] = True
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.user_id = self.register_user("alice", "password")
        self.token = self.login("alice", "password")
        self.room_id = self.helper.create_room_as(
            self.user_id, is_public=True, tok=self.token
        )
        self.remote_user_id = f"@bob:{self.OTHER_SERVER_NAME}"
        self.get_success(
            inject_member_event(hs, self.room_id, self.remote_user_id, "join")
        )

        self.send_direct_request = AsyncMock(return_value={"jwt": "thejwt"})
        hs.get_matrixrtc_livekit_handler()._federation_client = Mock(
            send_direct_request=self.send_direct_request
        )

    def _body(self, **overrides: object) -> JsonDict:
        body: JsonDict = {
            "server_name": self.OTHER_SERVER_NAME,
            "url": SFU_URL,
            "room_id": self.room_id,
            "slot_id": "m.call#room",
            "member_id": "member",
        }
        body.update(overrides)
        return {k: v for k, v in body.items() if v is not None}

    def _federation_body(self, slot_id: str = "m.call#room") -> JsonDict:
        return {
            "user_id": self.user_id,
            "url": SFU_URL,
            "room_id": self.room_id,
            "slot_id": slot_id,
            "member_id": "member",
        }

    def test_requires_authentication(self) -> None:
        channel = self.make_request("POST", STABLE_PATH, self._body())
        self.assertEqual(401, channel.code, channel.json_body)

    def test_relays_token_from_remote_server(self) -> None:
        for path in (STABLE_PATH, UNSTABLE_PATH):
            self.send_direct_request.reset_mock()
            channel = self.make_request(
                "POST", path, self._body(), access_token=self.token
            )
            self.assertEqual(200, channel.code, channel.json_body)
            self.assertEqual({"jwt": "thejwt"}, channel.json_body)
            self.send_direct_request.assert_awaited_once_with(
                "POST",
                self.OTHER_SERVER_NAME,
                FEDERATION_STABLE_PATH,
                json_body=self._federation_body(),
            )

    def test_only_jwt_is_relayed(self) -> None:
        self.send_direct_request.return_value = {"jwt": "thejwt", "extra": 1}
        channel = self.make_request(
            "POST", STABLE_PATH, self._body(), access_token=self.token
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"jwt": "thejwt"}, channel.json_body)

    def test_guest_can_request_token(self) -> None:
        channel = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(200, channel.code, channel.json_body)
        guest_tok = channel.json_body["access_token"]
        self.helper.send_state(
            self.room_id,
            "m.room.guest_access",
            {"guest_access": "can_join"},
            tok=self.token,
        )
        self.helper.join(self.room_id, channel.json_body["user_id"], tok=guest_tok)

        channel = self.make_request(
            "POST", STABLE_PATH, self._body(), access_token=guest_tok
        )
        self.assertEqual(200, channel.code, channel.json_body)

    def test_local_server_is_rejected(self) -> None:
        """This server has no SFU, so requests targeting it are rejected."""
        for server_name in (None, "", self.hs.hostname):
            channel = self.make_request(
                "POST",
                STABLE_PATH,
                self._body(server_name=server_name),
                access_token=self.token,
            )
            self.assertEqual(400, channel.code, channel.json_body)
            self.assertEqual("M_INVALID_PARAM", channel.json_body["errcode"])
        self.send_direct_request.assert_not_awaited()

    def test_invalid_server_name_is_rejected(self) -> None:
        channel = self.make_request(
            "POST",
            STABLE_PATH,
            self._body(server_name="not_a%valid/name"),
            access_token=self.token,
        )
        self.assertEqual(400, channel.code, channel.json_body)
        self.assertEqual("M_INVALID_PARAM", channel.json_body["errcode"])

    def test_missing_fields_are_rejected(self) -> None:
        for field in ("url", "room_id", "slot_id", "member_id"):
            for value in (None, ""):
                channel = self.make_request(
                    "POST",
                    STABLE_PATH,
                    self._body(**{field: value}),
                    access_token=self.token,
                )
                self.assertEqual(400, channel.code, channel.json_body)
        self.send_direct_request.assert_not_awaited()

    def test_user_not_joined_is_rejected(self) -> None:
        self.register_user("carol", "password")
        carol_tok = self.login("carol", "password")

        channel = self.make_request(
            "POST", STABLE_PATH, self._body(), access_token=carol_tok
        )
        self.assertEqual(403, channel.code, channel.json_body)
        self.assertEqual("M_FORBIDDEN", channel.json_body["errcode"])
        self.send_direct_request.assert_not_awaited()

    def test_remote_server_not_joined_is_rejected(self) -> None:
        channel = self.make_request(
            "POST",
            STABLE_PATH,
            self._body(server_name="unrelated.example.com"),
            access_token=self.token,
        )
        self.assertEqual(403, channel.code, channel.json_body)
        self.assertEqual("M_FORBIDDEN", channel.json_body["errcode"])
        self.send_direct_request.assert_not_awaited()

    def test_falls_back_to_unstable_federation_endpoint(self) -> None:
        self.send_direct_request.side_effect = [
            _error(404, "M_UNRECOGNIZED"),
            {"jwt": "thejwt"},
        ]
        channel = self.make_request(
            "POST", STABLE_PATH, self._body(), access_token=self.token
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"jwt": "thejwt"}, channel.json_body)
        self.assertEqual(2, self.send_direct_request.await_count)
        # The unstable endpoint expects the unstable `m.call` slot ID.
        self.send_direct_request.assert_awaited_with(
            "POST",
            self.OTHER_SERVER_NAME,
            FEDERATION_UNSTABLE_PATH,
            json_body=self._federation_body(slot_id="m.call#ROOM"),
        )

    def test_remote_errors_are_mapped(self) -> None:
        for error, expected_code, expected_errcode in (
            (_error(403, "M_FORBIDDEN"), 403, "M_FORBIDDEN"),
            (_error(400, "M_INVALID_PARAM"), 400, "M_INVALID_PARAM"),
            (_error(403, "M_UNKNOWN"), 502, "M_UNKNOWN"),
            (_error(400, "M_BAD_JSON"), 502, "M_UNKNOWN"),
            (_error(429, "M_LIMIT_EXCEEDED"), 502, "M_UNKNOWN"),
            (_error(500, "M_UNKNOWN"), 502, "M_UNKNOWN"),
            (RequestSendFailed(Exception("boom"), can_retry=False), 502, "M_UNKNOWN"),
        ):
            self.send_direct_request.side_effect = error
            channel = self.make_request(
                "POST", STABLE_PATH, self._body(), access_token=self.token
            )
            self.assertEqual(expected_code, channel.code, channel.json_body)
            self.assertEqual(expected_errcode, channel.json_body["errcode"])
            # Details of the remote's response must not be passed on.
            self.assertNotIn("nope", channel.json_body["error"])

    def test_unstable_endpoint_missing_too(self) -> None:
        self.send_direct_request.side_effect = _error(404, "M_UNRECOGNIZED")
        channel = self.make_request(
            "POST", STABLE_PATH, self._body(), access_token=self.token
        )
        self.assertEqual(502, channel.code, channel.json_body)
        self.assertEqual("M_UNKNOWN", channel.json_body["errcode"])
        self.assertEqual(2, self.send_direct_request.await_count)

    def test_response_without_jwt_is_rejected(self) -> None:
        for response in ({}, {"jwt": 1}):
            self.send_direct_request.return_value = response
            channel = self.make_request(
                "POST", STABLE_PATH, self._body(), access_token=self.token
            )
            self.assertEqual(502, channel.code, channel.json_body)
            self.assertEqual("M_UNKNOWN", channel.json_body["errcode"])

    def test_delegate_delayed_leave_rejects_requests(self) -> None:
        """This server has no SFU, so delayed leaves cannot be delegated."""
        for path in (STABLE_PATH, UNSTABLE_PATH):
            path = path.replace("get_token", "delegate_delayed_leave")
            body = {
                "url": SFU_URL,
                "room_id": self.room_id,
                "slot_id": "m.call#room",
                "member_id": "member",
                "delay_id": "1234567890",
            }

            channel = self.make_request("POST", path, body)
            self.assertEqual(401, channel.code, channel.json_body)

            channel = self.make_request("POST", path, body, access_token=self.token)
            self.assertEqual(400, channel.code, channel.json_body)
            self.assertEqual("M_INVALID_PARAM", channel.json_body["errcode"])

    def test_federation_endpoint_rejects_requests(self) -> None:
        """This server has no SFU, so federation requests are always rejected."""
        for path in (FEDERATION_STABLE_PATH, FEDERATION_UNSTABLE_PATH):
            channel = self.make_signed_federation_request(
                "POST",
                path,
                {
                    "user_id": self.remote_user_id,
                    "url": "wss://livekit.example.com",
                    "room_id": self.room_id,
                    "slot_id": "m.call#room",
                    "member_id": "member",
                },
            )
            self.assertEqual(400, channel.code, channel.json_body)
            self.assertEqual("M_INVALID_PARAM", channel.json_body["errcode"])

    def test_federation_endpoint_requires_authentication(self) -> None:
        channel = self.make_request("POST", FEDERATION_STABLE_PATH, {}, shorthand=False)
        self.assertEqual(401, channel.code, channel.json_body)


class MatrixRtcLiveKitAppserviceProxyTestCase(unittest.FederatingHomeserverTestCase):
    """Tests that an application service proxying the LiveKit endpoints takes
    precedence over the default implementation."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        matrixrtc.register_servlets,
        appservice_proxy.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="as_proxy_config", delete=False
        ) as f:
            self.addCleanup(os.remove, f.name)
            yaml.dump(
                {
                    "id": "proxy_as",
                    "url": None,
                    "as_token": "as_token",
                    "hs_token": "hs_token",
                    "sender_localpart": "proxy_bot",
                    "namespaces": {},
                    "io.element.msc4512.proxy_prefix": "rtc/livekit",
                    "io.element.msc4512.proxy_url": "http://appservice.example.com",
                },
                f,
            )
            config["app_service_config_files"] = [f.name]
        config.setdefault("experimental_features", {}).setdefault(
            "msc4512_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.agent = Mock()
        self.agent.request = Mock(
            side_effect=lambda *args, **kwargs: defer.succeed(
                FakeResponse.json(code=200, payload={"jwt": "fromappservice"})
            )
        )
        hs.get_proxied_http_client().agent = self.agent

        self.register_user("alice", "password")
        self.token = self.login("alice", "password")

    def test_client_request_is_proxied(self) -> None:
        # No `server_name`, which the default implementation would reject.
        channel = self.make_request(
            "POST",
            STABLE_PATH,
            {"url": SFU_URL, "room_id": "!r:test", "slot_id": "s", "member_id": "m"},
            shorthand=False,
            access_token=self.token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"jwt": "fromappservice"}, channel.json_body)
        self.agent.request.assert_called_once()

    def test_federation_request_is_proxied(self) -> None:
        channel = self.make_signed_federation_request(
            "POST", FEDERATION_STABLE_PATH, {"url": SFU_URL}
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual({"jwt": "fromappservice"}, channel.json_body)
        self.agent.request.assert_called_once()
