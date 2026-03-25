#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
# Copyright (C) 2023-2025 New Vector, Ltd
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
from http import HTTPStatus
from typing import ClassVar
from unittest.mock import AsyncMock

from parameterized import parameterized_class

from synapse.rest.client import auth_metadata

from tests.unittest import HomeserverTestCase, override_config, skip_unless
from tests.utils import HAS_AUTHLIB

ISSUER = "https://account.example.com/"


class AuthIssuerTestCase(HomeserverTestCase):
    servlets = [
        auth_metadata.register_servlets,
    ]

    def test_returns_404_when_msc3861_disabled(self) -> None:
        # Make an unauthenticated request for the discovery info.
        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/org.matrix.msc2965/auth_issuer",
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)

    @skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "experimental_features": {
                "msc3861": {
                    "enabled": True,
                    "issuer": ISSUER,
                    "client_id": "David Lister",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "Who shot Mister Burns?",
                }
            },
        }
    )
    def test_returns_issuer_when_oidc_enabled(self) -> None:
        # Patch the HTTP client to return the issuer metadata
        req_mock = AsyncMock(return_value={"issuer": ISSUER})
        self.hs.get_proxied_http_client().get_json = req_mock  # type: ignore[method-assign]

        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/org.matrix.msc2965/auth_issuer",
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"issuer": ISSUER})

        req_mock.assert_called_with(
            "https://account.example.com/.well-known/openid-configuration"
        )
        req_mock.reset_mock()

        # Second call it should use the cached value
        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/org.matrix.msc2965/auth_issuer",
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"issuer": ISSUER})
        req_mock.assert_not_called()


@parameterized_class(
    ("endpoint",),
    [
        ("/_matrix/client/unstable/org.matrix.msc2965/auth_metadata",),
        ("/_matrix/client/v1/auth_metadata",),
    ],
)
class AuthMetadataTestCase(HomeserverTestCase):
    endpoint: ClassVar[str]
    servlets = [
        auth_metadata.register_servlets,
    ]

    def test_returns_404_when_msc3861_disabled(self) -> None:
        # Make an unauthenticated request for the discovery info.
        channel = self.make_request("GET", self.endpoint)
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)

    @skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "experimental_features": {
                "msc3861": {
                    "enabled": True,
                    "issuer": ISSUER,
                    "client_id": "David Lister",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "Who shot Mister Burns?",
                }
            },
        }
    )
    def test_returns_issuer_when_oidc_enabled(self) -> None:
        # Patch the HTTP client to return the issuer metadata
        req_mock = AsyncMock(
            return_value={
                "issuer": ISSUER,
                "authorization_endpoint": "https://example.com/auth",
                "token_endpoint": "https://example.com/token",
            }
        )
        self.hs.get_proxied_http_client().get_json = req_mock  # type: ignore[method-assign]

        channel = self.make_request("GET", self.endpoint)

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(
            channel.json_body,
            {
                "issuer": ISSUER,
                "authorization_endpoint": "https://example.com/auth",
                "token_endpoint": "https://example.com/token",
            },
        )
