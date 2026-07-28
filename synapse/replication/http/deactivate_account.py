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
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationNotifyAccountDeactivatedServlet(ReplicationEndpoint):
    """Notify that an account has been deactivated.

    Request format:

        POST /_synapse/replication/notify_account_deactivated/:user_id

        {
            "by_admin": true,
        }

    """

    NAME = "notify_account_deactivated"
    PATH_ARGS = ("user_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.deactivate_account_handler = hs.get_deactivate_account_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        by_admin: bool,
    ) -> JsonDict:
        """
        Args:
            user_id: The user ID which has been deactivated.
            by_admin: Whether the user was deactivated by an admin.
        """
        return {
            "by_admin": by_admin,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        by_admin = content["by_admin"]
        await self.deactivate_account_handler.notify_account_deactivated(
            user_id, by_admin=by_admin
        )
        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationNotifyAccountDeactivatedServlet(hs).register(http_server)
