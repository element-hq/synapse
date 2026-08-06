#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""The Paginated Sync endpoint (MSC TBD): a dialect of Simplified Sliding Sync
(MSC4186) without lists/ranges/subscriptions - see
`synapse.handlers.sliding_sync.paginated` for the semantics.

The room and extension serialisation is inherited unchanged from the sliding
sync servlet; only the request parsing and the top-level response differ.
"""

import logging
from typing import TYPE_CHECKING

from synapse.http.server import HttpServer
from synapse.http.servlet import (
    parse_and_validate_json_object_from_request,
    parse_integer,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import log_kv, set_tag
from synapse.rest.client._base import client_patterns
from synapse.rest.client.sync import SlidingSyncRestServlet
from synapse.types import JsonDict, Requester, SlidingSyncStreamToken
from synapse.types.handlers.paginated_sync import (
    PaginatedSyncConfig,
    PaginatedSyncResult,
)
from synapse.types.rest.client import PaginatedSyncBody

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PaginatedSyncRestServlet(SlidingSyncRestServlet):
    """
    API endpoint for MSC TBD Paginated Sync. `POST` with a JSON body of
    `page_size`/`limit`/`history`/`required_state`/`extensions`; responds with
    the changed rooms (most recently active first, at most `page_size` of
    them), a `pending` count of rooms that didn't fit, and `total_rooms`.

    Request query parameters (as sliding sync):
        timeout: How long to wait for new events in milliseconds.
        pos: The position token from the previous response, if any.
    """

    PATTERNS = client_patterns(
        "/org.matrix.paginated_sync/sync$", releases=[], v1=False, unstable=True
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.paginated_sync_handler = hs.get_paginated_sync_handler()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user = requester.user

        timeout = parse_integer(request, "timeout", default=0)
        from_token_string = parse_string(request, "pos")

        from_token = None
        if from_token_string is not None:
            from_token = await SlidingSyncStreamToken.from_string(
                self.store, from_token_string
            )

        body = parse_and_validate_json_object_from_request(request, PaginatedSyncBody)

        set_tag(
            "paginated_sync.sync_type",
            "initial" if from_token is None else "incremental",
        )
        set_tag("paginated_sync.conn_id", body.conn_id or "")
        log_kv(
            {
                "paginated_sync.page_size": body.page_size,
                "paginated_sync.limit": body.limit,
                "paginated_sync.history": body.history,
            }
        )

        sync_config = PaginatedSyncConfig(
            user=user,
            requester=requester,
            # Namespace the connection ID so a paginated sync connection can
            # never collide with a sliding sync connection from the same device
            # in the shared per-connection tables.
            conn_id=f"paginated:{body.conn_id or ''}",
            page_size=body.page_size,
            limit=body.limit,
            history=body.history,
            required_state=body.required_state,
            extensions=body.extensions,
        )

        (
            paginated_sync_result,
            did_wait,
        ) = await self.paginated_sync_handler.wait_for_paginated_sync_for_user(
            requester,
            sync_config,
            from_token,
            timeout,
        )
        set_tag("paginated_sync.did_wait", str(did_wait))

        # The client may have disconnected by now; don't bother to serialize the
        # response if so.
        if request._disconnected:
            logger.info("Client has disconnected; not serializing response.")
            return 200, {}

        response_content = await self.encode_paginated_response(
            requester, paginated_sync_result
        )

        return 200, response_content

    async def encode_paginated_response(
        self,
        requester: Requester,
        result: PaginatedSyncResult,
    ) -> JsonDict:
        response: JsonDict = {}

        response["pos"] = await result.next_pos.to_string(self.store)
        response["rooms"] = await self.encode_rooms(requester, result.rooms)
        # `num_live` is derivable in this API (previously-sent rooms only ever
        # receive live events; `initial` rooms are all-historical), so it is
        # not part of the response.
        for room in response["rooms"].values():
            room.pop("num_live", None)
        response["extensions"] = await self.encode_extensions(
            requester, result.extensions, result.rooms
        )
        if result.pending:
            response["pending"] = result.pending
        response["total_rooms"] = result.total_rooms

        return response


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.paginated_sync_enabled:
        PaginatedSyncRestServlet(hs).register(http_server)
