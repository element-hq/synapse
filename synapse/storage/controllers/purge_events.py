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
    Set,
)

from sortedcontainers import SortedSet

from synapse.logging.context import nested_logging_context
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases import Databases
from synapse.types.storage import _BackgroundUpdates

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

MAX_PROCESSED_GROUPS = 1000


class PurgeEventsStorageController:
    """High level interface for purging rooms and event history."""

    def __init__(self, hs: "HomeServer", stores: Databases):
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
        max_state_group = progress.get("max_state_group")
        processed_groups: SortedSet[int] = SortedSet(progress.get("processed_groups"))

        if last_checked_state_group is None or max_state_group is None:
            # This is the first run.
            last_checked_state_group = 0

            max_state_group = await self.stores.state.db_pool.simple_select_one_onecol(
                table="state_groups",
                keyvalues={},
                retcol="MAX(id)",
                allow_none=True,
                desc="get_max_state_group",
            )
            if max_state_group is None:
                # There are no state groups so the background process is finished.
                await self.stores.state.db_pool.updates._end_background_update(
                    _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE
                )
                return batch_size

        (
            last_checked_state_group,
            processed_groups,
            final_batch,
        ) = await self._delete_unreferenced_state_groups_batch(
            last_checked_state_group,
            batch_size,
            max_state_group,
            processed_groups,
        )

        if not final_batch:
            # There are more state groups to check.
            progress = {
                "last_checked_state_group": last_checked_state_group,
                "max_state_group": max_state_group,
                "processed_groups": list(processed_groups),
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
        max_state_group: int,
        processed_groups: SortedSet[int],
    ) -> tuple[int, SortedSet[int], bool]:
        """Looks for unreferenced state groups starting from the last state group
        checked, and any state groups which would become unreferenced if a state group
        was deleted, and marks them for deletion.

        Args:
            last_checked_state_group: The last state group that was checked.
            batch_size: How many state groups to process in this iteration.
            max_state_group: The state group to check up to.
            processed_groups: State groups that have already been processed. Only
                includes state groups ahead of the last_checked_state_group.

        Returns:
            (last_checked_state_group, processed_groups, final_batch)
        """

        # Look for state groups that can be cleaned up.
        def get_next_state_groups_txn(txn: LoggingTransaction) -> Set[int]:
            state_group_sql = "SELECT id FROM state_groups WHERE ? < id AND id <= ? ORDER BY id LIMIT ?"
            txn.execute(
                state_group_sql, (last_checked_state_group, max_state_group, batch_size)
            )

            next_set = {row[0] for row in txn}

            return next_set

        next_set = await self.stores.state.db_pool.runInteraction(
            "get_next_state_groups", get_next_state_groups_txn
        )

        final_batch = False
        if len(next_set) < batch_size:
            final_batch = True
        else:
            last_checked_state_group = max(next_set)

        if len(next_set) == 0:
            return last_checked_state_group, SortedSet(), final_batch

        # Clear out old state groups from the processed set.
        # Otherwise this can grow very large and we'll spend all our time de/serializing
        # it to json.
        processed_index = processed_groups.bisect_left(last_checked_state_group)
        del processed_groups[:processed_index]

        # Update the state groups set by removing groups that have already been processed.
        next_set = set(next_set) - processed_groups

        # Find all state groups that can be deleted if any of the original set are deleted.
        (
            to_delete,
            processed_groups,
        ) = await self._find_unreferenced_groups_for_background_deletion(
            next_set,
            processed_groups,
        )

        if len(to_delete) == 0:
            return last_checked_state_group, processed_groups, final_batch

        await self.stores.state_deletion.mark_state_groups_as_pending_deletion(
            to_delete
        )

        return last_checked_state_group, processed_groups, final_batch

    async def _find_unreferenced_groups_for_background_deletion(
        self,
        state_groups: Set[int],
        processed_groups: SortedSet[int],
    ) -> tuple[Set[int], SortedSet[int]]:
        """Used when deleting unreferenced state groups in the background to figure out
        which state groups can be deleted.
        The deletion set includes any state groups that share edges with the state
        groups that are candidates for deletion.
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
            state_groups: Set of state groups referenced by events
                that are going to be deleted.
            processed_groups: State groups that have already been processed. Only
                includes state groups ahead of the last_checked_state_group.

        Returns:
            (to_delete, processed_groups)
        """
        # Set of groups that we have found to be referenced by events
        referenced_groups = set()

        # Set of state groups that share the same base state group
        group_chains = {
            group: [group] for group in state_groups
        }  # Map[start_group, group_chain]

        # Set of state groups we've already seen
        state_groups_seen = {
            group: group for group in state_groups
        }  # Map[seen_group, start_group]

        next_to_search = state_groups
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

            # We check to see if anything referencing the state groups are
            # also unreferenced. This helps speed up deletion by proactively finding
            # related state groups which can be deleted.
            next_edges = await self.stores.state.get_next_state_groups(current_search)
            nexts = set(next_edges.keys())
            nexts -= state_groups_seen.keys()

            # Filter out already processed groups from this batch
            nexts -= processed_groups
            next_to_search |= nexts
            for next, curr in next_edges.items():
                start_group = state_groups_seen[curr]
                state_groups_seen[next] = start_group

                # Store state group chains so we know which state groups to exclude if
                # we happen upon a referenced state group later in the chain.
                if start_group in group_chains:
                    group_chains[start_group].append(next)
                else:
                    group_chains[start_group] = [next]

        # Update the processed groups
        # Limit the number of state groups we track so we don't end up spending all our
        # time de/serializing the progress json.
        tracking_space = MAX_PROCESSED_GROUPS - len(processed_groups)
        newly_processed = state_groups_seen.keys() - processed_groups
        processed_groups |= set(itertools.islice(iter(newly_processed), tracking_space))

        for group in referenced_groups:
            # Remove each group along the same chain as the referenced group.
            # Deleting any of these state groups would lead to de-deltaing of state
            # groups and unwanted DB expansion.
            start_group = state_groups_seen[group]
            chain = group_chains[start_group]
            state_groups_seen = {
                key: value
                for key, value in state_groups_seen.items()
                if key not in chain
            }

        to_delete = set(state_groups_seen.keys())

        return to_delete, processed_groups
