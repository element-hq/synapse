#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from synapse.api.constants import MAIN_TIMELINE, ReceiptTypes
from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import EventID, JsonDict, RoomID

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReceiptRestServlet(RestServlet):
    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/receipt/(?P<receipt_type>[^/]*)/(?P<event_id>[^/]*)$"
    )
    CATEGORY = "Receipts requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.receipts_handler = hs.get_receipts_handler()
        self.read_marker_handler = hs.get_read_marker_handler()
        self.presence_handler = hs.get_presence_handler()
        self._main_store = hs.get_datastores().main

        self._known_receipt_types = {
            ReceiptTypes.READ,
            ReceiptTypes.READ_PRIVATE,
            ReceiptTypes.FULLY_READ,
        }

    async def on_POST(
        self, request: SynapseRequest, room_id: str, receipt_type: str, event_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        if not RoomID.is_valid(room_id) or not event_id.startswith(EventID.SIGIL):
            raise SynapseError(400, "A valid room ID and event ID must be specified")

        if receipt_type not in self._known_receipt_types:
            raise SynapseError(
                400,
                f"Receipt type must be {', '.join(self._known_receipt_types)}",
            )

        body = parse_json_object_from_request(request)

        # Pull the thread ID, if one exists.
        thread_id = None
        if "thread_id" in body:
            thread_id = body.get("thread_id")
            if not thread_id or not isinstance(thread_id, str):
                raise SynapseError(
                    400,
                    "thread_id field must be a non-empty string",
                    Codes.INVALID_PARAM,
                )

            if receipt_type == ReceiptTypes.FULLY_READ:
                raise SynapseError(
                    400,
                    f"thread_id is not compatible with {ReceiptTypes.FULLY_READ} receipts.",
                    Codes.INVALID_PARAM,
                )

            # Ensure the event ID roughly correlates to the thread ID.
            if not await self._is_event_in_thread(event_id, thread_id):
                raise SynapseError(
                    400,
                    f"event_id {event_id} is not related to thread {thread_id}",
                    Codes.INVALID_PARAM,
                )

        await self.presence_handler.bump_presence_active_time(
            requester.user, requester.device_id
        )

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
                thread_id=thread_id,
            )

        return 200, {}

    async def _is_event_in_thread(self, event_id: str, thread_id: str) -> bool:
        """
        The event must be related to the thread ID (in a vague sense) to ensure
        clients aren't sending bogus receipts.

        A thread ID is considered valid for a given event E if:

        1. E has a thread relation which matches the thread ID;
        2. E has another event which has a thread relation to E matching the
           thread ID; or
        3. E is recursively related (via any rel_type) to an event which
           satisfies 1 or 2.

        Given the following DAG:

            A <---[m.thread]-- B <--[m.annotation]-- C
            ^
            |--[m.reference]-- D <--[m.annotation]-- E

        It is valid to send a receipt for thread A on A, B, C, D, or E.

        It is valid to send a receipt for the main timeline on A, D, and E.

        Args:
            event_id: The event ID to check.
            thread_id: The thread ID the event is potentially part of.

        Returns:
            True if the event belongs to the given thread, otherwise False.
        """

        # If the receipt is on the main timeline, it is enough to check whether
        # the event is directly related to a thread.
        if thread_id == MAIN_TIMELINE:
            return MAIN_TIMELINE == await self._main_store.get_thread_id(event_id)

        # Otherwise, check if the event is directly part of a thread, or is the
        # root message (or related to the root message) of a thread.
        return thread_id == await self._main_store.get_thread_id_for_receipts(event_id)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReceiptRestServlet(hs).register(http_server)
