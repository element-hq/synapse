from http import HTTPStatus
from typing import Tuple

from synapse._pydantic_compat import StrictBool
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomID
from synapse.types.rest import RequestBodyModel


class ThreadSubscriptionsRestServlet(RestServlet):
    PATTERNS = client_patterns(
        "/io.element.msc4306/rooms/(?P<room_id>[^/]*)/thread/(?P<thread_root_id>[^/]*)/subscription$",
        unstable=True,
        releases=(),
    )
    CATEGORY = "Thread Subscriptions requests (unstable)"

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.is_mine = hs.is_mine
        self.store = hs.get_datastores().main
        self.handler = hs.get_thread_subscriptions_handler()

    class PutBody(RequestBodyModel):
        automatic: StrictBool

    async def on_GET(
        self, request: SynapseRequest, room_id: str, thread_root_id: str
    ) -> Tuple[int, JsonDict]:
        RoomID.from_string(room_id)
        if not thread_root_id.startswith("$"):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid event ID", errcode=Codes.INVALID_PARAM
            )
        requester = await self.auth.get_user_by_req(request)

        subscription = await self.handler.get_thread_subscription_settings(
            requester.user,
            room_id,
            thread_root_id,
        )

        if subscription is None:
            raise NotFoundError("Not subscribed.")

        return HTTPStatus.OK, {"automatic": subscription.automatic}

    async def on_PUT(
        self, request: SynapseRequest, room_id: str, thread_root_id: str
    ) -> Tuple[int, JsonDict]:
        RoomID.from_string(room_id)
        if not thread_root_id.startswith("$"):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid event ID", errcode=Codes.INVALID_PARAM
            )
        requester = await self.auth.get_user_by_req(request)

        body = parse_and_validate_json_object_from_request(request, self.PutBody)

        await self.handler.subscribe_user_to_thread(
            requester.user,
            room_id,
            thread_root_id,
            automatic=body.automatic,
        )

        return HTTPStatus.OK, {}

    async def on_DELETE(
        self, request: SynapseRequest, room_id: str, thread_root_id: str
    ) -> Tuple[int, JsonDict]:
        RoomID.from_string(room_id)
        if not thread_root_id.startswith("$"):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid event ID", errcode=Codes.INVALID_PARAM
            )
        requester = await self.auth.get_user_by_req(request)

        await self.handler.unsubscribe_user_from_thread(
            requester.user,
            room_id,
            thread_root_id,
        )

        return HTTPStatus.OK, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc4306_enabled:
        ThreadSubscriptionsRestServlet(hs).register(http_server)
