#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import itertools
import logging
from typing import (
    TYPE_CHECKING,
    Collection,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

from synapse.logging.opentracing import tag_args, trace
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.engines import PostgresEngine
from synapse.types import MutableStateMap, StateMap
from synapse.types.state import StateFilter
from synapse.types.storage import _BackgroundUpdates
from synapse.util.caches import intern_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


MAX_STATE_DELTA_HOPS = 100


class StateGroupBackgroundUpdateStore(SQLBaseStore):
    """Defines functions related to state groups needed to run the state background
    updates.
    """

    @trace
    @tag_args
    def _count_state_group_hops_txn(
        self, txn: LoggingTransaction, state_group: int
    ) -> int:
        """Given a state group, count how many hops there are in the tree.

        This is used to ensure the delta chains don't get too long.
        """
        if isinstance(self.database_engine, PostgresEngine):
            sql = """
                WITH RECURSIVE state(state_group) AS (
                    VALUES(?::bigint)
                    UNION ALL
                    SELECT prev_state_group FROM state_group_edges e, state s
                    WHERE s.state_group = e.state_group
                )
                SELECT count(*) FROM state;
            """

            txn.execute(sql, (state_group,))
            row = txn.fetchone()
            if row and row[0]:
                return row[0]
            else:
                return 0
        else:
            # We don't use WITH RECURSIVE on sqlite3 as there are distributions
            # that ship with an sqlite3 version that doesn't support it (e.g. wheezy)
            next_group: Optional[int] = state_group
            count = 0

            while next_group:
                next_group = self.db_pool.simple_select_one_onecol_txn(
                    txn,
                    table="state_group_edges",
                    keyvalues={"state_group": next_group},
                    retcol="prev_state_group",
                    allow_none=True,
                )
                if next_group:
                    count += 1

            return count

    @trace
    @tag_args
    def _get_state_groups_from_groups_txn(
        self,
        txn: LoggingTransaction,
        groups: List[int],
        state_filter: Optional[StateFilter] = None,
    ) -> Mapping[int, StateMap[str]]:
        """
        Given a number of state groups, fetch the latest state for each group.

        Args:
            txn: The transaction object.
            groups: The given state groups that you want to fetch the latest state for.
            state_filter: The state filter to apply the state we fetch state from the database.

        Returns:
            Map from state_group to a StateMap at that point.
        """
        if state_filter is None:
            state_filter = StateFilter.all()

        results: Dict[int, MutableStateMap[str]] = {group: {} for group in groups}

        if isinstance(self.database_engine, PostgresEngine):
            # Temporarily disable sequential scans in this transaction. This is
            # a temporary hack until we can add the right indices in
            txn.execute("SET LOCAL enable_seqscan=off")

            # The below query walks the state_group tree so that the "state"
            # table includes all state_groups in the tree. It then joins
            # against `state_groups_state` to fetch the latest state.
            # It assumes that previous state groups are always numerically
            # lesser.
            # This may return multiple rows per (type, state_key), but last_value
            # should be the same.
            sql = """
                WITH RECURSIVE sgs(state_group) AS (
                    VALUES(?::bigint)
                    UNION ALL
                    SELECT prev_state_group FROM state_group_edges e, sgs s
                    WHERE s.state_group = e.state_group
                )
                %s
            """

            overall_select_query_args: List[Union[int, str]] = []

            # This is an optimization to create a select clause per-condition. This
            # makes the query planner a lot smarter on what rows should pull out in the
            # first place and we end up with something that takes 10x less time to get a
            # result.
            use_condition_optimization = (
                not state_filter.include_others and not state_filter.is_full()
            )
            state_filter_condition_combos: List[Tuple[str, Optional[str]]] = []
            # We don't need to caclculate this list if we're not using the condition
            # optimization
            if use_condition_optimization:
                for etype, state_keys in state_filter.types.items():
                    if state_keys is None:
                        state_filter_condition_combos.append((etype, None))
                    else:
                        for state_key in state_keys:
                            state_filter_condition_combos.append((etype, state_key))
            # And here is the optimization itself. We don't want to do the optimization
            # if there are too many individual conditions. 10 is an arbitrary number
            # with no testing behind it but we do know that we specifically made this
            # optimization for when we grab the necessary state out for
            # `filter_events_for_client` which just uses 2 conditions
            # (`EventTypes.RoomHistoryVisibility` and `EventTypes.Member`).
            if use_condition_optimization and len(state_filter_condition_combos) < 10:
                select_clause_list: List[str] = []
                for etype, skey in state_filter_condition_combos:
                    if skey is None:
                        where_clause = "(type = ?)"
                        overall_select_query_args.extend([etype])
                    else:
                        where_clause = "(type = ? AND state_key = ?)"
                        overall_select_query_args.extend([etype, skey])

                    select_clause_list.append(
                        f"""
                        (
                            SELECT DISTINCT ON (type, state_key)
                                type, state_key, event_id
                            FROM state_groups_state
                            INNER JOIN sgs USING (state_group)
                            WHERE {where_clause}
                            ORDER BY type, state_key, state_group DESC
                        )
                        """
                    )

                overall_select_clause = " UNION ".join(select_clause_list)
            else:
                where_clause, where_args = state_filter.make_sql_filter_clause()
                # Unless the filter clause is empty, we're going to append it after an
                # existing where clause
                if where_clause:
                    where_clause = " AND (%s)" % (where_clause,)

                overall_select_query_args.extend(where_args)

                overall_select_clause = f"""
                    SELECT DISTINCT ON (type, state_key)
                        type, state_key, event_id
                    FROM state_groups_state
                    WHERE state_group IN (
                        SELECT state_group FROM sgs
                    ) {where_clause}
                    ORDER BY type, state_key, state_group DESC
                """

            for group in groups:
                args: List[Union[int, str]] = [group]
                args.extend(overall_select_query_args)

                txn.execute(sql % (overall_select_clause,), args)
                for row in txn:
                    typ, state_key, event_id = row
                    key = (intern_string(typ), intern_string(state_key))
                    results[group][key] = event_id
        else:
            max_entries_returned = state_filter.max_entries_returned()

            where_clause, where_args = state_filter.make_sql_filter_clause()
            # Unless the filter clause is empty, we're going to append it after an
            # existing where clause
            if where_clause:
                where_clause = " AND (%s)" % (where_clause,)

            # XXX: We could `WITH RECURSIVE` here since it's supported on SQLite 3.8.3
            # or higher and our minimum supported version is greater than that.
            #
            # We just haven't put in the time to refactor this.
            for group in groups:
                next_group: Optional[int] = group

                while next_group:
                    # We did this before by getting the list of group ids, and
                    # then passing that list to sqlite to get latest event for
                    # each (type, state_key). However, that was terribly slow
                    # without the right indices (which we can't add until
                    # after we finish deduping state, which requires this func)
                    args = [next_group]
                    args.extend(where_args)

                    txn.execute(
                        "SELECT type, state_key, event_id FROM state_groups_state"
                        " WHERE state_group = ? " + where_clause,
                        args,
                    )
                    results[group].update(
                        ((typ, state_key), event_id)
                        for typ, state_key, event_id in txn
                        if (typ, state_key) not in results[group]
                    )

                    # If the number of entries in the (type,state_key)->event_id dict
                    # matches the number of (type,state_keys) types we were searching
                    # for, then we must have found them all, so no need to go walk
                    # further down the tree... UNLESS our types filter contained
                    # wildcards (i.e. Nones) in which case we have to do an exhaustive
                    # search
                    if (
                        max_entries_returned is not None
                        and len(results[group]) == max_entries_returned
                    ):
                        break

                    next_group = self.db_pool.simple_select_one_onecol_txn(
                        txn,
                        table="state_group_edges",
                        keyvalues={"state_group": next_group},
                        retcol="prev_state_group",
                        allow_none=True,
                    )

        # The results shouldn't be considered mutable.
        return results


class StateBackgroundUpdateStore(StateGroupBackgroundUpdateStore):
    STATE_GROUP_DEDUPLICATION_UPDATE_NAME = "state_group_state_deduplication"
    STATE_GROUP_INDEX_UPDATE_NAME = "state_group_state_type_index"
    STATE_GROUPS_ROOM_INDEX_UPDATE_NAME = "state_groups_room_id_idx"
    STATE_GROUP_EDGES_UNIQUE_INDEX_UPDATE_NAME = "state_group_edges_unique_idx"

    CURRENT_STATE_EVENTS_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "current_state_events_stream_ordering_idx"
    )
    ROOM_MEMBERSHIPS_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "room_memberships_stream_ordering_idx"
    )
    LOCAL_CURRENT_MEMBERSHIP_STREAM_ORDERING_INDEX_UPDATE_NAME = (
        "local_current_membership_stream_ordering_idx"
    )

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self.db_pool.updates.register_background_update_handler(
            self.STATE_GROUP_DEDUPLICATION_UPDATE_NAME,
            self._background_deduplicate_state,
        )
        self.db_pool.updates.register_background_update_handler(
            self.STATE_GROUP_INDEX_UPDATE_NAME, self._background_index_state
        )
        self.db_pool.updates.register_background_index_update(
            self.STATE_GROUPS_ROOM_INDEX_UPDATE_NAME,
            index_name="state_groups_room_id_idx",
            table="state_groups",
            columns=["room_id"],
        )

        # `state_group_edges` can cause severe performance issues if duplicate
        # rows are introduced, which can accidentally be done by well-meaning
        # server admins when trying to restore a database dump, etc.
        # See https://github.com/matrix-org/synapse/issues/11779.
        # Introduce a unique index to guard against that.
        self.db_pool.updates.register_background_index_update(
            self.STATE_GROUP_EDGES_UNIQUE_INDEX_UPDATE_NAME,
            index_name="state_group_edges_unique_idx",
            table="state_group_edges",
            columns=["state_group", "prev_state_group"],
            unique=True,
            # The old index was on (state_group) and was not unique.
            replaces_index="state_group_edges_idx",
        )

        # These indices are needed to validate the foreign key constraint
        # when events are deleted.
        self.db_pool.updates.register_background_index_update(
            self.CURRENT_STATE_EVENTS_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="current_state_events_stream_ordering_idx",
            table="current_state_events",
            columns=["event_stream_ordering"],
        )
        self.db_pool.updates.register_background_index_update(
            self.ROOM_MEMBERSHIPS_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="room_memberships_stream_ordering_idx",
            table="room_memberships",
            columns=["event_stream_ordering"],
        )
        self.db_pool.updates.register_background_index_update(
            self.LOCAL_CURRENT_MEMBERSHIP_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="local_current_membership_stream_ordering_idx",
            table="local_current_membership",
            columns=["event_stream_ordering"],
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.DELETE_UNREFERENCED_STATE_GROUPS_BG_UPDATE,
            self._background_delete_unrefereneced_state_groups,
        )

    async def _background_deduplicate_state(
        self, progress: dict, batch_size: int
    ) -> int:
        """This background update will slowly deduplicate state by reencoding
        them as deltas.
        """
        last_state_group = progress.get("last_state_group", 0)
        rows_inserted = progress.get("rows_inserted", 0)
        max_group = progress.get("max_group", None)

        BATCH_SIZE_SCALE_FACTOR = 100

        batch_size = max(1, int(batch_size / BATCH_SIZE_SCALE_FACTOR))

        if max_group is None:
            rows = await self.db_pool.execute(
                "_background_deduplicate_state",
                "SELECT coalesce(max(id), 0) FROM state_groups",
            )
            max_group = rows[0][0]

        def reindex_txn(txn: LoggingTransaction) -> Tuple[bool, int]:
            new_last_state_group = last_state_group
            for count in range(batch_size):
                txn.execute(
                    "SELECT id, room_id FROM state_groups"
                    " WHERE ? < id AND id <= ?"
                    " ORDER BY id ASC"
                    " LIMIT 1",
                    (new_last_state_group, max_group),
                )
                row = txn.fetchone()
                if row:
                    state_group, room_id = row

                if not row or not state_group:
                    return True, count

                txn.execute(
                    "SELECT state_group FROM state_group_edges"
                    " WHERE state_group = ?",
                    (state_group,),
                )

                # If we reach a point where we've already started inserting
                # edges we should stop.
                if txn.fetchall():
                    return True, count

                txn.execute(
                    "SELECT coalesce(max(id), 0) FROM state_groups"
                    " WHERE id < ? AND room_id = ?",
                    (state_group, room_id),
                )
                # There will be a result due to the coalesce.
                (prev_group,) = txn.fetchone()  # type: ignore
                new_last_state_group = state_group

                if prev_group:
                    potential_hops = self._count_state_group_hops_txn(txn, prev_group)
                    if potential_hops >= MAX_STATE_DELTA_HOPS:
                        # We want to ensure chains are at most this long,#
                        # otherwise read performance degrades.
                        continue

                    prev_state_by_group = self._get_state_groups_from_groups_txn(
                        txn, [prev_group]
                    )
                    prev_state = prev_state_by_group[prev_group]

                    curr_state_by_group = self._get_state_groups_from_groups_txn(
                        txn, [state_group]
                    )
                    curr_state = curr_state_by_group[state_group]

                    if not set(prev_state.keys()) - set(curr_state.keys()):
                        # We can only do a delta if the current has a strict super set
                        # of keys

                        delta_state = {
                            key: value
                            for key, value in curr_state.items()
                            if prev_state.get(key, None) != value
                        }

                        self.db_pool.simple_delete_txn(
                            txn,
                            table="state_group_edges",
                            keyvalues={"state_group": state_group},
                        )

                        self.db_pool.simple_insert_txn(
                            txn,
                            table="state_group_edges",
                            values={
                                "state_group": state_group,
                                "prev_state_group": prev_group,
                            },
                        )

                        self.db_pool.simple_delete_txn(
                            txn,
                            table="state_groups_state",
                            keyvalues={"state_group": state_group},
                        )

                        self.db_pool.simple_insert_many_txn(
                            txn,
                            table="state_groups_state",
                            keys=(
                                "state_group",
                                "room_id",
                                "type",
                                "state_key",
                                "event_id",
                            ),
                            values=[
                                (state_group, room_id, key[0], key[1], state_id)
                                for key, state_id in delta_state.items()
                            ],
                        )

            progress = {
                "last_state_group": state_group,
                "rows_inserted": rows_inserted + batch_size,
                "max_group": max_group,
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, self.STATE_GROUP_DEDUPLICATION_UPDATE_NAME, progress
            )

            return False, batch_size

        finished, result = await self.db_pool.runInteraction(
            self.STATE_GROUP_DEDUPLICATION_UPDATE_NAME, reindex_txn
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                self.STATE_GROUP_DEDUPLICATION_UPDATE_NAME
            )

        return result * BATCH_SIZE_SCALE_FACTOR

    async def _background_index_state(self, progress: dict, batch_size: int) -> int:
        def reindex_txn(conn: LoggingDatabaseConnection) -> None:
            conn.rollback()
            if isinstance(self.database_engine, PostgresEngine):
                # postgres insists on autocommit for the index
                conn.engine.attempt_to_set_autocommit(conn.conn, True)
                try:
                    txn = conn.cursor()
                    txn.execute(
                        "CREATE INDEX CONCURRENTLY state_groups_state_type_idx"
                        " ON state_groups_state(state_group, type, state_key)"
                    )
                    txn.execute("DROP INDEX IF EXISTS state_groups_state_id")
                finally:
                    conn.engine.attempt_to_set_autocommit(conn.conn, False)
            else:
                txn = conn.cursor()
                txn.execute(
                    "CREATE INDEX state_groups_state_type_idx"
                    " ON state_groups_state(state_group, type, state_key)"
                )
                txn.execute("DROP INDEX IF EXISTS state_groups_state_id")

        await self.db_pool.runWithConnection(reindex_txn)

        await self.db_pool.updates._end_background_update(
            self.STATE_GROUP_INDEX_UPDATE_NAME
        )

        return 1

    async def _background_delete_unrefereneced_state_groups(
        self, progress: dict, batch_size: int
    ) -> int:
        """This background update will slowly delete any unreferenced state groups"""

        last_checked_state_group = progress.get("last_checked_state_group")

        if last_checked_state_group is None:
            # This is the first run.
            last_checked_state_group = 0

        (
            last_checked_state_group,
            final_batch,
        ) = await self._delete_unreferenced_state_groups_batch(
            last_checked_state_group, batch_size
        )

        if not final_batch:
            # There are more state groups to check.
            progress = {
                "last_checked_state_group": last_checked_state_group,
            }
            await self.db_pool.updates._background_update_progress(
                _BackgroundUpdates.DELETE_UNREFERENCED_STATE_GROUPS_BG_UPDATE,
                progress,
            )
        else:
            # This background process is finished.
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.DELETE_UNREFERENCED_STATE_GROUPS_BG_UPDATE
            )

        return batch_size

    async def _delete_unreferenced_state_groups_batch(
        self, last_checked_state_group: int, batch_size: int
    ) -> tuple[int, bool]:
        """Looks for unreferenced state groups starting from the last state group
        checked, and any state groups which would become unreferenced if a state group
        was deleted, and marks them for deletion.

        Args:
            last_checked_state_group: The last state group that was checked.
            batch_size: How many state groups to process in this iteration.

        Returns:
            (last_checked_state_group, final_batch)
        """

        # Look for state groups that can be cleaned up.
        def get_next_state_groups_txn(txn: LoggingTransaction) -> Set[int]:
            state_group_sql = (
                "SELECT id FROM state_groups WHERE id > ? ORDER BY id LIMIT ?"
            )
            txn.execute(state_group_sql, (last_checked_state_group, batch_size))

            next_set = {row[0] for row in txn}

            return next_set

        next_set = await self.db_pool.runInteraction(
            "get_next_state_groups", get_next_state_groups_txn
        )

        final_batch = False
        if len(next_set) < batch_size:
            final_batch = True
        else:
            last_checked_state_group = max(next_set)

        if len(next_set) == 0:
            return last_checked_state_group, final_batch

        # Discard any state groups referenced directly by an event...
        rows = cast(
            List[Tuple[int]],
            await self.db_pool.simple_select_many_batch(
                table="event_to_state_groups",
                column="state_group",
                iterable=next_set,
                keyvalues={},
                retcols=("DISTINCT state_group",),
                desc="get_referenced_state_groups",
            ),
        )

        next_set.difference_update({row[0] for row in rows})

        if len(next_set) == 0:
            return last_checked_state_group, final_batch

        # ... and discard any that are referenced by other state groups.
        rows = cast(
            List[Tuple[int]],
            await self.db_pool.simple_select_many_batch(
                table="state_group_edges",
                column="prev_state_group",
                iterable=next_set,
                keyvalues={},
                retcols=("DISTINCT prev_state_group",),
                desc="get_previous_state_groups",
            ),
        )

        next_set.difference_update(row[0] for row in rows)

        if len(next_set) == 0:
            return last_checked_state_group, final_batch

        # Find all state groups that can be deleted if the original set is deleted.
        # This set includes the original set, as well as any state groups that would
        # become unreferenced upon deleting the original set.
        to_delete = await find_unreferenced_groups(self.db_pool, next_set)

        if len(to_delete) == 0:
            return last_checked_state_group, final_batch

        def mark_for_deletion_txn(
            txn: LoggingTransaction, state_groups: Set[int]
        ) -> None:
            # Mark the state groups for deletion by the deletion background task.
            sql = """
                INSERT INTO state_groups_pending_deletion (state_group, insertion_ts)
                VALUES %s
                ON CONFLICT (state_group)
                DO NOTHING
            """

            now = self._clock.time_msec()
            deletion_rows = [
                (
                    state_group,
                    now,
                )
                for state_group in state_groups
            ]

            if isinstance(txn.database_engine, PostgresEngine):
                txn.execute_values(sql % ("?",), deletion_rows, fetch=False)
            else:
                txn.execute_batch(sql % ("(?, ?)",), deletion_rows)

        await self.db_pool.runInteraction(
            "mark_state_groups_for_deletion", mark_for_deletion_txn, to_delete
        )

        return last_checked_state_group, final_batch


