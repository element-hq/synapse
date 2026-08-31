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
    # attributes. TODO: can we get static analysis to enforce this?
    _curr_state_delta_stream_cache: StreamChangeCache
    _events_stream_cache: StreamChangeCache

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
        self.db_pool.updates.register_background_index_update(
            update_name="current_state_delta_stream_event_id_index",
            index_name="current_state_delta_stream_event_id_idx",
            table="current_state_delta_stream",
            columns=("event_id",),
            where_clause="event_id IS NOT NULL",
        )

    async def get_partial_current_state_deltas(
        self, prev_stream_id: int, max_stream_id: int, limit: int = 100
    ) -> tuple[int, list[StateDelta]]:
        """Fetch a list of room state changes since the given stream id.

        This may be the partial state if we're lazy joining the room.

        This method takes care to handle state deltas that share the same
        `stream_id`. That can happen when persisting state in a batch,
        potentially as the result of state resolution (both adding new state and
        undo'ing previous state).

        State deltas are grouped by `stream_id`. When hitting the given `limit`
        would return only part of a "group" of state deltas, that entire group
        is omitted. Thus, this function may return *up to* `limit` state deltas,
        or slightly more when a single group itself exceeds `limit`.

        Args:
            prev_stream_id: point to get changes since (exclusive)
            max_stream_id: the point that we know has been correctly persisted
                - ie, an upper limit to return changes from.
            limit: the maximum number of rows to return.

        Returns:
            A tuple consisting of:
                - the stream id which these results go up to
                - list of current_state_delta_stream rows. If it is empty, we are
                  up to date.
        """
        prev_stream_id = int(prev_stream_id)

        if limit <= 0:
            raise ValueError(
                "Invalid `limit` passed to `get_partial_current_state_deltas"
            )

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
            # First we group state deltas by `stream_id` and calculate which
            # groups can be returned without exceeding the provided `limit`.
            sql_grouped = """
                SELECT stream_id, COUNT(*) AS c
                FROM current_state_delta_stream
                WHERE stream_id > ? AND stream_id <= ?
                GROUP BY stream_id
                ORDER BY stream_id
                LIMIT ?
            """
            group_limit = limit + 1
            txn.execute(sql_grouped, (prev_stream_id, max_stream_id, group_limit))
            grouped_rows = txn.fetchall()

            if not grouped_rows:
                # Nothing to return in the range; we are up to date through max_stream_id.
                return max_stream_id, []

            # Always retrieve the first group, at the bare minimum. This ensures the
            # caller always makes progress, even if a single group exceeds `limit`.
            fetch_upto_stream_id, included_rows = grouped_rows[0]

            # Determine which other groups we can retrieve at the same time,
            # without blowing the budget.
            included_all_groups = True
            for stream_id, count in grouped_rows[1:]:
                if included_rows + count > limit:
                    included_all_groups = False
                    break
                included_rows += count
                fetch_upto_stream_id = stream_id

            # If we retrieved fewer groups than the limit *and* we didn't hit the
            # `LIMIT ?` cap on the grouping query, we know we've caught up with
            # the stream.
            caught_up_with_stream = (
                included_all_groups and len(grouped_rows) < group_limit
            )

            # At this point we should have advanced, or bailed out early above.
            assert fetch_upto_stream_id != prev_stream_id

            # 2) Fetch the actual rows for only the included stream_id groups.
            sql_rows = """
                SELECT stream_id, room_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
            """
            txn.execute(sql_rows, (prev_stream_id, fetch_upto_stream_id))
            rows = txn.fetchall()

            clipped_stream_id = (
                max_stream_id if caught_up_with_stream else fetch_upto_stream_id
            )

            return clipped_stream_id, [
                StateDelta(
                    stream_id=row[0],
                    room_id=row[1],
                    event_type=row[2],
                    state_key=row[3],
                    event_id=row[4],
                    prev_event_id=row[5],
                )
                for row in rows
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

    def get_current_state_deltas_for_room_by_event_position_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        *,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
        events_state_key_populated: bool = True,
    ) -> list[StateDelta]:
        """
        Get the state deltas between two tokens, bounding each delta on the
        position of its state event in the events stream rather than on the
        delta row's own `stream_id`.

        (> `from_token` and <= `to_token`; results are ordered by that
        effective position.)

        `current_state_delta_stream` rows are stamped with the *minimum*
        stream ordering of the persist batch of their event (see
        `_update_current_state_txn`), so bounding on `stream_id` alone drops
        the deltas of a batch's state events for any token that falls inside
        the batch -- a position that a worker reading the events stream from
        replication routinely observes, since RDATA advances the stream one
        event at a time.

        A delta's effective position is therefore taken to be the *maximum* of
        the row's `stream_id` and its event's own stream ordering:

        * for a state event persisted mid-batch, that is the event's own
          position, so the delta tracks the event exactly;
        * for rows with no event (e.g. the state clearance when the last
          local user leaves), the row's `stream_id` stands;
        * for rows stamped *after* their event (the partial-state room resync
          in `update_current_state` re-announces existing state at a fresh
          position), the row's `stream_id` stands, preserving the
          re-announcement.

        That maximum is not a bound an index can serve, so the window is
        fetched as the union of two index-driven sets (with the exact
        per-writer filtering done in Python, as for
        `get_current_state_deltas_for_room_txn`):

        * rows whose own `stream_id` is in the window -- an index range on
          `current_state_delta_stream(room_id, stream_id)`, exactly like
          `get_current_state_deltas_for_room_txn`; this is every row except
          the mid-batch stragglers;
        * rows whose *event* is in the window -- driven by the
          `events(room_id, stream_ordering)` index over the events in the
          window, joined back via the partial
          `current_state_delta_stream(event_id)` index. A row stamped below
          the window with an effective position inside it must have its event
          inside the window, so this query is what recovers the mid-batch
          stragglers, at a cost proportional to the number of state events in
          the window.
        """
        args: list[str | int] = [room_id]

        stream_id_from_clause = ""
        if from_token is not None:
            stream_id_from_clause = "AND ? < d.stream_id"
            args.append(from_token.stream)

        stream_id_to_clause = ""
        if to_token is not None:
            stream_id_to_clause = "AND d.stream_id <= ?"
            args.append(to_token.get_max_stream_pos())

        # Rows below the window's lower bound can only have an effective
        # position inside the window via their event, so the event-driven query is
        # only needed when there is a lower bound at all.
        by_event_position_sql = ""
        if from_token is not None:
            event_position_to_clause = ""
            if to_token is not None:
                event_position_to_clause = "AND e.stream_ordering <= ?"

            # Only state events can match the delta join, so once
            # `events.state_key` is reliable we restrict the scan to them and
            # spare one index probe per non-state event in the window.
            event_state_key_clause = ""
            if events_state_key_populated:
                event_state_key_clause = "AND e.state_key IS NOT NULL"

            by_event_position_sql = f"""
                UNION
                SELECT d.instance_name, d.stream_id, d.type, d.state_key,
                    d.event_id, d.prev_event_id,
                    e.instance_name, e.stream_ordering
                FROM events AS e
                INNER JOIN current_state_delta_stream AS d
                    ON d.event_id = e.event_id AND d.room_id = e.room_id
                WHERE e.room_id = ? {event_state_key_clause}
                    AND ? < e.stream_ordering {event_position_to_clause}
            """
            args.extend([room_id, from_token.stream])
            if to_token is not None:
                args.append(to_token.get_max_stream_pos())

        sql = f"""
                SELECT d.instance_name, d.stream_id, d.type, d.state_key,
                    d.event_id, d.prev_event_id,
                    e.instance_name, e.stream_ordering
                FROM current_state_delta_stream AS d
                LEFT JOIN events AS e ON e.event_id = d.event_id
                WHERE d.room_id = ? {stream_id_from_clause} {stream_id_to_clause}
                {by_event_position_sql}
            """
        txn.execute(sql, args)

        deltas = []
        for row in txn:
            (
                row_instance,
                row_stream,
                event_type,
                state_key,
                event_id,
                prev_event_id,
                event_instance,
                event_stream,
            ) = row

            # The effective position: the row's own stamp, unless the event
            # sits later in the stream.
            if event_stream is not None and event_stream > row_stream:
                effective_instance, effective_stream = event_instance, event_stream
            else:
                effective_instance, effective_stream = row_instance, row_stream

            if _filter_results_by_stream(
                from_token, to_token, effective_instance, effective_stream
            ):
                deltas.append(
                    (
                        effective_stream,
                        StateDelta(
                            stream_id=row_stream,
                            room_id=room_id,
                            event_type=event_type,
                            state_key=state_key,
                            event_id=event_id,
                            prev_event_id=prev_event_id,
                        ),
                    )
                )

        # Consumers rely on deltas being in stream order (the last delta for a
        # given state key wins), which for this query means effective-position
        # order.
        deltas.sort(key=lambda t: t[0])
        return [d for _, d in deltas]

    @trace
    async def get_current_state_deltas_for_room_by_event_position(
        self,
        room_id: str,
        *,
        from_token: RoomStreamToken | None,
        to_token: RoomStreamToken | None,
    ) -> list[StateDelta]:
        """
        Get the state deltas between two tokens, bounding each delta on the
        position of its state event rather than on the delta row's `stream_id`.
        See `get_current_state_deltas_for_room_by_event_position_txn`.

        (> `from_token` and <= `to_token`)

        Until the `current_state_delta_stream(event_id)` index has been built
        (a background update), this falls back to bounding on the rows' own
        `stream_id` -- the behaviour this method replaces, which can miss
        mid-batch deltas but never scans beyond the index.
        """
        # We can bail early if the `from_token` is after the `to_token`
        if (
            to_token is not None
            and from_token is not None
            and to_token.is_before_or_eq(from_token)
        ):
            return []

        # A delta's effective position is beyond `from_token` only if the row's
        # `stream_id` is (the delta stream cache) or its event's stream
        # ordering is (the events stream cache); if neither cache has seen the
        # room change there is nothing to return.
        if (
            from_token is not None
            and not self._curr_state_delta_stream_cache.has_entity_changed(
                room_id, from_token.stream
            )
            and not self._events_stream_cache.has_entity_changed(
                room_id, from_token.stream
            )
        ):
            return []

        # Without the `current_state_delta_stream(event_id)` index, the
        # event-driven query of the union has no index to join through and would
        # walk the room's entire delta history, so fall back to the plain
        # `stream_id` bounds until the background update has completed.
        if not await self.db_pool.updates.has_completed_background_update(
            "current_state_delta_stream_event_id_index"
        ):
            return await self.db_pool.runInteraction(
                "get_current_state_deltas_for_room_by_event_position_fallback",
                self.get_current_state_deltas_for_room_txn,
                room_id,
                from_token=from_token,
                to_token=to_token,
            )

        # `events.state_key` is back-populated by a schema-76 background
        # update; until it has completed, old state events may have a NULL
        # state_key and the event-driven query must not filter on it.
        # (`has_completed_background_update` memoises completion, so this is
        # only a query the first time.)
        events_state_key_populated = (
            await self.db_pool.updates.has_completed_background_update(
                "events_populate_state_key_rejections"
            )
        )

        return await self.db_pool.runInteraction(
            "get_current_state_deltas_for_room_by_event_position",
            self.get_current_state_deltas_for_room_by_event_position_txn,
            room_id,
            from_token=from_token,
            to_token=to_token,
            events_state_key_populated=events_state_key_populated,
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
