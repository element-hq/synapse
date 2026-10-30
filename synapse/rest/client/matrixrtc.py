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

from typing import TYPE_CHECKING

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class MatrixRTCTransportsRestServlet(RestServlet):
    """Implements `GET /_matrix/client/v1/rtc/transports` from MSC4143."""

    PATTERNS = client_patterns(r"/rtc/transports$", releases=("v1",), unstable=False)
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._transports = hs.config.matrix_rtc.transports

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        # Require authentication for this endpoint.
        await self._auth.get_user_by_req(request, allow_guest=True)

        return 200, {"transports": self._transports}


class UnstableMatrixRTCTransportsRestServlet(RestServlet):
    """Implements the unstable `/org.matrix.msc4143/rtc/transports` endpoint.

    This is retained for backwards compatibility with clients that don't yet support
    the stable endpoint. It can be removed once enough of the client eco system has
    implemented the stable endpoint.
    """

    PATTERNS = client_patterns(r"/org\.matrix\.msc4143/rtc/transports$", releases=())
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._transports = hs.config.matrix_rtc.transports

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        # Require authentication for this endpoint.
        await self._auth.get_user_by_req(request)

        if self._transports:
            return 200, {"rtc_transports": self._transports}

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    MatrixRTCTransportsRestServlet(hs).register(http_server)

    if hs.config.experimental.msc4143_enabled:
        UnstableMatrixRTCTransportsRestServlet(hs).register(http_server)
