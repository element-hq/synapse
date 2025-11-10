#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import re
from typing import TYPE_CHECKING, Annotated

from pydantic import StrictBool, StrictStr
from pydantic.types import StringConstraints

from synapse.api.constants import Direction, Membership
from synapse.api.errors import SynapseError
from synapse.events.utils import SerializeEventConfig
from synapse.handlers.relations import ThreadsListInclude
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_and_validate_json_object_from_request,
    parse_boolean,
    parse_integer,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.storage.databases.main.relations import ThreadsNextBatch
from synapse.streams.config import (
    PaginationConfig,
    extract_stream_token_from_pagination_token,
)
from synapse.types import JsonDict, RoomStreamToken, StreamKeyType, StreamToken, UserID
from synapse.types.handlers.sliding_sync import PerConnectionState, SlidingSyncConfig
from synapse.types.rest.client import RequestBodyModel, SlidingSyncBody

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ThreadUpdatesBody(RequestBodyModel):
    """
    Thread updates companion endpoint request body (MSC4360).

    Allows paginating thread updates using the same room selection as a sliding sync
    request. This enables clients to fetch thread updates for the same set of rooms
    that were included in their sliding sync response.

    Attributes:
        lists: Sliding window API lists, using the same structure as SlidingSyncBody.lists.
            If provided along with room_subscriptions, the union of rooms from both will
            be used.
        room_subscriptions: Room subscription API rooms, using the same structure as
            SlidingSyncBody.room_subscriptions. If provided along with lists, the union
            of rooms from both will be used.
        include_roots: Whether to include the thread root events in the response.
            Defaults to False.

    If neither lists nor room_subscriptions are provided, thread updates from all
    joined rooms are returned.
    """

    lists: (
        dict[
            Annotated[str, StringConstraints(max_length=64, strict=True)],
            SlidingSyncBody.SlidingSyncList,
        ]
        | None
    ) = None
    room_subscriptions: dict[StrictStr, SlidingSyncBody.RoomSubscription] | None = None
    include_roots: StrictBool = False


class RelationPaginationServlet(RestServlet):
    """API to paginate relations on an event by topological ordering, optionally
    filtered by relation type and event type.
    """

    PATTERNS = client_patterns(
        "/rooms/(?P<room_id>[^/]*)/relations/(?P<parent_id>[^/]*)"
        "(/(?P<relation_type>[^/]*)(/(?P<event_type>[^/]*))?)?$",
        releases=("v1",),
    )
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self,
        request: SynapseRequest,
        room_id: str,
        parent_id: str,
        relation_type: str | None = None,
        event_type: str | None = None,
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        pagination_config = await PaginationConfig.from_request(
            self._store, request, default_limit=5, default_dir=Direction.BACKWARDS
        )
        recurse = parse_boolean(request, "recurse", default=False) or parse_boolean(
            request, "org.matrix.msc3981.recurse", default=False
        )

        # The unstable version of this API returns an extra field for client
        # compatibility, see https://github.com/matrix-org/synapse/issues/12930.
        assert request.path is not None
        include_original_event = request.path.startswith(b"/_matrix/client/unstable/")

        # Return the relations
        result = await self._relations_handler.get_relations(
            requester=requester,
            event_id=parent_id,
            room_id=room_id,
            pagin_config=pagination_config,
            recurse=recurse,
            include_original_event=include_original_event,
            relation_type=relation_type,
            event_type=event_type,
        )

        return 200, result


class ThreadsServlet(RestServlet):
    PATTERNS = (re.compile("^/_matrix/client/v1/rooms/(?P<room_id>[^/]*)/threads"),)
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._relations_handler = hs.get_relations_handler()

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        limit = parse_integer(request, "limit", default=5)
        from_token_str = parse_string(request, "from")
        include = parse_string(
            request,
            "include",
            default=ThreadsListInclude.all.value,
            allowed_values=[v.value for v in ThreadsListInclude],
        )

        # Return the relations
        from_token = None
        if from_token_str:
            from_token = ThreadsNextBatch.from_string(from_token_str)

        result = await self._relations_handler.get_threads(
            requester=requester,
            room_id=room_id,
            include=ThreadsListInclude(include),
            limit=limit,
            from_token=from_token,
        )

        return 200, result


