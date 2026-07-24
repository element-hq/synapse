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

import tempfile
from unittest.mock import Mock

import yaml

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor
from twisted.web.http_headers import Headers

from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils import FakeResponse

APPSERVICE_URL = "http://appservice.example.com"
APPSERVICE_PREFIX = "rtc/livekit"
VERSIONED_PREFIX = f"v1/{APPSERVICE_PREFIX}"


class ApplicationServiceFederationProxyTestCase(unittest.FederatingHomeserverTestCase):
    def default_config(self) -> JsonDict:
        config = super().default_config()
        _, path = tempfile.mkstemp(prefix="as_fed_proxy_config")
        with open(path, "w") as f:
            yaml.dump(
                {
                    "id": "proxy_as",
                    "url": None,
                    "as_token": "as_token",
                    "hs_token": "hs_token",
                    "sender_localpart": "proxy_bot",
                    "namespaces": {},
                    "io.element.msc4512.proxy_prefix": APPSERVICE_PREFIX,
                    "io.element.msc4512.proxy_url": APPSERVICE_URL,
                },
                f,
            )
        config["app_service_config_files"] = [path]
        config.setdefault("experimental_features", {}).setdefault(
            "msc4512_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.agent = Mock()
        hs.get_proxied_http_client().agent = self.agent

    def test_signed_get_is_proxied(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(
                FakeResponse.json(code=200, payload={"ok": True})
            )
        )

        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"ok": True})

        ((method, uri), kwargs) = self.agent.request.call_args

        self.assertEqual(method, b"GET")
        self.assertEqual(
            uri,
            f"{APPSERVICE_URL}/_matrix/federation/{VERSIONED_PREFIX}/some/path".encode(),
        )

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Authorization"))
        self.assertEqual(
            headers.getRawHeaders(b"X-Matrix-Origin"),
            [self.OTHER_SERVER_NAME.encode("ascii")],
        )

    def test_signed_get_is_proxied_at_root_path(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(
                FakeResponse.json(code=200, payload={"ok": True})
            )
        )

        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}"
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"ok": True})

        ((method, uri), kwargs) = self.agent.request.call_args

        self.assertEqual(method, b"GET")
        self.assertEqual(
            uri,
            f"{APPSERVICE_URL}/_matrix/federation/{VERSIONED_PREFIX}".encode(),
        )

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Authorization"))
        self.assertEqual(
            headers.getRawHeaders(b"X-Matrix-Origin"),
            [self.OTHER_SERVER_NAME.encode("ascii")],
        )

    def test_signed_post_is_proxied(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(
                FakeResponse.json(code=200, payload={"ok": True})
            )
        )

        content = {"key": "value"}
        channel = self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/{VERSIONED_PREFIX}/some/path",
            content=content,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"ok": True})

        ((method, uri), kwargs) = self.agent.request.call_args

        self.assertEqual(method, b"POST")
        self.assertEqual(
            uri,
            f"{APPSERVICE_URL}/_matrix/federation/{VERSIONED_PREFIX}/some/path".encode(),
        )

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Authorization"))
        self.assertEqual(
            headers.getRawHeaders(b"X-Matrix-Origin"),
            [self.OTHER_SERVER_NAME.encode("ascii")],
        )

        body_producer = kwargs["bodyProducer"]
        self.assertGreater(body_producer.length, 0)

    def test_connection_header_not_forwarded(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(FakeResponse.json(code=200, payload={}))
        )

        self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/{VERSIONED_PREFIX}/some/path",
            custom_headers=[("Connection", "close"), ("X-Forward", "forward")],
        )

        ((_method, _uri), kwargs) = self.agent.request.call_args

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Connection"))
        self.assertEqual(headers.getRawHeaders(b"X-Forward"), [b"forward"])

    def test_connection_header_with_named_request_headers_not_forwarded(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(FakeResponse.json(code=200, payload={}))
        )

        self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/{VERSIONED_PREFIX}/some/path",
            custom_headers=[
                ("Connection", "close, X-Omit"),
                ("X-Omit", "omit"),
                ("X-Forward", "forward"),
            ],
        )

        ((_method, _uri), kwargs) = self.agent.request.call_args

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Connection"))
        self.assertIsNone(headers.getRawHeaders(b"X-Omit"))
        self.assertEqual(headers.getRawHeaders(b"X-Forward"), [b"forward"])

    def test_host_and_content_length_headers_not_forwarded(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(FakeResponse.json(code=200, payload={}))
        )

        self.make_signed_federation_request(
            "POST",
            f"/_matrix/federation/{VERSIONED_PREFIX}/some/path",
            content={"key": "value"},
            custom_headers=[("Host", "original-client-facing-host.example")],
        )

        ((_method, _uri), kwargs) = self.agent.request.call_args

        headers: Headers = kwargs["headers"]
        self.assertIsNone(headers.getRawHeaders(b"Host"))
        self.assertIsNone(headers.getRawHeaders(b"Content-Length"))

    def test_response_headers_forwarded(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(
                FakeResponse(
                    code=200,
                    body=b"hello",
                    headers=Headers({"X-Forward": ["forward"]}),
                )
            )
        )

        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.result["body"], b"hello")
        self.assertEqual(channel.headers.getRawHeaders(b"X-Forward"), [b"forward"])

    def test_unsigned_get_is_rejected(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(FakeResponse.json(code=200, payload={}))
        )

        channel = self.make_request(
            "GET",
            f"/_matrix/federation/{VERSIONED_PREFIX}/some/path",
            shorthand=False,
        )

        self.assertEqual(channel.code, 401)
        self.agent.request.assert_not_called()

    @unittest.override_config({"rc_federation": {"reject_limit": -1}})
    def test_rate_limited_request_is_rejected(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(FakeResponse.json(code=200, payload={}))
        )

        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 429)
        self.agent.request.assert_not_called()

    def test_non_existing_path_under_proxy_prefix_is_rejected(self) -> None:
        self.agent.request = Mock(return_value=defer.fail(Exception("boom")))

        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 404)
        self.agent.request.assert_called()

    def test_unregistered_prefix_is_rejected(self) -> None:
        channel = self.make_signed_federation_request(
            "GET", "/_matrix/federation/not_a_registered_prefix/some/path"
        )

        self.assertEqual(channel.code, 404)

    def test_unregistered_prefix_with_suffix_is_rejected(self) -> None:
        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}-2"
        )

        self.assertEqual(channel.code, 404)

    def test_missing_version_segment_is_rejected(self) -> None:
        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{APPSERVICE_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 404)
        self.agent.request.assert_not_called()

    def test_unstable_version_segment_is_proxied(self) -> None:
        self.agent.request = Mock(
            return_value=defer.succeed(
                FakeResponse.json(code=200, payload={"ok": True})
            )
        )

        path = (
            f"/_matrix/federation/unstable/org.example.msc9999/"
            f"{APPSERVICE_PREFIX}/some/path"
        )
        channel = self.make_signed_federation_request("GET", path)

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"ok": True})

        ((method, uri), _kwargs) = self.agent.request.call_args
        self.assertEqual(method, b"GET")
        self.assertEqual(uri, f"{APPSERVICE_URL}{path}".encode())

    @unittest.override_config({"experimental_features": {"msc4512_enabled": False}})
    def test_proxy_route_not_registered_when_msc4512_disabled(self) -> None:
        channel = self.make_signed_federation_request(
            "GET", f"/_matrix/federation/{VERSIONED_PREFIX}/some/path"
        )

        self.assertEqual(channel.code, 404)
        self.agent.request.assert_not_called()
