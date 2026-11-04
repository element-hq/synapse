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

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import set_tag
from synapse.rest.admin._base import (
    admin_patterns,
    assert_requester_is_admin,
    assert_user_is_admin,
)
from synapse.rest.client.transactions import HttpTransactionCache
from synapse.types import JsonDict, Requester

if TYPE_CHECKING:
    from synapse.server import HomeServer


def _serialize_room(room: ServerAnnouncementRoom, room_id: str | None) -> JsonDict:
    """Describe a configured announcement room, as returned by the admin API.

    Args:
        room: The room's configuration entry.
        room_id: The room's Matrix room ID, or None if it has not been created.
    """
    return {
        "id": room.config_id,
        "room_id": room_id,
        "room_name": room.room_name,
        "room_topic": room.room_topic,
        "room_avatar_url": room.room_avatar_url,
        "allowed_user_ids": list(room.allowed_user_ids),
        "excluded_user_ids": list(room.excluded_user_ids),
    }


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
        self._store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        return HTTPStatus.OK, {
            "management_user_id": self._config.room_management_mxid,
            "rooms": [
                _serialize_room(
                    room,
                    await self._store.get_server_announcement_room(room.config_id),
                )
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
        self._store = hs.get_datastores().main

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

        room_id = await self._store.get_server_announcement_room(config_id)
        return HTTPStatus.OK, _serialize_room(room, room_id)


class SendServerAnnouncementRestServlet(RestServlet):
    """Send an event as the management user into a server announcement room.

    POST /_synapse/admin/v1/server_announcements/<config_id>/send
    PUT /_synapse/admin/v1/server_announcements/<config_id>/send/<txn_id>
    {
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": "This is my message"
        }
    }

    returns:

    {
        "event_id": "$1895723857jgskldgujpious",
        "room_id": "!abc:example.com"
    }
    """

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._config = hs.config.serverannouncements
        self._handler = hs.get_server_announcements_handler()
        self.txns = HttpTransactionCache(hs)

    def register(self, json_resource: HttpServer) -> None:
        PATTERN = "/server_announcements/(?P<config_id>[^/]*)/send"
        json_resource.register_paths(
            "POST", admin_patterns(PATTERN + "$"), self.on_POST, self.__class__.__name__
        )
        json_resource.register_paths(
            "PUT",
            admin_patterns(PATTERN + "/(?P<txn_id>[^/]*)$"),
            self.on_PUT,
            self.__class__.__name__,
        )

    async def _do(
        self,
        request: SynapseRequest,
        requester: Requester,
        config_id: str,
        txn_id: str | None,
    ) -> tuple[int, JsonDict]:
        await assert_user_is_admin(self._auth, requester)
        body = parse_json_object_from_request(request)
        assert_params_in_dict(body, ("content",))
        event_type = body.get("type", EventTypes.Message)

        # Synapse manages the state of announcement rooms itself.
        if "state_key" in body:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Cannot send state events to announcement rooms",
                Codes.INVALID_PARAM,
            )

        if not self._config.rooms:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Server announcements are not enabled on this server",
            )

        room_id, event_id = await self._handler.send_announcement(
            requester, config_id, event_type, body["content"], txn_id
        )
        return HTTPStatus.OK, {"event_id": event_id, "room_id": room_id}

    async def on_POST(
        self, request: SynapseRequest, config_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        return await self._do(request, requester, config_id, None)

    async def on_PUT(
        self, request: SynapseRequest, config_id: str, txn_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        set_tag("txn_id", txn_id)
        return await self.txns.fetch_or_execute_request(
            request, requester, self._do, request, requester, config_id, txn_id
        )
