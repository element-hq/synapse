#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from synapse.api.constants import Direction
from synapse.api.errors import NotFoundError, SynapseError
from synapse.federation.transport.server import Authenticator
from synapse.http.servlet import RestServlet, parse_enum, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.storage.databases.main.transactions import DestinationSortOrder
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ListDestinationsRestServlet(RestServlet):
    """Get request to list all destinations.
    This needs user to have administrator access in Synapse.

    GET /_synapse/admin/v1/federation/destinations?from=0&limit=10

    returns:
        200 OK with list of destinations if success otherwise an error.

    The parameters `from` and `limit` are required only for pagination.
    By default, a `limit` of 100 is used.
    The parameter `destination` can be used to filter by destination.
    The parameter `order_by` can be used to order the result.
    """

    PATTERNS = admin_patterns("/federation/destinations$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)

        destination = parse_string(request, "destination")

        order_by = parse_string(
            request,
            "order_by",
            default=DestinationSortOrder.DESTINATION.value,
            allowed_values=[dest.value for dest in DestinationSortOrder],
        )

        direction = parse_enum(request, "dir", Direction, default=Direction.FORWARDS)

        destinations, total = await self._store.get_destinations_paginate(
            start, limit, destination, order_by, direction
        )
        response = {
            "destinations": [
                {
                    "destination": r[0],
                    "retry_last_ts": r[1] or 0,
                    "retry_interval": r[2] or 0,
                    "failure_ts": r[3],
                    "last_successful_stream_ordering": r[4],
                }
                for r in destinations
            ],
            "total": total,
        }
        if (start + limit) < total:
            response["next_token"] = str(start + len(destinations))

        return HTTPStatus.OK, response


class DestinationRestServlet(RestServlet):
    """Get details of a destination.
    This needs user to have administrator access in Synapse.

    GET /_synapse/admin/v1/federation/destinations/<destination>

    returns:
        200 OK with details of a destination if success otherwise an error.
    """

    PATTERNS = admin_patterns("/federation/destinations/(?P<destination>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(
        self, request: SynapseRequest, destination: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if not await self._store.is_destination_known(destination):
            raise NotFoundError("Unknown destination")

        destination_retry_timings = await self._store.get_destination_retry_timings(
            destination
        )

        last_successful_stream_ordering = (
            await self._store.get_destination_last_successful_stream_ordering(
                destination
            )
        )

        response: JsonDict = {
            "destination": destination,
            "last_successful_stream_ordering": last_successful_stream_ordering,
        }

        if destination_retry_timings:
            response = {
                **response,
                "failure_ts": destination_retry_timings.failure_ts,
                "retry_last_ts": destination_retry_timings.retry_last_ts,
                "retry_interval": destination_retry_timings.retry_interval,
            }
        else:
            response = {
                **response,
                "failure_ts": None,
                "retry_last_ts": 0,
                "retry_interval": 0,
            }

        return HTTPStatus.OK, response


class DestinationMembershipRestServlet(RestServlet):
    """Get list of rooms of a destination.
    This needs user to have administrator access in Synapse.

    GET /_synapse/admin/v1/federation/destinations/<destination>/rooms?from=0&limit=10

    returns:
        200 OK with a list of rooms if success otherwise an error.

    The parameters `from` and `limit` are required only for pagination.
    By default, a `limit` of 100 is used.
    """

    PATTERNS = admin_patterns("/federation/destinations/(?P<destination>[^/]*)/rooms$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main

    async def on_GET(
        self, request: SynapseRequest, destination: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if not await self._store.is_destination_known(destination):
            raise NotFoundError("Unknown destination")

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)

        direction = parse_enum(request, "dir", Direction, default=Direction.FORWARDS)

        rooms, total = await self._store.get_destination_rooms_paginate(
            destination, start, limit, direction
        )
        response = {
            "rooms": [
                {"room_id": room_id, "stream_ordering": stream_ordering}
                for room_id, stream_ordering in rooms
            ],
            "total": total,
        }
        if (start + limit) < total:
            response["next_token"] = str(start + len(rooms))

        return HTTPStatus.OK, response


class DestinationResetConnectionRestServlet(RestServlet):
    """Reset destinations' connection timeouts and wake it up.
    This needs user to have administrator access in Synapse.

    POST /_synapse/admin/v1/federation/destinations/<destination>/reset_connection
    {}

    returns:
        200 OK otherwise an error.
    """

    PATTERNS = admin_patterns(
        "/federation/destinations/(?P<destination>[^/]+)/reset_connection$"
    )

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._authenticator = Authenticator(hs)

    async def on_POST(
        self, request: SynapseRequest, destination: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        if not await self._store.is_destination_known(destination):
            raise NotFoundError("Unknown destination")

        retry_timings = await self._store.get_destination_retry_timings(destination)
        if not (retry_timings and retry_timings.retry_last_ts):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "The retry timing does not need to be reset for this destination.",
            )

        # reset timings and wake up
        await self._authenticator.reset_retry_timings(destination)

        return HTTPStatus.OK, {}
