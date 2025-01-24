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
    Set,
    Tuple,
)

from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.metrics.background_process_metrics import wrap_as_background_process
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


class StateEpochDataStore:
    """Manages state epochs and checks for state group deletion.

    Deleting state groups is challenging as before we actually delete them we
    need to ensure that there are no in-flight events that refer to the state
    groups that we want to delete.

    To handle this, we take two approaches. First, before we persist any event
    we ensure that the state groups still exist and mark in the
    `state_groups_persisting` table that the state group is about to be used.
    (Note that we have to have the extra table here as state groups and events
    can be in different databases, and thus we can't check for the existence of
    state groups in the persist event transaction). Once the event has been
    persisted, we can remove the row from  `state_groups_persisting`. So long as
    we check that table before deleting state groups, we can ensure that we
    never persist events that reference deleted state groups, maintaining
    database integrity.

    However, we want to avoid throwing exceptions so deep in the process of
    persisting events. So we use a concept of `state_epochs`, where we mark
    state groups as pending/proposed for deletion and wait for a certain number
    epoch increments before performing the deletion. When we come to handle new
    events that reference state groups, we check if they are pending deletion
    and bump the epoch when they'll be deleted in (to give a chance for the
    event to be persisted, or not).
    """

    # How frequently, roughly, to increment epochs.
    TIME_BETWEEN_EPOCH_INCREMENTS_MS = 5 * 60 * 1000

    # The number of epoch increases that must have happened between marking a
    # state group as pending and actually deleting it.
    NUMBER_EPOCHS_BEFORE_DELETION = 3

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self._clock = hs.get_clock()
        self.db_pool = database
        self._instance_name = hs.get_instance_name()

        # TODO: Clear from `state_groups_persisting` any holdovers from previous
        # running instance.

        if hs.config.worker.run_background_tasks:
            # Add a background loop to periodically check if we should bump
            # state epoch.
            self._clock.looping_call_now(
                self._advance_state_epoch, self.TIME_BETWEEN_EPOCH_INCREMENTS_MS / 5
            )

    @wrap_as_background_process("_advance_state_epoch")
    async def _advance_state_epoch(self) -> None:
        """Advances the state epoch, checking that we haven't advanced it too
        recently.
        """

        now = self._clock.time_msec()
        update_if_before_ts = now - self.TIME_BETWEEN_EPOCH_INCREMENTS_MS

        def advance_state_epoch_txn(txn: LoggingTransaction) -> None:
            sql = """
                UPDATE state_epoch
                SET state_epoch = state_epoch + 1, updated_ts = ?
                WHERE updated_ts <= ?
            """
            txn.execute(sql, (now, update_if_before_ts))

        await self.db_pool.runInteraction(
            "_advance_state_epoch", advance_state_epoch_txn, db_autocommit=True
        )

    async def get_state_epoch(self) -> int:
        """Get the current state epoch"""
        return await self.db_pool.simple_select_one_onecol(
            table="state_epoch",
            retcol="state_epoch",
            keyvalues={},
            desc="get_state_epoch",
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
        )

    def _check_state_groups_and_bump_deletion_txn(
        self, txn: LoggingTransaction, state_groups: AbstractSet[int]
    ) -> Collection[int]:
        existing_state_groups = self._get_existing_groups_with_lock(txn, state_groups)
        if state_groups - existing_state_groups:
            return state_groups - existing_state_groups

        clause, args = make_in_list_sql_clause(
            self.db_pool.engine, "state_group", state_groups
        )
        sql = f"""
            UPDATE state_groups_pending_deletion
            SET state_epoch = (SELECT state_epoch FROM state_epoch)
            WHERE {clause}
        """

        txn.execute(sql, args)

        return ()

    def _get_existing_groups_with_lock(
        self, txn: LoggingTransaction, state_groups: Collection[int]
    ) -> AbstractSet[int]:
        """Return which of the given state groups are in the database, and locks
        those rows with `KEY SHARE` to ensure they don't get concurrently
        deleted."""
        clause, args = make_in_list_sql_clause(self.db_pool.engine, "id", state_groups)

        sql = f"""
            SELECT id FROM state_groups
            WHERE {clause}
        """
        if isinstance(self.db_pool.engine, PostgresEngine):
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
        self, event_and_contexts: Collection[Tuple[EventBase, EventContext]]
    ) -> AsyncIterator[None]:
        """Wraps the persistence of the given events and contexts, ensuring that
        any state groups referenced still exist and that they don't get deleted
        during this."""

        referenced_state_groups: Set[int] = set()
        state_epochs = []
        for event, ctx in event_and_contexts:
            if ctx.rejected or event.internal_metadata.is_outlier():
                continue

            assert ctx.state_epoch is not None
            assert ctx.state_group is not None

            state_epochs.append(ctx.state_epoch)

            referenced_state_groups.add(ctx.state_group)

            if ctx.state_group_before_event:
                referenced_state_groups.add(ctx.state_group_before_event)

        if not referenced_state_groups:
            # We don't reference any state groups, so nothing to do
            yield
            return

        assert state_epochs  # If we have state groups we have a state epoch
        # min_state_epoch = min(state_epochs)  # TODO

        await self.db_pool.runInteraction(
            "mark_state_groups_as_used",
            self._mark_state_groups_as_used_txn,
            referenced_state_groups,
        )

        try:
            yield None
        finally:
            await self.db_pool.simple_delete_many(
                table="state_groups_persisting",
                column="state_group",
                iterable=referenced_state_groups,
                keyvalues={"instance_name": self._instance_name},
                desc="persisting_state_group_references_delete",
            )

    def _mark_state_groups_as_used_txn(
        self, txn: LoggingTransaction, state_groups: Set[int]
    ) -> None:
        """Marks the given state groups as used. Also checks that the given
        state epoch is not too old."""

        existing_state_groups = self._get_existing_groups_with_lock(txn, state_groups)
        missing_state_groups = state_groups - existing_state_groups
        if missing_state_groups:
            raise Exception(
                f"state groups have been deleted: {shortstr(missing_state_groups)}"
            )

        self.db_pool.simple_delete_many_batch_txn(
            txn,
            table="state_groups_pending_deletion",
            keys=("state_group",),
            values=[(state_group,) for state_group in state_groups],
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="state_groups_persisting",
            keys=("state_group", "instance_name"),
            values=[(state_group, self._instance_name) for state_group in state_groups],
        )

    def get_state_groups_that_can_be_purged_txn(
        self, txn: LoggingTransaction, state_groups: Collection[int]
    ) -> Collection[int]:
        """Given a set of state groups, return which state groups can be deleted."""

        if not state_groups:
            return state_groups

        if isinstance(self.db_pool.engine, PostgresEngine):
            # On postgres we want to lock the rows FOR UPDATE as early as
            # possible to help conflicts.
            clause, args = make_in_list_sql_clause(
                self.db_pool.engine, "id", state_groups
            )
            sql = """
                SELECT id FROM state_groups
                WHERE {clause}
                FOR UPDATE
            """
            txn.execute(sql, args)

        current_state_epoch = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="state_epoch",
            retcol="state_epoch",
            keyvalues={},
        )

        # Check the deletion status in the DB of the given state groups
        clause, args = make_in_list_sql_clause(
            self.db_pool.engine, column="state_group", iterable=state_groups
        )

        sql = f"""
            SELECT state_group, state_epoch FROM (
                SELECT state_group, state_epoch FROM state_groups_pending_deletion
                UNION
                SELECT state_group, null FROM state_groups_persisting
            ) AS s
            WHERE {clause}
        """

        txn.execute(sql, args)

        can_delete = set()
        for state_group, state_epoch in txn:
            if state_epoch is None:
                # A null state epoch means that we are currently persisting
                # events that reference the state group, so we don't delete
                # them.
                continue

            if current_state_epoch - state_epoch < self.NUMBER_EPOCHS_BEFORE_DELETION:
                # Not enough state epochs have occurred to allow us to delete.
                continue

            can_delete.add(state_group)

        return can_delete
