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

from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.api.errors import SynapseError
from synapse.config import ConfigError
from synapse.handlers.server_announcement_avatar import ServerAnnouncementAvatarManager
from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ReplicationServerAnnouncementAvatarRestServlet(ReplicationEndpoint):
    """Upload an announcement avatar on the designated media instance.

    POST /_synapse/replication/server_announcement_avatar/<announcement_id>/<txn_id>

    The caller supplies only the logical announcement ID. The target resolves the
    trusted file path from its own administrator configuration, never from the
    request. Authentication uses the standard replication shared secret.
    """

    NAME = "server_announcement_avatar"
    PATH_ARGS = ("announcement_id",)

    # Keep the standard bounded connection retries, but do not retry timeouts
    # indefinitely: the main-process reconciler owns the overall retry budget.
    RETRY_ON_TIMEOUT = False

    def __init__(self, hs: "HomeServer") -> None:
        super().__init__(hs)
        self._hs = hs

    @staticmethod
    async def _serialize_payload(announcement_id: str) -> JsonDict:  # type: ignore[override]
        return {}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, announcement_id: str
    ) -> tuple[int, JsonDict]:
        config = self._hs.config.serverannouncements
        if self._hs.get_instance_name() != config.media_instance_name:
            raise SynapseError(
                400, "This instance is not the configured announcement avatar uploader"
            )
        if not self._hs.config.media.can_load_media_repo:
            raise SynapseError(
                400,
                "The announcement avatar uploader must have the media repository enabled",
            )

        room = next((room for room in config.rooms if room.id == announcement_id), None)
        if room is None:
            raise SynapseError(
                404,
                f"Server announcement room {announcement_id!r} is not configured on this instance",
            )
        if room.room_avatar_path is None:
            raise SynapseError(
                400,
                f"Server announcement room {room.id!r} has no room_avatar_path configured",
            )
        if (
            await self._hs.get_datastores().main.get_server_announcement_room(room.id)
            is None
        ):
            raise SynapseError(
                400, f"Server announcement room {room.id!r} has no room mapping"
            )

        try:
            avatar_url = await ServerAnnouncementAvatarManager(self._hs).get_avatar_url(
                room
            )
        except ConfigError as e:
            # Preserve actionable file/validation/quarantine errors for the main
            # process instead of returning the generic internal-server-error text.
            raise SynapseError(500, str(e)) from e
        return 200, {"avatar_url": avatar_url}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationServerAnnouncementAvatarRestServlet(hs).register(http_server)
