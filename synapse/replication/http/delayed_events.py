#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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
from synapse.types import JsonDict, JsonMapping

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationAddedDelayedEventRestServlet(ReplicationEndpoint):
    """Handle a delayed event being added by another worker.

    Request format:

        POST /_synapse/replication/delayed_event_added/

        {}
    """

    NAME = "added_delayed_event"
    PATH_ARGS = ()
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_delayed_events_handler()

    @staticmethod
    async def _serialize_payload(next_send_ts: int) -> JsonDict:  # type: ignore[override]
        return {"next_send_ts": next_send_ts}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict
    ) -> tuple[int, dict[str, JsonMapping | None]]:
        self.handler.on_added(int(content["next_send_ts"]))

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationAddedDelayedEventRestServlet(hs).register(http_server)
