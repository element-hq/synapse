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

from http import HTTPStatus
from typing import Dict, List, Tuple

from twisted.web.resource import Resource

from synapse.api.errors import Codes
from synapse.federation.transport.server import BaseFederationServlet
from synapse.federation.transport.server._base import Authenticator, _parse_auth_header
from synapse.http.server import JsonResource
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.cancellation import cancellable
from synapse.util.ratelimitutils import FederationRateLimiter

from tests import unittest
from tests.http.server._base import test_disconnect


class CancellableFederationServlet(BaseFederationServlet):
    PATH = "/sleep"

    def __init__(
        self,
        hs: HomeServer,
        authenticator: Authenticator,
        ratelimiter: FederationRateLimiter,
        server_name: str,
    ):
        super().__init__(hs, authenticator, ratelimiter, server_name)
        self.clock = hs.get_clock()

    @cancellable
    async def on_GET(
        self, origin: str, content: None, query: Dict[bytes, List[bytes]]
    ) -> Tuple[int, JsonDict]:
        await self.clock.sleep(1.0)
        return HTTPStatus.OK, {"result": True}

    async def on_POST(
        self, origin: str, content: JsonDict, query: Dict[bytes, List[bytes]]
    ) -> Tuple[int, JsonDict]:
        await self.clock.sleep(1.0)
        return HTTPStatus.OK, {"result": True}


class BaseFederationServletCancellationTests(unittest.FederatingHomeserverTestCase):
    """Tests for `BaseFederationServlet` cancellation."""

    skip = "`BaseFederationServlet` does not support cancellation yet."

    path = f"{CancellableFederationServlet.PREFIX}{CancellableFederationServlet.PATH}"

    def create_test_resource(self) -> Resource:
        """Overrides `HomeserverTestCase.create_test_resource`."""
        resource = JsonResource(self.hs)

        CancellableFederationServlet(
            hs=self.hs,
            authenticator=Authenticator(self.hs),
            ratelimiter=self.hs.get_federation_ratelimiter(),
            server_name=self.hs.hostname,
        ).register(resource)

        return resource

    def test_cancellable_disconnect(self) -> None:
        """Test that handlers with the `@cancellable` flag can be cancelled."""
        channel = self.make_signed_federation_request(
            "GET", self.path, await_result=False
        )

        # Advance past all the rate limiting logic. If we disconnect too early, the
        # request won't be processed.
        self.pump()

        test_disconnect(
            self.reactor,
            channel,
            expect_cancellation=True,
            expected_body={"error": "Request cancelled", "errcode": Codes.UNKNOWN},
        )

    def test_uncancellable_disconnect(self) -> None:
        """Test that handlers without the `@cancellable` flag cannot be cancelled."""
        channel = self.make_signed_federation_request(
            "POST",
            self.path,
            content={},
            await_result=False,
        )

        # Advance past all the rate limiting logic. If we disconnect too early, the
        # request won't be processed.
        self.pump()

        test_disconnect(
            self.reactor,
            channel,
            expect_cancellation=False,
            expected_body={"result": True},
        )


class BaseFederationAuthorizationTests(unittest.TestCase):
    def test_authorization_header(self) -> None:
        """Tests that the Authorization header is parsed correctly."""

        # test a "normal" Authorization header
        self.assertEqual(
            _parse_auth_header(
                b'X-Matrix origin=foo,key="ed25519:1",sig="sig",destination="bar"'
            ),
            ("foo", "ed25519:1", "sig", "bar"),
        )
        # test an Authorization with extra spaces, upper-case names, and escaped
        # characters
        self.assertEqual(
            _parse_auth_header(
                b'X-Matrix  ORIGIN=foo,KEY="ed25\\519:1",SIG="sig",destination="bar"'
            ),
            ("foo", "ed25519:1", "sig", "bar"),
        )
        self.assertEqual(
            _parse_auth_header(
                b'X-Matrix origin=foo,key="ed25519:1",sig="sig",destination="bar",extra_field=ignored'
            ),
            ("foo", "ed25519:1", "sig", "bar"),
        )
