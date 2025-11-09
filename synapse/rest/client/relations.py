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
from typing import TYPE_CHECKING

from synapse.api.constants import Direction
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
from synapse.types import JsonDict, RoomStreamToken, StreamKeyType, StreamToken
from synapse.types.rest.client import ThreadUpdatesBody

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


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
        # TODO: Get sliding sync handler for filter_rooms logic
        # self.sliding_sync_handler = hs.get_sliding_sync_handler()

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
        from_token: RoomStreamToken | None = None
        to_token: RoomStreamToken | None = None

        if from_token_str:
            try:
                stream_token_str = extract_stream_token_from_pagination_token(
                    from_token_str
                )
                stream_token = await StreamToken.from_string(
                    self.store, stream_token_str
                )
                from_token = stream_token.room_key
            except Exception:
                raise SynapseError(400, "'from' parameter is invalid")

        if to_token_str:
            try:
                stream_token_str = extract_stream_token_from_pagination_token(
                    to_token_str
                )
                stream_token = await StreamToken.from_string(
                    self.store, stream_token_str
                )
                to_token = stream_token.room_key
            except Exception:
                raise SynapseError(400, "'to' parameter is invalid")

        # Get the list of rooms to fetch thread updates for
        # Start with all joined rooms, then apply filters if provided
        user_id = requester.user.to_string()
        room_ids = await self.store.get_rooms_for_user(user_id)

        if body.filters is not None:
            # TODO: Apply filters using sliding sync room filter logic
            # For now, if filters are provided, we need to call the sliding sync
            # filter_rooms method to get the applicable room IDs
            raise SynapseError(501, "Room filters not yet implemented")

        # Fetch thread updates from storage
        # For backward pagination:
        # - 'from' (upper bound, exclusive) maps to 'to_token' (inclusive with <=)
        #   Since next_batch is (last_returned - 1), <= excludes the last returned item
        # - 'to' (lower bound, exclusive) maps to 'from_token' (exclusive with >)
        (
            all_thread_updates,
            prev_batch_token,
        ) = await self.store.get_thread_updates_for_rooms(
            room_ids=room_ids,
            from_token=to_token,
            to_token=from_token,
            limit=limit,
        )

        if len(all_thread_updates) == 0:
            return 200, {"chunk": {}}

        # Filter thread updates for visibility
        filtered_updates = (
            await self.relations_handler.process_thread_updates_for_visibility(
                all_thread_updates, user_id
            )
        )

        if not filtered_updates:
            return 200, {"chunk": {}}

        # Fetch thread root events and their bundled aggregations
        (
            thread_root_event_map,
            aggregations_map,
        ) = await self.relations_handler.fetch_thread_roots_and_aggregations(
            filtered_updates.keys(), user_id
        )

        # Build response with per-thread data
        # Updates are already sorted by stream_ordering DESC from the database query,
        # and filter_events_for_client preserves order, so updates[0] is guaranteed to be
        # the latest event for each thread.
        time_now = self.clock.time_msec()
        serialize_options = SerializeEventConfig(requester=requester)
        chunk: dict[str, dict[str, JsonDict]] = {}

        for thread_root_id, updates in filtered_updates.items():
            # We only care about the latest update for the thread
            latest_update = updates[0]
            room_id = latest_update.room_id

            if room_id not in chunk:
                chunk[room_id] = {}

            update_dict: JsonDict = {}

            # Add thread root if present
            thread_root_event = thread_root_event_map.get(thread_root_id)
            if thread_root_event is not None:
                bundle_aggs_map = (
                    {thread_root_id: aggregations_map[thread_root_id]}
                    if thread_root_id in aggregations_map
                    else None
                )
                serialized_events = await self.event_serializer.serialize_events(
                    [thread_root_event],
                    time_now,
                    config=serialize_options,
                    bundle_aggregations=bundle_aggs_map,
                )
                if serialized_events:
                    update_dict["thread_root"] = serialized_events[0]

            # Add per-thread prev_batch if this thread has multiple visible updates
            if len(updates) > 1:
                # Create a token pointing to one position before the latest event's stream position.
                # This makes it exclusive - /relations with dir=b won't return the latest event again.
                per_thread_prev_batch = StreamToken.START.copy_and_replace(
                    StreamKeyType.ROOM,
                    RoomStreamToken(stream=latest_update.stream_ordering - 1),
                )
                update_dict["prev_batch"] = await per_thread_prev_batch.to_string(
                    self.store
                )

            chunk[room_id][thread_root_id] = update_dict

        # Build response
        response: JsonDict = {"chunk": chunk}

        # Add next_batch token for pagination
        if prev_batch_token is not None:
            response["next_batch"] = await prev_batch_token.to_string(self.store)

        return 200, response


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RelationPaginationServlet(hs).register(http_server)
    ThreadsServlet(hs).register(http_server)
    if hs.config.experimental.msc4360_enabled:
        ThreadUpdatesServlet(hs).register(http_server)
