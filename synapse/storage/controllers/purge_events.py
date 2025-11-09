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

import itertools
import logging
from typing import (
    TYPE_CHECKING,
    Collection,
    Mapping,
)

from synapse.logging.context import nested_logging_context
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases import Databases
from synapse.types.storage import _BackgroundUpdates
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class PurgeEventsStorageController:
    """High level interface for purging rooms and event history."""

    def __init__(self, hs: "HomeServer", stores: Databases):
        self.hs = hs  # nb must be called this for @wrap_as_background_process
        self.server_name = hs.hostname
        self.stores = stores

        if hs.config.worker.run_background_tasks:
            self._delete_state_loop_call = hs.get_clock().looping_call(
                self._delete_state_groups_loop, 60 * 1000
            )

        self.stores.state.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE,
            self._background_delete_unrefereneced_state_groups,
        )

    async def purge_room(self, room_id: str) -> None:
        """Deletes all record of a room"""

        with nested_logging_context(room_id):
            await self.stores.main.purge_room(room_id)
            await self.stores.state.purge_room_state(room_id)

    async def purge_history(
        self, room_id: str, token: str, delete_local_events: bool
    ) -> None:
        """Deletes room history before a certain point

        Args:
            room_id: The room ID

            token: A topological token to delete events before

            delete_local_events:
                if True, we will delete local events as well as remote ones
                (instead of just marking them as outliers and deleting their
                state groups).
        """
        with nested_logging_context(room_id):
            state_groups = await self.stores.main.purge_history(
                room_id, token, delete_local_events
            )

            logger.info("[purge] finding state groups that can be deleted")
            sg_to_delete = await self._find_unreferenced_groups(state_groups)

            # Mark these state groups as pending deletion, they will actually
            # get deleted automatically later.
            await self.stores.state_deletion.mark_state_groups_as_pending_deletion(
                sg_to_delete
            )

    async def _find_unreferenced_groups(
        self,
        state_groups: Collection[int],
    ) -> set[int]:
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

            referenced = await self.stores.main.get_referenced_state_groups(
                current_search
            )
            referenced_groups |= referenced

            # We don't continue iterating up the state group graphs for state
            # groups that are referenced.
            current_search -= referenced

            edges = await self.stores.state.get_previous_state_groups(current_search)

            prevs = set(edges.values())
            # We don't bother re-handling groups we've already seen
            prevs -= state_groups_seen
            next_to_search |= prevs
            state_groups_seen |= prevs

            # We also check to see if anything referencing the state groups are
            # also unreferenced. This helps ensure that we delete unreferenced
            # state groups, if we don't then we will de-delta them when we
            # delete the other state groups leading to increased DB usage.
            next_edges = await self.stores.state.get_next_state_groups(current_search)
            nexts = set(next_edges.keys())
            nexts -= state_groups_seen
            next_to_search |= nexts
            state_groups_seen |= nexts

        to_delete = state_groups_seen - referenced_groups

        return to_delete

    @wrap_as_background_process("_delete_state_groups_loop")
    async def _delete_state_groups_loop(self) -> None:
        """Background task that deletes any state groups that may be pending
        deletion."""

        while True:
            next_to_delete = await self.stores.state_deletion.get_next_state_group_collection_to_delete()
            if next_to_delete is None:
                break

            (room_id, groups_to_sequences) = next_to_delete

            logger.info(
                "[purge] deleting state groups for room %s: %s",
                room_id,
                shortstr(groups_to_sequences.keys(), maxitems=10),
            )
            made_progress = await self._delete_state_groups(
                room_id, groups_to_sequences
            )

            # If no progress was made in deleting the state groups, then we
            # break to allow a pause before trying again next time we get
            # called.
            if not made_progress:
                break

    async def _delete_state_groups(
        self, room_id: str, groups_to_sequences: Mapping[int, int]
    ) -> bool:
        """Tries to delete the given state groups.

        Returns:
            Whether we made progress in deleting the state groups (or marking
            them as referenced).
        """

        # We double check if any of the state groups have become referenced.
        # This shouldn't happen, as any usages should cause the state group to
        # be removed as pending deletion.
        referenced_state_groups = await self.stores.main.get_referenced_state_groups(
            groups_to_sequences
        )

        if referenced_state_groups:
            # We mark any state groups that have become referenced as being
            # used.
            await self.stores.state_deletion.mark_state_groups_as_used(
                referenced_state_groups
            )

            # Update list of state groups to remove referenced ones
            groups_to_sequences = {
                state_group: sequence_number
                for state_group, sequence_number in groups_to_sequences.items()
                if state_group not in referenced_state_groups
            }

        if not groups_to_sequences:
            # We made progress here as long as we marked some state groups as
            # now referenced.
            return len(referenced_state_groups) > 0

        return await self.stores.state.purge_unreferenced_state_groups(
            room_id,
            groups_to_sequences,
        )

    async def _background_delete_unrefereneced_state_groups(
        self, progress: dict, batch_size: int
    ) -> int:
        """This background update will slowly delete any unreferenced state groups"""

        last_checked_state_group = progress.get("last_checked_state_group")

        if last_checked_state_group is None:
            # This is the first run.
            last_checked_state_group = (
                await self.stores.state.db_pool.simple_select_one_onecol(
                    table="state_groups",
                    keyvalues={},
                    retcol="MAX(id)",
                    allow_none=True,
                    desc="get_max_state_group",
                )
            )
            if last_checked_state_group is None:
                # There are no state groups so the background process is finished.
                await self.stores.state.db_pool.updates._end_background_update(
                    _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE
                )
                return batch_size
            last_checked_state_group += 1

        (
            last_checked_state_group,
            final_batch,
        ) = await self._delete_unreferenced_state_groups_batch(
            last_checked_state_group,
            batch_size,
        )

        if not final_batch:
            # There are more state groups to check.
            progress = {
                "last_checked_state_group": last_checked_state_group,
            }
            await self.stores.state.db_pool.updates._background_update_progress(
                _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE,
                progress,
            )
        else:
            # This background process is finished.
            await self.stores.state.db_pool.updates._end_background_update(
                _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE
            )

        return batch_size

    async def _delete_unreferenced_state_groups_batch(
        self,
        last_checked_state_group: int,
        batch_size: int,
    ) -> tuple[int, bool]:
        """Looks for unreferenced state groups starting from the last state group
        checked and marks them for deletion.

        Args:
            last_checked_state_group: The last state group that was checked.
            batch_size: How many state groups to process in this iteration.

        Returns:
            (last_checked_state_group, final_batch)
        """

        # Find all state groups that can be deleted if any of the original set are deleted.
        (
            to_delete,
            last_checked_state_group,
            final_batch,
        ) = await self._find_unreferenced_groups_for_background_deletion(
            last_checked_state_group, batch_size
        )

        if len(to_delete) == 0:
            return last_checked_state_group, final_batch

        await self.stores.state_deletion.mark_state_groups_as_pending_deletion(
            to_delete
        )

        return last_checked_state_group, final_batch

    async def _find_unreferenced_groups_for_background_deletion(
        self,
        last_checked_state_group: int,
        batch_size: int,
    ) -> tuple[set[int], int, bool]:
        """Used when deleting unreferenced state groups in the background to figure out
        which state groups can be deleted.
        To avoid increased DB usage due to de-deltaing state groups, this returns only
        state groups which are free standing (ie. no shared edges with referenced groups) or
        state groups which do not share edges which result in a future referenced group.

        The following scenarios outline the possibilities based on state group data in
        the DB.

        ie. Free standing -> state groups 1-N would be returned:
            SG_1
            |
            ...
            |
            SG_N

        ie. Previous reference -> state groups 2-N would be returned:
            SG_1 <- referenced by event
            |
            SG_2
            |
            ...
            |
            SG_N

        ie. Future reference -> none of the following state groups would be returned:
            SG_1
            |
            SG_2
            |
            ...
            |
            SG_N <- referenced by event

        Args:
            last_checked_state_group: The last state group that was checked.
            batch_size: How many state groups to process in this iteration.

        Returns:
            (to_delete, last_checked_state_group, final_batch)
        """

        # If a state group's next edge is not pending deletion then we don't delete the state group.
        # If there is no next edge or the next edges are all marked for deletion, then delete
        # the state group.
        # This holds since we walk backwards from the latest state groups, ensuring that
        # we've already checked newer state groups for event references along the way.
        def get_next_state_groups_marked_for_deletion_txn(
            txn: LoggingTransaction,
        ) -> tuple[dict[int, bool], dict[int, int]]:
            state_group_sql = """
                SELECT s.id, e.state_group, d.state_group
                FROM (
                    SELECT id FROM state_groups
                    WHERE id < ? ORDER BY id DESC LIMIT ?
                ) as s
                LEFT JOIN state_group_edges AS e ON (s.id = e.prev_state_group)
                LEFT JOIN state_groups_pending_deletion AS d ON (e.state_group = d.state_group)
            """
            txn.execute(state_group_sql, (last_checked_state_group, batch_size))

            # Mapping from state group to whether we should delete it.
            state_groups_to_deletion: dict[int, bool] = {}

            # Mapping from state group to prev state group.
            state_groups_to_prev: dict[int, int] = {}

            for row in txn:
                state_group = row[0]
                next_edge = row[1]
                pending_deletion = row[2]

                if next_edge is not None:
                    state_groups_to_prev[next_edge] = state_group

                if next_edge is not None and not pending_deletion:
                    # We have found an edge not marked for deletion.
                    # Check previous results to see if this group is part of a chain
                    # within this batch that qualifies for deletion.
                    # ie. batch contains:
                    # SG_1 -> SG_2 -> SG_3
                    # If SG_3 is a candidate for deletion, then SG_2 & SG_1 should also
                    # be, even though they have edges which may not be marked for
                    # deletion.
                    # This relies on SQL results being sorted in DESC order to work.
                    next_is_deletion_candidate = state_groups_to_deletion.get(next_edge)
                    if (
                        next_is_deletion_candidate is None
                        or not next_is_deletion_candidate
                    ):
                        state_groups_to_deletion[state_group] = False
                    else:
                        state_groups_to_deletion.setdefault(state_group, True)
                else:
                    # This state group may be a candidate for deletion
                    state_groups_to_deletion.setdefault(state_group, True)

            return state_groups_to_deletion, state_groups_to_prev

        (
            state_groups_to_deletion,
            state_group_edges,
        ) = await self.stores.state.db_pool.runInteraction(
            "get_next_state_groups_marked_for_deletion",
            get_next_state_groups_marked_for_deletion_txn,
        )
        deletion_candidates = {
            state_group
            for state_group, deletion in state_groups_to_deletion.items()
            if deletion
        }

        final_batch = False
        state_groups = state_groups_to_deletion.keys()
        if len(state_groups) < batch_size:
            final_batch = True
        else:
            last_checked_state_group = min(state_groups)

        if len(state_groups) == 0:
            return set(), last_checked_state_group, final_batch

        # Determine if any of the remaining state groups are directly referenced.
        referenced = await self.stores.main.get_referenced_state_groups(
            deletion_candidates
        )

        # Remove state groups from deletion_candidates which are directly referenced or share a
        # future edge with a referenced state group within this batch.
        def filter_reference_chains(group: int | None) -> None:
            while group is not None:
                deletion_candidates.discard(group)
                group = state_group_edges.get(group)

        for referenced_group in referenced:
            filter_reference_chains(referenced_group)

        return deletion_candidates, last_checked_state_group, final_batch
