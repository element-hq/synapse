#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, MSC3244_CAPABILITIES
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class CapabilitiesRestServlet(RestServlet):
    """End point to expose the capabilities of the server."""

    PATTERNS = client_patterns("/capabilities$")
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.config = hs.config
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await self.auth.get_user_by_req(request, allow_guest=True)
        change_password = self.auth_handler.can_change_password()

        response: JsonDict = {
            "capabilities": {
                "m.room_versions": {
                    "default": self.config.server.default_room_version.identifier,
                    "available": {
                        v.identifier: v.disposition
                        for v in KNOWN_ROOM_VERSIONS.values()
                    },
                },
                "m.change_password": {"enabled": change_password},
                "m.set_displayname": {
                    "enabled": self.config.registration.enable_set_displayname
                },
                "m.set_avatar_url": {
                    "enabled": self.config.registration.enable_set_avatar_url
                },
                "m.3pid_changes": {
                    "enabled": self.config.registration.enable_3pid_changes
                },
                "m.get_login_token": {
                    "enabled": self.config.auth.login_via_existing_enabled,
                },
            }
        }

        if self.config.experimental.msc3244_enabled:
            response["capabilities"]["m.room_versions"][
                "org.matrix.msc3244.room_capabilities"
            ] = MSC3244_CAPABILITIES

        if self.config.experimental.msc3720_enabled:
            response["capabilities"]["org.matrix.msc3720.account_status"] = {
                "enabled": True,
            }

        if self.config.experimental.msc3664_enabled:
            response["capabilities"]["im.nheko.msc3664.related_event_match"] = {
                "enabled": self.config.experimental.msc3664_enabled,
            }

        disallowed_profile_fields = []
        response["capabilities"]["m.profile_fields"] = {"enabled": True}
        if not self.config.registration.enable_set_displayname:
            disallowed_profile_fields.append("displayname")
        if not self.config.registration.enable_set_avatar_url:
            disallowed_profile_fields.append("avatar_url")
        if disallowed_profile_fields:
            response["capabilities"]["m.profile_fields"]["disallowed"] = (
                disallowed_profile_fields
            )

        # For transition from unstable to stable identifiers.
        if self.config.experimental.msc4133_enabled:
            response["capabilities"]["uk.tcpip.msc4133.profile_fields"] = response[
                "capabilities"
            ]["m.profile_fields"]

        if self.config.experimental.msc4267_enabled:
            response["capabilities"]["org.matrix.msc4267.forget_forced_upon_leave"] = {
                "enabled": self.config.room.forget_on_leave,
            }

        return HTTPStatus.OK, response


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    CapabilitiesRestServlet(hs).register(http_server)
