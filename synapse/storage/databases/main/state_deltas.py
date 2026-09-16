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
from typing import TYPE_CHECKING, cast

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
        Get the state deltas between two tokens, bounded on the delta rows'
        own `stream_id`. See `get_current_state_deltas_for_room` for when that
        is the wrong bound.

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
        Get the state deltas between two tokens, bounded on the delta rows'
        own `stream_id`.

        (> `from_token` and <= `to_token`)

        Rows carry the minimum stream ordering of their persist batch as
        `stream_id` (see `_update_current_state_txn`), so a token that falls inside
        a batch -- which a worker reading the events stream from replication
        routinely observes -- misses that batch's deltas here. Callers that
        pair the deltas with the events in the same window (a timeline)
        should use `get_current_state_deltas_for_room_by_event_position`
        instead, which bounds each delta on its event's position.
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
    ) -> list[StateDelta]:
        """
        Get the state deltas of a room between two tokens.

        A delta is included if its position is > `from_token` and <= `to_token`
        (a `None` token is unbounded). The position of a delta is the later of
        the delta row's own `stream_id` and the stream ordering of its event:

        * a delta whose event was persisted later in the stream than the row's
          `stream_id` is positioned at the event, so it is reported in the same
          window as the event itself;
        * a delta with no event (state that was removed, e.g. when the last
          local user left the room) is positioned at the row's `stream_id`;
        * a delta whose row was written later than its event (existing state
          re-announced by a partial-state resync) is positioned at the row's
          `stream_id`.

        Returns:
            The deltas in `stream_id` order, which for any two deltas of
            different persist batches is also position order. The order of
            deltas within one batch is unspecified; a batch writes at most one
            delta per state key.
        """
        # `current_state_delta_stream.stream_id` is not the position of the
        # delta's event: a persist batch writes all its rows with the *first*
        # stream ordering of the batch (see `_update_current_state_txn`), so a
        # state event persisted mid-batch has `stream_id` < `stream_ordering`.
        # A worker reading the events stream from replication advances one
        # event at a time, so its tokens routinely fall inside a batch:
        #
        #                            stream_ordering  event          delta row `stream_id`
        #   from-token batch           10             message
        #     from_token = 11 ->       11             message
        #                              12             state event X  X: 10
        #   ...                                       any number of batches
        #   to-token batch             90             state event Y  Y: 90
        #     to_token = 91   ->       91             message
        #                              92             state event Z  Z: 90
        #
        # On `stream_id` alone, the window (11, 91] misses X's delta (10 <= 11,
        # though event 12 is in the window) and includes Z's (90 <= 91, though
        # event 92 is past it). Y's delta is right either way.
        #
        # Batches of a room follow each other in the stream, so only two of
        # them can be cut by a token: the one that contains `from_token` (the
        # from-token batch) and the one that contains `to_token` (the to-token
        # batch). Every other batch is entirely inside the window or entirely
        # outside it, and so are its delta rows, whether judged on `stream_id`
        # or on `stream_ordering`.
        #
        # The from-token batch is found without looking at `events`: its rows in
        # `current_state_delta_stream` carry the latest `stream_id` <=
        # `from_token` in the room, since every later batch writes its rows
        # after `from_token`. So instead of fetching the delta rows with
        # `from_token` < `stream_id`, fetch from that batch's `stream_id`
        # inclusive (`from_batch_stream_id` below): the same delta rows plus
        # those of the from-token batch. (If it changed no state, this picks the
        # previous batch that did; its events are all before `from_token`, so
        # its delta rows are dropped below.)
        #
        # Among the fetched delta rows, the from-token batch's are those at
        # `from_batch_stream_id`, and the to-token batch's are found the same
        # way, at the latest `stream_id` <= `to_token`. Only these need their
        # event's `stream_ordering`, looked up in `events` by `event_id`; such a
        # delta row is kept when
        #     `from_token` < max(`stream_id`, `stream_ordering`) <= `to_token`.
        # Every other delta row is kept when
        #     `from_token` < `stream_id` <= `to_token`.
        args: list[str | int] = [room_id]

        lower_clause = ""
        if from_token is not None:
            # The from-token batch's `stream_id`: the latest `stream_id` <=
            # `from_token`.
            # (`from_token.stream` is the minimum over writers; the delta rows
            # of a writer that is further ahead are in the range anyway and are
            # judged against that writer's position below.)
            txn.execute(
                """
                SELECT MAX(stream_id) FROM current_state_delta_stream
                WHERE room_id = ? AND stream_id <= ?
                """,
                (room_id, from_token.stream),
            )
            row = txn.fetchone()
            from_batch_stream_id: int | None = row[0] if row is not None else None
            if from_batch_stream_id is not None:
                # No delta row has `from_batch_stream_id` < `stream_id` <=
                # `from_token.stream`, so this is the from-token batch plus
                # everything after `from_token`.
                lower_clause = "AND ? <= stream_id"
                args.append(from_batch_stream_id)
            else:
                lower_clause = "AND ? < stream_id"
                args.append(from_token.stream)

        upper_clause = ""
        if to_token is not None:
            upper_clause = "AND stream_id <= ?"
            args.append(to_token.get_max_stream_pos())

        sql = f"""
                SELECT instance_name, stream_id, type, state_key, event_id, prev_event_id
                FROM current_state_delta_stream
                WHERE room_id = ? {lower_clause} {upper_clause}
                ORDER BY stream_id ASC
            """
        txn.execute(sql, args)
        rows = cast(
            list[tuple[str | None, int, str, str, str | None, str | None]],
            txn.fetchall(),
        )

        # The `stream_id` of the from-token batch and of the to-token batch,
        # per writer: the latest `stream_id` <= the writer's position in
        # `from_token` and in `to_token`. Historic delta rows have no instance
        # name and count as "master", as in `_filter_results_by_stream`.
        token_batch_stream_ids: set[tuple[str, int]] = set()
        for token in (from_token, to_token):
            if token is None:
                continue
            latest_by_instance: dict[str, int] = {}
            for instance_name, stream_id, _, _, _, _ in rows:
                instance_name = instance_name or "master"
                if stream_id <= token.get_stream_pos_for_instance(instance_name):
                    latest_by_instance[instance_name] = max(
                        latest_by_instance.get(instance_name, stream_id), stream_id
                    )
            token_batch_stream_ids.update(latest_by_instance.items())

        # Only the delta rows of the two token batches need their event's
        # position.
        token_batch_event_ids = [
            event_id
            for instance_name, stream_id, _, _, event_id, _ in rows
            if event_id is not None
            and (instance_name or "master", stream_id) in token_batch_stream_ids
        ]
        event_positions: dict[str, tuple[str | None, int]] = {}
        for chunk in batch_iter(token_batch_event_ids, 1000):
            clause, clause_args = make_in_list_sql_clause(
                self.database_engine, "event_id", chunk
            )
            txn.execute(
                f"""
                SELECT event_id, instance_name, stream_ordering
                FROM events
                WHERE {clause}
                """,
                clause_args,
            )
            for event_id, event_instance, event_stream in txn:
                if event_stream is not None:
                    event_positions[event_id] = (event_instance, event_stream)

        deltas = []
        for (
            row_instance,
            row_stream,
            event_type,
            state_key,
            event_id,
            prev_event_id,
        ) in rows:
            # The delta's position: its `stream_id`, unless it belongs to
            # a token batch and its event sits later in the stream.
            effective_instance, effective_stream = row_instance, row_stream
            if (
                event_id is not None
                and (row_instance or "master", row_stream) in token_batch_stream_ids
            ):
                position = event_positions.get(event_id)
                if position is not None and position[1] > row_stream:
                    effective_instance, effective_stream = position

            if _filter_results_by_stream(
                from_token, to_token, effective_instance, effective_stream
            ):
                deltas.append(
                    StateDelta(
                        stream_id=row_stream,
                        room_id=room_id,
                        event_type=event_type,
                        state_key=state_key,
                        event_id=event_id,
                        prev_event_id=prev_event_id,
                    )
                )

        return deltas

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

        return await self.db_pool.runInteraction(
            "get_current_state_deltas_for_room_by_event_position",
            self.get_current_state_deltas_for_room_by_event_position_txn,
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
