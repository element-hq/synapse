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

from typing import TYPE_CHECKING, Annotated

from pydantic import StrictStr, StringConstraints

from synapse.handlers.matrixrtc_livekit import no_local_sfu_error
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict
from synapse.types.rest import RequestBodyModel

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
        await self._auth.get_user_by_req(request, allow_guest=True)

        if self._transports:
            return 200, {"rtc_transports": self._transports}

        return 200, {}


NonEmptyStr = Annotated[StrictStr, StringConstraints(min_length=1)]


class MatrixRTCLiveKitGetTokenRestServlet(RestServlet):
    """Default implementation of `POST /_matrix/client/v1/rtc/livekit/get_token`
    from MSC4195. Used when no application service proxies the LiveKit endpoints.
    The unstable path is retained for backwards compatibility with clients that
    don't yet support the stable endpoint.
    """

    PATTERNS = client_patterns(
        r"/rtc/livekit/get_token$",
        releases=("v1", r"unstable/io\.element\.msc4195"),
        unstable=False,
    )
    CATEGORY = "Client API requests"

    class PostBody(RequestBodyModel):
        server_name: StrictStr | None = None
        url: NonEmptyStr
        room_id: NonEmptyStr
        slot_id: NonEmptyStr
        member_id: NonEmptyStr

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._ratelimiter = hs.get_request_ratelimiter()
        self._handler = hs.get_matrixrtc_livekit_handler()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request, allow_guest=True)
        await self._ratelimiter.ratelimit(requester)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        jwt = await self._handler.get_token(
            requester,
            server_name=body.server_name,
            url=body.url,
            room_id=body.room_id,
            slot_id=body.slot_id,
            member_id=body.member_id,
        )
        return 200, {"jwt": jwt}


class MatrixRTCLiveKitDelegateDelayedLeaveRestServlet(RestServlet):
    """Default implementation of
    `POST /_matrix/client/v1/rtc/livekit/delegate_delayed_leave` from MSC4195. Used
    when no application service proxies the LiveKit endpoints. Delayed leave events
    can only be delegated for a local SFU. By default, a server has no SFU of its own,
    so every request is rejected.
    """

    PATTERNS = client_patterns(
        r"/rtc/livekit/delegate_delayed_leave$",
        releases=("v1", r"unstable/io\.element\.msc4195"),
        unstable=False,
    )
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._ratelimiter = hs.get_request_ratelimiter()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request, allow_guest=True)
        await self._ratelimiter.ratelimit(requester)

        raise no_local_sfu_error()


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    MatrixRTCTransportsRestServlet(hs).register(http_server)

    if hs.config.experimental.msc4143_enabled:
        UnstableMatrixRTCTransportsRestServlet(hs).register(http_server)

    # An application service that proxies the LiveKit endpoints takes precedence
    # over the default implementation.
    if not hs.config.matrix_rtc.livekit_proxied_to_appservice:
        MatrixRTCLiveKitGetTokenRestServlet(hs).register(http_server)
        MatrixRTCLiveKitDelegateDelayedLeaveRestServlet(hs).register(http_server)
