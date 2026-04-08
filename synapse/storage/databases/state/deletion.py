#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#


import contextlib
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    AsyncIterator,
    Collection,
    Mapping,
)

from synapse.events.snapshot import EventPersistencePair
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.engines import PostgresEngine
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer


class StateDeletionDataStore:
    """Manages deletion of state groups in a safe manner.

    Deleting state groups is challenging as before we actually delete them we
    need to ensure that there are no in-flight events that refer to the state
    groups that we want to delete.

    To handle this, we take two approaches. First, before we persist any event
    we ensure that the state group still exists and mark in the
    `state_groups_persisting` table that the state group is about to be used.
    (Note that we have to have the extra table here as state groups and events
    can be in different databases, and thus we can't check for the existence of
    state groups in the persist event transaction). Once the event has been
    persisted, we can remove the row from  `state_groups_persisting`. So long as
    we check that table before deleting state groups, we can ensure that we
    never persist events that reference deleted state groups, maintaining
    database integrity.

    However, we want to avoid throwing exceptions so deep in the process of
    persisting events. So instead of deleting state groups immediately, we mark
    them as pending/proposed for deletion and wait for a certain amount of time
    before performing the deletion. When we come to handle new events that
    reference state groups, we check if they are pending deletion and bump the
    time for when they'll be deleted (to give a chance for the event to be
    persisted, or not).

    When deleting, we need to check that state groups remain unreferenced. There
    is a race here where we a) fetch state groups that are ready for deletion,
    b) check they're unreferenced, c) the state group becomes referenced but
    then gets marked as pending deletion again, d) during the deletion
    transaction we recheck `state_groups_pending_deletion` table again and see
    that it exists and so continue with the deletion. To prevent this from
    happening we add a `sequence_number` column to
    `state_groups_pending_deletion`, and during deletion we ensure that for a
    state group we're about to delete that the sequence number doesn't change
    between steps (a) and (d). So long as we always bump the sequence number
    whenever an event may become used the race can never happen.
    """

    # How long to wait before we delete state groups. This should be long enough
    # for any in-flight events to be persisted. If events take longer to persist
    # and any of the state groups they reference have been deleted, then the
    # event will fail to persist (as well as any event in the same batch).
    DELAY_BEFORE_DELETION_MS = 10 * 60 * 1000

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self._clock = hs.get_clock()
        self.db_pool = database
        self._instance_name = hs.get_instance_name()

        with db_conn.cursor(txn_name="_clear_existing_persising") as txn:
            self._clear_existing_persising(txn)

    def _clear_existing_persising(self, txn: LoggingTransaction) -> None:
        """On startup we clear any entries in `state_groups_persisting` that
        match our instance name, in case of a previous unclean shutdown"""

        self.db_pool.simple_delete_txn(
            txn,
            table="state_groups_persisting",
            keyvalues={"instance_name": self._instance_name},
        )

    async def check_state_groups_and_bump_deletion(
        self, state_groups: AbstractSet[int]
    ) -> Collection[int]:
        """Checks to make sure that the state groups haven't been deleted, and
        if they're pending deletion we delay it (allowing time for any event
        that will use them to finish persisting).

        Returns:
            The state groups that are missing, if any.
        """

        return await self.db_pool.runInteraction(
            "check_state_groups_and_bump_deletion",
            self._check_state_groups_and_bump_deletion_txn,
            state_groups,
            # We don't need to lock if we're just doing a quick check, as the
            # lock doesn't prevent any races here.
            lock=False,
        )

    def _check_state_groups_and_bump_deletion_txn(
        self, txn: LoggingTransaction, state_groups: AbstractSet[int], lock: bool = True
    ) -> Collection[int]:
        """Checks to make sure that the state groups haven't been deleted, and
        if they're pending deletion we delay it (allowing time for any event
        that will use them to finish persisting).

        The `lock` flag sets if we should lock the `state_group` rows we're
        checking, which we should do when storing new groups.

        Returns:
            The state groups that are missing, if any.
        """

        existing_state_groups = self._get_existing_groups_with_lock(
            txn, state_groups, lock=lock
        )

        self._bump_deletion_txn(txn, existing_state_groups)

        missing_state_groups = state_groups - existing_state_groups
        if missing_state_groups:
            return missing_state_groups

        return ()

    def _bump_deletion_txn(
        self, txn: LoggingTransaction, state_groups: Collection[int]
    ) -> None:
        """Update any pending deletions of the state group that they may now be
        referenced."""

        if not state_groups:
            return

        now = self._clock.time_msec()
        if isinstance(self.db_pool.engine, PostgresEngine):
            clause, args = make_in_list_sql_clause(
                self.db_pool.engine, "state_group", state_groups
            )
            sql = f"""
                UPDATE state_groups_pending_deletion
                SET sequence_number = DEFAULT, insertion_ts = ?
                WHERE {clause}
            """
            args.insert(0, now)
            txn.execute(sql, args)
        else:
            rows = self.db_pool.simple_select_many_txn(
                txn,
                table="state_groups_pending_deletion",
                column="state_group",
                iterable=state_groups,
                keyvalues={},
                retcols=("state_group",),
            )
            if not rows:
                return

            state_groups_to_update = [state_group for (state_group,) in rows]

            self.db_pool.simple_delete_many_txn(
                txn,
                table="state_groups_pending_deletion",
                column="state_group",
                values=state_groups_to_update,
                keyvalues={},
            )
            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_pending_deletion",
                keys=("state_group", "insertion_ts"),
                values=[(state_group, now) for state_group in state_groups_to_update],
            )

    def _get_existing_groups_with_lock(
        self, txn: LoggingTransaction, state_groups: Collection[int], lock: bool = True
    ) -> AbstractSet[int]:
        """Return which of the given state groups are in the database, and locks
        those rows with `KEY SHARE` to ensure they don't get concurrently
        deleted (if `lock` is true)."""
        clause, args = make_in_list_sql_clause(self.db_pool.engine, "id", state_groups)

        sql = f"""
            SELECT id FROM state_groups
            WHERE {clause}
        """
        if lock and isinstance(self.db_pool.engine, PostgresEngine):
            # On postgres we add a row level lock to the rows to ensure that we
            # conflict with any concurrent DELETEs. `FOR KEY SHARE` lock will
            # not conflict with other read
            sql += """
            FOR KEY SHARE
            """

        txn.execute(sql, args)
        return {state_group for (state_group,) in txn}

    @contextlib.asynccontextmanager
    async def persisting_state_group_references(
        self, event_and_contexts: Collection[EventPersistencePair]
    ) -> AsyncIterator[None]:
        """Wraps the persistence of the given events and contexts, ensuring that
        any state groups referenced still exist and that they don't get deleted
        during this."""

        referenced_state_groups: set[int] = set()
        for event, ctx in event_and_contexts:
            if ctx.rejected or event.internal_metadata.is_outlier():
                continue

            assert ctx.state_group is not None

            referenced_state_groups.add(ctx.state_group)

            if ctx.state_group_before_event:
                referenced_state_groups.add(ctx.state_group_before_event)

        if not referenced_state_groups:
            # We don't reference any state groups, so nothing to do
            yield
            return

        await self.db_pool.runInteraction(
            "mark_state_groups_as_persisting",
            self._mark_state_groups_as_persisting_txn,
            referenced_state_groups,
        )

        error = True
        try:
            yield None
            error = False
        finally:
            await self.db_pool.runInteraction(
                "finish_persisting",
                self._finish_persisting_txn,
                referenced_state_groups,
                error=error,
            )

    def _mark_state_groups_as_persisting_txn(
        self, txn: LoggingTransaction, state_groups: set[int]
    ) -> None:
        """Marks the given state groups as being persisted."""

        existing_state_groups = self._get_existing_groups_with_lock(txn, state_groups)
        missing_state_groups = state_groups - existing_state_groups
        if missing_state_groups:
            raise Exception(
                f"state groups have been deleted: {shortstr(missing_state_groups)}"
            )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="state_groups_persisting",
            keys=("state_group", "instance_name"),
            values=[(state_group, self._instance_name) for state_group in state_groups],
        )

    def _finish_persisting_txn(
        self, txn: LoggingTransaction, state_groups: Collection[int], error: bool
    ) -> None:
        """Mark the state groups as having finished persistence.

        If `error` is true then we assume the state groups were not persisted,
        and so we do not clear them from the pending deletion table.
        """
        self.db_pool.simple_delete_many_txn(
            txn,
            table="state_groups_persisting",
            column="state_group",
            values=state_groups,
            keyvalues={"instance_name": self._instance_name},
        )

        if error:
            # The state groups may or may not have been persisted, so we need to
            # bump the deletion to ensure we recheck if they have become
            # referenced.
            self._bump_deletion_txn(txn, state_groups)
            return

        self.db_pool.simple_delete_many_batch_txn(
            txn,
            table="state_groups_pending_deletion",
            keys=("state_group",),
            values=[(state_group,) for state_group in state_groups],
        )

    async def mark_state_groups_as_pending_deletion(
        self, state_groups: Collection[int]
    ) -> None:
        """Mark the given state groups as pending deletion.

        If any of the state groups are already pending deletion, then those records are
        left as is.
        """

        await self.db_pool.runInteraction(
            "mark_state_groups_as_pending_deletion",
            self._mark_state_groups_as_pending_deletion_txn,
            state_groups,
        )

    def _mark_state_groups_as_pending_deletion_txn(
        self,
        txn: LoggingTransaction,
        state_groups: Collection[int],
    ) -> None:
        sql = """
        INSERT INTO state_groups_pending_deletion (state_group, insertion_ts)
        VALUES %s
        ON CONFLICT (state_group)
        DO NOTHING
        """

        now = self._clock.time_msec()
        rows = [
            (
                state_group,
                now,
            )
            for state_group in state_groups
        ]
        if isinstance(txn.database_engine, PostgresEngine):
            txn.execute_values(sql % ("?",), rows, fetch=False)
        else:
            txn.execute_batch(sql % ("(?, ?)",), rows)

    async def mark_state_groups_as_used(self, state_groups: Collection[int]) -> None:
        """Mark the given state groups as now being referenced"""

        await self.db_pool.simple_delete_many(
            table="state_groups_pending_deletion",
            column="state_group",
            iterable=state_groups,
            keyvalues={},
            desc="mark_state_groups_as_used",
        )

    async def get_pending_deletions(
        self, state_groups: Collection[int]
    ) -> Mapping[int, int]:
        """Get which state groups are pending deletion.

        Returns:
            a mapping from state groups that are pending deletion to their
            sequence number
        """

        rows = await self.db_pool.simple_select_many_batch(
            table="state_groups_pending_deletion",
            column="state_group",
            iterable=state_groups,
            retcols=("state_group", "sequence_number"),
            keyvalues={},
            desc="get_pending_deletions",
        )

        return dict(rows)

    def get_state_groups_ready_for_potential_deletion_txn(
        self,
        txn: LoggingTransaction,
        state_groups_to_sequence_numbers: Mapping[int, int],
    ) -> Collection[int]:
        """Given a set of state groups, return which state groups can
        potentially be deleted.

        The state groups must have been checked to see if they remain
        unreferenced before calling this function.

        Note: This must be called within the same transaction that the state
        groups are deleted.

        Args:
            state_groups_to_sequence_numbers: The state groups, and the sequence
                numbers from before the state groups were checked to see if they
                were unreferenced.

        Returns:
            The subset of state groups that can safely be deleted

        """

        if not state_groups_to_sequence_numbers:
            return state_groups_to_sequence_numbers

        if isinstance(self.db_pool.engine, PostgresEngine):
            # On postgres we want to lock the rows FOR UPDATE as early as
            # possible to help conflicts.
            clause, args = make_in_list_sql_clause(
                self.db_pool.engine, "id", state_groups_to_sequence_numbers
            )
            sql = f"""
                SELECT id FROM state_groups
                WHERE {clause}
                FOR UPDATE
            """
            txn.execute(sql, args)

        # Check the deletion status in the DB of the given state groups
        clause, args = make_in_list_sql_clause(
            self.db_pool.engine,
            column="state_group",
            iterable=state_groups_to_sequence_numbers,
        )

        sql = f"""
            SELECT state_group, insertion_ts, sequence_number FROM (
                SELECT state_group, insertion_ts, sequence_number FROM state_groups_pending_deletion
                UNION
                SELECT state_group, null, null FROM state_groups_persisting
            ) AS s
            WHERE {clause}
        """

        txn.execute(sql, args)

        # The above query will return potentially two rows per state group (one
        # for each table), so we track which state groups have enough time
        # elapsed and which are not ready to be persisted.
        ready_to_be_deleted = set()
        not_ready_to_be_deleted = set()

        now = self._clock.time_msec()
        for state_group, insertion_ts, sequence_number in txn:
            if insertion_ts is None:
                # A null insertion_ts means that we are currently persisting
                # events that reference the state group, so we don't delete
                # them.
                not_ready_to_be_deleted.add(state_group)
                continue

            # We know this can't be None if insertion_ts is not None
            assert sequence_number is not None

            # Check if the sequence number has changed, if it has then it
            # indicates that the state group may have become referenced since we
            # checked.
            if state_groups_to_sequence_numbers[state_group] != sequence_number:
                not_ready_to_be_deleted.add(state_group)
                continue

            if now - insertion_ts < self.DELAY_BEFORE_DELETION_MS:
                # Not enough time has elapsed to allow us to delete.
                not_ready_to_be_deleted.add(state_group)
                continue

            ready_to_be_deleted.add(state_group)

        can_be_deleted = ready_to_be_deleted - not_ready_to_be_deleted
        if not_ready_to_be_deleted:
            # If there are any state groups that aren't ready to be deleted,
            # then we also need to remove any state groups that are referenced
            # by them.
            clause, args = make_in_list_sql_clause(
                self.db_pool.engine,
                column="state_group",
                iterable=state_groups_to_sequence_numbers,
            )
            sql = f"""
                WITH RECURSIVE ancestors(state_group) AS (
                    SELECT DISTINCT prev_state_group
                    FROM state_group_edges WHERE {clause}
                    UNION
                    SELECT prev_state_group
                    FROM state_group_edges
                    INNER JOIN ancestors USING (state_group)
                )
                SELECT state_group FROM ancestors
            """
            txn.execute(sql, args)

            can_be_deleted.difference_update(state_group for (state_group,) in txn)

        return can_be_deleted

    async def get_next_state_group_collection_to_delete(
        self,
    ) -> tuple[str, Mapping[int, int]] | None:
        """Get the next set of state groups to try and delete

        Returns:
            2-tuple of room_id and mapping of state groups to sequence number.
        """
        return await self.db_pool.runInteraction(
            "get_next_state_group_collection_to_delete",
            self._get_next_state_group_collection_to_delete_txn,
        )

    def _get_next_state_group_collection_to_delete_txn(
        self,
        txn: LoggingTransaction,
    ) -> tuple[str, Mapping[int, int]] | None:
        """Implementation of `get_next_state_group_collection_to_delete`"""

        # We want to return chunks of state groups that were marked for deletion
        # at the same time (this isn't necessary, just more efficient). We do
        # this by looking for the oldest insertion_ts, and then pulling out all
        # rows that have the same insertion_ts (and room ID).
        now = self._clock.time_msec()

        sql = """
            SELECT room_id, insertion_ts
            FROM state_groups_pending_deletion AS sd
            INNER JOIN state_groups AS sg ON (id = sd.state_group)
            LEFT JOIN state_groups_persisting AS sp USING (state_group)
            WHERE insertion_ts < ? AND sp.state_group IS NULL
            ORDER BY insertion_ts
            LIMIT 1
        """
        txn.execute(sql, (now - self.DELAY_BEFORE_DELETION_MS,))
        row = txn.fetchone()
        if not row:
            return None

        (room_id, insertion_ts) = row

        sql = """
            SELECT state_group, sequence_number
            FROM state_groups_pending_deletion AS sd
            INNER JOIN state_groups AS sg ON (id = sd.state_group)
            LEFT JOIN state_groups_persisting AS sp USING (state_group)
            WHERE room_id = ? AND insertion_ts = ? AND sp.state_group IS NULL
            ORDER BY insertion_ts
        """
        txn.execute(sql, (room_id, insertion_ts))

        return room_id, dict(txn)
