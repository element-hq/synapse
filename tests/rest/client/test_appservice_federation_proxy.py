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

import json
from unittest import mock

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import Codes
from synapse.appservice import ApplicationService
from synapse.rest import admin
from synapse.rest.client import appservice_federation_proxy, login
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel
from tests.test_utils import FakeResponse

APPSERVICE_URL = "http://appservice.example.com"
APPSERVICE_PREFIX = "rtc/livekit"
AS_TOKEN = "as_token"


class ApplicationServiceFederationProxyTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        appservice_federation_proxy.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {}).setdefault(
            "msc4512_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.appservice = ApplicationService(
            AS_TOKEN,
            id="proxy_as",
            sender=UserID.from_string("@proxy_bot:test"),
            namespaces={},
            url=APPSERVICE_URL,
            proxy_prefix=APPSERVICE_PREFIX,
            proxy_url=APPSERVICE_URL,
        )
        hs.get_datastores().main.services_cache.append(self.appservice)

        self.agent_request = mock.AsyncMock()
        hs.get_federation_http_client().agent.request = self.agent_request  # type: ignore[method-assign]

    def _fed_proxy(
        self, content: dict, access_token: str | None = AS_TOKEN
    ) -> FakeChannel:
        return self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4512/appservice/fed_proxy",
            content,
            access_token=access_token,
        )

    def test_get_is_sent_and_relayed(self) -> None:
        self.agent_request.return_value = FakeResponse.json(
            code=200, payload={"hello": "world"}
        )

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "query": {"foo": "bar"},
            }
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"status": 200, "content": {"hello": "world"}}
        )

        ((method, uri), kwargs) = self.agent_request.call_args
        self.assertEqual(method, b"GET")
        self.assertEqual(
            uri,
            f"matrix-federation://remote.example.com/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )

        self.assertIsNone(kwargs["bodyProducer"])

        headers = kwargs["headers"]
        expected_auth_headers = self.hs.get_federation_http_client().build_auth_headers(
            b"remote.example.com",
            b"GET",
            f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )
        self.assertEqual(headers.getRawHeaders(b"Authorization"), expected_auth_headers)

    def test_delete_is_sent_and_relayed(self) -> None:
        self.agent_request.return_value = FakeResponse.json(
            code=200, payload={"hello": "world"}
        )

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "DELETE",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "query": {"foo": "bar"},
            }
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"status": 200, "content": {"hello": "world"}}
        )

        ((method, uri), kwargs) = self.agent_request.call_args
        self.assertEqual(method, b"DELETE")
        self.assertEqual(
            uri,
            f"matrix-federation://remote.example.com/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )

        self.assertIsNone(kwargs["bodyProducer"])

        headers = kwargs["headers"]
        expected_auth_headers = self.hs.get_federation_http_client().build_auth_headers(
            b"remote.example.com",
            b"DELETE",
            f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )
        self.assertEqual(headers.getRawHeaders(b"Authorization"), expected_auth_headers)

    def test_post_with_body_is_sent_and_relayed(self) -> None:
        self.agent_request.return_value = FakeResponse.json(
            code=200, payload={"hello": "world"}
        )

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "POST",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "body": {"key": "value"},
                "query": {"foo": "bar"},
            }
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"status": 200, "content": {"hello": "world"}}
        )

        ((method, uri), kwargs) = self.agent_request.call_args
        self.assertEqual(method, b"POST")
        self.assertEqual(
            uri,
            f"matrix-federation://remote.example.com/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )

        body_producer = kwargs["bodyProducer"]
        self.assertEqual(
            json.loads(body_producer._inputFile.getvalue()),
            {"key": "value"},
        )

        headers = kwargs["headers"]
        expected_auth_headers = self.hs.get_federation_http_client().build_auth_headers(
            b"remote.example.com",
            b"POST",
            f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
            content={"key": "value"},
        )
        self.assertEqual(headers.getRawHeaders(b"Authorization"), expected_auth_headers)

    def test_put_with_body_is_sent_and_relayed(self) -> None:
        self.agent_request.return_value = FakeResponse.json(
            code=200, payload={"hello": "world"}
        )

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "PUT",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "body": {"key": "value"},
                "query": {"foo": "bar"},
            }
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"status": 200, "content": {"hello": "world"}}
        )

        ((method, uri), kwargs) = self.agent_request.call_args
        self.assertEqual(method, b"PUT")
        self.assertEqual(
            uri,
            f"matrix-federation://remote.example.com/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
        )

        body_producer = kwargs["bodyProducer"]
        self.assertEqual(
            json.loads(body_producer._inputFile.getvalue()),
            {"key": "value"},
        )

        headers = kwargs["headers"]
        expected_auth_headers = self.hs.get_federation_http_client().build_auth_headers(
            b"remote.example.com",
            b"PUT",
            f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path?foo=bar".encode(),
            content={"key": "value"},
        )
        self.assertEqual(headers.getRawHeaders(b"Authorization"), expected_auth_headers)

    def test_remote_error_response_is_relayed(self) -> None:
        self.agent_request.return_value = FakeResponse.json(
            code=400, payload={"errcode": "M_UNRECOGNIZED"}
        )

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            }
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {"status": 400, "content": {"errcode": "M_UNRECOGNIZED"}},
        )

    @unittest.override_config({"federation": {"max_short_retries": 0}})
    def test_connection_failure_causes_502(self) -> None:
        self.agent_request.side_effect = Exception("boom")

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            }
        )

        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_CONNECTION_FAILED
        )

    def test_denied_destination_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "not a valid server name",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            }
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_DESTINATION_DENIED
        )
        self.agent_request.assert_not_called()

    def test_self_destination_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": self.hs.hostname,
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            }
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_DESTINATION_DENIED
        )
        self.agent_request.assert_not_called()

    def test_path_traversal_segment_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/../../v1/send/txn1",
            }
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_PATH_NOT_ALLOWED
        )
        self.agent_request.assert_not_called()

    def test_get_with_body_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "body": {"key": "value"},
            }
        )

        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], Codes.INVALID_PARAM)
        self.agent_request.assert_not_called()

    def test_delete_with_body_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "DELETE",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "body": {"key": "value"},
            }
        )

        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], Codes.INVALID_PARAM)
        self.agent_request.assert_not_called()

    def test_non_string_query_value_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
                "query": {"active": True},
            }
        )

        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], Codes.INVALID_PARAM)
        self.agent_request.assert_not_called()

    def test_path_outside_prefix_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": "/_matrix/federation/v1/send/txnid",
            }
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_PATH_NOT_ALLOWED
        )
        self.agent_request.assert_not_called()

    def test_appservice_without_proxy_prefix_is_rejected(self) -> None:
        other_token = "other_as_token"
        other_appservice = ApplicationService(
            other_token,
            id="other_as",
            sender=UserID.from_string("@other_bot:test"),
            namespaces={},
            url=APPSERVICE_URL,
        )
        self.hs.get_datastores().main.services_cache.append(other_appservice)

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            },
            access_token=other_token,
        )

        self.assertEqual(channel.code, 400)
        self.assertEqual(
            channel.json_body["errcode"], Codes.AS_FEDPROXY_NO_PROXY_PREFIX
        )
        self.agent_request.assert_not_called()

    def test_unauthenticated_request_is_rejected(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            },
            access_token=None,
        )

        self.assertEqual(channel.code, 401)
        self.agent_request.assert_not_called()

    def test_non_appservice_token_is_rejected(self) -> None:
        self.register_user("normal_user", "password")
        user_token = self.login("normal_user", "password")

        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            },
            access_token=user_token,
        )

        self.assertEqual(channel.code, 403)
        self.agent_request.assert_not_called()

    @unittest.override_config({"experimental_features": {"msc4512_enabled": False}})
    def test_endpoint_not_registered_when_msc4512_disabled(self) -> None:
        channel = self._fed_proxy(
            {
                "destination": "remote.example.com",
                "method": "GET",
                "path": f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path",
            }
        )

        self.assertEqual(channel.code, 404)
        self.agent_request.assert_not_called()
