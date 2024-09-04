#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

"""This module contains REST servlets to do with event streaming, /events."""

import logging
from typing import TYPE_CHECKING, Dict, List, Tuple, Union

from synapse.api.errors import SynapseError
from synapse.events.utils import SerializeEventConfig
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_string
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.streams.config import PaginationConfig
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class EventStreamRestServlet(RestServlet):
    PATTERNS = client_patterns("/events$", v1=True)
    CATEGORY = "Sync requests"

    DEFAULT_LONGPOLL_TIME_MS = 30000

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.event_stream_handler = hs.get_event_stream_handler()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        args: Dict[bytes, List[bytes]] = request.args  # type: ignore
        if requester.is_guest:
            if b"room_id" not in args:
                raise SynapseError(400, "Guest users must specify room_id param")
        room_id = parse_string(request, "room_id")

        pagin_config = await PaginationConfig.from_request(
            self.store, request, default_limit=10
        )
        timeout = EventStreamRestServlet.DEFAULT_LONGPOLL_TIME_MS
        if b"timeout" in args:
            try:
                timeout = int(args[b"timeout"][0])
            except ValueError:
                raise SynapseError(400, "timeout must be in milliseconds.")

        as_client_event = b"raw" not in args

        chunk = await self.event_stream_handler.get_stream(
            requester,
            pagin_config,
            timeout=timeout,
            as_client_event=as_client_event,
            affect_presence=(not requester.is_guest),
            room_id=room_id,
        )

        return 200, chunk


class EventRestServlet(RestServlet):
    PATTERNS = client_patterns("/events/(?P<event_id>[^/]*)$", v1=True)
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.clock = hs.get_clock()
        self.event_handler = hs.get_event_handler()
        self.auth = hs.get_auth()
        self._event_serializer = hs.get_event_client_serializer()

    async def on_GET(
        self, request: SynapseRequest, event_id: str
    ) -> Tuple[int, Union[str, JsonDict]]:
        requester = await self.auth.get_user_by_req(request)
        event = await self.event_handler.get_event(requester.user, None, event_id)

        if event:
            result = await self._event_serializer.serialize_event(
                event,
                self.clock.time_msec(),
                config=SerializeEventConfig(requester=requester),
            )
            return 200, result
        else:
            return 404, "Event not found."


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    EventStreamRestServlet(hs).register(http_server)
    EventRestServlet(hs).register(http_server)
