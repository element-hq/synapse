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

import logging
from typing import TYPE_CHECKING

import attr

from synapse.logging.opentracing import trace
from synapse.storage.databases.main import DataStore
from synapse.types import SlidingSyncStreamToken
from synapse.types.handlers.sliding_sync import (
    MutablePerConnectionState,
    PerConnectionState,
    SlidingSyncConfig,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True)
class SlidingSyncConnectionStore:
    """In-memory store of per-connection state, including what rooms we have
    previously sent down a sliding sync connection.

    Note: This is NOT safe to run in a worker setup because connection positions will
    point to different sets of rooms on different workers. e.g. for the same connection,
    a connection position of 5 might have totally different states on worker A and
    worker B.

    One complication that we need to deal with here is needing to handle requests being
    resent, i.e. if we sent down a room in a response that the client received, we must
    consider the room *not* sent when we get the request again.

    This is handled by using an integer "token", which is returned to the client
    as part of the sync token. For each connection we store a mapping from
    tokens to the room states, and create a new entry when we send down new
    rooms.

    Note that for any given sliding sync connection we will only store a maximum
    of two different tokens: the previous token from the request and a new token
    sent in the response. When we receive a request with a given token, we then
    clear out all other entries with a different token.

    Attributes:
        _connections: Mapping from `(user_id, conn_id)` to mapping of `token`
            to mapping of room ID to `HaveSentRoom`.
    """

    store: "DataStore"

    async def get_and_clear_connection_positions(
        self,
        sync_config: SlidingSyncConfig,
        from_token: SlidingSyncStreamToken | None,
    ) -> PerConnectionState:
        """Fetch the per-connection state for the token.

        Raises:
            SlidingSyncUnknownPosition if the connection_token is unknown
        """
        # If this is our first request, there is no previous connection state to fetch out of the database
        if from_token is None or from_token.connection_position == 0:
            return PerConnectionState()

        conn_id = sync_config.conn_id or ""

        device_id = sync_config.requester.device_id
        assert device_id is not None

        return await self.store.get_and_clear_connection_positions(
            sync_config.user.to_string(),
            device_id,
            conn_id,
            from_token.connection_position,
        )

    @trace
    async def record_new_state(
        self,
        sync_config: SlidingSyncConfig,
        from_token: SlidingSyncStreamToken | None,
        new_connection_state: MutablePerConnectionState,
    ) -> int:
        """Record updated per-connection state, returning the connection
        position associated with the new state.
        If there are no changes to the state this may return the same token as
        the existing per-connection state.
        """
        if not new_connection_state.has_updates():
            if from_token is not None:
                return from_token.connection_position
            else:
                return 0

        # A from token with a zero connection position means there was no
        # previously stored connection state, so we treat a zero the same as
        # there being no previous position.
        previous_connection_position = None
        if from_token is not None and from_token.connection_position != 0:
            previous_connection_position = from_token.connection_position

        conn_id = sync_config.conn_id or ""

        device_id = sync_config.requester.device_id
        assert device_id is not None

        return await self.store.persist_per_connection_state(
            sync_config.user.to_string(),
            device_id,
            conn_id,
            previous_connection_position,
            new_connection_state,
        )
