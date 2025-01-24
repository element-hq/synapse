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
from typing import TYPE_CHECKING, AsyncIterator, Collection, Dict, Optional, Set, Tuple

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

    Deleting state groups is challenging as we need to ensure that any in-flight
    events that are yet to be persisted do not refer to any state groups that we
    want to delete.

    To handle this, we have a concept of "state epochs", which slowly increment
    over time. To delete a state group we first add it to the list of "pending
    deletions" with the current epoch, and wait until a certain number of epochs
    have passed before attempting to actually delete the state group. If during
    this period an event that references the state group tries to be persisted,
    then we check if too many state epochs have passed, if they have we reject
    the attempt to persist the event, and if not we clear the state groups from
    the pending deletion list (as they're now referenced).
    """

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
            self._clock.looping_call_now(self._advance_state_epoch, 2 * 60 * 1000)

    @wrap_as_background_process("_advance_state_epoch")
    async def _advance_state_epoch(self) -> None:
        """Advances the state epoch, checking that we haven't advanced it too
        recently.
        """

        now = self._clock.time_msec()
        update_if_before_ts = now - 10 * 60 * 1000

        def advance_state_epoch_txn(txn: LoggingTransaction) -> None:
            sql = """
                UPDATE state_epoch
                SET state_epoch = state_epoch + 1, updated_ts = ?
                WHERE updated_ts <= ?
            """
            txn.execute(
                sql,
                (
                    now,
                    update_if_before_ts,
                ),
            )

        await self.db_pool.runInteraction(
            "_advance_state_epoch", advance_state_epoch_txn, db_autocommit=True
        )

    async def get_state_epoch(self) -> int:
        return await self.db_pool.simple_select_one_onecol(
            table="state_epoch",
            retcol="state_epoch",
            keyvalues={},
            desc="get_state_epoch",
        )

    def _mark_state_groups_as_used_txn(
        self, txn: LoggingTransaction, state_epoch: int, state_groups: Set[int]
    ) -> None:
        current_state_epoch = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="state_epoch",
            retcol="state_epoch",
            keyvalues={},
        )

        # TODO: Move to constant. Is the equality correct?
        if current_state_epoch - state_epoch >= 2:
            raise Exception("FOO")

        clause, values = make_in_list_sql_clause(
            txn.database_engine,
            "id",
            state_groups,
        )
        sql = f"""
            SELECT id, state_epoch
            FROM state_groups
            LEFT JOIN state_groups_pending_deletion ON (id = state_group)
            WHERE {clause}
        """

        if isinstance(self.db_pool.engine, PostgresEngine):
            # On postgres we add a row level lock to the rows to ensure that we
            # conflict with any concurrent DELETEs. `FOR KEY SHARE` lock will
            # not conflict with other reads.
            sql += """
            FOR KEY SHARE OF state_groups
            """

        txn.execute(sql, values)

        state_group_to_epoch: Dict[int, Optional[int]] = {row[0]: row[1] for row in txn}

        missing_state_groups = state_groups - state_group_to_epoch.keys()
        if missing_state_groups:
            raise Exception(
                f"state groups have been deleted: {shortstr(missing_state_groups)}"
            )

        for state_epoch_deletion in state_group_to_epoch.values():
            if state_epoch_deletion is None:
                continue

            if current_state_epoch - state_epoch_deletion >= 2:
                raise Exception("FOO")  # TODO

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

    async def is_state_group_pending_deletion(self, state_group: int) -> bool:
        """Check if a state group is marked as pending deletion."""

        def is_state_group_pending_deletion_txn(txn: LoggingTransaction) -> bool:
            sql = """
                SELECT 1 FROM state_groups_pending_deletion
                WHERE state_group = ?
            """
            txn.execute(sql, (state_group,))

            return txn.fetchone() is not None

        return await self.db_pool.runInteraction(
            "is_state_group_pending_deletion",
            is_state_group_pending_deletion_txn,
        )

    async def are_state_groups_pending_deletion(
        self, state_groups: Collection[int]
    ) -> Collection[int]:
        rows = await self.db_pool.simple_select_many_batch(
            table="state_groups_pending_deletion",
            column="state_group",
            iterable=state_groups,
            retcols=("state_group",),
            desc="are_state_groups_pending_deletion",
        )
        return {row[0] for row in rows}

    async def mark_state_group_as_used(self, state_group: int) -> None:
        """Mark that a given state group is used"""

        # TODO: Also assert that the state group hasn't advanced too much

        await self.db_pool.simple_delete(
            table="state_groups_pending_deletion",
            keyvalues={"state_group": state_group},
            desc="mark_state_group_as_used",
        )

    def check_prev_group_before_insertion_txn(
        self, txn: LoggingTransaction, prev_group: int, new_groups: Collection[int]
    ) -> None:
        sql = """
            SELECT state_epoch, (SELECT state_epoch FROM state_epoch)
            FROM state_groups_pending_deletion
            WHERE state_group = ?
        """
        txn.execute(sql, (prev_group,))
        row = txn.fetchone()
        if row is not None:
            pending_deletion_epoch, current_epoch = row
            if current_epoch - pending_deletion_epoch >= 2:
                raise Exception("")  # TODO

            self.db_pool.simple_update_txn(
                txn,
                table="state_groups_pending_deletion",
                keyvalues={"state_group": prev_group},
                updatevalues={"state_epoch": current_epoch},
            )
            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_pending_deletion",
                keys=("state_group", "state_epoch"),
                values=[(state_group, current_epoch) for state_group in new_groups],
            )

    @contextlib.asynccontextmanager
    async def persisting_state_group_references(
        self, event_and_contexts: Collection[Tuple[EventBase, EventContext]]
    ) -> AsyncIterator[None]:
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
        min_state_epoch = min(state_epochs)

        await self.db_pool.runInteraction(
            "mark_state_groups_as_used",
            self._mark_state_groups_as_used_txn,
            min_state_epoch,
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

    def get_state_groups_that_can_be_purged(
        self, txn: LoggingTransaction, state_groups: Collection[int]
    ) -> Collection[int]:
        if not state_groups:
            return state_groups

        current_state_epoch = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="state_epoch",
            retcol="state_epoch",
            keyvalues={},
        )

        clause, args = make_in_list_sql_clause(
            self.db_pool.engine, column="state_group", iterable=state_groups
        )

        sql = f"""
            SELECT state_group FROM (
                SELECT state_group FROM state_groups_pending_deletion
                WHERE state_epoch > ?
                UNION
                SELECT state_group FROM state_groups_persisting
            ) AS s
            WHERE {clause}
        """
        args.insert(0, current_state_epoch - 2)

        txn.execute(sql, args)

        can_delete = set(state_groups)
        for (state_group,) in txn:
            can_delete.discard(state_group)

        return can_delete
