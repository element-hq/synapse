# This module contains REST servlets to do with delayed events: /delayed_events/<paths>

import logging
from enum import Enum
from http import HTTPStatus
from typing import TYPE_CHECKING, Tuple

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


# TODO: Needs unit testing
class UpdateDelayedEventServlet(RestServlet):
    PATTERNS = client_patterns(
        r"/org\.matrix\.msc4140/delayed_events/(?P<delay_id>[^/]*)$",
        releases=(),
    )
    CATEGORY = "Delayed event management requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.delayed_events_handler = hs.get_delayed_events_handler()

    async def on_POST(
        self, request: SynapseRequest, delay_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

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
            await self.delayed_events_handler.cancel(requester, delay_id)
        elif enum_action == _UpdateDelayedEventAction.RESTART:
            await self.delayed_events_handler.restart(requester, delay_id)
        elif enum_action == _UpdateDelayedEventAction.SEND:
            await self.delayed_events_handler.send(requester, delay_id)
        return 200, {}


# TODO: Needs unit testing
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

    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        # TODO: Support Pagination stream API ("from" query parameter)
        delayed_events = await self.delayed_events_handler.get_all_for_user(requester)

        ret = {"delayed_events": delayed_events}
        return 200, ret


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    # The following can't currently be instantiated on workers.
    if hs.config.worker.worker_app is None:
        UpdateDelayedEventServlet(hs).register(http_server)
    DelayedEventsServlet(hs).register(http_server)
