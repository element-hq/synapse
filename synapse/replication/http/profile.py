#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations, Ltd
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
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.api.constants import Membership
from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationProfileUserRoomMembershipChange(ReplicationEndpoint):
    """Store user profile update action regarding membership changes.

    The POST looks like:

        POST /_synapse/replication/profile_user_room_membership_change/<user_id>

        {
            "room_id": "!1234:domain.tld",
            "membership": "join | leave"
        }

        200 OK

        {}
    """

    NAME = "profile_user_room_membership_change"
    PATH_ARGS = ("user_id",)
    METHOD = "POST"
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._profile_handler = hs.get_profile_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        room_id: str,
        membership: str,
    ) -> JsonDict:
        assert membership in (Membership.JOIN, Membership.LEAVE)
        return {
            "room_id": room_id,
            "membership": membership,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        assert content["membership"] in (Membership.JOIN, Membership.LEAVE)
        if content["membership"] == Membership.JOIN:
            await self._profile_handler.user_joined_room(
                user_id=UserID.from_string(user_id),
                room_id=content["room_id"],
            )
        else:
            await self._profile_handler.user_left_room(
                user_id=UserID.from_string(user_id),
                room_id=content["room_id"],
            )

        return (200, {})


class ReplicationProfileRecordFieldUpdates(ReplicationEndpoint):
    """Record user profile field updates for the profile updates stream.

    The POST looks like:

        POST /_synapse/replication/profile_record_field_updates/<user_id>

        {
            "updated_fields": ["list", "of", "fields"]
        }

        200 OK

        {}
    """

    NAME = "profile_record_field_updates"
    PATH_ARGS = ("user_id",)
    METHOD = "POST"
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._profile_handler = hs.get_profile_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        updated_fields: set[str],
    ) -> JsonDict:
        assert len(updated_fields) > 0
        return {
            "updated_fields": list(updated_fields),
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        assert len(content["updated_fields"]) > 0
        await self._profile_handler.record_profile_updates(
            user_id=UserID.from_string(user_id),
            updated_fields=set(content["updated_fields"]),
        )

        return (200, {})


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.server.include_profile_updates_in_sync:
        ReplicationProfileUserRoomMembershipChange(hs).register(http_server)
        ReplicationProfileRecordFieldUpdates(hs).register(http_server)
