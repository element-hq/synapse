#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Matrix.org Foundation C.I.C.
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

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from typing import Any, ClassVar, Coroutine, Generator, TypeVar, Union
from unittest.mock import ANY, AsyncMock, Mock
from urllib.parse import parse_qs

from parameterized.parameterized import parameterized_class
from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import sign_json

from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.api.auth.mas import MasDelegatedAuth
from synapse.api.errors import (
    AuthError,
    Codes,
    HttpResponseException,
    InvalidClientTokenError,
    SynapseError,
)
from synapse.appservice import ApplicationService
from synapse.http.site import SynapseRequest
from synapse.rest import admin
from synapse.rest.client import account, devices, keys, login, logout, register
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock

from tests.server import FakeChannel
from tests.test_utils import get_awaitable_result
from tests.unittest import HomeserverTestCase, override_config, skip_unless
from tests.utils import HAS_AUTHLIB, checked_cast, mock_getRawHeaders

# These are a few constants that are used as config parameters in the tests.
SERVER_NAME = "test"
ISSUER = "https://issuer/"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
BASE_URL = "https://synapse/"
SCOPES = ["openid"]

AUTHORIZATION_ENDPOINT = ISSUER + "authorize"
TOKEN_ENDPOINT = ISSUER + "token"
USERINFO_ENDPOINT = ISSUER + "userinfo"
WELL_KNOWN = ISSUER + ".well-known/openid-configuration"
JWKS_URI = ISSUER + ".well-known/jwks.json"
INTROSPECTION_ENDPOINT = ISSUER + "introspect"

SYNAPSE_ADMIN_SCOPE = "urn:synapse:admin:*"
DEVICE = "AABBCCDD"
SUBJECT = "abc-def-ghi"
USERNAME = "test-user"
USER_ID = "@" + USERNAME + ":" + SERVER_NAME
OIDC_ADMIN_USERID = f"@__oidc_admin:{SERVER_NAME}"


async def get_json(url: str) -> JsonDict:
    # Mock get_json calls to handle jwks & oidc discovery endpoints
    if url == WELL_KNOWN:
        # Minimal discovery document, as defined in OpenID.Discovery
        # https://openid.net/specs/openid-connect-discovery-1_0.html#ProviderMetadata
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
            "userinfo_endpoint": USERINFO_ENDPOINT,
            "introspection_endpoint": INTROSPECTION_ENDPOINT,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
    elif url == JWKS_URI:
        return {"keys": []}

    return {}


