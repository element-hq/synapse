from http import HTTPStatus
from typing import TYPE_CHECKING

import attr
from typing_extensions import TypeAlias

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
    parse_integer,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.types import (
    JsonDict,
    RoomID,
    SlidingSyncStreamToken,
    ThreadSubscriptionsToken,
)
from synapse.types.handlers.sliding_sync import SlidingSyncResult
from synapse.types.rest import RequestBodyModel
from synapse.util.pydantic_models import AnyEventId

if TYPE_CHECKING:
    from synapse.server import HomeServer

_ThreadSubscription: TypeAlias = (
    SlidingSyncResult.Extensions.ThreadSubscriptionsExtension.ThreadSubscription
)
_ThreadUnsubscription: TypeAlias = (
    SlidingSyncResult.Extensions.ThreadSubscriptionsExtension.ThreadUnsubscription
)


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
        automatic: AnyEventId | None = None
        """
        If supplied, the event ID of an event giving rise to this automatic subscription.

        If omitted, this subscription is a manual subscription.
        """

    async def on_GET(
        self, request: SynapseRequest, room_id: str, thread_root_id: str
    ) -> tuple[int, JsonDict]:
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
    ) -> tuple[int, JsonDict]:
        RoomID.from_string(room_id)
        if not thread_root_id.startswith("$"):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid event ID", errcode=Codes.INVALID_PARAM
            )
        body = parse_and_validate_json_object_from_request(request, self.PutBody)

        requester = await self.auth.get_user_by_req(request)

        await self.handler.subscribe_user_to_thread(
            requester.user,
            room_id,
            thread_root_id,
            automatic_event_id=body.automatic,
        )

        return HTTPStatus.OK, {}

    async def on_DELETE(
        self, request: SynapseRequest, room_id: str, thread_root_id: str
    ) -> tuple[int, JsonDict]:
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


class ThreadSubscriptionsPaginationRestServlet(RestServlet):
    PATTERNS = client_patterns(
        "/io.element.msc4308/thread_subscriptions$",
        unstable=True,
        releases=(),
    )
    CATEGORY = "Thread Subscriptions requests (unstable)"

    # Maximum number of thread subscriptions to return in one request.
    MAX_LIMIT = 512

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.is_mine = hs.is_mine
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        limit = min(
            parse_integer(request, "limit", default=100, negative=False),
            ThreadSubscriptionsPaginationRestServlet.MAX_LIMIT,
        )
        from_end_opt = parse_string(request, "from", required=False)
        to_start_opt = parse_string(request, "to", required=False)
        _direction = parse_string(request, "dir", required=True, allowed_values=("b",))

        if limit <= 0:
            # condition needed because `negative=False` still allows 0
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "limit must be greater than 0",
                errcode=Codes.INVALID_PARAM,
            )

        if from_end_opt is not None:
            try:
                # because of backwards pagination, the `from` token is actually the
                # bound closest to the end of the stream
                end_stream_id = ThreadSubscriptionsToken.from_string(
                    from_end_opt
                ).stream_id
            except ValueError:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "`from` is not a valid token",
                    errcode=Codes.INVALID_PARAM,
                )
        else:
            end_stream_id = self.store.get_max_thread_subscriptions_stream_id()

        if to_start_opt is not None:
            # because of backwards pagination, the `to` token is actually the
            # bound closest to the start of the stream
            try:
                start_stream_id = ThreadSubscriptionsToken.from_string(
                    to_start_opt
                ).stream_id
            except ValueError:
                # we also accept sliding sync `pos` tokens on this parameter
                try:
                    sliding_sync_pos = await SlidingSyncStreamToken.from_string(
                        self.store, to_start_opt
                    )
                    start_stream_id = (
                        sliding_sync_pos.stream_token.thread_subscriptions_key
                    )
                except ValueError:
                    raise SynapseError(
                        HTTPStatus.BAD_REQUEST,
                        "`to` is not a valid token",
                        errcode=Codes.INVALID_PARAM,
                    )
        else:
            # the start of time is ID 1; the lower bound is exclusive though
            start_stream_id = 0

        subscriptions = (
            await self.store.get_latest_updated_thread_subscriptions_for_user(
                requester.user.to_string(),
                from_id=start_stream_id,
                to_id=end_stream_id,
                limit=limit,
            )
        )

        subscribed_threads: dict[str, dict[str, JsonDict]] = {}
        unsubscribed_threads: dict[str, dict[str, JsonDict]] = {}
        for stream_id, room_id, thread_root_id, subscribed, automatic in subscriptions:
            if subscribed:
                subscribed_threads.setdefault(room_id, {})[thread_root_id] = (
                    attr.asdict(
                        _ThreadSubscription(
                            automatic=automatic,
                            bump_stamp=stream_id,
                        )
                    )
                )
            else:
                unsubscribed_threads.setdefault(room_id, {})[thread_root_id] = (
                    attr.asdict(_ThreadUnsubscription(bump_stamp=stream_id))
                )

        result: JsonDict = {}
        if subscribed_threads:
            result["subscribed"] = subscribed_threads
        if unsubscribed_threads:
            result["unsubscribed"] = unsubscribed_threads

        if len(subscriptions) == limit:
            # We hit the limit, so there might be more entries to return.
            # Generate a new token that has moved backwards, ready for the next
            # request.
            min_returned_stream_id, _, _, _, _ = subscriptions[0]
            result["end"] = ThreadSubscriptionsToken(
                # We subtract one because the 'later in the stream' bound is inclusive,
                # and we already saw the element at index 0.
                stream_id=min_returned_stream_id - 1
            ).to_string()

        return HTTPStatus.OK, result


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc4306_enabled:
        ThreadSubscriptionsRestServlet(hs).register(http_server)
        ThreadSubscriptionsPaginationRestServlet(hs).register(http_server)
