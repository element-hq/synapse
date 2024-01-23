#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING

import attr

from synapse.replication.tcp.streams._base import _StreamFromIdGen

if TYPE_CHECKING:
    from synapse.server import HomeServer


@attr.s(slots=True, frozen=True, auto_attribs=True)
class UnPartialStatedRoomStreamRow:
    # ID of the room that has been un-partial-stated.
    room_id: str


class UnPartialStatedRoomStream(_StreamFromIdGen):
    """
    Stream to notify about rooms becoming un-partial-stated;
    that is, when the background sync finishes such that we now have full state for
    the room.
    """

    NAME = "un_partial_stated_room"
    ROW_TYPE = UnPartialStatedRoomStreamRow

    def __init__(self, hs: "HomeServer"):
        store = hs.get_datastores().main
        super().__init__(
            hs.get_instance_name(),
            store.get_un_partial_stated_rooms_from_stream,
            store._un_partial_stated_rooms_stream_id_gen,
        )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class UnPartialStatedEventStreamRow:
    # ID of the event that has been un-partial-stated.
    event_id: str

    # True iff the rejection status of the event changed as a result of being
    # un-partial-stated.
    rejection_status_changed: bool


class UnPartialStatedEventStream(_StreamFromIdGen):
    """
    Stream to notify about events becoming un-partial-stated.
    """

    NAME = "un_partial_stated_event"
    ROW_TYPE = UnPartialStatedEventStreamRow

    def __init__(self, hs: "HomeServer"):
        store = hs.get_datastores().main
        super().__init__(
            hs.get_instance_name(),
            store.get_un_partial_stated_events_from_stream,
            store._un_partial_stated_events_stream_id_gen,
        )
