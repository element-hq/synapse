#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

MAX_TAG_LENGTH = 255


class TagListServlet(RestServlet):
    """
    GET /user/{user_id}/rooms/{room_id}/tags HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/rooms/(?P<room_id>[^/]*)/tags$"
    )
    CATEGORY = "Account data requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot get tags for other users.")

        tags = await self.store.get_tags_for_room(user_id, room_id)

        return 200, {"tags": tags}


class TagServlet(RestServlet):
    """
    PUT /user/{user_id}/rooms/{room_id}/tags/{tag} HTTP/1.1
    DELETE /user/{user_id}/rooms/{room_id}/tags/{tag} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/rooms/(?P<room_id>[^/]*)/tags/(?P<tag>[^/]*)"
    )
    CATEGORY = "Account data requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_account_data_handler()
        self.room_member_handler = hs.get_room_member_handler()

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, room_id: str, tag: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add tags for other users.")

        # check if the tag exceeds the length allowed by the matrix-specification
        # as defined in: https://spec.matrix.org/v1.15/client-server-api/#events-14
        if len(tag.encode("utf-8")) > MAX_TAG_LENGTH:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "tag parameter's length is over 255 bytes",
                errcode=Codes.INVALID_PARAM,
            )

        # Check if the user has any membership in the room and raise error if not.
        # Although it's not harmful for users to tag random rooms, it's just superfluous
        # data we don't need to track or allow.
        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        body = parse_json_object_from_request(request)

        await self.handler.add_tag_to_room(user_id, room_id, tag, body)

        return 200, {}

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, room_id: str, tag: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add tags for other users.")

        await self.handler.remove_tag_from_room(user_id, room_id, tag)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    TagListServlet(hs).register(http_server)
    TagServlet(hs).register(http_server)
