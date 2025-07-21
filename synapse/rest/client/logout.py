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

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class LogoutRestServlet(RestServlet):
    PATTERNS = client_patterns("/logout$", v1=True)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self._auth_handler = hs.get_auth_handler()
        self._device_handler = hs.get_device_handler()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(
            request, allow_expired=True, allow_locked=True
        )

        if requester.device_id is None:
            # The access token wasn't associated with a device.
            # Just delete the access token
            access_token = self.auth.get_access_token_from_request(request)
            await self._auth_handler.delete_access_token(access_token)
        else:
            await self._device_handler.delete_devices(
                requester.user.to_string(), [requester.device_id]
            )

        return 200, {}


class LogoutAllRestServlet(RestServlet):
    PATTERNS = client_patterns("/logout/all$", v1=True)

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self._auth_handler = hs.get_auth_handler()
        self._device_handler = hs.get_device_handler()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(
            request, allow_expired=True, allow_locked=True
        )
        user_id = requester.user.to_string()

        # first delete all of the user's devices
        await self._device_handler.delete_all_devices_for_user(user_id)

        # .. and then delete any access tokens which weren't associated with
        # devices.
        await self._auth_handler.delete_access_tokens_for_user(user_id)
        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc3861.enabled:
        return

    LogoutRestServlet(hs).register(http_server)
    LogoutAllRestServlet(hs).register(http_server)