@skip_unless(HAS_AUTHLIB, "requires authlib")
@parameterized_class(
    ("device_scope_prefix", "api_scope"),
    [
        ("urn:matrix:client:device:", "urn:matrix:client:api:*"),
        (
            "urn:matrix:org.matrix.msc2967.client:device:",
            "urn:matrix:org.matrix.msc2967.client:api:*",
        ),
    ],
)
class MSC3861OAuthDelegation(HomeserverTestCase):
    device_scope_prefix: ClassVar[str]
    api_scope: ClassVar[str]

    @property
    def device_scope(self) -> str:
        return self.device_scope_prefix + DEVICE

    servlets = [
        account.register_servlets,
        keys.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL
        config["disable_registration"] = True
        config["experimental_features"] = {
            "msc3861": {
                "enabled": True,
                "issuer": ISSUER,
                "client_id": CLIENT_ID,
                "client_auth_method": "client_secret_post",
                "client_secret": CLIENT_SECRET,
                "admin_token": "admin_token_value",
            }
        }
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock(spec=["get_json"])
        self.http_client.get_json.side_effect = get_json
        self.http_client.user_agent = b"Synapse Test"

        hs = self.setup_test_homeserver(proxied_http_client=self.http_client)

        # Import this here so that we've checked that authlib is available.
        from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

        self.auth = checked_cast(MSC3861DelegatedAuth, hs.get_auth())

        self._rust_client = Mock(spec=["post"])
        self.auth._rust_http_client = self._rust_client

        return hs

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Provision the user and the device we use in the tests.
        store = homeserver.get_datastores().main
        self.get_success(store.register_user(USER_ID))
        self.get_success(
            store.store_device(USER_ID, DEVICE, initial_device_display_name=None)
        )

    def _set_introspection_returnvalue(self, response_value: Any) -> AsyncMock:
        self._rust_client.post = mock = AsyncMock(
            return_value=json.dumps(response_value).encode("utf-8")
        )
        return mock

    def _assertParams(self) -> None:
        """Assert that the request parameters are correct."""
        params = parse_qs(self._rust_client.post.call_args[1]["request_body"])
        self.assertEqual(params["token"], ["mockAccessToken"])
        self.assertEqual(params["client_id"], [CLIENT_ID])
        self.assertEqual(params["client_secret"], [CLIENT_SECRET])

    def test_inactive_token(self) -> None:
        """The handler should return a 403 where the token is inactive."""

        self._set_introspection_returnvalue({"active": False})
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()

    def test_active_no_scope(self) -> None:
        """The handler should return a 403 where no scope is given."""

        self._set_introspection_returnvalue({"active": True})
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()

    def test_active_user_no_subject(self) -> None:
        """The handler should return a 500 when no subject is present."""

        self._set_introspection_returnvalue(
            {"active": True, "scope": " ".join([self.api_scope])},
        )

        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()

    def test_active_no_user_scope(self) -> None:
        """The handler should return a 500 when no subject is present."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([self.device_scope]),
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()

    def test_active_admin_not_user(self) -> None:
        """The handler should raise when the scope has admin right but not user."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([SYNAPSE_ADMIN_SCOPE]),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()

    def test_active_admin(self) -> None:
        """The handler should return a requester with admin rights."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([SYNAPSE_ADMIN_SCOPE, self.api_scope]),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()
        self.assertEqual(requester.user.to_string(), "@%s:%s" % (USERNAME, SERVER_NAME))
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(requester.device_id, None)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), True
        )

    def test_active_admin_highest_privilege(self) -> None:
        """The handler should resolve to the most permissive scope."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([SYNAPSE_ADMIN_SCOPE, self.api_scope]),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()
        self.assertEqual(requester.user.to_string(), "@%s:%s" % (USERNAME, SERVER_NAME))
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(requester.device_id, None)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), True
        )

    def test_active_user(self) -> None:
        """The handler should return a requester with normal user rights."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([self.api_scope]),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()
        self.assertEqual(requester.user.to_string(), "@%s:%s" % (USERNAME, SERVER_NAME))
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(requester.device_id, None)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), False
        )

    def test_active_user_with_device(self) -> None:
        """The handler should return a requester with normal user rights and a device ID."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([self.api_scope, self.device_scope]),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        self._assertParams()
        self.assertEqual(requester.user.to_string(), "@%s:%s" % (USERNAME, SERVER_NAME))
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), False
        )
        self.assertEqual(requester.device_id, DEVICE)

    def test_active_user_with_device_explicit_device_id(self) -> None:
        """The handler should return a requester with normal user rights and a device ID, given explicitly, as supported by MAS 0.15+"""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([self.api_scope]),
                "device_id": DEVICE,
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.http_client.get_json.assert_called_once_with(WELL_KNOWN)
        self._rust_client.post.assert_called_once_with(
            url=INTROSPECTION_ENDPOINT,
            response_limit=ANY,
            request_body=ANY,
            headers=ANY,
        )
        # It should have called with the 'X-MAS-Supports-Device-Id: 1' header
        self.assertEqual(
            self._rust_client.post.call_args[1]["headers"].get(
                "X-MAS-Supports-Device-Id",
            ),
            "1",
        )
        self._assertParams()
        self.assertEqual(requester.user.to_string(), "@%s:%s" % (USERNAME, SERVER_NAME))
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), False
        )
        self.assertEqual(requester.device_id, DEVICE)

    def test_multiple_devices(self) -> None:
        """The handler should raise an error if multiple devices are found in the scope."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join(
                    [
                        self.api_scope,
                        f"{self.device_scope_prefix}AABBCC",
                        f"{self.device_scope_prefix}DDEEFF",
                    ]
                ),
                "username": USERNAME,
            }
        )
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        self.get_failure(self.auth.get_user_by_req(request), AuthError)

    def test_unavailable_introspection_endpoint(self) -> None:
        """The handler should return an internal server error."""
        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()

        # The introspection endpoint is returning an error.
        self._rust_client.post = AsyncMock(
            side_effect=HttpResponseException(
                code=500, msg="Internal Server Error", response=b"{}"
            )
        )
        error = self.get_failure(self.auth.get_user_by_req(request), SynapseError)
        self.assertEqual(error.value.code, 503)

        # The introspection endpoint request fails.
        self._rust_client.post = AsyncMock(side_effect=Exception())
        error = self.get_failure(self.auth.get_user_by_req(request), SynapseError)
        self.assertEqual(error.value.code, 503)

        # The introspection endpoint does not return a JSON object.
        self._set_introspection_returnvalue(["this is an array", "not an object"])

        error = self.get_failure(self.auth.get_user_by_req(request), SynapseError)
        self.assertEqual(error.value.code, 503)

        # The introspection endpoint does not return valid JSON.
        self._set_introspection_returnvalue("this is not valid JSON")

        error = self.get_failure(self.auth.get_user_by_req(request), SynapseError)
        self.assertEqual(error.value.code, 503)

    def test_cached_expired_introspection(self) -> None:
        """The handler should raise an error if the introspection response gives
        an expiry time, the introspection response is cached and then the entry is
        re-requested after it has expired."""

        introspection_mock = self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join(
                    [
                        self.api_scope,
                        f"{self.device_scope_prefix}AABBCC",
                    ]
                ),
                "username": USERNAME,
                "expires_in": 60,
            }
        )

        request = Mock(args={})
        request.args[b"access_token"] = [b"mockAccessToken"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()

        # The first CS-API request causes a successful introspection
        self.get_success(self.auth.get_user_by_req(request))
        self.assertEqual(introspection_mock.call_count, 1)

        # Sleep for 60 seconds so the token expires.
        self.reactor.advance(60.0)

        # Now the CS-API request fails because the token expired
        self.get_failure(self.auth.get_user_by_req(request), InvalidClientTokenError)
        # Ensure another introspection request was not sent
        self.assertEqual(introspection_mock.call_count, 1)

    def make_device_keys(self, user_id: str, device_id: str) -> JsonDict:
        # We only generate a master key to simplify the test.
        master_signing_key = generate_signing_key(device_id)
        master_verify_key = encode_verify_key_base64(get_verify_key(master_signing_key))

        return {
            "master_key": sign_json(
                {
                    "user_id": user_id,
                    "usage": ["master"],
                    "keys": {"ed25519:" + master_verify_key: master_verify_key},
                },
                user_id,
                master_signing_key,
            ),
        }

    def test_cross_signing(self) -> None:
        """Try uploading device keys with OAuth delegation enabled."""

        self._set_introspection_returnvalue(
            {
                "active": True,
                "sub": SUBJECT,
                "scope": " ".join([self.api_scope, self.device_scope]),
                "username": USERNAME,
            }
        )
        keys_upload_body = self.make_device_keys(USER_ID, DEVICE)
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys_upload_body,
            access_token="mockAccessToken",
        )

        self.assertEqual(channel.code, 200, channel.json_body)

        # Try uploading *different* keys; it should cause a 501 error.
        keys_upload_body = self.make_device_keys(USER_ID, DEVICE)
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys_upload_body,
            access_token="mockAccessToken",
        )

        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)

    def test_admin_token(self) -> None:
        """The handler should return a requester with admin rights when admin_token is used."""
        self._set_introspection_returnvalue({"active": False})

        request = Mock(args={})
        request.args[b"access_token"] = [b"admin_token_value"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        requester = self.get_success(self.auth.get_user_by_req(request))
        self.assertEqual(
            requester.user.to_string(),
            OIDC_ADMIN_USERID,
        )
        self.assertEqual(requester.is_guest, False)
        self.assertEqual(requester.device_id, None)
        self.assertEqual(
            get_awaitable_result(self.auth.is_server_admin(requester)), True
        )

        # There should be no call to the introspection endpoint
        self._rust_client.post.assert_not_called()

    @override_config({"mau_stats_only": True})
    def test_request_tracking(self) -> None:
        """Using an access token should update the client_ips and MAU tables."""
        # To start, there are no MAU users.
        store = self.hs.get_datastores().main
        mau = self.get_success(store.get_monthly_active_count())
        self.assertEqual(mau, 0)

        known_token = "token-token-GOOD-:)"

        async def mock_http_client_request(
            url: str, request_body: str, **kwargs: Any
        ) -> bytes:
            """Mocked auth provider response."""
            token = parse_qs(request_body)["token"][0]
            if token == known_token:
                return json.dumps(
                    {
                        "active": True,
                        "scope": self.api_scope,
                        "sub": SUBJECT,
                        "username": USERNAME,
                    },
                ).encode("utf-8")

            return json.dumps({"active": False}).encode("utf-8")

        self._rust_client.post = mock_http_client_request

        EXAMPLE_IPV4_ADDR = "123.123.123.123"
        EXAMPLE_USER_AGENT = "httprettygood"

        # First test a known access token
        channel = FakeChannel(self.site, self.reactor)
        # type-ignore: FakeChannel is a mock of an HTTPChannel, not a proper HTTPChannel
        req = SynapseRequest(channel, self.site, self.hs.hostname)  # type: ignore[arg-type]
        req.client.host = EXAMPLE_IPV4_ADDR
        req.requestHeaders.addRawHeader("Authorization", f"Bearer {known_token}")
        req.requestHeaders.addRawHeader("User-Agent", EXAMPLE_USER_AGENT)
        req.content = BytesIO(b"")
        req.requestReceived(
            b"GET",
            b"/_matrix/client/v3/account/whoami",
            b"1.1",
        )
        channel.await_result()
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(channel.json_body["user_id"], USER_ID, channel.json_body)

        # Expect to see one MAU entry, from the first request
        mau = self.get_success(store.get_monthly_active_count())
        self.assertEqual(mau, 1)

        conn_infos = self.get_success(
            store.get_user_ip_and_agents(UserID.from_string(USER_ID))
        )
        self.assertEqual(len(conn_infos), 1, conn_infos)
        conn_info = conn_infos[0]
        self.assertEqual(conn_info["access_token"], known_token)
        self.assertEqual(conn_info["ip"], EXAMPLE_IPV4_ADDR)
        self.assertEqual(conn_info["user_agent"], EXAMPLE_USER_AGENT)

        # Now test MAS making a request using the special __oidc_admin token
        MAS_IPV4_ADDR = "127.0.0.1"
        MAS_USER_AGENT = "masmasmas"

        channel = FakeChannel(self.site, self.reactor)
        req = SynapseRequest(channel, self.site, self.hs.hostname)  # type: ignore[arg-type]
        req.client.host = MAS_IPV4_ADDR
        req.requestHeaders.addRawHeader(
            "Authorization", f"Bearer {self.auth._admin_token()}"
        )
        req.requestHeaders.addRawHeader("User-Agent", MAS_USER_AGENT)
        req.content = BytesIO(b"")
        req.requestReceived(
            b"GET",
            b"/_matrix/client/v3/account/whoami",
            b"1.1",
        )
        channel.await_result()
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        self.assertEqual(
            channel.json_body["user_id"], OIDC_ADMIN_USERID, channel.json_body
        )

        # Still expect to see one MAU entry, from the first request
        mau = self.get_success(store.get_monthly_active_count())
        self.assertEqual(mau, 1)

        conn_infos = self.get_success(
            store.get_user_ip_and_agents(UserID.from_string(OIDC_ADMIN_USERID))
        )
        self.assertEqual(conn_infos, [])


class FakeMasHandler(BaseHTTPRequestHandler):
    server: "FakeMasServer"

    def do_POST(self) -> None:
        self.server.calls += 1

        if self.path != "/oauth2/introspect":
            self.send_response(404)
            self.end_headers()
            self.wfile.close()
            return

        auth = self.headers.get("Authorization")
        if auth is None or auth != f"Bearer {self.server.secret}":
            self.send_response(401)
            self.end_headers()
            self.wfile.close()
            return

        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_response(400)
            self.end_headers()
            self.wfile.close()
            return

        raw_body = self.rfile.read(int(content_length))
        body = parse_qs(raw_body)
        param = body.get(b"token")
        if param is None:
            self.send_response(400)
            self.end_headers()
            self.wfile.close()
            return

        self.server.last_token_seen = param[0].decode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.server.introspection_response).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        # Don't log anything; by default, the server logs to stderr
        pass


class FakeMasServer(HTTPServer):
    """A fake MAS server for testing.

    This opens a real HTTP server on a random port, on a separate thread.
    """

    introspection_response: JsonDict = {}
    """Determines what the response to the introspection endpoint will be."""

    secret: str = "verysecret"
    """The shared secret used to authenticate the introspection endpoint."""

    last_token_seen: str | None = None
    """What is the last access token seen by the introspection endpoint."""

    calls: int = 0
    """How many times has the introspection endpoint been called."""

    _thread: threading.Thread

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeMasHandler)

        self._thread = threading.Thread(
            target=self.serve_forever,
            name="FakeMasServer",
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        super().shutdown()
        self._thread.join()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"


T = TypeVar("T")


@parameterized_class(
    ("device_scope_prefix", "api_scope"),
    [
        ("urn:matrix:client:device:", "urn:matrix:client:api:*"),
        (
            "urn:matrix:org.matrix.msc2967.client:device:",
            "urn:matrix:org.matrix.msc2967.client:api:*",
        ),
    ],
)
class MasAuthDelegation(HomeserverTestCase):
    server: FakeMasServer
    device_scope_prefix: ClassVar[str]
    api_scope: ClassVar[str]

    @property
    def device_scope(self) -> str:
        return self.device_scope_prefix + DEVICE

    def till_deferred_has_result(
        self,
        awaitable: Union[
            "Coroutine[Deferred[Any], Any, T]",
            "Generator[Deferred[Any], Any, T]",
            "Deferred[T]",
        ],
    ) -> "Deferred[T]":
        """Wait until a deferred has a result.

        This is useful because the Rust HTTP client will resolve the deferred
        using reactor.callFromThread, which are only run when we call
        reactor.advance.
        """
        deferred = ensureDeferred(awaitable)
        tries = 0
        while not deferred.called:
            time.sleep(0.1)
            self.reactor.advance(0)
            tries += 1
            if tries > 100:
                raise Exception("Timed out waiting for deferred to resolve")

        return deferred

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL
        config["disable_registration"] = True
        config["matrix_authentication_service"] = {
            "enabled": True,
            "endpoint": self.server.endpoint,
            "secret": self.server.secret,
        }
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.server = FakeMasServer()
        hs = self.setup_test_homeserver()
        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()
        self._auth = checked_cast(MasDelegatedAuth, hs.get_auth())
        return hs

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Provision the user and the device we use in the tests.
        store = homeserver.get_datastores().main
        self.get_success(store.register_user(USER_ID))
        self.get_success(
            store.store_device(USER_ID, DEVICE, initial_device_display_name=None)
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def test_simple_introspection(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([self.api_scope, self.device_scope]),
            "username": USERNAME,
            "expires_in": 60,
        }

        requester = self.get_success(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            )
        )

        self.assertEquals(requester.user.to_string(), USER_ID)
        self.assertEquals(requester.device_id, DEVICE)
        self.assertFalse(self.get_success(self._auth.is_server_admin(requester)))

        self.assertEquals(
            self.server.last_token_seen,
            "some_token",
        )

    def test_unexpiring_token(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([self.api_scope, self.device_scope]),
            "username": USERNAME,
        }

        requester = self.get_success(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            )
        )

        self.assertEquals(requester.user.to_string(), USER_ID)
        self.assertEquals(requester.device_id, DEVICE)
        self.assertFalse(self.get_success(self._auth.is_server_admin(requester)))

        self.assertEquals(
            self.server.last_token_seen,
            "some_token",
        )

    def test_inexistent_device(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([self.api_scope, f"{self.device_scope_prefix}ABCDEF"]),
            "username": USERNAME,
            "expires_in": 60,
        }

        failure = self.get_failure(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            ),
            InvalidClientTokenError,
        )
        self.assertEqual(failure.value.code, 401)

    def test_inexistent_user(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([self.api_scope]),
            "username": "inexistent_user",
            "expires_in": 60,
        }

        failure = self.get_failure(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            ),
            AuthError,
        )
        # This is a 500, it should never happen really
        self.assertEqual(failure.value.code, 500)

    def test_missing_scope(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": "openid",
            "username": USERNAME,
            "expires_in": 60,
        }

        failure = self.get_failure(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            ),
            InvalidClientTokenError,
        )
        self.assertEqual(failure.value.code, 401)

    def test_invalid_response(self) -> None:
        self.server.introspection_response = {}

        failure = self.get_failure(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            ),
            SynapseError,
        )
        self.assertEqual(failure.value.code, 503)

    def test_device_id_in_body(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": self.api_scope,
            "username": USERNAME,
            "expires_in": 60,
            "device_id": DEVICE,
        }

        requester = self.get_success(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            )
        )

        self.assertEqual(requester.device_id, DEVICE)

    def test_admin_scope(self) -> None:
        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([SYNAPSE_ADMIN_SCOPE, self.api_scope]),
            "username": USERNAME,
            "expires_in": 60,
        }

        requester = self.get_success(
            self.till_deferred_has_result(
                self._auth.get_user_by_access_token("some_token")
            )
        )

        self.assertEqual(requester.user.to_string(), USER_ID)
        self.assertTrue(self.get_success(self._auth.is_server_admin(requester)))

    def test_cached_expired_introspection(self) -> None:
        """The handler should raise an error if the introspection response gives
        an expiry time, the introspection response is cached and then the entry is
        re-requested after it has expired."""

        self.server.introspection_response = {
            "active": True,
            "sub": SUBJECT,
            "scope": " ".join([self.api_scope, self.device_scope]),
            "username": USERNAME,
            "expires_in": 60,
        }

        self.assertEqual(self.server.calls, 0)

        request = Mock(args={})
        request.args[b"access_token"] = [b"some_token"]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()

        # The first CS-API request causes a successful introspection
        self.get_success(
            self.till_deferred_has_result(self._auth.get_user_by_req(request))
        )
        self.assertEqual(self.server.calls, 1)

        # Sleep for 60 seconds so the token expires.
        self.reactor.advance(60.0)

        # Now the CS-API request fails because the token expired
        self.assertFailure(
            self.till_deferred_has_result(self._auth.get_user_by_req(request)),
            InvalidClientTokenError,
        )
        # Ensure another introspection request was not sent
        self.assertEqual(self.server.calls, 1)


class MasAuthDelegationWithSubpath(MasAuthDelegation):
    """Test MAS delegation when the MAS server is hosted on a subpath."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        # Override the endpoint to include a subpath
        config["matrix_authentication_service"]["endpoint"] = (
            self.server.endpoint + "auth/path/"
        )
        return config

    def test_introspection_endpoint_uses_subpath(self) -> None:
        """Test that the introspection endpoint correctly uses the configured subpath."""
        expected_introspection_url = (
            self.server.endpoint + "auth/path/oauth2/introspect"
        )
        self.assertEqual(self._auth._introspection_endpoint, expected_introspection_url)

    def test_metadata_url_uses_subpath(self) -> None:
        """Test that the metadata URL correctly uses the configured subpath."""
        expected_metadata_url = (
            self.server.endpoint + "auth/path/.well-known/openid-configuration"
        )
        self.assertEqual(self._auth._metadata_url, expected_metadata_url)


