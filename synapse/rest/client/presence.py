#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

""" This module contains REST servlets to do with presence: /presence/<paths>
"""
import logging
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import AuthError, SynapseError
from synapse.handlers.presence import format_user_presence_state
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PresenceStatusRestServlet(RestServlet):
    PATTERNS = client_patterns("/presence/(?P<user_id>[^/]*)/status", v1=True)
    CATEGORY = "Presence requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.presence_handler = hs.get_presence_handler()
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)

        if not self.hs.config.server.presence_enabled:
            return 200, {"presence": "offline"}

        if requester.user != user:
            allowed = await self.presence_handler.is_visible(
                observed_user=user, observer_user=requester.user
            )

            if not allowed:
                raise AuthError(403, "You are not allowed to see their presence.")

        state = await self.presence_handler.get_state(target_user=user)
        result = format_user_presence_state(
            state, self.clock.time_msec(), include_user_id=False
        )

        return 200, result

    async def on_PUT(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user = UserID.from_string(user_id)

        if requester.user != user:
            raise AuthError(403, "Can only set your own presence state")

        state = {}

        content = parse_json_object_from_request(request)

        try:
            state["presence"] = content.pop("presence")

            if "status_msg" in content:
                state["status_msg"] = content.pop("status_msg")
                if not isinstance(state["status_msg"], str):
                    raise SynapseError(400, "status_msg must be a string.")

            if content:
                raise KeyError()
        except SynapseError as e:
            raise e
        except Exception:
            raise SynapseError(400, "Unable to parse state")

        if self.hs.config.server.track_presence:
            await self.presence_handler.set_state(user, requester.device_id, state)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    PresenceStatusRestServlet(hs).register(http_server)
