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
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import attr

from synapse.api.errors import SlidingSyncUnknownPosition
from synapse.logging.opentracing import trace
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

    # `(user_id, conn_id)` -> `connection_position` -> `PerConnectionState`
    _connections: Dict[Tuple[str, str], Dict[int, PerConnectionState]] = attr.Factory(
        dict
    )

    async def is_valid_token(
        self, sync_config: SlidingSyncConfig, connection_token: int
    ) -> bool:
        """Return whether the connection token is valid/recognized"""
        if connection_token == 0:
            return True

        conn_key = self._get_connection_key(sync_config)
        return connection_token in self._connections.get(conn_key, {})

    async def get_per_connection_state(
        self,
        sync_config: SlidingSyncConfig,
        from_token: Optional[SlidingSyncStreamToken],
    ) -> PerConnectionState:
        """Fetch the per-connection state for the token.

        Raises:
            SlidingSyncUnknownPosition if the connection_token is unknown
        """
        if from_token is None:
            return PerConnectionState()

        connection_position = from_token.connection_position
        if connection_position == 0:
            # Initial sync (request without a `from_token`) starts at `0` so
            # there is no existing per-connection state
            return PerConnectionState()

        conn_key = self._get_connection_key(sync_config)
        sync_statuses = self._connections.get(conn_key, {})
        connection_state = sync_statuses.get(connection_position)

        if connection_state is None:
            raise SlidingSyncUnknownPosition()

        return connection_state

    @trace
    async def record_new_state(
        self,
        sync_config: SlidingSyncConfig,
        from_token: Optional[SlidingSyncStreamToken],
        new_connection_state: MutablePerConnectionState,
    ) -> int:
        """Record updated per-connection state, returning the connection
        position associated with the new state.
        If there are no changes to the state this may return the same token as
        the existing per-connection state.
        """
        prev_connection_token = 0
        if from_token is not None:
            prev_connection_token = from_token.connection_position

        if not new_connection_state.has_updates():
            return prev_connection_token

        conn_key = self._get_connection_key(sync_config)
        sync_statuses = self._connections.setdefault(conn_key, {})

        # Generate a new token, removing any existing entries in that token
        # (which can happen if requests get resent).
        new_store_token = prev_connection_token + 1
        sync_statuses.pop(new_store_token, None)

        # We copy the `MutablePerConnectionState` so that the inner `ChainMap`s
        # don't grow forever.
        sync_statuses[new_store_token] = new_connection_state.copy()

        return new_store_token

    @trace
    async def mark_token_seen(
        self,
        sync_config: SlidingSyncConfig,
        from_token: Optional[SlidingSyncStreamToken],
    ) -> None:
        """We have received a request with the given token, so we can clear out
        any other tokens associated with the connection.

        If there is no from token then we have started afresh, and so we delete
        all tokens associated with the device.
        """
        # Clear out any tokens for the connection that doesn't match the one
        # from the request.

        conn_key = self._get_connection_key(sync_config)
        sync_statuses = self._connections.pop(conn_key, {})
        if from_token is None:
            return

        sync_statuses = {
            connection_token: room_statuses
            for connection_token, room_statuses in sync_statuses.items()
            if connection_token == from_token.connection_position
        }
        if sync_statuses:
            self._connections[conn_key] = sync_statuses

    @staticmethod
    def _get_connection_key(sync_config: SlidingSyncConfig) -> Tuple[str, str]:
        """Return a unique identifier for this connection.

        The first part is simply the user ID.

        The second part is generally a combination of device ID and conn_id.
        However, both these two are optional (e.g. puppet access tokens don't
        have device IDs), so this handles those edge cases.

        We use this over the raw `conn_id` to avoid clashes between different
        clients that use the same `conn_id`. Imagine a user uses a web client
        that uses `conn_id: main_sync_loop` and an Android client that also has
        a `conn_id: main_sync_loop`.
        """

        user_id = sync_config.user.to_string()

        # Only one sliding sync connection is allowed per given conn_id (empty
        # or not).
        conn_id = sync_config.conn_id or ""

        if sync_config.requester.device_id:
            return (user_id, f"D/{sync_config.requester.device_id}/{conn_id}")

        if sync_config.requester.access_token_id:
            # If we don't have a device, then the access token ID should be a
            # stable ID.
            return (user_id, f"A/{sync_config.requester.access_token_id}/{conn_id}")

        # If we have neither then its likely an AS or some weird token. Either
        # way we can just fail here.
        raise Exception("Cannot use sliding sync with access token type")
