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


from typing import TYPE_CHECKING, Collection, Tuple

from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer


class StateEpochDataStore:
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self._clock = hs.get_clock()
        self.db_pool = database

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

    async def mark_state_groups_as_used(
        self, event_and_contexts: Collection[Tuple[EventBase, EventContext]]
    ) -> None:
        referenced_state_groups = []
        state_epochs = []
        for event, ctx in event_and_contexts:
            if ctx.rejected or event.internal_metadata.is_outlier():
                continue

            assert ctx.state_epoch is not None
            assert ctx.state_group is not None

            state_epochs.append(ctx.state_epoch)

            referenced_state_groups.append(ctx.state_group)

            if ctx.state_group_before_event:
                referenced_state_groups.append(ctx.state_group_before_event)

        if not referenced_state_groups:
            # We don't reference any state groups, so nothing to do
            return

        assert state_epochs  # If we have state groups we have a state epoch
        min_state_epoch = min(state_epochs)

        await self.db_pool.runInteraction(
            "mark_state_groups_as_used",
            self._mark_state_groups_as_used_txn,
            min_state_epoch,
            referenced_state_groups,
        )

    def _mark_state_groups_as_used_txn(
        self, txn: LoggingTransaction, state_epoch: int, state_groups: Collection[int]
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

        self.db_pool.simple_delete_many_batch_txn(
            txn,
            table="state_groups_pending_deletion",
            keys=("state_group",),
            values=[(state_group,) for state_group in state_groups],
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
        return {row["state_group"] for row in rows}

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
