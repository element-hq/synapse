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
from typing import TYPE_CHECKING, List, Tuple

from twisted.web.server import Request

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, Requester, UserID
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.storage.databases.main import DataStore

logger = logging.getLogger(__name__)


class ReplicationSendEventRestServlet(ReplicationEndpoint):
    """Handles events newly created on workers, including persisting and
    notifying.

    The API looks like:

        POST /_synapse/replication/send_event/:event_id/:txn_id

        {
            "event": { .. serialized event .. },
            "room_version": .., // "1", "2", "3", etc: the version of the room
                                // containing the event
            "event_format_version": .., // 1,2,3 etc: the event format version
            "internal_metadata": { .. serialized internal_metadata .. },
            "outlier": true|false,
            "rejected_reason": ..,   // The event.rejected_reason field
            "context": { .. serialized event context .. },
            "requester": { .. serialized requester .. },
            "ratelimit": true,
            "extra_users": [],
        }

        200 OK

        { "stream_id": 12345, "event_id": "$abcdef..." }

    Responds with a 409 when a `PartialStateConflictError` is raised due to an event
    context that needs to be recomputed due to the un-partial stating of a room.

    The returned event ID may not match the sent event if it was deduplicated.
    """

    NAME = "send_event"
    PATH_ARGS = ("event_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.server_name = hs.hostname
        self.event_creation_handler = hs.get_event_creation_handler()
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        event_id: str,
        store: "DataStore",
        event: EventBase,
        context: EventContext,
        requester: Requester,
        ratelimit: bool,
        extra_users: List[UserID],
    ) -> JsonDict:
        """
        Args:
            event_id
            store
            requester
            event
            context
            ratelimit
            extra_users: Any extra users to notify about event
        """
        serialized_context = await context.serialize(event, store)

        payload = {
            "event": event.get_pdu_json(),
            "room_version": event.room_version.identifier,
            "event_format_version": event.format_version,
            "internal_metadata": event.internal_metadata.get_dict(),
            "outlier": event.internal_metadata.is_outlier(),
            "rejected_reason": event.rejected_reason,
            "context": serialized_context,
            "requester": requester.serialize(),
            "ratelimit": ratelimit,
            "extra_users": [u.to_string() for u in extra_users],
        }

        return payload

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, event_id: str
    ) -> Tuple[int, JsonDict]:
        with Measure(
            self.clock, name="repl_send_event_parse", server_name=self.server_name
        ):
            event_dict = content["event"]
            room_ver = KNOWN_ROOM_VERSIONS[content["room_version"]]
            internal_metadata = content["internal_metadata"]
            rejected_reason = content["rejected_reason"]

            event = make_event_from_dict(
                event_dict, room_ver, internal_metadata, rejected_reason
            )
            event.internal_metadata.outlier = content["outlier"]

            requester = Requester.deserialize(self.store, content["requester"])
            context = EventContext.deserialize(
                self._storage_controllers, content["context"]
            )

            ratelimit = content["ratelimit"]
            extra_users = [UserID.from_string(u) for u in content["extra_users"]]

        logger.info(
            "Got event to send with ID: %s into room: %s", event.event_id, event.room_id
        )

        event = await self.event_creation_handler.persist_and_notify_client_events(
            requester, [(event, context)], ratelimit=ratelimit, extra_users=extra_users
        )

        return (
            200,
            {
                "stream_id": event.internal_metadata.stream_ordering,
                "event_id": event.event_id,
            },
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationSendEventRestServlet(hs).register(http_server)
