#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationSendServerNoticeServlet(ReplicationEndpoint):
    """Send a server notice to a user"""

    NAME = "send_server_notice"
    PATH_ARGS = ()

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.server_notices_manager = hs.get_server_notices_manager()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str,
        event_content: dict,
        type: str,
        state_key: Optional[str] = None,
        txn_id: Optional[str] = None,
    ) -> JsonDict:
        """
        Args:
            user_id: mxid of user to send event to.
            event_content: content of event to send
            type: type of event
            state_key: the state key for the event, if it is a state event
            txn_id: the transaction ID
        """
        return {
            "user_id": user_id,
            "event_content": event_content,
            "type": type,
            "state_key": state_key,
            "txn_id": txn_id,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict
    ) -> Tuple[int, JsonDict]:
        event = await self.server_notices_manager.send_notice(
            user_id=content["user_id"],
            event_content=content["event_content"],
            type=content["type"],
            state_key=content["state_key"],
            txn_id=content["txn_id"],
        )

        return 200, event


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationSendServerNoticeServlet(hs).register(http_server)
