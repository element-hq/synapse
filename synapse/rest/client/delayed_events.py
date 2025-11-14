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

# This module contains REST servlets to do with delayed events: /delayed_events/<paths>

import logging
from enum import Enum
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class _UpdateDelayedEventAction(Enum):
    CANCEL = "cancel"
    RESTART = "restart"
    SEND = "send"


class UpdateDelayedEventServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events/(?P<delay_id>[^/]+)$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_POST(
        self, request: SynapseRequest, delay_id: str
    ) -> tuple[int, JsonDict]:
        body = parse_json_object_from_request(request)
        try:
            action = str(body["action"])
        except KeyError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "'action' is missing",
                Codes.MISSING_PARAM,
            )
        try:
            enum_action = _UpdateDelayedEventAction(action)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "'action' is not one of "
                + ", ".join(f"'{m.value}'" for m in _UpdateDelayedEventAction),
                Codes.INVALID_PARAM,
            )

        if enum_action == _UpdateDelayedEventAction.CANCEL:
            await self.delayed_events_handler.cancel(request, delay_id)
        elif enum_action == _UpdateDelayedEventAction.RESTART:
            await self.delayed_events_handler.restart(request, delay_id)
        elif enum_action == _UpdateDelayedEventAction.SEND:
            await self.delayed_events_handler.send(request, delay_id)
        return 200, {}


class CancelDelayedEventServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events/(?P<delay_id>[^/]+)/cancel$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_POST(
        self, request: SynapseRequest, delay_id: str
    ) -> tuple[int, JsonDict]:
        await self.delayed_events_handler.cancel(request, delay_id)
        return 200, {}


class RestartDelayedEventServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events/(?P<delay_id>[^/]+)/restart$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_POST(
        self, request: SynapseRequest, delay_id: str
    ) -> tuple[int, JsonDict]:
        await self.delayed_events_handler.restart(request, delay_id)
        return 200, {}


class SendDelayedEventServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events/(?P<delay_id>[^/]+)/send$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_POST(
        self, request: SynapseRequest, delay_id: str
    ) -> tuple[int, JsonDict]:
        await self.delayed_events_handler.send(request, delay_id)
        return 200, {}


class DelayedEventsServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        # TODO: Support Pagination stream API ("from" query parameter)
        delayed_events = await self.delayed_events_handler.get_all_for_user(requester)

        ret = {"delayed_events": delayed_events}
        return 200, ret


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    # The following can't currently be instantiated on workers.
    if hs.config.worker.worker_app is None:
        UpdateDelayedEventServlet(hs).register(http_server)
        CancelDelayedEventServlet(hs).register(http_server)
        RestartDelayedEventServlet(hs).register(http_server)
        SendDelayedEventServlet(hs).register(http_server)
    DelayedEventsServlet(hs).register(http_server)
