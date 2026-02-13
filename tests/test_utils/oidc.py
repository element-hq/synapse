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


import base64
import json
from hashlib import sha256
from typing import Any, ContextManager
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import attr

from twisted.web.http_headers import Headers
from twisted.web.iweb import IResponse

from synapse.server import HomeServer
from synapse.util.clock import Clock
from synapse.util.stringutils import random_string

from tests.test_utils import FakeResponse


@attr.s(slots=True, frozen=True, auto_attribs=True)
class FakeAuthorizationGrant:
    userinfo: dict
    client_id: str
    redirect_uri: str
    scope: str
    nonce: str | None
    sid: str | None


class FakeOidcServer:
    """A fake OpenID Connect Provider."""

    # All methods here are mocks, so we can track when they are called, and override
    # their values
    request: Mock
    get_jwks_handler: Mock
    get_metadata_handler: Mock
    get_userinfo_handler: Mock
    post_token_handler: Mock

    sid_counter: int = 0

    def __init__(self, clock: Clock, issuer: str):
        from authlib.jose import ECKey, KeySet

        self._clock = clock
        self.issuer = issuer

        self.request = Mock(side_effect=self._request)
        self.get_jwks_handler = Mock(side_effect=self._get_jwks_handler)
        self.get_metadata_handler = Mock(side_effect=self._get_metadata_handler)
        self.get_userinfo_handler = Mock(side_effect=self._get_userinfo_handler)
        self.post_token_handler = Mock(side_effect=self._post_token_handler)

        # A code -> grant mapping
        self._authorization_grants: dict[str, FakeAuthorizationGrant] = {}
        # An access token -> grant mapping
        self._sessions: dict[str, FakeAuthorizationGrant] = {}

        # We generate here an ECDSA key with the P-256 curve (ES256 algorithm) used for
        # signing JWTs. ECDSA keys are really quick to generate compared to RSA.
        self._key = ECKey.generate_key(crv="P-256", is_private=True)
        self._jwks = KeySet([ECKey.import_key(self._key.as_pem(is_private=False))])

        self._id_token_overrides: dict[str, Any] = {}

    def reset_mocks(self) -> None:
        self.request.reset_mock()
        self.get_jwks_handler.reset_mock()
        self.get_metadata_handler.reset_mock()
        self.get_userinfo_handler.reset_mock()
        self.post_token_handler.reset_mock()

    def patch_homeserver(self, hs: HomeServer) -> ContextManager[Mock]:
        """Patch the ``HomeServer`` HTTP client to handle requests through the ``FakeOidcServer``.

        This patch should be used whenever the HS is expected to perform request to the
        OIDC provider, e.g.::

            fake_oidc_server = self.helper.fake_oidc_server()
            with fake_oidc_server.patch_homeserver(hs):
                self.make_request("GET", "/_matrix/client/r0/login/sso/redirect")
        """
        return patch.object(hs.get_proxied_http_client(), "request", self.request)

    @property
    def authorization_endpoint(self) -> str:
        return self.issuer + "authorize"

    @property
    def token_endpoint(self) -> str:
        return self.issuer + "token"

    @property
    def userinfo_endpoint(self) -> str:
        return self.issuer + "userinfo"

    @property
    def metadata_endpoint(self) -> str:
        return self.issuer + ".well-known/openid-configuration"

    @property
    def jwks_uri(self) -> str:
        return self.issuer + "jwks"

    def get_metadata(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "jwks_uri": self.jwks_uri,
            "userinfo_endpoint": self.userinfo_endpoint,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["ES256"],
        }

    def get_jwks(self) -> dict:
        return self._jwks.as_dict()

    def get_userinfo(self, access_token: str) -> dict | None:
        """Given an access token, get the userinfo of the associated session."""
        session = self._sessions.get(access_token, None)
        if session is None:
            return None
        return session.userinfo

    def _sign(self, payload: dict) -> str:
        from authlib.jose import JsonWebSignature

        jws = JsonWebSignature()
        kid = self.get_jwks()["keys"][0]["kid"]
        protected = {"alg": "ES256", "kid": kid}
        json_payload = json.dumps(payload)
        return jws.serialize_compact(protected, json_payload, self._key).decode("utf-8")

    def generate_id_token(
        self, grant: FakeAuthorizationGrant, access_token: str
    ) -> str:
        # Generate a hash of the access token for the optional
        # `at_hash` field in an ID Token.
        #
        # 3.1.3.6. ID Token, https://openid.net/specs/openid-connect-core-1_0.html#CodeIDToken
        at_hash = (
            base64.urlsafe_b64encode(sha256(access_token.encode("ascii")).digest()[:16])
            .rstrip(b"=")
            .decode("ascii")
        )

        now = int(self._clock.time())
        id_token = {
            **grant.userinfo,
            "at_hash": at_hash,
            "iss": self.issuer,
            "aud": grant.client_id,
            "iat": now,
            "nbf": now,
            "exp": now + 600,
        }

        if grant.nonce is not None:
            id_token["nonce"] = grant.nonce

        if grant.sid is not None:
            id_token["sid"] = grant.sid

        id_token.update(self._id_token_overrides)

        return self._sign(id_token)

    def generate_logout_token(self, grant: FakeAuthorizationGrant) -> str:
        now = int(self._clock.time())
        logout_token = {
            "iss": self.issuer,
            "aud": grant.client_id,
            "iat": now,
            "jti": random_string(10),
            "events": {
                "http://schemas.openid.net/event/backchannel-logout": {},
            },
        }

        if grant.sid is not None:
            logout_token["sid"] = grant.sid

        if "sub" in grant.userinfo:
            logout_token["sub"] = grant.userinfo["sub"]

        return self._sign(logout_token)

    def id_token_override(self, overrides: dict) -> ContextManager[dict]:
        """Temporarily patch the ID token generated by the token endpoint."""
        return patch.object(self, "_id_token_overrides", overrides)

    def start_authorization(
        self,
        client_id: str,
        scope: str,
        redirect_uri: str,
        userinfo: dict,
        nonce: str | None = None,
        with_sid: bool = False,
    ) -> tuple[str, FakeAuthorizationGrant]:
        """Start an authorization request, and get back the code to use on the authorization endpoint."""
        code = random_string(10)
        sid = None
        if with_sid:
            sid = str(self.sid_counter)
            self.sid_counter += 1

        grant = FakeAuthorizationGrant(
            userinfo=userinfo,
            scope=scope,
            redirect_uri=redirect_uri,
            nonce=nonce,
            client_id=client_id,
            sid=sid,
        )
        self._authorization_grants[code] = grant

        return code, grant

    def exchange_code(self, code: str) -> dict[str, Any] | None:
        grant = self._authorization_grants.pop(code, None)
        if grant is None:
            return None

        access_token = random_string(10)
        self._sessions[access_token] = grant

        token = {
            "token_type": "Bearer",
            "access_token": access_token,
            "expires_in": 3600,
            "scope": grant.scope,
        }

        if "openid" in grant.scope:
            token["id_token"] = self.generate_id_token(grant, access_token)

        return dict(token)

    def buggy_endpoint(
        self,
        *,
        jwks: bool = False,
        metadata: bool = False,
        token: bool = False,
        userinfo: bool = False,
    ) -> ContextManager[dict[str, Mock]]:
        """A context which makes a set of endpoints return a 500 error.

        Args:
            jwks: If True, makes the JWKS endpoint return a 500 error.
            metadata: If True, makes the OIDC Discovery endpoint return a 500 error.
            token: If True, makes the token endpoint return a 500 error.
            userinfo: If True, makes the userinfo endpoint return a 500 error.
        """
        buggy = FakeResponse(code=500, body=b"Internal server error")

        patches = {}
        if jwks:
            patches["get_jwks_handler"] = Mock(return_value=buggy)
        if metadata:
            patches["get_metadata_handler"] = Mock(return_value=buggy)
        if token:
            patches["post_token_handler"] = Mock(return_value=buggy)
        if userinfo:
            patches["get_userinfo_handler"] = Mock(return_value=buggy)

        return patch.multiple(self, **patches)

    async def _request(
        self,
        method: str,
        uri: str,
        data: bytes | None = None,
        headers: Headers | None = None,
    ) -> IResponse:
        """The override of the SimpleHttpClient#request() method"""
        access_token: str | None = None

        if headers is None:
            headers = Headers()

        # Try to find the access token in the headers if any
        auth_headers = headers.getRawHeaders(b"Authorization")
        if auth_headers:
            parts = auth_headers[0].split(b" ")
            if parts[0] == b"Bearer" and len(parts) == 2:
                access_token = parts[1].decode("ascii")

        if method == "POST":
            # If the method is POST, assume it has an url-encoded body
            if data is None or headers.getRawHeaders(b"Content-Type") != [
                b"application/x-www-form-urlencoded"
            ]:
                return FakeResponse.json(code=400, payload={"error": "invalid_request"})

            params = parse_qs(data.decode("utf-8"))

            if uri == self.token_endpoint:
                # Even though this endpoint should be protected, this does not check
                # for client authentication. We're not checking it for simplicity,
                # and because client authentication is tested in other standalone tests.
                return self.post_token_handler(params)

        elif method == "GET":
            if uri == self.jwks_uri:
                return self.get_jwks_handler()
            elif uri == self.metadata_endpoint:
                return self.get_metadata_handler()
            elif uri == self.userinfo_endpoint:
                return self.get_userinfo_handler(access_token=access_token)

        return FakeResponse(code=404, body=b"404 not found")

    # Request handlers
    def _get_jwks_handler(self) -> IResponse:
        """Handles requests to the JWKS URI."""
        return FakeResponse.json(payload=self.get_jwks())

    def _get_metadata_handler(self) -> IResponse:
        """Handles requests to the OIDC well-known document."""
        return FakeResponse.json(payload=self.get_metadata())

    def _get_userinfo_handler(self, access_token: str | None) -> IResponse:
        """Handles requests to the userinfo endpoint."""
        if access_token is None:
            return FakeResponse(code=401)
        user_info = self.get_userinfo(access_token)
        if user_info is None:
            return FakeResponse(code=401)

        return FakeResponse.json(payload=user_info)

    def _post_token_handler(self, params: dict[str, list[str]]) -> IResponse:
        """Handles requests to the token endpoint."""
        code = params.get("code", [])

        if len(code) != 1:
            return FakeResponse.json(code=400, payload={"error": "invalid_request"})

        grant = self.exchange_code(code=code[0])
        if grant is None:
            return FakeResponse.json(code=400, payload={"error": "invalid_grant"})

        return FakeResponse.json(payload=grant)
