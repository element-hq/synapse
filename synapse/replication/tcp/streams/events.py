#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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
import heapq
from collections import defaultdict
from typing import TYPE_CHECKING, Iterable, Optional, Tuple, Type, TypeVar, cast

import attr

from synapse.replication.tcp.streams._base import (
    StreamRow,
    StreamUpdateResult,
    Token,
    _StreamFromIdGen,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

"""Handling of the 'events' replication stream

This stream contains rows of various types. Each row therefore contains a 'type'
identifier before the real data. For example::

    RDATA events batch ["state", ["!room:id", "m.type", "", "$event:id"]]
    RDATA events 12345 ["ev", ["$event:id", "!room:id", "m.type", null, null]]

An "ev" row is sent for each new event. The fields in the data part are:

 * The new event id
 * The room id for the event
 * The type of the new event
 * The state key of the event, for state events
 * The event id of an event which is redacted by this event.

A "state" row is sent whenever the "current state" in a room changes. The fields in the
data part are:

 * The room id for the state change
 * The event type of the state which has changed
 * The state_key of the state which has changed
 * The event id of the new state

A "state-all" row is sent whenever the "current state" in a room changes, but there are
too many state updates for a particular room in the same update. This replaces any
"state" rows on a per-room basis. The fields in the data part are:

* The room id for the state changes

"""

# Any room with more than _MAX_STATE_UPDATES_PER_ROOM will send a EventsStreamAllStateRow
# instead of individual EventsStreamEventRow. This is predominantly useful when
# purging large rooms.
_MAX_STATE_UPDATES_PER_ROOM = 150


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventsStreamRow:
    """A parsed row from the events replication stream"""

    type: str  # the TypeId of one of the *EventsStreamRows
    data: "BaseEventsStreamRow"


T = TypeVar("T", bound="BaseEventsStreamRow")


class BaseEventsStreamRow:
    """Base class for rows to be sent in the events stream.

    Specifies how to identify, serialize and deserialize the different types.
    """

    # Unique string that ids the type. Must be overridden in sub classes.
    TypeId: str

    @classmethod
    def from_data(cls: Type[T], data: Iterable[Optional[str]]) -> T:
        """Parse the data from the replication stream into a row.

        By default we just call the constructor with the data list as arguments

        Args:
            data: The value of the data object from the replication stream
        """
        return cls(*data)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventsStreamEventRow(BaseEventsStreamRow):
    TypeId = "ev"

    event_id: str
    room_id: str
    type: str
    state_key: Optional[str]
    redacts: Optional[str]
    relates_to: Optional[str]
    membership: Optional[str]
    rejected: bool
    outlier: bool


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventsStreamCurrentStateRow(BaseEventsStreamRow):
    TypeId = "state"

    room_id: str
    type: str
    state_key: str
    event_id: Optional[str]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventsStreamAllStateRow(BaseEventsStreamRow):
    TypeId = "state-all"

    room_id: str


_EventRows: Tuple[Type[BaseEventsStreamRow], ...] = (
    EventsStreamEventRow,
    EventsStreamCurrentStateRow,
    EventsStreamAllStateRow,
)

TypeToRow = {Row.TypeId: Row for Row in _EventRows}


class EventsStream(_StreamFromIdGen):
    """We received a new event, or an event went from being an outlier to not"""

    NAME = "events"

    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        super().__init__(
            hs.get_instance_name(), self._update_function, self._store._stream_id_gen
        )

    async def _update_function(
        self,
        instance_name: str,
        from_token: Token,
        current_token: Token,
        target_row_count: int,
    ) -> StreamUpdateResult:
        # The events stream cannot be "reset", so its safe to return early if
        # the from token is larger than the current token (the DB query will
        # trivially return 0 rows anyway).
        if from_token >= current_token:
            return [], current_token, False

        # the events stream merges together three separate sources:
        #  * new events
        #  * current_state changes
        #  * events which were previously outliers, but have now been de-outliered.
        #
        # The merge operation is complicated by the fact that we only have a single
        # "stream token" which is supposed to indicate how far we have got through
        # all three streams. It's therefore no good to return rows 1-1000 from the
        # "new events" table if the state_deltas are limited to rows 1-100 by the
        # target_row_count.
        #
        # In other words: we must pick a new upper limit, and must return *all* rows
        # up to that point for each of the three sources.
        #
        # Start by trying to split the target_row_count up. We expect to have a
        # negligible number of ex-outliers, and a rough approximation based on recent
        # traffic on sw1v.org shows that there are approximately the same number of
        # event rows between a given pair of stream ids as there are state
        # updates, so let's split our target_row_count among those two types. The target
        # is only an approximation - it doesn't matter if we end up going a bit over it.

        target_row_count //= 2

        # now we fetch up to that many rows from the events table

        event_rows = await self._store.get_all_new_forward_event_rows(
            instance_name, from_token, current_token, target_row_count
        )

        # we rely on get_all_new_forward_event_rows strictly honouring the limit, so
        # that we know it is safe to just take upper_limit = event_rows[-1][0].
        assert (
            len(event_rows) <= target_row_count
        ), "get_all_new_forward_event_rows did not honour row limit"

        # if we hit the limit on event_updates, there's no point in going beyond the
        # last stream_id in the batch for the other sources.

        if len(event_rows) == target_row_count:
            limited = True
            upper_limit: int = event_rows[-1][0]
        else:
            limited = False
            upper_limit = current_token

        # next up is the state delta table.
        (
            state_rows,
            upper_limit,
            state_rows_limited,
        ) = await self._store.get_all_updated_current_state_deltas(
            instance_name, from_token, upper_limit, target_row_count
        )

        limited = limited or state_rows_limited

        # finally, fetch the ex-outliers rows. We assume there are few enough of these
        # not to bother with the limit.

        ex_outliers_rows = await self._store.get_ex_outlier_stream_rows(
            instance_name, from_token, upper_limit
        )

        # we now need to turn the raw database rows returned into tuples suitable
        # for the replication protocol (basically, we add an identifier to
        # distinguish the row type). At the same time, we can limit the event_rows
        # to the max stream_id from state_rows.

        event_updates: Iterable[Tuple[int, Tuple]] = (
            (stream_id, (EventsStreamEventRow.TypeId, rest))
            for (stream_id, *rest) in event_rows
            if stream_id <= upper_limit
        )

        # Separate out rooms that have many state updates, listeners should clear
        # all state for those rooms.
        state_updates_by_room = defaultdict(list)
        for stream_id, room_id, _type, _state_key, _event_id in state_rows:
            state_updates_by_room[room_id].append(stream_id)

        state_all_rows = [
            (stream_ids[-1], room_id)
            for room_id, stream_ids in state_updates_by_room.items()
            if len(stream_ids) >= _MAX_STATE_UPDATES_PER_ROOM
        ]
        state_all_updates: Iterable[Tuple[int, Tuple]] = (
            (max_stream_id, (EventsStreamAllStateRow.TypeId, (room_id,)))
            for (max_stream_id, room_id) in state_all_rows
        )

        # Any remaining state updates are sent individually.
        state_all_rooms = {room_id for _, room_id in state_all_rows}
        state_updates: Iterable[Tuple[int, Tuple]] = (
            (stream_id, (EventsStreamCurrentStateRow.TypeId, rest))
            for (stream_id, *rest) in state_rows
            if rest[0] not in state_all_rooms
        )

        ex_outliers_updates: Iterable[Tuple[int, Tuple]] = (
            (stream_id, (EventsStreamEventRow.TypeId, rest))
            for (stream_id, *rest) in ex_outliers_rows
        )

        # we need to return a sorted list, so merge them together.
        updates = list(
            heapq.merge(
                event_updates, state_all_updates, state_updates, ex_outliers_updates
            )
        )
        return updates, upper_limit, limited

    @classmethod
    def parse_row(cls, row: StreamRow) -> "EventsStreamRow":
        (typ, data) = cast(Tuple[str, Iterable[Optional[str]]], row)
        event_stream_row_data = TypeToRow[typ].from_data(data)
        return EventsStreamRow(typ, event_stream_row_data)
