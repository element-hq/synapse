#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#
import logging
from typing import TYPE_CHECKING, Optional

from synapse.api.constants import EventTypes, Membership
from synapse.events import JsonDict
from synapse.replication.http.server_notices import (
    ReplicationSendServerNoticeServlet,
)
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class WorkerServerNoticesManager:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._server_notices_mxid = hs.config.servernotices.server_notices_mxid
        self._send_server_notice = ReplicationSendServerNoticeServlet.make_client(hs)

    def is_enabled(self) -> bool:
        """Checks if server notices are enabled on this server."""
        return self._server_notices_mxid is not None

    async def send_notice(
        self,
        user_id: str,
        event_content: dict,
        type: str = EventTypes.Message,
        state_key: Optional[str] = None,
        txn_id: Optional[str] = None,
    ) -> JsonDict:
        """Send a notice to the given user

        Creates the server notices room, if none exists.

        Args:
            user_id: mxid of user to send event to.
            event_content: content of event to send
            type: type of event
            is_state_event: Is the event a state event
            txn_id: The transaction ID.
        """
        return await self._send_server_notice(
            user_id=user_id,
            event_content=event_content,
            type=type,
            state_key=state_key,
            txn_id=txn_id,
        )

    @cached()
    async def maybe_get_notice_room_for_user(self, user_id: str) -> Optional[str]:
        """Try to look up the server notice room for this user if it exists.

        Does not create one if none can be found.

        Args:
            user_id: the user we want a server notice room for.

        Returns:
            The room's ID, or None if no room could be found.
        """
        # If there is no server notices MXID, then there is no server notices room
        if self._server_notices_mxid is None:
            return None

        rooms = await self._store.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.INVITE, Membership.JOIN]
        )
        for room in rooms:
            # it's worth noting that there is an asymmetry here in that we
            # expect the user to be invited or joined, but the system user must
            # be joined. This is kinda deliberate, in that if somebody somehow
            # manages to invite the system user to a room, that doesn't make it
            # the server notices room.
            is_server_notices_room = await self._store.check_local_user_in_room(
                user_id=self._server_notices_mxid, room_id=room.room_id
            )
            if is_server_notices_room:
                # we found a room which our user shares with the system notice
                # user
                return room.room_id

        return None
