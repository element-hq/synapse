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
from typing import TYPE_CHECKING

from pydantic import StrictStr

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
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        body = parse_json_object_from_request(request)

        if not isinstance(body.get("reason", ""), str):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Param 'reason' must be a string",
                Codes.BAD_JSON,
            )
        if (
            not self.hs.config.experimental.msc4277_enabled
            and type(body.get("score", 0)) is not int
        ):  # noqa: E721
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
            if self.hs.config.experimental.msc4277_enabled:
                # Respond with 200 and no content regardless of whether the event
                # exists to prevent enumeration attacks.
                return 200, {}
            else:
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

    Introduced by MSC4151: https://github.com/matrix-org/matrix-spec-proposals/pull/4151
    """

    # Cast the Iterable to a list so that we can `append` below.
    PATTERNS = list(
        client_patterns(
            "/rooms/(?P<room_id>[^/]*)/report$",
            releases=("v3",),
            unstable=False,
            v1=False,
        )
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
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        room = await self.store.get_room(room_id)
        if room is None:
            if self.hs.config.experimental.msc4277_enabled:
                # Respond with 200 and no content regardless of whether the room
                # exists to prevent enumeration attacks.
                return 200, {}
            else:
                raise NotFoundError("Room does not exist")

        await self.store.add_room_report(
            room_id=room_id,
            user_id=user_id,
            reason=body.reason,
            received_ts=self.clock.time_msec(),
        )

        return 200, {}


class ReportUserRestServlet(RestServlet):
    """This endpoint lets clients report a user for abuse.

    Introduced by MSC4260: https://github.com/matrix-org/matrix-spec-proposals/pull/4260
    """

    PATTERNS = list(
        client_patterns(
            "/users/(?P<target_user_id>[^/]*)/report$",
            releases=("v3",),
            unstable=False,
            v1=False,
        )
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main
        self.handler = hs.get_reports_handler()

    class PostBody(RequestBodyModel):
        reason: StrictStr

    async def on_POST(
        self, request: SynapseRequest, target_user_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        body = parse_and_validate_json_object_from_request(request, self.PostBody)

        await self.handler.report_user(requester, target_user_id, body.reason)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReportEventRestServlet(hs).register(http_server)
    ReportRoomRestServlet(hs).register(http_server)
    ReportUserRestServlet(hs).register(http_server)
