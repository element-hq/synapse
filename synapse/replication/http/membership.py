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
from typing import TYPE_CHECKING, List, Optional, Tuple

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.http.site import SynapseRequest
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, Requester, UserID
from synapse.util.distributor import user_left_room

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationRemoteJoinRestServlet(ReplicationEndpoint):
    """Does a remote join for the given user to the given room

    Request format:

        POST /_synapse/replication/remote_join/:room_id/:user_id

        {
            "requester": ...,
            "remote_room_hosts": [...],
            "content": { ... }
        }
    """

    NAME = "remote_join"
    PATH_ARGS = ("room_id", "user_id")

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.federation_handler = hs.get_federation_handler()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        requester: Requester,
        room_id: str,
        user_id: str,
        remote_room_hosts: List[str],
        content: JsonDict,
    ) -> JsonDict:
        """
        Args:
            requester: The user making the request according to the access token
            room_id: The ID of the room.
            user_id: The ID of the user.
            remote_room_hosts: Servers to try and join via
            content: The event content to use for the join event

        Returns:
            A dict representing the payload of the request.
        """
        return {
            "requester": requester.serialize(),
            "remote_room_hosts": remote_room_hosts,
            "content": content,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: SynapseRequest, content: JsonDict, room_id: str, user_id: str
    ) -> Tuple[int, JsonDict]:
        remote_room_hosts = content["remote_room_hosts"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])
        request.requester = requester

        logger.info("remote_join: %s into room: %s", user_id, room_id)

        event_id, stream_id = await self.federation_handler.do_invite_join(
            remote_room_hosts, room_id, user_id, event_content
        )

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationRemoteKnockRestServlet(ReplicationEndpoint):
    """Perform a remote knock for the given user on the given room

    Request format:

        POST /_synapse/replication/remote_knock/:room_id/:user_id

        {
            "requester": ...,
            "remote_room_hosts": [...],
            "content": { ... }
        }
    """

    NAME = "remote_knock"
    PATH_ARGS = ("room_id", "user_id")

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.federation_handler = hs.get_federation_handler()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        requester: Requester,
        room_id: str,
        user_id: str,
        remote_room_hosts: List[str],
        content: JsonDict,
    ) -> JsonDict:
        """
        Args:
            requester: The user making the request, according to the access token.
            room_id: The ID of the room to knock on.
            user_id: The ID of the knocking user.
            remote_room_hosts: Servers to try and send the knock via.
            content: The event content to use for the knock event.
        """
        return {
            "requester": requester.serialize(),
            "remote_room_hosts": remote_room_hosts,
            "content": content,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: SynapseRequest, content: JsonDict, room_id: str, user_id: str
    ) -> Tuple[int, JsonDict]:
        remote_room_hosts = content["remote_room_hosts"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])
        request.requester = requester

        logger.debug("remote_knock: %s on room: %s", user_id, room_id)

        event_id, stream_id = await self.federation_handler.do_knock(
            remote_room_hosts, room_id, user_id, event_content
        )

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationRemoteRejectInviteRestServlet(ReplicationEndpoint):
    """Rejects an out-of-band invite we have received from a remote server

    Request format:

        POST /_synapse/replication/remote_reject_invite/:event_id

        {
            "txn_id": ...,
            "requester": ...,
            "content": { ... }
        }
    """

    NAME = "remote_reject_invite"
    PATH_ARGS = ("invite_event_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.member_handler = hs.get_room_member_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        invite_event_id: str,
        txn_id: Optional[str],
        requester: Requester,
        content: JsonDict,
    ) -> JsonDict:
        """
        Args:
            invite_event_id: The ID of the invite to be rejected.
            txn_id: Optional transaction ID supplied by the client
            requester: User making the rejection request, according to the access token
            content: Additional content to include in the rejection event.
               Normally an empty dict.

        Returns:
            A dict representing the payload of the request.
        """
        return {
            "txn_id": txn_id,
            "requester": requester.serialize(),
            "content": content,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: SynapseRequest, content: JsonDict, invite_event_id: str
    ) -> Tuple[int, JsonDict]:
        txn_id = content["txn_id"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])
        request.requester = requester

        # hopefully we're now on the master, so this won't recurse!
        event_id, stream_id = await self.member_handler.remote_reject_invite(
            invite_event_id,
            txn_id,
            requester,
            event_content,
        )

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationRemoteRescindKnockRestServlet(ReplicationEndpoint):
    """Rescinds a local knock made on a remote room

    Request format:

        POST /_synapse/replication/remote_rescind_knock/:event_id

        {
            "txn_id": ...,
            "requester": ...,
            "content": { ... }
        }
    """

    NAME = "remote_rescind_knock"
    PATH_ARGS = ("knock_event_id",)

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.member_handler = hs.get_room_member_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        knock_event_id: str,
        txn_id: Optional[str],
        requester: Requester,
        content: JsonDict,
    ) -> JsonDict:
        """
        Args:
            knock_event_id: The ID of the knock to be rescinded.
            txn_id: An optional transaction ID supplied by the client.
            requester: The user making the rescind request, according to the access token.
            content: The content to include in the rescind event.
        """
        return {
            "txn_id": txn_id,
            "requester": requester.serialize(),
            "content": content,
        }

    async def _handle_request(  # type: ignore[override]
        self, request: SynapseRequest, content: JsonDict, knock_event_id: str
    ) -> Tuple[int, JsonDict]:
        txn_id = content["txn_id"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])
        request.requester = requester

        # hopefully we're now on the master, so this won't recurse!
        event_id, stream_id = await self.member_handler.remote_rescind_knock(
            knock_event_id,
            txn_id,
            requester,
            event_content,
        )

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationUserJoinedLeftRoomRestServlet(ReplicationEndpoint):
    """Notifies that a user has joined or left the room

    Request format:

        POST /_synapse/replication/membership_change/:room_id/:user_id/:change

        {}
    """

    NAME = "membership_change"
    PATH_ARGS = ("room_id", "user_id", "change")
    CACHE = False  # No point caching as should return instantly.

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.registration_handler = hs.get_registration_handler()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.distributor = hs.get_distributor()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        room_id: str, user_id: str, change: str
    ) -> JsonDict:
        """
        Args:
            room_id: The ID of the room.
            user_id: The ID of the user.
            change: "left"

        Returns:
            A dict representing the payload of the request.
        """
        assert change == "left"

        return {}

    async def _handle_request(  # type: ignore[override]
        self,
        request: Request,
        content: JsonDict,
        room_id: str,
        user_id: str,
        change: str,
    ) -> Tuple[int, JsonDict]:
        logger.info("user membership change: %s in %s", user_id, room_id)

        user = UserID.from_string(user_id)

        if change == "left":
            user_left_room(self.distributor, user, room_id)
        else:
            raise Exception("Unrecognized change: %r", change)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationRemoteJoinRestServlet(hs).register(http_server)
    ReplicationRemoteRejectInviteRestServlet(hs).register(http_server)
    ReplicationUserJoinedLeftRoomRestServlet(hs).register(http_server)
    ReplicationRemoteKnockRestServlet(hs).register(http_server)
    ReplicationRemoteRescindKnockRestServlet(hs).register(http_server)
