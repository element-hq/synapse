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
from typing import TYPE_CHECKING

from synapse.api.constants import ReceiptTypes
from synapse.events.utils import (
    SerializeEventConfig,
    format_event_for_client_v2_without_room_id,
)
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ...api.errors import SynapseError
from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class NotificationsServlet(RestServlet):
    PATTERNS = client_patterns("/notifications$")

    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()
        self.clock = hs.get_clock()
        self._event_serializer = hs.get_event_client_serializer()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()

        # While this is intended to be "string" to clients, the 'from' token
        # is actually based on a numeric ID. So it must parse to an int.
        from_token_str = parse_string(request, "from", required=False)
        if from_token_str is not None:
            # Parse to an integer.
            try:
                from_token = int(from_token_str)
            except ValueError:
                # If it doesn't parse to an integer, then this cannot possibly be a valid
                # pagination token, as we only hand out integers.
                raise SynapseError(
                    400, 'Query parameter "from" contains unrecognised token'
                )
        else:
            from_token = None

        limit = parse_integer(request, "limit", default=50)
        only = parse_string(request, "only", required=False)

        limit = min(limit, 500)

        push_actions = await self.store.get_push_actions_for_user(
            user_id, from_token, limit, only_highlight=(only == "highlight")
        )

        receipts_by_room = await self.store.get_receipts_for_user_with_orderings(
            user_id,
            [
                ReceiptTypes.READ,
                ReceiptTypes.READ_PRIVATE,
            ],
        )

        notif_event_ids = [pa.event_id for pa in push_actions]
        notif_events = await self.store.get_events(notif_event_ids)

        returned_push_actions = []

        next_token = None

        serialize_options = SerializeEventConfig(
            event_format=format_event_for_client_v2_without_room_id,
            requester=requester,
        )
        now = self.clock.time_msec()

        for pa in push_actions:
            returned_pa = {
                "room_id": pa.room_id,
                "profile_tag": pa.profile_tag,
                "actions": pa.actions,
                "ts": pa.received_ts,
                "event": (
                    await self._event_serializer.serialize_event(
                        notif_events[pa.event_id],
                        now,
                        config=serialize_options,
                    )
                ),
            }

            if pa.room_id not in receipts_by_room:
                returned_pa["read"] = False
            else:
                receipt = receipts_by_room[pa.room_id]

                returned_pa["read"] = (
                    receipt["topological_ordering"],
                    receipt["stream_ordering"],
                ) >= (pa.topological_ordering, pa.stream_ordering)
            returned_push_actions.append(returned_pa)
            next_token = str(pa.stream_ordering)

        return 200, {"notifications": returned_push_actions, "next_token": next_token}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    NotificationsServlet(hs).register(http_server)
