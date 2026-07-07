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
from synapse.synapse_rust.types import Requester
from synapse.types import JsonDict, JsonValue, UserID, create_requester

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


class ReplicationProfileSetField(ReplicationEndpoint):
    """Update a profile field for a user.

    The POST looks like:

        POST /_synapse/replication/profile_set_field/<user_id>

        {
            "target_user": "@user:hs",
            "requester": "@admin:hs",
            "field_name": "displayname",
            "new_value": "Alice",
            "by_admin": true,
            "propagate": false
        }

        200 OK

        {}
    """

    NAME = "profile_set_field"
    PATH_ARGS = ("user_id",)
    METHOD = "POST"
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._profile_handler = hs.get_profile_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: UserID,
        requester: Requester,
        field_name: str,
        new_value: JsonValue | dict[str, JsonValue],
        by_admin: bool,
        propagate: bool,
    ) -> JsonDict:
        return {
            "target_user": user_id.to_string(),
            "requester": requester.user.to_string(),
            "field_name": field_name,
            "new_value": new_value,
            "by_admin": by_admin,
            "propagate": propagate,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        await self._profile_handler.set_field(
            target_user=UserID.from_string(user_id),
            requester=create_requester(content["requester"]),
            field_name=content["field_name"],
            new_value=content["new_value"],
            by_admin=content["by_admin"],
            propagate=content["propagate"],
        )

        return (200, {})


class ReplicationProfileDeleteField(ReplicationEndpoint):
    """Delete a profile field for a user.

    The POST looks like:

        POST /_synapse/replication/profile_delete_field/<user_id>

        {
            "target_user": "@user:hs",
            "requester": "@admin:hs",
            "field_name": "displayname",
            "by_admin": true
        }

        200 OK

        {}
    """

    NAME = "profile_delete_field"
    PATH_ARGS = ("user_id",)
    METHOD = "POST"
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._profile_handler = hs.get_profile_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: UserID,
        requester: Requester,
        field_name: str,
        by_admin: bool,
    ) -> JsonDict:
        return {
            "target_user": user_id.to_string(),
            "requester": requester.user.to_string(),
            "field_name": field_name,
            "by_admin": by_admin,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        await self._profile_handler.delete_profile_field(
            target_user=UserID.from_string(user_id),
            requester=create_requester(content["requester"]),
            field_name=content["field_name"],
            by_admin=content["by_admin"],
        )

        return (200, {})


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.server.include_profile_updates_in_sync:
        ReplicationProfileUserRoomMembershipChange(hs).register(http_server)
        ReplicationProfileSetField(hs).register(http_server)
        ReplicationProfileDeleteField(hs).register(http_server)
