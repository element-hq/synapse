#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Half-Shot
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
from typing import TYPE_CHECKING, Dict, List, Tuple

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_strings_from_args
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class UserMutualRoomsServlet(RestServlet):
    """
    GET /uk.half-shot.msc2666/user/mutual_rooms?user_id={user_id} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/uk.half-shot.msc2666/user/mutual_rooms$",
        releases=(),  # This is an unstable feature
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        # twisted.web.server.Request.args is incorrectly defined as Optional[Any]
        args: Dict[bytes, List[bytes]] = request.args  # type: ignore

        user_ids = parse_strings_from_args(args, "user_id", required=True)

        if len(user_ids) > 1:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Duplicate user_id query parameter",
                errcode=Codes.INVALID_PARAM,
            )

        # We don't do batching, so a batch token is illegal by default
        if b"batch_token" in args:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Unknown batch_token",
                errcode=Codes.INVALID_PARAM,
            )

        user_id = user_ids[0]

        requester = await self.auth.get_user_by_req(request)
        if user_id == requester.user.to_string():
            raise SynapseError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "You cannot request a list of shared rooms with yourself",
                errcode=Codes.INVALID_PARAM,
            )

        rooms = await self.store.get_mutual_rooms_between_users(
            frozenset((requester.user.to_string(), user_id))
        )

        return 200, {"joined": list(rooms)}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    UserMutualRoomsServlet(hs).register(http_server)