async def find_unreferenced_groups(
    db_pool: DatabasePool, state_groups: Collection[int]
) -> Set[int]:
    """Used when purging history to figure out which state groups can be
    deleted.

    Args:
        state_groups: Set of state groups referenced by events
            that are going to be deleted.

    Returns:
        The set of state groups that can be deleted.
    """
    # Set of events that we have found to be referenced by events
    referenced_groups = set()

    # Set of state groups we've already seen
    state_groups_seen = set(state_groups)

    # Set of state groups to handle next.
    next_to_search = set(state_groups)
    while next_to_search:
        # We bound size of groups we're looking up at once, to stop the
        # SQL query getting too big
        if len(next_to_search) < 100:
            current_search = next_to_search
            next_to_search = set()
        else:
            current_search = set(itertools.islice(next_to_search, 100))
            next_to_search -= current_search

        referenced_state_groups = await db_pool.simple_select_many_batch(
            table="event_to_state_groups",
            column="state_group",
            iterable=current_search,
            keyvalues={},
            retcols=("DISTINCT state_group",),
            desc="get_referenced_state_groups",
        )

        referenced = {row[0] for row in referenced_state_groups}
        referenced_groups |= referenced

        # We don't continue iterating up the state group graphs for state
        # groups that are referenced.
        current_search -= referenced

        prev_state_groups = await db_pool.simple_select_many_batch(
            table="state_group_edges",
            column="state_group",
            iterable=current_search,
            keyvalues={},
            retcols=("state_group", "prev_state_group"),
            desc="get_previous_state_groups",
        )

        edges = dict(prev_state_groups)

        prevs = set(edges.values())
        # We don't bother re-handling groups we've already seen
        prevs -= state_groups_seen
        next_to_search |= prevs
        state_groups_seen |= prevs

        # We also check to see if anything referencing the state groups are
        # also unreferenced. This helps ensure that we delete unreferenced
        # state groups, if we don't then we will de-delta them when we
        # delete the other state groups leading to increased DB usage.
        next_state_groups = await db_pool.simple_select_many_batch(
            table="state_group_edges",
            column="prev_state_group",
            iterable=current_search,
            keyvalues={},
            retcols=("state_group", "prev_state_group"),
            desc="get_next_state_groups",
        )

        next_edges = dict(next_state_groups)
        nexts = set(next_edges.keys())
        nexts -= state_groups_seen
        next_to_search |= nexts
        state_groups_seen |= nexts

    to_delete = state_groups_seen - referenced_groups

    return to_delete
