#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
import collections.abc
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

import attr

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.api.errors import NotFoundError, UnsupportedRoomVersionError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersion
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.logging.opentracing import trace
from synapse.replication.tcp.streams import UnPartialStatedEventStream
from synapse.replication.tcp.streams.partial_state import UnPartialStatedEventStreamRow
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.databases.main.roommember import RoomMemberWorkerStore
from synapse.types import JsonDict, JsonMapping, StateKey, StateMap, StrCollection
from synapse.types.state import StateFilter
from synapse.util.caches import intern_string
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.cancellation import cancellable
from synapse.util.iterutils import batch_iter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

MAX_STATE_DELTA_HOPS = 100


# Freeze so it's immutable and we can use it as a cache value
@attr.s(slots=True, frozen=True, auto_attribs=True)
class Sentinel:
    pass


ROOM_UNKNOWN_SENTINEL = Sentinel()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventMetadata:
    """Returned by `get_metadata_for_events`"""

    room_id: str
    event_type: str
    state_key: Optional[str]
    rejection_reason: Optional[str]


def _retrieve_and_check_room_version(room_id: str, room_version_id: str) -> RoomVersion:
    v = KNOWN_ROOM_VERSIONS.get(room_version_id)
    if not v:
        raise UnsupportedRoomVersionError(
            "Room %s uses a room version %s which is no longer supported"
            % (room_id, room_version_id)
        )
    return v


