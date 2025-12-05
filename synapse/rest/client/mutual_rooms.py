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
from bisect import bisect
from http import HTTPStatus
from typing import TYPE_CHECKING

from unpaddedbase64 import decode_base64, encode_base64

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_strings_from_args
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

MUTUAL_ROOMS_BATCH_LIMIT = 100


def _parse_mutual_rooms_batch_token_args(args: dict[bytes, list[bytes]]) -> str | None:
    from_batches = parse_strings_from_args(args, "from")
    if not from_batches:
        return None
    if len(from_batches) > 1:
        raise SynapseError(
            HTTPStatus.BAD_REQUEST,
            "Duplicate from query parameter",
            errcode=Codes.INVALID_PARAM,
        )
    if from_batches[0]:
        try:
            return decode_base64(from_batches[0]).decode("utf-8")
        except Exception:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Malformed from token",
                errcode=Codes.INVALID_PARAM,
            )
    return None


class UserMutualRoomsServlet(RestServlet):
    """
    GET /uk.half-shot.msc2666/user/mutual_rooms?user_id={user_id}&from={token} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/uk.half-shot.msc2666/user/mutual_rooms$",
        releases=(),  # This is an unstable feature
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        # twisted.web.server.Request.args is incorrectly defined as Any | None
        args: dict[bytes, list[bytes]] = request.args  # type: ignore

        user_ids = parse_strings_from_args(args, "user_id", required=True)
        from_batch = _parse_mutual_rooms_batch_token_args(args)

        if len(user_ids) > 1:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Duplicate user_id query parameter",
                errcode=Codes.INVALID_PARAM,
            )

        user_id = user_ids[0]

        requester = await self.auth.get_user_by_req(request)
        if user_id == requester.user.to_string():
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "You cannot request a list of shared rooms with yourself",
                errcode=Codes.UNKNOWN,
            )

        # Sort here instead of the database function, so that we don't expose
        # clients to any unrelated changes to the sorting algorithm.
        rooms = sorted(
            await self.store.get_mutual_rooms_between_users(
                frozenset((requester.user.to_string(), user_id))
            )
        )

        if from_batch:
            # A from_batch token was provided, so cut off any rooms where the ID is
            # lower than or equal to the token. This method doesn't care whether the
            # provided token room still exists, nor whether it's even a real room ID.
            #
            # However, if rooms with a lower ID are added after the token was issued,
            # they will not be included until the client makes a new request without a
            # from token. This is considered acceptable, as clients generally won't
            # persist these results for long periods.
            rooms = rooms[bisect(rooms, from_batch) :]

        if len(rooms) <= MUTUAL_ROOMS_BATCH_LIMIT:
            # We've reached the end of the list, don't return a batch token
            return 200, {"joined": rooms}

        rooms = rooms[:MUTUAL_ROOMS_BATCH_LIMIT]
        # We use urlsafe unpadded base64 encoding for the batch token in order to
        # handle funny room IDs in old pre-v12 rooms properly. We also truncate it
        # to stay within the 255-character limit of opaque tokens.
        next_batch = encode_base64(rooms[-1].encode("utf-8"), urlsafe=True)[:255]
        # Due to the truncation, it is technically possible to have conflicting next
        # batches by creating hundreds of rooms with the same 191 character prefix
        # in the room ID. In the event that some silly user does that, don't let
        # them paginate further.
        if next_batch == from_batch:
            return 200, {"joined": rooms}

        return 200, {"joined": list(rooms), "next_batch": next_batch}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc2666_enabled:
        UserMutualRoomsServlet(hs).register(http_server)
