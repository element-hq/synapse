#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Dirk Klimpel
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

from synapse.api.constants import Direction
from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_enum, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.storage.databases.main.stats import UserSortOrder
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class UserMediaStatisticsRestServlet(RestServlet):
    """
    Get statistics about uploaded media by users.
    """

    PATTERNS = admin_patterns("/statistics/users/media$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        order_by = parse_string(
            request,
            "order_by",
            default=UserSortOrder.USER_ID.value,
            allowed_values=(
                UserSortOrder.MEDIA_LENGTH.value,
                UserSortOrder.MEDIA_COUNT.value,
                UserSortOrder.USER_ID.value,
                UserSortOrder.DISPLAYNAME.value,
            ),
        )

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)
        from_ts = parse_integer(request, "from_ts", default=0)
        until_ts = parse_integer(request, "until_ts")

        if until_ts is not None:
            if until_ts <= from_ts:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "Query parameter until_ts must be greater than from_ts.",
                    errcode=Codes.INVALID_PARAM,
                )

        search_term = parse_string(request, "search_term")
        if search_term == "":
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Query parameter search_term cannot be an empty string.",
                errcode=Codes.INVALID_PARAM,
            )

        direction = parse_enum(request, "dir", Direction, default=Direction.FORWARDS)

        users_media, total = await self.store.get_users_media_usage_paginate(
            start, limit, from_ts, until_ts, order_by, direction, search_term
        )
        ret = {
            "users": [
                {
                    "user_id": r[0],
                    "displayname": r[1],
                    "media_count": r[2],
                    "media_length": r[3],
                }
                for r in users_media
            ],
            "total": total,
        }
        if (start + limit) < total:
            ret["next_token"] = start + len(users_media)

        return HTTPStatus.OK, ret


class LargestRoomsStatistics(RestServlet):
    """Get the largest rooms by database size.

    Only works when using PostgreSQL.
    """

    PATTERNS = admin_patterns("/statistics/database/rooms$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.stats_controller = hs.get_storage_controllers().stats

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        room_sizes = await self.stats_controller.get_room_db_size_estimate()

        return HTTPStatus.OK, {
            "rooms": [
                {"room_id": room_id, "estimated_size": size}
                for room_id, size in room_sizes
            ]
        }