class ThreadUpdatesServlet(RestServlet):
    """
    Companion endpoint to the Sliding Sync threads extension (MSC4360).
    Allows clients to bulk fetch thread updates across all joined rooms.
    """

    PATTERNS = client_patterns(
        "/io.element.msc4360/thread_updates$",
        unstable=True,
        releases=(),
    )
    CATEGORY = "Client API requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.clock = hs.get_clock()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.relations_handler = hs.get_relations_handler()
        self.event_serializer = hs.get_event_client_serializer()
        self._storage_controllers = hs.get_storage_controllers()
        self.sliding_sync_handler = hs.get_sliding_sync_handler()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)

        # Parse request body
        body = parse_and_validate_json_object_from_request(request, ThreadUpdatesBody)

        # Parse query parameters
        dir_str = parse_string(request, "dir", default="b")
        if dir_str != "b":
            raise SynapseError(
                400,
                "The 'dir' parameter must be 'b' (backward). Forward pagination is not supported.",
            )

        limit = parse_integer(request, "limit", default=100)
        if limit <= 0:
            raise SynapseError(400, "The 'limit' parameter must be positive.")

        from_token_str = parse_string(request, "from")
        to_token_str = parse_string(request, "to")

        # Parse pagination tokens
        from_token: StreamToken | None = None
        to_token: StreamToken | None = None

        if from_token_str:
            try:
                stream_token_str = extract_stream_token_from_pagination_token(
                    from_token_str
                )
                from_token = await StreamToken.from_string(self.store, stream_token_str)
            except Exception as e:
                logger.exception("Error parsing 'from' token: %s", from_token_str)
                raise SynapseError(400, "'from' parameter is invalid") from e

        if to_token_str:
            try:
                stream_token_str = extract_stream_token_from_pagination_token(
                    to_token_str
                )
                to_token = await StreamToken.from_string(self.store, stream_token_str)
            except Exception:
                raise SynapseError(400, "'to' parameter is invalid")

        # Get the list of rooms to fetch thread updates for
        user_id = requester.user.to_string()
        user = UserID.from_string(user_id)

        # Get the current stream token for membership lookup
        if from_token is None:
            max_stream_ordering = self.store.get_room_max_stream_ordering()
            current_token = StreamToken.START.copy_and_replace(
                StreamKeyType.ROOM, RoomStreamToken(stream=max_stream_ordering)
            )
        else:
            current_token = from_token

        # Get room membership information to properly handle LEAVE/BAN rooms
        (
            room_membership_for_user_at_to_token_map,
            _,
            _,
        ) = await self.sliding_sync_handler.room_lists.get_room_membership_for_user_at_to_token(
            user=user,
            to_token=current_token,
            from_token=None,
        )

        # Determine which rooms to fetch updates for based on lists/room_subscriptions
        if body.lists is not None or body.room_subscriptions is not None:
            # Use sliding sync room selection logic
            sync_config = SlidingSyncConfig(
                user=user,
                requester=requester,
                lists=body.lists,
                room_subscriptions=body.room_subscriptions,
            )

            # Use the sliding sync room list handler to get the same set of rooms
            interested_rooms = (
                await self.sliding_sync_handler.room_lists.compute_interested_rooms(
                    sync_config=sync_config,
                    previous_connection_state=PerConnectionState(),
                    to_token=current_token,
                    from_token=None,
                )
            )

            room_ids = frozenset(interested_rooms.relevant_room_map.keys())
        else:
            # No lists or room_subscriptions, use only joined rooms
            room_ids = frozenset(
                room_id
                for room_id, membership_info in room_membership_for_user_at_to_token_map.items()
                if membership_info.membership == Membership.JOIN
            )

        # Get thread updates using unified helper
        (
            thread_updates,
            prev_batch_token,
        ) = await self.relations_handler.get_thread_updates_for_rooms(
            room_ids=room_ids,
            room_membership_map=room_membership_for_user_at_to_token_map,
            user_id=user_id,
            from_token=to_token,
            to_token=from_token if from_token else current_token,
            limit=limit,
            include_roots=body.include_roots,
        )

        if not thread_updates:
            return 200, {"chunk": {}}

        # Serialize thread updates using shared helper
        time_now = self.clock.time_msec()
        serialize_options = SerializeEventConfig(requester=requester)

        serialized = await self.relations_handler.serialize_thread_updates(
            thread_updates=thread_updates,
            prev_batch_token=prev_batch_token,
            event_serializer=self.event_serializer,
            time_now=time_now,
            store=self.store,
            serialize_options=serialize_options,
        )

        # Build response with "chunk" wrapper and "next_batch" key
        # (companion endpoint uses different key names than sliding sync)
        response: JsonDict = {"chunk": serialized["updates"]}
        if "prev_batch" in serialized:
            response["next_batch"] = serialized["prev_batch"]

        return 200, response


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RelationPaginationServlet(hs).register(http_server)
    ThreadsServlet(hs).register(http_server)
    if hs.config.experimental.msc4360_enabled:
        ThreadUpdatesServlet(hs).register(http_server)