# this inherits from EventsWorkerStore because it calls self.get_events
class StateGroupWorkerStore(EventsWorkerStore, SQLBaseStore):
    """The parts of StateGroupStore that can be called from workers."""

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self._instance_name: str = hs.get_instance_name()

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == UnPartialStatedEventStream.NAME:
            for row in rows:
                assert isinstance(row, UnPartialStatedEventStreamRow)
                self._get_state_group_for_event.invalidate((row.event_id,))
                self.is_partial_state_event.invalidate((row.event_id,))

        super().process_replication_rows(stream_name, instance_name, token, rows)

    async def get_room_version(self, room_id: str) -> RoomVersion:
        """Get the room_version of a given room
        Raises:
            NotFoundError: if the room is unknown
            UnsupportedRoomVersionError: if the room uses an unknown room version.
                Typically this happens if support for the room's version has been
                removed from Synapse.
        """
        room_version_id = await self.get_room_version_id(room_id)
        return _retrieve_and_check_room_version(room_id, room_version_id)

    def get_room_version_txn(
        self, txn: LoggingTransaction, room_id: str
    ) -> RoomVersion:
        """Get the room_version of a given room
        Args:
            txn: Transaction object
            room_id: The room_id of the room you are trying to get the version for
        Raises:
            NotFoundError: if the room is unknown
            UnsupportedRoomVersionError: if the room uses an unknown room version.
                Typically this happens if support for the room's version has been
                removed from Synapse.
        """
        room_version_id = self.get_room_version_id_txn(txn, room_id)
        return _retrieve_and_check_room_version(room_id, room_version_id)

    @cached(max_entries=10000)
    async def get_room_version_id(self, room_id: str) -> str:
        """Get the room_version of a given room
        Raises:
            NotFoundError: if the room is unknown
        """
        return await self.db_pool.runInteraction(
            "get_room_version_id_txn",
            self.get_room_version_id_txn,
            room_id,
        )

    def get_room_version_id_txn(self, txn: LoggingTransaction, room_id: str) -> str:
        """Get the room_version of a given room
        Args:
            txn: Transaction object
            room_id: The room_id of the room you are trying to get the version for
        Raises:
            NotFoundError: if the room is unknown
        """

        # We really should have an entry in the rooms table for every room we
        # care about, but let's be a bit paranoid.
        room_version = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="rooms",
            keyvalues={"room_id": room_id},
            retcol="room_version",
            allow_none=True,
        )

        if room_version is None:
            raise NotFoundError("Could not find room_version for %s" % (room_id,))

        return room_version

    @trace
    async def get_metadata_for_events(
        self, event_ids: Collection[str]
    ) -> Dict[str, EventMetadata]:
        """Get some metadata (room_id, type, state_key) for the given events.

        This method is a faster alternative than fetching the full events from
        the DB, and should be used when the full event is not needed.

        Returns metadata for rejected and redacted events. Events that have not
        been persisted are omitted from the returned dict.
        """

        def get_metadata_for_events_txn(
            txn: LoggingTransaction,
            batch_ids: Collection[str],
        ) -> Dict[str, EventMetadata]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "e.event_id", batch_ids
            )

            sql = f"""
                SELECT e.event_id, e.room_id, e.type, se.state_key, r.reason
                FROM events AS e
                LEFT JOIN state_events se USING (event_id)
                LEFT JOIN rejections r USING (event_id)
                WHERE {clause}
            """

            txn.execute(sql, args)
            return {
                event_id: EventMetadata(
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    rejection_reason=rejection_reason,
                )
                for event_id, room_id, event_type, state_key, rejection_reason in txn
            }

        result_map: Dict[str, EventMetadata] = {}
        for batch_ids in batch_iter(event_ids, 1000):
            result_map.update(
                await self.db_pool.runInteraction(
                    "get_metadata_for_events",
                    get_metadata_for_events_txn,
                    batch_ids=batch_ids,
                )
            )

        return result_map

    async def get_room_predecessor(self, room_id: str) -> Optional[JsonMapping]:
        """Get the predecessor of an upgraded room if it exists.
        Otherwise return None.

        Args:
            room_id: The room ID.

        Returns:
            A dictionary containing the structure of the predecessor
            field from the room's create event. The structure is subject to other servers,
            but it is expected to be:
                * room_id (str): The room ID of the predecessor room
                * event_id (str): The ID of the tombstone event in the predecessor room

            None if a predecessor key is not found, or is not a dictionary.

        Raises:
            NotFoundError if the given room is unknown
        """
        # Retrieve the room's create event
        create_event = await self.get_create_event_for_room(room_id)

        # Retrieve the predecessor key of the create event
        predecessor = create_event.content.get("predecessor", None)

        # Ensure the key is a dictionary
        if not isinstance(predecessor, collections.abc.Mapping):
            return None

        # The keys must be strings since the data is JSON.
        return predecessor

    async def get_create_event_for_room(self, room_id: str) -> EventBase:
        """Get the create state event for a room.

        Args:
            room_id: The room ID.

        Returns:
            The room creation event.

        Raises:
            NotFoundError if the room is unknown
        """
        state_ids = await self.get_partial_current_state_ids(room_id)

        if not state_ids:
            raise NotFoundError(f"Current state for room {room_id} is empty")

        create_id = state_ids.get((EventTypes.Create, ""))

        # If we can't find the create event, assume we've hit a dead end
        if not create_id:
            raise NotFoundError(f"No create event in current state for room {room_id}")

        # Retrieve the room's create event and return
        create_event = await self.get_event(create_id)
        return create_event

    @cached(max_entries=10000)
    async def get_room_type(self, room_id: str) -> Optional[str]:
        raise NotImplementedError()

    @cachedList(cached_method_name="get_room_type", list_name="room_ids")
    async def bulk_get_room_type(
        self, room_ids: Set[str]
    ) -> Mapping[str, Union[Optional[str], Sentinel]]:
        """
        Bulk fetch room types for the given rooms (via current state).

        Since this function is cached, any missing values would be cached as `None`. In
        order to distinguish between an unencrypted room that has `None` encryption and
        a room that is unknown to the server where we might want to omit the value
        (which would make it cached as `None`), instead we use the sentinel value
        `ROOM_UNKNOWN_SENTINEL`.

        Returns:
            A mapping from room ID to the room's type (`None` is a valid room type).
            Rooms unknown to this server will return `ROOM_UNKNOWN_SENTINEL`.
        """

        def txn(
            txn: LoggingTransaction,
        ) -> MutableMapping[str, Union[Optional[str], Sentinel]]:
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "room_id", room_ids
            )

            # We can't rely on `room_stats_state.room_type` if the server has left the
            # room because the `room_id` will still be in the table but everything will
            # be set to `None` but `None` is a valid room type value. We join against
            # the `room_stats_current` table which keeps track of the
            # `current_state_events` count (and a proxy value `local_users_in_room`
            # which can used to assume the server is participating in the room and has
            # current state) to ensure that the data in `room_stats_state` is up-to-date
            # with the current state.
            #
            # FIXME: Use `room_stats_current.current_state_events` instead of
            # `room_stats_current.local_users_in_room` once
            # https://github.com/element-hq/synapse/issues/17457 is fixed.
            sql = f"""
                SELECT room_id, room_type
                FROM room_stats_state
                INNER JOIN room_stats_current USING (room_id)
                WHERE
                    {clause}
                    AND local_users_in_room > 0
            """

            txn.execute(sql, args)

            room_id_to_type_map = {}
            for row in txn:
                room_id_to_type_map[row[0]] = row[1]

            return room_id_to_type_map

        results = await self.db_pool.runInteraction(
            "bulk_get_room_type",
            txn,
        )

        # If we haven't updated `room_stats_state` with the room yet, query the
        # create events directly. This should happen only rarely so we don't
        # mind if we do this in a loop.
        for room_id in room_ids - results.keys():
            try:
                create_event = await self.get_create_event_for_room(room_id)
                room_type = create_event.content.get(EventContentFields.ROOM_TYPE)
                results[room_id] = room_type
            except NotFoundError:
                # We use the sentinel value to distinguish between `None` which is a
                # valid room type and a room that is unknown to the server so the value
                # is just unset.
                results[room_id] = ROOM_UNKNOWN_SENTINEL

        return results

    @cached(max_entries=10000)
    async def get_room_encryption(self, room_id: str) -> Optional[str]:
        raise NotImplementedError()

    @cachedList(cached_method_name="get_room_encryption", list_name="room_ids")
    async def bulk_get_room_encryption(
        self, room_ids: Set[str]
    ) -> Mapping[str, Union[Optional[str], Sentinel]]:
        """
        Bulk fetch room encryption for the given rooms (via current state).

        Since this function is cached, any missing values would be cached as `None`. In
        order to distinguish between an unencrypted room that has `None` encryption and
        a room that is unknown to the server where we might want to omit the value
        (which would make it cached as `None`), instead we use the sentinel value
        `ROOM_UNKNOWN_SENTINEL`.

        Returns:
            A mapping from room ID to the room's encryption algorithm if the room is
            encrypted, otherwise `None`. Rooms unknown to this server will return
            `ROOM_UNKNOWN_SENTINEL`.
        """

        def txn(
            txn: LoggingTransaction,
        ) -> MutableMapping[str, Union[Optional[str], Sentinel]]:
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "room_id", room_ids
            )

            # We can't rely on `room_stats_state.encryption` if the server has left the
            # room because the `room_id` will still be in the table but everything will
            # be set to `None` but `None` is a valid encryption value. We join against
            # the `room_stats_current` table which keeps track of the
            # `current_state_events` count (and a proxy value `local_users_in_room`
            # which can used to assume the server is participating in the room and has
            # current state) to ensure that the data in `room_stats_state` is up-to-date
            # with the current state.
            #
            # FIXME: Use `room_stats_current.current_state_events` instead of
            # `room_stats_current.local_users_in_room` once
            # https://github.com/element-hq/synapse/issues/17457 is fixed.
            sql = f"""
                SELECT room_id, encryption
                FROM room_stats_state
                INNER JOIN room_stats_current USING (room_id)
                WHERE
                    {clause}
                    AND local_users_in_room > 0
            """

            txn.execute(sql, args)

            room_id_to_encryption_map = {}
            for row in txn:
                room_id_to_encryption_map[row[0]] = row[1]

            return room_id_to_encryption_map

        results = await self.db_pool.runInteraction(
            "bulk_get_room_encryption",
            txn,
        )

        # If we haven't updated `room_stats_state` with the room yet, query the state
        # directly. This should happen only rarely so we don't mind if we do this in a
        # loop.
        encryption_event_ids: List[str] = []
        for room_id in room_ids - results.keys():
            state_map = await self.get_partial_filtered_current_state_ids(
                room_id,
                state_filter=StateFilter.from_types(
                    [
                        (EventTypes.Create, ""),
                        (EventTypes.RoomEncryption, ""),
                    ]
                ),
            )
            # We can use the create event as a canary to tell whether the server has
            # seen the room before
            create_event_id = state_map.get((EventTypes.Create, ""))
            encryption_event_id = state_map.get((EventTypes.RoomEncryption, ""))

            if create_event_id is None:
                # We use the sentinel value to distinguish between `None` which is a
                # valid room type and a room that is unknown to the server so the value
                # is just unset.
                results[room_id] = ROOM_UNKNOWN_SENTINEL
                continue

            if encryption_event_id is None:
                results[room_id] = None
            else:
                encryption_event_ids.append(encryption_event_id)

        encryption_event_map = await self.get_events(encryption_event_ids)

        for encryption_event_id in encryption_event_ids:
            encryption_event = encryption_event_map.get(encryption_event_id)
            # If the curent state says there is an encryption event, we should have it
            # in the database.
            assert encryption_event is not None

            results[encryption_event.room_id] = encryption_event.content.get(
                EventContentFields.ENCRYPTION_ALGORITHM
            )

        return results

    @cached(max_entries=100000, iterable=True)
    async def get_partial_current_state_ids(self, room_id: str) -> StateMap[str]:
        """Get the current state event ids for a room based on the
        current_state_events table.

        This may be the partial state if we're lazy joining the room.

        Args:
            room_id: The room to get the state IDs of.

        Returns:
            The current state of the room.
        """

        def _get_current_state_ids_txn(txn: LoggingTransaction) -> StateMap[str]:
            txn.execute(
                """SELECT type, state_key, event_id FROM current_state_events
                WHERE room_id = ?
                """,
                (room_id,),
            )

            return {(intern_string(r[0]), intern_string(r[1])): r[2] for r in txn}

        return await self.db_pool.runInteraction(
            "get_partial_current_state_ids", _get_current_state_ids_txn
        )

    async def check_if_events_in_current_state(
        self, event_ids: StrCollection
    ) -> FrozenSet[str]:
        """Checks and returns which of the given events is part of the current state."""
        rows = await self.db_pool.simple_select_many_batch(
            table="current_state_events",
            column="event_id",
            iterable=event_ids,
            retcols=("event_id",),
            desc="check_if_events_in_current_state",
        )

        return frozenset(event_id for (event_id,) in rows)

    # FIXME: how should this be cached?
    @cancellable
    async def get_partial_filtered_current_state_ids(
        self, room_id: str, state_filter: Optional[StateFilter] = None
    ) -> StateMap[str]:
        """Get the current state event of a given type for a room based on the
        current_state_events table.  This may not be as up-to-date as the result
        of doing a fresh state resolution as per state_handler.get_current_state

        This may be the partial state if we're lazy joining the room.

        Args:
            room_id
            state_filter: The state filter used to fetch state
                from the database.

        Returns:
            Map from type/state_key to event ID.
        """

        where_clause, where_args = (
            state_filter or StateFilter.all()
        ).make_sql_filter_clause()

        if not where_clause:
            # We delegate to the cached version
            return await self.get_partial_current_state_ids(room_id)

        def _get_filtered_current_state_ids_txn(
            txn: LoggingTransaction,
        ) -> StateMap[str]:
            results = StateMapWrapper(state_filter=state_filter or StateFilter.all())

            sql = """
                SELECT type, state_key, event_id FROM current_state_events
                WHERE room_id = ?
            """

            if where_clause:
                sql += " AND (%s)" % (where_clause,)

            args = [room_id]
            args.extend(where_args)
            txn.execute(sql, args)
            for row in txn:
                typ, state_key, event_id = row
                key = (intern_string(typ), intern_string(state_key))
                results[key] = event_id

            return results

        return await self.db_pool.runInteraction(
            "get_filtered_current_state_ids", _get_filtered_current_state_ids_txn
        )

    @cached(max_entries=50000)
    async def _get_state_group_for_event(self, event_id: str) -> Optional[int]:
        return await self.db_pool.simple_select_one_onecol(
            table="event_to_state_groups",
            keyvalues={"event_id": event_id},
            retcol="state_group",
            allow_none=True,
            desc="_get_state_group_for_event",
        )

    @cachedList(
        cached_method_name="_get_state_group_for_event",
        list_name="event_ids",
        num_args=1,
    )
    async def _get_state_group_for_events(
        self, event_ids: Collection[str]
    ) -> Mapping[str, int]:
        """Returns mapping event_id -> state_group.

        Raises:
             RuntimeError if the state is unknown at any of the given events
        """
        rows = cast(
            List[Tuple[str, int]],
            await self.db_pool.simple_select_many_batch(
                table="event_to_state_groups",
                column="event_id",
                iterable=event_ids,
                keyvalues={},
                retcols=("event_id", "state_group"),
                desc="_get_state_group_for_events",
            ),
        )

        res = dict(rows)
        for e in event_ids:
            if e not in res:
                raise RuntimeError("No state group for unknown or outlier event %s" % e)
        return res

    async def get_referenced_state_groups(
        self, state_groups: Iterable[int]
    ) -> Set[int]:
        """Check if the state groups are referenced by events.

        Args:
            state_groups

        Returns:
            The subset of state groups that are referenced.
        """

        rows = cast(
            List[Tuple[int]],
            await self.db_pool.simple_select_many_batch(
                table="event_to_state_groups",
                column="state_group",
                iterable=state_groups,
                keyvalues={},
                retcols=("DISTINCT state_group",),
                desc="get_referenced_state_groups",
            ),
        )

        return {row[0] for row in rows}

    async def update_state_for_partial_state_event(
        self,
        event: EventBase,
        context: EventContext,
    ) -> None:
        """Update the state group for a partial state event"""
        async with self._un_partial_stated_events_stream_id_gen.get_next() as un_partial_state_event_stream_id:
            await self.db_pool.runInteraction(
                "update_state_for_partial_state_event",
                self._update_state_for_partial_state_event_txn,
                event,
                context,
                un_partial_state_event_stream_id,
            )

    def _update_state_for_partial_state_event_txn(
        self,
        txn: LoggingTransaction,
        event: EventBase,
        context: EventContext,
        un_partial_state_event_stream_id: int,
    ) -> None:
        # we shouldn't have any outliers here
        assert not event.internal_metadata.is_outlier()

        # anything that was rejected should have the same state as its
        # predecessor.
        if context.rejected:
            state_group = context.state_group_before_event
        else:
            state_group = context.state_group

        self.db_pool.simple_update_txn(
            txn,
            table="event_to_state_groups",
            keyvalues={"event_id": event.event_id},
            updatevalues={"state_group": state_group},
        )

        # the event may now be rejected where it was not before, or vice versa,
        # in which case we need to update the rejected flags.
        rejection_status_changed = bool(context.rejected) != (
            event.rejected_reason is not None
        )
        if rejection_status_changed:
            self.mark_event_rejected_txn(txn, event.event_id, context.rejected)

        self.db_pool.simple_delete_one_txn(
            txn,
            table="partial_state_events",
            keyvalues={"event_id": event.event_id},
        )

        txn.call_after(self.is_partial_state_event.invalidate, (event.event_id,))
        txn.call_after(
            self._get_state_group_for_event.prefill,
            (event.event_id,),
            state_group,
        )

        self.db_pool.simple_insert_txn(
            txn,
            "un_partial_stated_event_stream",
            {
                "stream_id": un_partial_state_event_stream_id,
                "instance_name": self._instance_name,
                "event_id": event.event_id,
                "rejection_status_changed": rejection_status_changed,
            },
        )
        txn.call_after(self.hs.get_notifier().on_new_replication_data)


