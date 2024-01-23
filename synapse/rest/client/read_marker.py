#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

from synapse.api.constants import ReceiptTypes
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReadMarkerRestServlet(RestServlet):
    PATTERNS = client_patterns("/rooms/(?P<room_id>[^/]*)/read_markers$")
    CATEGORY = "Receipts requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.config = hs.config
        self.receipts_handler = hs.get_receipts_handler()
        self.read_marker_handler = hs.get_read_marker_handler()
        self.presence_handler = hs.get_presence_handler()

        self._known_receipt_types = {
            ReceiptTypes.READ,
            ReceiptTypes.FULLY_READ,
            ReceiptTypes.READ_PRIVATE,
        }

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        await self.presence_handler.bump_presence_active_time(
            requester.user, requester.device_id
        )

        body = parse_json_object_from_request(request)

        unrecognized_types = set(body.keys()) - self._known_receipt_types
        if unrecognized_types:
            # It's fine if there are unrecognized receipt types, but let's log
            # it to help debug clients that have typoed the receipt type.
            #
            # We specifically *don't* error here, as a) it stops us processing
            # the valid receipts, and b) we need to be extensible on receipt
            # types.
            logger.info("Ignoring unrecognized receipt types: %s", unrecognized_types)

        for receipt_type in self._known_receipt_types:
            event_id = body.get(receipt_type, None)
            # TODO Add validation to reject non-string event IDs.
            if not event_id:
                continue

            if receipt_type == ReceiptTypes.FULLY_READ:
                await self.read_marker_handler.received_client_read_marker(
                    room_id,
                    user_id=requester.user.to_string(),
                    event_id=event_id,
                )
            else:
                await self.receipts_handler.received_client_receipt(
                    room_id,
                    receipt_type,
                    user_id=requester.user,
                    event_id=event_id,
                    # Setting the thread ID is not possible with the /read_markers endpoint.
                    thread_id=None,
                )

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReadMarkerRestServlet(hs).register(http_server)
