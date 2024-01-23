#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationRemovePusherRestServlet(ReplicationEndpoint):
    """Deletes the given pusher.

    Request format:

        POST /_synapse/replication/remove_pusher/:user_id

        {
            "app_id": "<some_id>",
            "pushkey": "<some_key>"
        }

    """

    NAME = "add_user_account_data"
    PATH_ARGS = ("user_id",)
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.pusher_pool = hs.get_pusherpool()

    @staticmethod
    async def _serialize_payload(app_id: str, pushkey: str, user_id: str) -> JsonDict:  # type: ignore[override]
        payload = {
            "app_id": app_id,
            "pushkey": pushkey,
        }

        return payload

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> Tuple[int, JsonDict]:
        app_id = content["app_id"]
        pushkey = content["pushkey"]

        await self.pusher_pool.remove_pusher(app_id, pushkey, user_id)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationRemovePusherRestServlet(hs).register(http_server)
