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
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.constants import Direction
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.servlet import RestServlet, parse_enum, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RoomReportsRestServlet(RestServlet):
    """
    List all existing rooms that have been reported to the homeserver. Results are returned
    in a dictionary containing report information. Supports pagination. Does not return results
    for deleted/purged rooms.
    The requester must have administrator access in Synapse.

    GET /_synapse/admin/v1/room_reports
    returns:
        200 OK with list of reports if success otherwise an error.

    Args:
        The parameters `from` and `limit` are required only for pagination.
        By default, a `limit` of 100 is used.
        The parameter `dir` can be used to define the order of results.
        The `room_id` query parameter filters by room id.
        The `user_id` query parameter filters by the user ID of the reporter of the room.
    Returns:
        A list of reported rooms and an integer representing the total number of
        reported rooms that exist given this query
    """

    PATTERNS = admin_patterns("/room_reports$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)
        direction = parse_enum(request, "dir", Direction, Direction.BACKWARDS)
        room_id = parse_string(request, "room_id")
        user_id = parse_string(request, "user_id")

        if start < 0:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "The start parameter must be a positive integer.",
                errcode=Codes.INVALID_PARAM,
            )

        if limit < 0:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "The limit parameter must be a positive integer.",
                errcode=Codes.INVALID_PARAM,
            )

        room_reports, total = await self._store.get_room_reports_paginate(
            start, limit, direction, user_id, room_id
        )
        ret = {"room_reports": room_reports, "total": total}
        if (start + limit) < total:
            ret["next_token"] = start + len(room_reports)

        return HTTPStatus.OK, ret


class RoomReportDetailRestServlet(RestServlet):
    """
    Get a specific reported room that is known to the homeserver. Results are returned
    in a dictionary containing report information.
    The requester must have administrator access in Synapse.

    GET /_synapse/admin/v1/room_reports/<report_id>
    returns:
        200 OK with details report if success otherwise an error.

    Args:
        The parameter `report_id` is the ID of the room report in the database.
    Returns:
        JSON blob of information about the room report
    """

    PATTERNS = admin_patterns("/room_reports/(?P<report_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(
        self, request: SynapseRequest, report_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        message = (
            "The report_id parameter must be a string representing a positive integer."
        )
        try:
            resolved_report_id = int(report_id)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, message, errcode=Codes.INVALID_PARAM
            )

        ret = await self._store.get_room_report(resolved_report_id)
        if not ret:
            raise NotFoundError("Room report not found")

        return HTTPStatus.OK, ret

    async def on_DELETE(
        self, request: SynapseRequest, report_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        message = (
            "The report_id parameter must be a string representing a positive integer."
        )
        try:
            resolved_report_id = int(report_id)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, message, errcode=Codes.INVALID_PARAM
            )

        if resolved_report_id < 0:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, message, errcode=Codes.INVALID_PARAM
            )

        if await self._store.delete_room_report(resolved_report_id):
            return HTTPStatus.OK, {}

        raise NotFoundError("Room report not found")