@parameterized_class(
    ("config",),
    [
        (
            {
                "matrix_authentication_service": {
                    "enabled": True,
                    "endpoint": "http://localhost:1234/",
                    "secret": "secret",
                },
            },
        ),
    ]
    # Run the tests with experimental delegation only if authlib is available
    + [
        (
            {
                "experimental_features": {
                    "msc3861": {
                        "enabled": True,
                        "issuer": ISSUER,
                        "client_id": CLIENT_ID,
                        "client_auth_method": "client_secret_post",
                        "client_secret": CLIENT_SECRET,
                        "admin_token": "admin_token_value",
                    }
                }
            },
        ),
    ]
    * HAS_AUTHLIB,
)
class DisabledEndpointsTestCase(HomeserverTestCase):
    servlets = [
        account.register_servlets,
        devices.register_servlets,
        keys.register_servlets,
        register.register_servlets,
        login.register_servlets,
        logout.register_servlets,
        admin.register_servlets,
    ]

    config: dict[str, Any]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL
        config["disable_registration"] = True
        config.update(self.config)
        return config

    def expect_unauthorized(
        self, method: str, path: str, content: bytes | str | JsonDict = ""
    ) -> None:
        channel = self.make_request(method, path, content, shorthand=False)

        self.assertEqual(channel.code, 401, channel.json_body)

    def expect_unrecognized(
        self,
        method: str,
        path: str,
        content: bytes | str | JsonDict = "",
        auth: bool = False,
    ) -> None:
        channel = self.make_request(
            method, path, content, access_token="token" if auth else None
        )

        self.assertEqual(channel.code, 404, channel.json_body)
        self.assertEqual(
            channel.json_body["errcode"], Codes.UNRECOGNIZED, channel.json_body
        )

    def expect_forbidden(
        self, method: str, path: str, content: bytes | str | JsonDict = ""
    ) -> None:
        channel = self.make_request(method, path, content)

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["errcode"], Codes.FORBIDDEN, channel.json_body
        )

    def test_uia_endpoints(self) -> None:
        """Test that endpoints that were removed in MSC2964 are no longer available."""

        # This is just an endpoint that should remain visible (but requires auth):
        self.expect_unauthorized("GET", "/_matrix/client/v3/devices")

        # This remains usable, but will require a uia scope:
        self.expect_unauthorized(
            "POST", "/_matrix/client/v3/keys/device_signing/upload"
        )

    def test_3pid_endpoints(self) -> None:
        """Test that 3pid account management endpoints that were removed in MSC2964 are no longer available."""

        # Remains and requires auth:
        self.expect_unauthorized("GET", "/_matrix/client/v3/account/3pid")
        self.expect_unauthorized(
            "POST",
            "/_matrix/client/v3/account/3pid/bind",
            {
                "client_secret": "foo",
                "id_access_token": "bar",
                "id_server": "foo",
                "sid": "bar",
            },
        )
        self.expect_unauthorized("POST", "/_matrix/client/v3/account/3pid/unbind", {})

        # These are gone:
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/account/3pid"
        )  # deprecated
        self.expect_unrecognized("POST", "/_matrix/client/v3/account/3pid/add")
        self.expect_unrecognized("POST", "/_matrix/client/v3/account/3pid/delete")
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/account/3pid/email/requestToken"
        )
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/account/3pid/msisdn/requestToken"
        )

    def test_account_management_endpoints_removed(self) -> None:
        """Test that account management endpoints that were removed in MSC2964 are no longer available."""
        self.expect_unrecognized("POST", "/_matrix/client/v3/account/deactivate")
        self.expect_unrecognized("POST", "/_matrix/client/v3/account/password")
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/account/password/email/requestToken"
        )
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/account/password/msisdn/requestToken"
        )

    def test_registration_endpoints_removed(self) -> None:
        """Test that registration endpoints that were removed in MSC2964 are no longer available."""
        appservice = ApplicationService(
            token="i_am_an_app_service",
            id="1234",
            namespaces={"users": [{"regex": r"@alice:.+", "exclusive": True}]},
            sender=UserID.from_string("@as_main:test"),
        )

        self.hs.get_datastores().main.services_cache = [appservice]
        self.expect_unrecognized(
            "GET", "/_matrix/client/v1/register/m.login.registration_token/validity"
        )

        # Registration is disabled
        self.expect_forbidden(
            "POST",
            "/_matrix/client/v3/register",
            {"username": "alice", "password": "hunter2"},
        )

        # This is still available for AS registrations
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/register",
            {
                "username": "alice",
                "type": "m.login.application_service",
                "inhibit_login": True,
            },
            shorthand=False,
            access_token="i_am_an_app_service",
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        self.expect_unrecognized("GET", "/_matrix/client/v3/register/available")
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/register/email/requestToken"
        )
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/register/msisdn/requestToken"
        )

    def test_session_management_endpoints_removed(self) -> None:
        """Test that session management endpoints that were removed in MSC2964 are no longer available."""
        self.expect_unrecognized("GET", "/_matrix/client/v3/login")
        self.expect_unrecognized("POST", "/_matrix/client/v3/login")
        self.expect_unrecognized("GET", "/_matrix/client/v3/login/sso/redirect")
        self.expect_unrecognized("POST", "/_matrix/client/v3/logout")
        self.expect_unrecognized("POST", "/_matrix/client/v3/logout/all")
        self.expect_unrecognized("POST", "/_matrix/client/v3/refresh")
        self.expect_unrecognized("GET", "/_matrix/static/client/login")

    def test_device_management_endpoints_removed(self) -> None:
        """Test that device management endpoints that were removed in MSC2964 are no longer available."""

        # Because we still support those endpoints with ASes, it checks the
        # access token before returning 404
        self.hs.get_auth().get_user_by_req = AsyncMock(  # type: ignore[method-assign]
            return_value=create_requester(
                user_id=USER_ID,
                device_id=DEVICE,
            )
        )

        self.expect_unrecognized("POST", "/_matrix/client/v3/delete_devices", auth=True)
        self.expect_unrecognized(
            "DELETE", "/_matrix/client/v3/devices/{DEVICE}", auth=True
        )

    def test_openid_endpoints_removed(self) -> None:
        """Test that OpenID id_token endpoints that were removed in MSC2964 are no longer available."""
        self.expect_unrecognized(
            "POST", "/_matrix/client/v3/user/{USERNAME}/openid/request_token"
        )

    def test_admin_api_endpoints_removed(self) -> None:
        """Test that admin API endpoints that were removed in MSC2964 are no longer available."""
        self.expect_unrecognized("GET", "/_synapse/admin/v1/registration_tokens")
        self.expect_unrecognized("POST", "/_synapse/admin/v1/registration_tokens/new")
        self.expect_unrecognized("GET", "/_synapse/admin/v1/registration_tokens/abcd")
        self.expect_unrecognized("PUT", "/_synapse/admin/v1/registration_tokens/abcd")
        self.expect_unrecognized(
            "DELETE", "/_synapse/admin/v1/registration_tokens/abcd"
        )
        self.expect_unrecognized("POST", "/_synapse/admin/v1/reset_password/foo")
        self.expect_unrecognized("POST", "/_synapse/admin/v1/users/foo/login")
        self.expect_unrecognized("GET", "/_synapse/admin/v1/register")
        self.expect_unrecognized("POST", "/_synapse/admin/v1/register")
        self.expect_unrecognized("GET", "/_synapse/admin/v1/users/foo/admin")
        self.expect_unrecognized("PUT", "/_synapse/admin/v1/users/foo/admin")
        self.expect_unrecognized("POST", "/_synapse/admin/v1/account_validity/validity")
