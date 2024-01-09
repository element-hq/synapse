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
from typing import TYPE_CHECKING, Optional, Tuple, cast

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RegisterDeviceReplicationServlet(ReplicationEndpoint):
    """Ensure a device is registered, generating a new access token for the
    device.

    Used during registration and login.
    """

    NAME = "device_check_registered"
    PATH_ARGS = ("user_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.registration_handler = hs.get_registration_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        device_id: Optional[str],
        initial_display_name: Optional[str],
        is_guest: bool,
        is_appservice_ghost: bool,
        should_issue_refresh_token: bool,
        auth_provider_id: Optional[str],
        auth_provider_session_id: Optional[str],
    ) -> JsonDict:
        """
        Args:
            user_id
            device_id: Device ID to use, if None a new one is generated.
            initial_display_name
            is_guest
            is_appservice_ghost
            should_issue_refresh_token
        """
        return {
            "device_id": device_id,
            "initial_display_name": initial_display_name,
            "is_guest": is_guest,
            "is_appservice_ghost": is_appservice_ghost,
            "should_issue_refresh_token": should_issue_refresh_token,
            "auth_provider_id": auth_provider_id,
            "auth_provider_session_id": auth_provider_session_id,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> Tuple[int, JsonDict]:
        device_id = content["device_id"]
        initial_display_name = content["initial_display_name"]
        is_guest = content["is_guest"]
        is_appservice_ghost = content["is_appservice_ghost"]
        should_issue_refresh_token = content["should_issue_refresh_token"]
        auth_provider_id = content["auth_provider_id"]
        auth_provider_session_id = content["auth_provider_session_id"]

        res = await self.registration_handler.register_device_inner(
            user_id,
            device_id,
            initial_display_name,
            is_guest,
            is_appservice_ghost=is_appservice_ghost,
            should_issue_refresh_token=should_issue_refresh_token,
            auth_provider_id=auth_provider_id,
            auth_provider_session_id=auth_provider_session_id,
        )

        return 200, cast(JsonDict, res)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RegisterDeviceReplicationServlet(hs).register(http_server)
