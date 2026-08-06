#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Types for Paginated Sync (MSC TBD): a dialect of Simplified Sliding Sync
(MSC4186) without lists/ranges/subscriptions, where the server pages the client
through changed rooms (most recently active first) with bounded responses.

The room results, extensions and per-connection state are shared with sliding
sync; only the request shape and the top-level response differ.
"""

import attr
from pydantic import ConfigDict

from synapse.types import Requester, SlidingSyncStreamToken, UserID
from synapse.types.handlers.sliding_sync import SlidingSyncResult
from synapse.types.rest.client import PaginatedSyncBody


class PaginatedSyncConfig(PaginatedSyncBody):
    """
    Inherit from `PaginatedSyncBody` since we need all of the same fields and add a few
    extra fields that we need in the handler
    """

    user: UserID
    requester: Requester

    # The connection store and room-list helpers are shared with sliding sync
    # and duck-type on the config; these attributes exist so that shared code
    # which checks for lists/subscriptions sees none.
    lists: None = None
    room_subscriptions: None = None

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        # Allow custom types like `UserID` to be used in the model.
        arbitrary_types_allowed=True,
    )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class PaginatedSyncResult:
    """
    The response body for a paginated sync request.

    Attributes:
        next_pos: The next position token to request (same format as sliding sync).
        rooms: A map of room ID to room results, exactly as sliding sync's.
        extensions: Extensions API results, exactly as sliding sync's.
        pending: The number of further rooms with undelivered updates which did
            not fit into `page_size`. While non-zero the client should sync
            again immediately to drain the backlog.
        total_rooms: The total number of rooms in the user's account, for
            cold-start progress reporting.
    """

    next_pos: SlidingSyncStreamToken
    rooms: dict[str, SlidingSyncResult.RoomResult]
    extensions: SlidingSyncResult.Extensions
    pending: int
    total_rooms: int

    def __bool__(self) -> bool:
        """Whether there are any updates that should be returned immediately to
        the client (used by the notifier to decide whether to keep waiting).

        `pending` is included so that a response which delivered nothing but
        knows there is a backlog still returns immediately.
        """
        return bool(self.rooms or self.extensions or self.pending)

    @staticmethod
    def empty(next_pos: SlidingSyncStreamToken) -> "PaginatedSyncResult":
        "Return a new empty result"
        return PaginatedSyncResult(
            next_pos=next_pos,
            rooms={},
            extensions=SlidingSyncResult.Extensions(),
            pending=0,
            total_rooms=0,
        )