class MainStateBackgroundUpdateStore(RoomMemberWorkerStore):
    CURRENT_STATE_INDEX_UPDATE_NAME = "current_state_members_idx"
    EVENT_STATE_GROUP_INDEX_UPDATE_NAME = "event_to_state_groups_sg_index"
    DELETE_CURRENT_STATE_UPDATE_NAME = "delete_old_current_state_events"
    MEMBERS_CURRENT_STATE_UPDATE_NAME = "current_state_events_members_room_index"

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.server_name: str = hs.hostname

        self.db_pool.updates.register_background_index_update(
            self.CURRENT_STATE_INDEX_UPDATE_NAME,
            index_name="current_state_events_member_index",
            table="current_state_events",
            columns=["state_key"],
            where_clause="type='m.room.member'",
        )
        self.db_pool.updates.register_background_index_update(
            self.EVENT_STATE_GROUP_INDEX_UPDATE_NAME,
            index_name="event_to_state_groups_sg_index",
            table="event_to_state_groups",
            columns=["state_group"],
        )
        self.db_pool.updates.register_background_update_handler(
            self.DELETE_CURRENT_STATE_UPDATE_NAME,
            self._background_remove_left_rooms,
        )
        self.db_pool.updates.register_background_index_update(
            self.MEMBERS_CURRENT_STATE_UPDATE_NAME,
            index_name="current_state_events_members_room_index",
            table="current_state_events",
            columns=["room_id", "membership"],
            where_clause="type='m.room.member'",
        )

    async def _background_remove_left_rooms(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Background update to delete rows from `current_state_events` and
        `event_forward_extremities` tables of rooms that the server is no
        longer joined to.
        """

        last_room_id = progress.get("last_room_id", "")

        def _background_remove_left_rooms_txn(
            txn: LoggingTransaction,
        ) -> Tuple[bool, Set[str]]:
            # get a batch of room ids to consider
            sql = """
                SELECT DISTINCT room_id FROM current_state_events
                WHERE room_id > ? ORDER BY room_id LIMIT ?
            """

            txn.execute(sql, (last_room_id, batch_size))
            room_ids = [row[0] for row in txn]
            if not room_ids:
                return True, set()

            ###########################################################################
            #
            # exclude rooms where we have active members

            sql = """
                SELECT room_id
                FROM local_current_membership
                WHERE
                    room_id > ? AND room_id <= ?
                    AND membership = 'join'
                GROUP BY room_id
            """

            txn.execute(sql, (last_room_id, room_ids[-1]))
            joined_room_ids = {row[0] for row in txn}
            to_delete = set(room_ids) - joined_room_ids

            ###########################################################################
            #
            # exclude rooms which we are in the process of constructing; these otherwise
            # qualify as "rooms with no local users", and would have their
            # forward extremities cleaned up.

            # the following query will return a list of rooms which have forward
            # extremities that are *not* also the create event in the room - ie
            # those that are not being created currently.

            sql = """
                SELECT DISTINCT efe.room_id
                FROM event_forward_extremities efe
                LEFT JOIN current_state_events cse ON
                    cse.event_id = efe.event_id
                    AND cse.type = 'm.room.create'
                    AND cse.state_key = ''
                WHERE
                    cse.event_id IS NULL
                    AND efe.room_id > ? AND efe.room_id <= ?
            """

            txn.execute(sql, (last_room_id, room_ids[-1]))

            # build a set of those rooms within `to_delete` that do not appear in
            # the above, leaving us with the rooms in `to_delete` that *are* being
            # created.
            creating_rooms = to_delete.difference(row[0] for row in txn)
            logger.info("skipping rooms which are being created: %s", creating_rooms)

            # now remove the rooms being created from the list of those to delete.
            #
            # (we could have just taken the intersection of `to_delete` with the result
            # of the sql query, but it's useful to be able to log `creating_rooms`; and
            # having done so, it's quicker to remove the (few) creating rooms from
            # `to_delete` than it is to form the intersection with the (larger) list of
            # not-creating-rooms)

            to_delete -= creating_rooms

            ###########################################################################
            #
            # now clear the state for the rooms

            logger.info("Deleting current state left rooms: %r", to_delete)

            # First we get all users that we still think were joined to the
            # room. This is so that we can mark those device lists as
            # potentially stale, since there may have been a period where the
            # server didn't share a room with the remote user and therefore may
            # have missed any device updates.
            rows = cast(
                List[Tuple[str]],
                self.db_pool.simple_select_many_txn(
                    txn,
                    table="current_state_events",
                    column="room_id",
                    iterable=to_delete,
                    keyvalues={
                        "type": EventTypes.Member,
                        "membership": Membership.JOIN,
                    },
                    retcols=("state_key",),
                ),
            )

            potentially_left_users = {row[0] for row in rows}

            # Now lets actually delete the rooms from the DB.
            self.db_pool.simple_delete_many_txn(
                txn,
                table="current_state_events",
                column="room_id",
                values=to_delete,
                keyvalues={},
            )

            self.db_pool.simple_delete_many_txn(
                txn,
                table="event_forward_extremities",
                column="room_id",
                values=to_delete,
                keyvalues={},
            )

            self.db_pool.updates._background_update_progress_txn(
                txn,
                self.DELETE_CURRENT_STATE_UPDATE_NAME,
                {"last_room_id": room_ids[-1]},
            )

            return False, potentially_left_users

        finished, potentially_left_users = await self.db_pool.runInteraction(
            "_background_remove_left_rooms", _background_remove_left_rooms_txn
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                self.DELETE_CURRENT_STATE_UPDATE_NAME
            )

        # Now go and check if we still share a room with the remote users in
        # the deleted rooms. If not mark their device lists as stale.
        joined_users = await self.get_users_server_still_shares_room_with(
            potentially_left_users
        )

        for user_id in potentially_left_users - joined_users:
            await self.mark_remote_user_device_list_as_unsubscribed(user_id)  # type: ignore[attr-defined]

        return batch_size


class StateStore(StateGroupWorkerStore, MainStateBackgroundUpdateStore):
    """Keeps track of the state at a given event.

    This is done by the concept of `state groups`. Every event is a assigned
    a state group (identified by an arbitrary string), which references a
    collection of state events. The current state of an event is then the
    collection of state events referenced by the event's state group.

    Hence, every change in the current state causes a new state group to be
    generated. However, if no change happens (e.g., if we get a message event
    with only one parent it inherits the state group from its parent.)

    There are three tables:
      * `state_groups`: Stores group name, first event with in the group and
        room id.
      * `event_to_state_groups`: Maps events to state groups.
      * `state_groups_state`: Maps state group to state events.
    """

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)


@attr.s(auto_attribs=True, slots=True)
class StateMapWrapper(Dict[StateKey, str]):
    """A wrapper around a StateMap[str] to ensure that we only query for items
    that were not filtered out.

    This is to help prevent bugs where we filter out state but other bits of the
    code expect the state to be there.
    """

    state_filter: StateFilter

    def __getitem__(self, key: StateKey) -> str:
        if key not in self.state_filter:
            raise Exception("State map was filtered and doesn't include: %s", key)
        return super().__getitem__(key)

    @overload
    def get(self, key: Tuple[str, str]) -> Optional[str]: ...

    @overload
    def get(self, key: Tuple[str, str], default: Union[str, _T]) -> Union[str, _T]: ...

    def get(
        self, key: StateKey, default: Union[str, _T, None] = None
    ) -> Union[str, _T, None]:
        if key not in self.state_filter:
            raise Exception("State map was filtered and doesn't include: %s", key)
        return super().get(key, default)

    def __contains__(self, key: Any) -> bool:
        if key not in self.state_filter:
            raise Exception("State map was filtered and doesn't include: %s", key)

        return super().__contains__(key)
