#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from enum import Enum
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Counter,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

from twisted.internet.defer import DeferredLock

from synapse.api.constants import Direction, EventContentFields, EventTypes, Membership
from synapse.api.errors import StoreError
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.events_worker import InvalidEventError
from synapse.storage.databases.main.state_deltas import StateDeltasStore
from synapse.types import JsonDict
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# these fields track absolutes (e.g. total number of rooms on the server)
# You can think of these as Prometheus Gauges.
# You can draw these stats on a line graph.
# Example: number of users in a room
ABSOLUTE_STATS_FIELDS = {
    "room": (
        "current_state_events",
        "joined_members",
        "invited_members",
        "knocked_members",
        "left_members",
        "banned_members",
        "local_users_in_room",
    ),
    "user": ("joined_rooms",),
}

TYPE_TO_TABLE = {"room": ("room_stats", "room_id"), "user": ("user_stats", "user_id")}

# these are the tables (& ID columns) which contain our actual subjects
TYPE_TO_ORIGIN_TABLE = {"room": ("rooms", "room_id"), "user": ("users", "name")}


class UserSortOrder(Enum):
    """
    Enum to define the sorting method used when returning users
    with get_users_paginate in __init__.py
    and get_users_media_usage_paginate in stats.py

    When moves this to __init__.py gets `builtins.ImportError` with
    `most likely due to a circular import`

    MEDIA_LENGTH = ordered by size of uploaded media.
    MEDIA_COUNT = ordered by number of uploaded media.
    USER_ID = ordered alphabetically by `user_id`.
    NAME = ordered alphabetically by `user_id`. This is for compatibility reasons,
    as the user_id is returned in the name field in the response in list users admin API.
    DISPLAYNAME = ordered alphabetically by `displayname`
    GUEST = ordered by `is_guest`
    ADMIN = ordered by `admin`
    DEACTIVATED = ordered by `deactivated`
    USER_TYPE = ordered alphabetically by `user_type`
    AVATAR_URL = ordered alphabetically by `avatar_url`
    SHADOW_BANNED = ordered by `shadow_banned`
    CREATION_TS = ordered by `creation_ts`
    """

    MEDIA_LENGTH = "media_length"
    MEDIA_COUNT = "media_count"
    USER_ID = "user_id"
    NAME = "name"
    DISPLAYNAME = "displayname"
    GUEST = "is_guest"
    ADMIN = "admin"
    DEACTIVATED = "deactivated"
    USER_TYPE = "user_type"
    AVATAR_URL = "avatar_url"
    SHADOW_BANNED = "shadow_banned"
    CREATION_TS = "creation_ts"
    LAST_SEEN_TS = "last_seen_ts"
    LOCKED = "locked"


