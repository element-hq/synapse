#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Tuple

from synapse._pydantic_compat import HAS_PYDANTIC_V2
from synapse.api.errors import AuthError, Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.types.rest import RequestBodyModel

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import StrictStr
else:
    from pydantic import StrictStr

logger = logging.getLogger(__name__)


class ReportEventRestServlet(RestServlet):
    PATTERNS = client_patterns("/rooms/(?P<room_id>[^/]*)/report/(?P<event_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main
        self._event_handler = self.hs.get_event_handler()

    async def on_POST(
        self, request: SynapseRequest, room_id: str, event_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        body = parse_json_object_from_request(request)

        if not isinstance(body.get("reason", ""), str):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Param 'reason' must be a string",
                Codes.BAD_JSON,
            )
        if type(body.get("score", 0)) is not int:  # noqa: E721
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Param 'score' must be an integer",
                Codes.BAD_JSON,
            )

        try:
            event = await self._event_handler.get_event(
                requester.user, room_id, event_id, show_redacted=False
            )
        except AuthError:
            # The event exists, but this user is not allowed to access this event.
            event = None

        if event is None:
            raise NotFoundError(
                "Unable to report event: "
                "it does not exist or you aren't able to see it."
            )

        await self.store.add_event_report(
            room_id=room_id,
            event_id=event_id,
            user_id=user_id,
            reason=body.get("reason"),
            content=body,
            received_ts=self.clock.time_msec(),
        )

        return 200, {}


class ReportRoomRestServlet(RestServlet):
    """This endpoint lets clients report a room for abuse.

    Whilst MSC4151 is not yet merged, this unstable endpoint is enabled on matrix.org
    for content moderation purposes, and therefore backwards compatibility should be
    carefully considered when changing anything on this endpoint.

    More details on the MSC: https://github.com/matrix-org/matrix-spec-proposals/pull/4151
    """

    PATTERNS = client_patterns(
        "/org.matrix.msc4151/rooms/(?P<room_id>[^/]*)/report$",
        releases=[],
        v1=False,
        unstable=True,
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main

    class PostBody(RequestBodyModel):
        reason: StrictStr

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        room = await self.store.get_room(room_id)
        if room is None:
            raise NotFoundError("Room does not exist")

        await self.store.add_room_report(
            room_id=room_id,
            user_id=user_id,
            reason=body.reason,
            received_ts=self.clock.time_msec(),
        )

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReportEventRestServlet(hs).register(http_server)

    if hs.config.experimental.msc4151_enabled:
        ReportRoomRestServlet(hs).register(http_server)
