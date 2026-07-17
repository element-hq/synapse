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

import logging
import re
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import Codes, SynapseError
from synapse.appservice import SCOPE_QUERY_ROOM_MEMBERSHIP
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_string
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, RoomID, UserID
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class AppserviceRoomMembershipRestServlet(RestServlet):
    PATTERNS = [
        re.compile(
            r"^/_matrix/client/unstable/io\.element\.msc4502/rooms/(?P<room_id>[^/]*)/is_joined$"
        )
    ]
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.is_mine_id = hs.is_mine_id
        self.is_mine_server_name = hs.is_mine_server_name

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=False)

        app_service = (
            self.store.get_app_service_by_id(requester.app_service_id)
            if requester.app_service_id
            else None
        )

        # Users and appservices can call this endpoint if they have the scope.
        has_scope = (
            app_service is not None
            and app_service.has_scope(SCOPE_QUERY_ROOM_MEMBERSHIP)
        ) or SCOPE_QUERY_ROOM_MEMBERSHIP in requester.scope

        if not has_scope:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                f"Missing {SCOPE_QUERY_ROOM_MEMBERSHIP} scope",
                Codes.FORBIDDEN,
            )

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid room ID", Codes.INVALID_PARAM
            )

        mxid = parse_string(request, "mxid")
        server_name = parse_string(request, "server_name")

        if (mxid is None) == (server_name is None):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Exactly one of 'mxid' or 'server_name' query parameters must be given",
                Codes.MISSING_PARAM,
            )

        if mxid is not None:
            if not UserID.is_valid(mxid):
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid MXID: %s" % (mxid,),
                    Codes.INVALID_PARAM,
                )
            if self.is_mine_id(mxid):
                (
                    membership,
                    _,
                ) = await self.store.get_local_current_membership_for_user_in_room(
                    mxid, room_id
                )
                joined = membership == Membership.JOIN
            else:
                event = await self.storage_controllers.state.get_current_state_event(
                    room_id, EventTypes.Member, mxid
                )
                joined = (
                    event is not None
                    and event.content.get("membership") == Membership.JOIN
                )
        else:
            assert server_name is not None
            try:
                parse_and_validate_server_name(server_name)
            except ValueError:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid server name: %s" % (server_name,),
                    Codes.INVALID_PARAM,
                )
            if self.is_mine_server_name(server_name):
                joined = await self.store.is_locally_joined(room_id)
            else:
                joined = await self.store.is_host_joined(room_id, server_name)

        return HTTPStatus.OK, {"joined": joined}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc4502_enabled:
        AppserviceRoomMembershipRestServlet(hs).register(http_server)