class StatsStore(StateDeltasStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.server_name: str = hs.hostname
        self.clock = self.hs.get_clock()
        self.stats_enabled = hs.config.stats.stats_enabled

        self.stats_delta_processing_lock = DeferredLock()

        self.db_pool.updates.register_background_update_handler(
            "populate_stats_process_rooms", self._populate_stats_process_rooms
        )
        self.db_pool.updates.register_background_update_handler(
            "populate_stats_process_users", self._populate_stats_process_users
        )

    async def _populate_stats_process_users(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        This is a background update which regenerates statistics for users.
        """
        if not self.stats_enabled:
            await self.db_pool.updates._end_background_update(
                "populate_stats_process_users"
            )
            return 1

        last_user_id = progress.get("last_user_id", "")

        def _get_next_batch(txn: LoggingTransaction) -> List[str]:
            sql = """
                    SELECT DISTINCT name FROM users
                    WHERE name > ?
                    ORDER BY name ASC
                    LIMIT ?
                """
            txn.execute(sql, (last_user_id, batch_size))
            return [r for (r,) in txn]

        users_to_work_on = await self.db_pool.runInteraction(
            "_populate_stats_process_users", _get_next_batch
        )

        # No more rooms -- complete the transaction.
        if not users_to_work_on:
            await self.db_pool.updates._end_background_update(
                "populate_stats_process_users"
            )
            return 1

        for user_id in users_to_work_on:
            await self._calculate_and_set_initial_state_for_user(user_id)
            progress["last_user_id"] = user_id

        await self.db_pool.runInteraction(
            "populate_stats_process_users",
            self.db_pool.updates._background_update_progress_txn,
            "populate_stats_process_users",
            progress,
        )

        return len(users_to_work_on)

    async def _populate_stats_process_rooms(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """This is a background update which regenerates statistics for rooms."""
        if not self.stats_enabled:
            await self.db_pool.updates._end_background_update(
                "populate_stats_process_rooms"
            )
            return 1

        last_room_id = progress.get("last_room_id", "")

        def _get_next_batch(txn: LoggingTransaction) -> List[str]:
            sql = """
                    SELECT DISTINCT room_id FROM current_state_events
                    WHERE room_id > ?
                    ORDER BY room_id ASC
                    LIMIT ?
                """
            txn.execute(sql, (last_room_id, batch_size))
            return [r for (r,) in txn]

        rooms_to_work_on = await self.db_pool.runInteraction(
            "populate_stats_rooms_get_batch", _get_next_batch
        )

        # No more rooms -- complete the transaction.
        if not rooms_to_work_on:
            await self.db_pool.updates._end_background_update(
                "populate_stats_process_rooms"
            )
            return 1

        for room_id in rooms_to_work_on:
            await self._calculate_and_set_initial_state_for_room(room_id)
            progress["last_room_id"] = room_id

        await self.db_pool.runInteraction(
            "_populate_stats_process_rooms",
            self.db_pool.updates._background_update_progress_txn,
            "populate_stats_process_rooms",
            progress,
        )

        return len(rooms_to_work_on)

    async def get_stats_positions(self) -> int:
        """
        Returns the stats processor positions.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="stats_incremental_position",
            keyvalues={},
            retcol="stream_id",
            desc="stats_incremental_position",
        )

    async def update_room_state(self, room_id: str, fields: Dict[str, Any]) -> None:
        """Update the state of a room.

        fields can contain the following keys with string values:
        * join_rules
        * history_visibility
        * encryption
        * name
        * topic
        * avatar
        * canonical_alias
        * guest_access
        * room_type

        A is_federatable key can also be included with a boolean value.

        Args:
            room_id: The room ID to update the state of.
            fields: The fields to update. This can include a partial list of the
                above fields to only update some room information.
        """
        # Ensure that the values to update are valid, they should be strings and
        # not contain any null bytes.
        #
        # Invalid data gets overwritten with null.
        #
        # Note that a missing value should not be overwritten (it keeps the
        # previous value).
        sentinel = object()
        for col in (
            "join_rules",
            "history_visibility",
            "encryption",
            "name",
            "topic",
            "avatar",
            "canonical_alias",
            "guest_access",
            "room_type",
        ):
            field = fields.get(col, sentinel)
            if field is not sentinel and (not isinstance(field, str) or "\0" in field):
                fields[col] = None

        await self.db_pool.simple_upsert(
            table="room_stats_state",
            keyvalues={"room_id": room_id},
            values=fields,
            desc="update_room_state",
        )

    @cached()
    async def get_earliest_token_for_stats(
        self, stats_type: str, id: str
    ) -> Optional[int]:
        """
        Fetch the "earliest token". This is used by the room stats delta
        processor to ignore deltas that have been processed between the
        start of the background task and any particular room's stats
        being calculated.

        Returns:
            The earliest token.
        """
        table, id_col = TYPE_TO_TABLE[stats_type]

        return await self.db_pool.simple_select_one_onecol(
            "%s_current" % (table,),
            keyvalues={id_col: id},
            retcol="completed_delta_stream_id",
            allow_none=True,
            desc="get_earliest_token_for_stats",
        )

    async def bulk_update_stats_delta(
        self, ts: int, updates: Dict[str, Dict[str, Counter[str]]], stream_id: int
    ) -> None:
        """Bulk update stats tables for a given stream_id and updates the stats
        incremental position.

        Args:
            ts: Current timestamp in ms
            updates: The updates to commit as a mapping of
                stats_type -> stats_id -> field -> delta.
            stream_id: Current position.
        """

        def _bulk_update_stats_delta_txn(txn: LoggingTransaction) -> None:
            for stats_type, stats_updates in updates.items():
                for stats_id, fields in stats_updates.items():
                    logger.debug(
                        "Updating %s stats for %s: %s", stats_type, stats_id, fields
                    )
                    self._update_stats_delta_txn(
                        txn,
                        ts=ts,
                        stats_type=stats_type,
                        stats_id=stats_id,
                        fields=fields,
                        complete_with_stream_id=stream_id,
                    )

            self.db_pool.simple_update_one_txn(
                txn,
                table="stats_incremental_position",
                keyvalues={},
                updatevalues={"stream_id": stream_id},
            )

        await self.db_pool.runInteraction(
            "bulk_update_stats_delta", _bulk_update_stats_delta_txn
        )

    async def update_stats_delta(
        self,
        ts: int,
        stats_type: str,
        stats_id: str,
        fields: Dict[str, int],
        complete_with_stream_id: int,
        absolute_field_overrides: Optional[Dict[str, int]] = None,
    ) -> None:
        """
        Updates the statistics for a subject, with a delta (difference/relative
        change).

        Args:
            ts: timestamp of the change
            stats_type: "room" or "user" – the kind of subject
            stats_id: the subject's ID (room ID or user ID)
            fields: Deltas of stats values.
            complete_with_stream_id:
                If supplied, converts an incomplete row into a complete row,
                with the supplied stream_id marked as the stream_id where the
                row was completed.
            absolute_field_overrides: Current stats values (i.e. not deltas) of
                absolute fields. Does not work with per-slice fields.
        """

        await self.db_pool.runInteraction(
            "update_stats_delta",
            self._update_stats_delta_txn,
            ts,
            stats_type,
            stats_id,
            fields,
            complete_with_stream_id=complete_with_stream_id,
            absolute_field_overrides=absolute_field_overrides,
        )

    def _update_stats_delta_txn(
        self,
        txn: LoggingTransaction,
        ts: int,
        stats_type: str,
        stats_id: str,
        fields: Dict[str, int],
        complete_with_stream_id: int,
        absolute_field_overrides: Optional[Dict[str, int]] = None,
    ) -> None:
        if absolute_field_overrides is None:
            absolute_field_overrides = {}

        table, id_col = TYPE_TO_TABLE[stats_type]

        # Lets be paranoid and check that all the given field names are known
        abs_field_names = ABSOLUTE_STATS_FIELDS[stats_type]
        for field in chain(fields.keys(), absolute_field_overrides.keys()):
            if field not in abs_field_names:
                # guard against potential SQL injection dodginess
                raise ValueError(
                    "%s is not a recognised field"
                    " for stats type %s" % (field, stats_type)
                )

        # Per slice fields do not get added to the _current table

        # This calculates the deltas (`field = field + ?` values)
        # for absolute fields,
        # * defaulting to 0 if not specified
        #     (required for the INSERT part of upserting to work)
        # * omitting overrides specified in `absolute_field_overrides`
        deltas_of_absolute_fields = {
            key: fields.get(key, 0)
            for key in abs_field_names
            if key not in absolute_field_overrides
        }

        # Keep the delta stream ID field up to date
        absolute_field_overrides = absolute_field_overrides.copy()
        absolute_field_overrides["completed_delta_stream_id"] = complete_with_stream_id

        # first upsert the `_current` table
        self._upsert_with_additive_relatives_txn(
            txn=txn,
            table=table + "_current",
            keyvalues={id_col: stats_id},
            absolutes=absolute_field_overrides,
            additive_relatives=deltas_of_absolute_fields,
        )

    def _upsert_with_additive_relatives_txn(
        self,
        txn: LoggingTransaction,
        table: str,
        keyvalues: Dict[str, Any],
        absolutes: Dict[str, Any],
        additive_relatives: Dict[str, int],
    ) -> None:
        """Used to update values in the stats tables.

        This is basically a slightly convoluted upsert that *adds* to any
        existing rows.

        Args:
            table: Table name
            keyvalues: Row-identifying key values
            absolutes: Absolute (set) fields
            additive_relatives: Fields that will be added onto if existing row present.
        """
        absolute_updates = [
            "%(field)s = EXCLUDED.%(field)s" % {"field": field}
            for field in absolutes.keys()
        ]

        relative_updates = [
            "%(field)s = EXCLUDED.%(field)s + COALESCE(%(table)s.%(field)s, 0)"
            % {"table": table, "field": field}
            for field in additive_relatives.keys()
        ]

        insert_cols = []
        qargs = []

        for key, val in chain(
            keyvalues.items(), absolutes.items(), additive_relatives.items()
        ):
            insert_cols.append(key)
            qargs.append(val)

        sql = """
            INSERT INTO %(table)s (%(insert_cols_cs)s)
            VALUES (%(insert_vals_qs)s)
            ON CONFLICT (%(key_columns)s) DO UPDATE SET %(updates)s
        """ % {
            "table": table,
            "insert_cols_cs": ", ".join(insert_cols),
            "insert_vals_qs": ", ".join(
                ["?"] * (len(keyvalues) + len(absolutes) + len(additive_relatives))
            ),
            "key_columns": ", ".join(keyvalues),
            "updates": ", ".join(chain(absolute_updates, relative_updates)),
        }

        txn.execute(sql, qargs)

    async def _calculate_and_set_initial_state_for_room(self, room_id: str) -> None:
        """Calculate and insert an entry into room_stats_current.

        Args:
            room_id: The room ID under calculation.
        """

        def _fetch_current_state_stats(
            txn: LoggingTransaction,
        ) -> Tuple[List[str], Dict[str, int], int, List[str], int]:
            pos = self.get_room_max_stream_ordering()  # type: ignore[attr-defined]

            rows = cast(
                List[Tuple[str]],
                self.db_pool.simple_select_many_txn(
                    txn,
                    table="current_state_events",
                    column="type",
                    iterable=[
                        EventTypes.Create,
                        EventTypes.JoinRules,
                        EventTypes.RoomHistoryVisibility,
                        EventTypes.RoomEncryption,
                        EventTypes.Name,
                        EventTypes.Topic,
                        EventTypes.RoomAvatar,
                        EventTypes.CanonicalAlias,
                    ],
                    keyvalues={"room_id": room_id, "state_key": ""},
                    retcols=["event_id"],
                ),
            )

            event_ids = [row[0] for row in rows]

            txn.execute(
                """
                    SELECT membership, count(*) FROM current_state_events
                    WHERE room_id = ? AND type = 'm.room.member'
                    GROUP BY membership
                """,
                (room_id,),
            )
            membership_counts = dict(cast(Iterable[Tuple[str, int]], txn))

            txn.execute(
                """
                    SELECT COUNT(*) FROM current_state_events
                    WHERE room_id = ?
                """,
                (room_id,),
            )

            current_state_events_count = cast(Tuple[int], txn.fetchone())[0]

            users_in_room = self.get_users_in_room_txn(txn, room_id)  # type: ignore[attr-defined]

            return (
                event_ids,
                membership_counts,
                current_state_events_count,
                users_in_room,
                pos,
            )

        (
            event_ids,
            membership_counts,
            current_state_events_count,
            users_in_room,
            pos,
        ) = await self.db_pool.runInteraction(
            "get_initial_state_for_room", _fetch_current_state_stats
        )

        try:
            state_event_map = await self.get_events(event_ids, get_prev_content=False)  # type: ignore[attr-defined]
        except InvalidEventError as e:
            # If an exception occurs fetching events then the room is broken;
            # skip process it to avoid being stuck on a room.
            logger.warning(
                "Failed to fetch events for room %s, skipping stats calculation: %r.",
                room_id,
                e,
            )
            return

        room_state: Dict[str, Union[None, bool, str]] = {
            "join_rules": None,
            "history_visibility": None,
            "encryption": None,
            "name": None,
            "topic": None,
            "avatar": None,
            "canonical_alias": None,
            "is_federatable": True,
            "room_type": None,
        }

        for event in state_event_map.values():
            if event.type == EventTypes.JoinRules:
                room_state["join_rules"] = event.content.get("join_rule")
            elif event.type == EventTypes.RoomHistoryVisibility:
                room_state["history_visibility"] = event.content.get(
                    "history_visibility"
                )
            elif event.type == EventTypes.RoomEncryption:
                room_state["encryption"] = event.content.get("algorithm")
            elif event.type == EventTypes.Name:
                room_state["name"] = event.content.get("name")
            elif event.type == EventTypes.Topic:
                room_state["topic"] = event.content.get("topic")
            elif event.type == EventTypes.RoomAvatar:
                room_state["avatar"] = event.content.get("url")
            elif event.type == EventTypes.CanonicalAlias:
                room_state["canonical_alias"] = event.content.get("alias")
            elif event.type == EventTypes.Create:
                room_state["is_federatable"] = (
                    event.content.get(EventContentFields.FEDERATE, True) is True
                )
                room_type = event.content.get(EventContentFields.ROOM_TYPE)
                if isinstance(room_type, str):
                    room_state["room_type"] = room_type

        await self.update_room_state(room_id, room_state)

        local_users_in_room = [u for u in users_in_room if self.hs.is_mine_id(u)]

        await self.update_stats_delta(
            ts=self.clock.time_msec(),
            stats_type="room",
            stats_id=room_id,
            fields={},
            complete_with_stream_id=pos,
            absolute_field_overrides={
                "current_state_events": current_state_events_count,
                "joined_members": membership_counts.get(Membership.JOIN, 0),
                "invited_members": membership_counts.get(Membership.INVITE, 0),
                "left_members": membership_counts.get(Membership.LEAVE, 0),
                "banned_members": membership_counts.get(Membership.BAN, 0),
                "knocked_members": membership_counts.get(Membership.KNOCK, 0),
                "local_users_in_room": len(local_users_in_room),
            },
        )

    async def _calculate_and_set_initial_state_for_user(self, user_id: str) -> None:
        def _calculate_and_set_initial_state_for_user_txn(
            txn: LoggingTransaction,
        ) -> Tuple[int, int]:
            pos = self._get_max_stream_id_in_current_state_deltas_txn(txn)

            txn.execute(
                """
                SELECT COUNT(distinct room_id) FROM current_state_events
                    WHERE type = 'm.room.member' AND state_key = ?
                        AND membership = 'join'
                """,
                (user_id,),
            )
            count = cast(Tuple[int], txn.fetchone())[0]
            return count, pos

        joined_rooms, pos = await self.db_pool.runInteraction(
            "calculate_and_set_initial_state_for_user",
            _calculate_and_set_initial_state_for_user_txn,
        )

        await self.update_stats_delta(
            ts=self.clock.time_msec(),
            stats_type="user",
            stats_id=user_id,
            fields={},
            complete_with_stream_id=pos,
            absolute_field_overrides={"joined_rooms": joined_rooms},
        )

    async def get_users_media_usage_paginate(
        self,
        start: int,
        limit: int,
        from_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
        order_by: Optional[str] = UserSortOrder.USER_ID.value,
        direction: Direction = Direction.FORWARDS,
        search_term: Optional[str] = None,
    ) -> Tuple[List[Tuple[str, Optional[str], int, int]], int]:
        """Function to retrieve a paginated list of users and their uploaded local media
        (size and number). This will return a json list of users and the
        total number of users matching the filter criteria.

        Args:
            start: offset to begin the query from
            limit: number of rows to retrieve
            from_ts: request only media that are created later than this timestamp (ms)
            until_ts: request only media that are created earlier than this timestamp (ms)
            order_by: the sort order of the returned list
            direction: sort ascending or descending
            search_term: a string to filter user names by

        Returns:
            A tuple of:
                A list of tuples of user information (the user ID, displayname,
                total number of media, total length of media) and

                An integer representing the total number of users that exist
                given this query
        """

        def get_users_media_usage_paginate_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[Tuple[str, Optional[str], int, int]], int]:
            filters = []
            args: list = []

            if search_term:
                filters.append("(lmr.user_id LIKE ? OR displayname LIKE ?)")
                args.extend(["@%" + search_term + "%:%", "%" + search_term + "%"])

            if from_ts:
                filters.append("created_ts >= ?")
                args.extend([from_ts])
            if until_ts:
                filters.append("created_ts <= ?")
                args.extend([until_ts])

            # Set ordering
            if UserSortOrder(order_by) == UserSortOrder.MEDIA_LENGTH:
                order_by_column = "media_length"
            elif UserSortOrder(order_by) == UserSortOrder.MEDIA_COUNT:
                order_by_column = "media_count"
            elif UserSortOrder(order_by) == UserSortOrder.USER_ID:
                order_by_column = "lmr.user_id"
            elif UserSortOrder(order_by) == UserSortOrder.DISPLAYNAME:
                order_by_column = "displayname"
            else:
                raise StoreError(
                    500, "Incorrect value for order_by provided: %s" % order_by
                )

            if direction == Direction.BACKWARDS:
                order = "DESC"
            else:
                order = "ASC"

            where_clause = "WHERE " + " AND ".join(filters) if len(filters) > 0 else ""

            sql_base = """
                FROM local_media_repository as lmr
                LEFT JOIN profiles AS p ON lmr.user_id = p.full_user_id
                {}
                GROUP BY lmr.user_id, displayname
            """.format(where_clause)

            # SQLite does not support SELECT COUNT(*) OVER()
            sql = """
                SELECT COUNT(*) FROM (
                    SELECT lmr.user_id
                    {sql_base}
                ) AS count_user_ids
            """.format(
                sql_base=sql_base,
            )
            txn.execute(sql, args)
            count = cast(Tuple[int], txn.fetchone())[0]

            sql = """
                SELECT
                    lmr.user_id,
                    displayname,
                    COUNT(lmr.user_id) as media_count,
                    SUM(media_length) as media_length
                    {sql_base}
                ORDER BY {order_by_column} {order}
                LIMIT ? OFFSET ?
            """.format(
                sql_base=sql_base,
                order_by_column=order_by_column,
                order=order,
            )

            args += [limit, start]
            txn.execute(sql, args)
            users = cast(List[Tuple[str, Optional[str], int, int]], txn.fetchall())

            return users, count

        return await self.db_pool.runInteraction(
            "get_users_media_usage_paginate_txn", get_users_media_usage_paginate_txn
        )
