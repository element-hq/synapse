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

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, UserID, create_requester

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationProfileSetFieldValue(ReplicationEndpoint):
    """Set profile field for a user.

    The POST looks like:

        POST /_synapse/replication/profile_set_field_value/<user_id>

        {
            "requester_id": "@user:domain.tld",
            "field_name": "displayname",
            "new_value": "User Display Name",
            "by_admin": False,
            "propagate": False,
        }

        200 OK

        {}
    """

    NAME = "profile_set_field_value"
    PATH_ARGS = ("user_id",)
    METHOD = "POST"
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._profile_handler = hs.get_profile_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        requester_id: str,
        field_name: str,
        new_value: str | None,
        by_admin: bool = False,
        propagate: bool = False,
        authenticated_entity: str | None = None,
    ) -> JsonDict:
        return {
            "requester_id": requester_id,
            "field_name": field_name,
            "new_value": new_value,
            "by_admin": by_admin,
            "propagate": propagate,
            "authenticated_entity": authenticated_entity,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        # Create a requester object with potentially an authenticated_entity,
        # ie an admin who has done the request on behalf of the user.
        requester = create_requester(
            user_id=user_id,
            authenticated_entity=content["authenticated_entity"] if content["by_admin"] else None,
        )
        await self._profile_handler.set_field(
            target_user=UserID.from_string(user_id),
            requester=requester,
            field_name=content["field_name"],
            new_value=content["new_value"],
            by_admin=content["by_admin"],
            propagate=content["propagate"],
        )

        return (200, {})


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationProfileSetFieldValue(hs).register(http_server)
