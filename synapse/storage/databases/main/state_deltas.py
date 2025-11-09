#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2018 Vector Creations Ltd
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
from typing import TYPE_CHECKING

import attr

from synapse.logging.opentracing import trace
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.stream import _filter_results_by_stream
from synapse.types import RoomStreamToken, StrCollection
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.iterutils import batch_iter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class StateDelta:
    stream_id: int
    room_id: str
    event_type: str
    state_key: str

    event_id: str | None
    """new event_id for this state key. None if the state has been deleted."""

    prev_event_id: str | None
    """previous event_id for this state key. None if it's new state."""


class StateDeltasStore(SQLBaseStore):
    # This class must be mixed in with a child class which provides the following
    # attribute. TODO: can we get static analysis to enforce this?
    _curr_state_delta_stream_cache: StreamChangeCache

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_index_update(
            update_name="current_state_delta_stream_room_index",
            index_name="current_state_delta_stream_room_idx",
            table="current_state_delta_stream",
            columns=("room_id", "stream_id"),
        )

    async def get_partial_current_state_deltas(
        self, prev_stream_id: int, max_stream_id: int
    ) -> tuple[int, list[StateDelta]]:
        """Fetch a list of room state changes since the given stream id

        This may be the partial state if we're lazy joining the room.

        Args:
            prev_stream_id: point to get changes since (exclusive)
            max_stream_id: the point that we know has been correctly persisted
                - ie, an upper limit to return changes from.

        Returns:
            A tuple consisting of:
                - the stream id which these results go up to
                - list of current_state_delta_stream rows. If it is empty, we are
                  up to date.

            A maximum of 100 rows will be returned.
        """
        prev_stream_id = int(prev_stream_id)

        # check we're not going backwards
        assert prev_stream_id <= max_stream_id, (
            f"New stream id {max_stream_id} is smaller than prev stream id {prev_stream_id}"
        )

        if not self._curr_state_delta_stream_cache.has_any_entity_changed(
            prev_stream_id
        ):
            # if the CSDs haven't changed between prev_stream_id and now, we
            # know for certain that they haven't changed between prev_stream_id and
            # max_stream_id.
            return max_stream_id, []

        def get_current_state_deltas_txn(
            txn: LoggingTransaction,
        ) -> tuple[int, list[StateDelta]]:
            # First we calculate the max stream id that will give us less than
            # N results.
            # We arbitrarily limit to 100 stream_id entries to ensure we don't
            # select toooo many.
            sql = """
                SELECT stream_id, count(*)
                FROM current_state_delta_stream
                WHERE stream_id > ? AND stream_id <= ?
                GROUP BY stream_id
                ORDER BY stream_id ASC
                LIMIT 100
            """
            txn.execute(sql, (prev_stream_id, max_stream_id))

            total = 0

            for stream_id, count in txn:
                total += count
                if total > 100:
                    # We arbitrarily limit to 100 entries to ensure we don't
                    # select toooo many.
                    logger.debug(
                        "Clipping current_state_delta_stream rows to stream_id %i",
                        stream_id,
                    )
                    clipped_stream_id = stream_id
                    break
            else:
                # if there's no problem, we may as well go right up to the max_stream_id
                clipped_stream_id = max_stream_id

            # Now actually get the deltas
            sql = """
                SELECT stream_id, room_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
            """
            txn.execute(sql, (prev_stream_id, clipped_stream_id))
            return clipped_stream_id, [
                StateDelta(
                    stream_id=row[0],
                    room_id=row[1],
                    event_type=row[2],
                    state_key=row[3],
                    event_id=row[4],
                    prev_event_id=row[5],
                )
                for row in txn.fetchall()
            ]

        return await self.db_pool.runInteraction(
            "get_current_state_deltas", get_current_state_deltas_txn
        )

    def _get_max_stream_id_in_current_state_deltas_txn(
        self, txn: LoggingTransaction
    ) -> int:
        return self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="current_state_delta_stream",
            keyvalues={},
            retcol="COALESCE(MAX(stream_id), -1)",
        )

    async def get_max_stream_id_in_current_state_deltas(self) -> int:
        return await self.db_pool.runInteraction(
            "get_max_stream_id_in_current_state_deltas",
            self._get_max_stream_id_in_current_state_deltas_txn,
        )

    def get_current_state_deltas_for_room_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        *,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
    ) -> list[StateDelta]:
        """
        Get the state deltas between two tokens.

        (> `from_token` and <= `to_token`)
        """
        from_clause = ""
        from_args = []
        if from_token is not None:
            from_clause = "AND ? < stream_id"
            from_args = [from_token.stream]

        to_clause = ""
        to_args = []
        if to_token is not None:
            to_clause = "AND stream_id <= ?"
            to_args = [to_token.get_max_stream_pos()]

        sql = f"""
                SELECT instance_name, stream_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE room_id = ? {from_clause} {to_clause}
                ORDER BY stream_id ASC
            """
        txn.execute(sql, [room_id] + from_args + to_args)

        return [
            StateDelta(
                stream_id=row[1],
                room_id=room_id,
                event_type=row[2],
                state_key=row[3],
                event_id=row[4],
                prev_event_id=row[5],
            )
            for row in txn
            if _filter_results_by_stream(from_token, to_token, row[0], row[1])
        ]

    @trace
    async def get_current_state_deltas_for_room(
        self,
        room_id: str,
        *,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
    ) -> list[StateDelta]:
        """
        Get the state deltas between two tokens.

        (> `from_token` and <= `to_token`)
        """
        # We can bail early if the `from_token` is after the `to_token`
        if (
            to_token is not None
            and from_token is not None
            and to_token.is_before_or_eq(from_token)
        ):
            return []

        if (
            from_token is not None
            and not self._curr_state_delta_stream_cache.has_entity_changed(
                room_id, from_token.stream
            )
        ):
            return []

        return await self.db_pool.runInteraction(
            "get_current_state_deltas_for_room",
            self.get_current_state_deltas_for_room_txn,
            room_id,
            from_token=from_token,
            to_token=to_token,
        )

    @trace
    async def get_current_state_deltas_for_rooms(
        self,
        room_ids: StrCollection,
        from_token: RoomStreamToken,
        to_token: RoomStreamToken,
    ) -> list[StateDelta]:
        """Get the state deltas between two tokens for the set of rooms."""

        room_ids = self._curr_state_delta_stream_cache.get_entities_changed(
            room_ids, from_token.stream
        )
        if not room_ids:
            return []

        def get_current_state_deltas_for_rooms_txn(
            txn: LoggingTransaction,
            room_ids: StrCollection,
        ) -> list[StateDelta]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", room_ids
            )

            sql = f"""
                SELECT instance_name, stream_id, room_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE {clause} AND ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
            """
            args.append(from_token.stream)
            args.append(to_token.get_max_stream_pos())

            txn.execute(sql, args)

            return [
                StateDelta(
                    stream_id=row[1],
                    room_id=row[2],
                    event_type=row[3],
                    state_key=row[4],
                    event_id=row[5],
                    prev_event_id=row[6],
                )
                for row in txn
                if _filter_results_by_stream(from_token, to_token, row[0], row[1])
            ]

        results = []
        for batch in batch_iter(room_ids, 1000):
            deltas = await self.db_pool.runInteraction(
                "get_current_state_deltas_for_rooms",
                get_current_state_deltas_for_rooms_txn,
                batch,
            )

            results.extend(deltas)

        return results
