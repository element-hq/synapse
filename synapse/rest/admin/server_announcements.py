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

from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import NotFoundError
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ServerAnnouncementsRestServlet(RestServlet):
    """List the configured server announcement rooms.

    GET /_synapse/admin/v1/server_announcements

    returns:

    {
        "management_user_id": "@_server_announcements:example.com",
        "rooms": [{"id": "general", "room_id": "!abc:example.com", ...}]
    }
    """

    PATTERNS = admin_patterns("/server_announcements$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._config = hs.config.serverannouncements
        self._handler = hs.get_server_announcements_handler()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        room_ids = await self._handler.get_room_ids_by_config_id()
        return HTTPStatus.OK, {
            "management_user_id": self._config.room_management_mxid,
            "rooms": [
                self._handler.get_room_info(room, room_ids.get(room.config_id))
                for room in self._config.rooms
            ],
        }


class ServerAnnouncementRestServlet(RestServlet):
    """Get a configured server announcement room by its config ID.

    GET /_synapse/admin/v1/server_announcements/<config_id>
    """

    PATTERNS = admin_patterns("/server_announcements/(?P<config_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_server_announcements_handler()

    async def on_GET(
        self, request: SynapseRequest, config_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        room = self._handler.get_configured_room(config_id)
        if room is None:
            raise NotFoundError(
                f"No server announcement room with id {config_id!r} in "
                "experimental_features.server_announcements.rooms"
            )

        room_ids = await self._handler.get_room_ids_by_config_id()
        return HTTPStatus.OK, self._handler.get_room_info(room, room_ids.get(config_id))
