#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import Codes, ShadowBanError, SynapseError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.handlers.worker_lock import NEW_EVENT_DURING_PURGE_LOCK_NAME
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.util import stringutils

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RoomUpgradeRestServlet(RestServlet):
    """Handler for room upgrade requests.

    Handles requests of the form:

        POST /_matrix/client/r0/rooms/$roomid/upgrade HTTP/1.1
        Content-Type: application/json

        {
            "new_version": "2",
        }

    Creates a new room and shuts down the old one. Returns the ID of the new room.
    """

    PATTERNS = client_patterns(
        # /rooms/$roomid/upgrade
        "/rooms/(?P<room_id>[^/]*)/upgrade$"
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self._room_creation_handler = hs.get_room_creation_handler()
        self._auth = hs.get_auth()
        self._worker_lock_handler = hs.get_worker_locks_handler()

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)

        content = parse_json_object_from_request(request)
        assert_params_in_dict(content, ("new_version",))

        new_version = KNOWN_ROOM_VERSIONS.get(content["new_version"])
        if new_version is None:
            raise SynapseError(
                400,
                "Your homeserver does not support this room version",
                Codes.UNSUPPORTED_ROOM_VERSION,
            )

        try:
            async with self._worker_lock_handler.acquire_read_write_lock(
                NEW_EVENT_DURING_PURGE_LOCK_NAME, room_id, write=False
            ):
                new_room_id = await self._room_creation_handler.upgrade_room(
                    requester, room_id, new_version
                )
        except ShadowBanError:
            # Generate a random room ID.
            new_room_id = stringutils.random_string(18)

        ret = {"replacement_room": new_room_id}

        return 200, ret


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RoomUpgradeRestServlet(hs).register(http_server)
