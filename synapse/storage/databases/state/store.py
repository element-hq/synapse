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

import logging
from typing import (
    TYPE_CHECKING,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    cast,
)

import attr

from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.events.snapshot import UnpersistedEventContext, UnpersistedEventContextBase
from synapse.logging.opentracing import tag_args, trace
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.state.bg_updates import StateBackgroundUpdateStore
from synapse.storage.types import Cursor
from synapse.storage.util.sequence import build_sequence_generator
from synapse.types import MutableStateMap, StateKey, StateMap
from synapse.types.state import StateFilter
from synapse.util.caches.descriptors import cached
from synapse.util.caches.dictionary_cache import DictionaryCache
from synapse.util.cancellation import cancellable

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


MAX_STATE_DELTA_HOPS = 100


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _GetStateGroupDelta:
    """Return type of get_state_group_delta that implements __len__, which lets
    us use the iterable flag when caching
    """

    prev_group: Optional[int]
    delta_ids: Optional[StateMap[str]]

    def __len__(self) -> int:
        return len(self.delta_ids) if self.delta_ids else 0


class StateGroupDataStore(StateBackgroundUpdateStore, SQLBaseStore):
    """A data store for fetching/storing state groups."""

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        # Originally the state store used a single DictionaryCache to cache the
        # event IDs for the state types in a given state group to avoid hammering
        # on the state_group* tables.
        #
        # The point of using a DictionaryCache is that it can cache a subset
        # of the state events for a given state group (i.e. a subset of the keys for a
        # given dict which is an entry in the cache for a given state group ID).
        #
        # However, this poses problems when performing complicated queries
        # on the store - for instance: "give me all the state for this group, but
        # limit members to this subset of users", as DictionaryCache's API isn't
        # rich enough to say "please cache any of these fields, apart from this subset".
        # This is problematic when lazy loading members, which requires this behaviour,
        # as without it the cache has no choice but to speculatively load all
        # state events for the group, which negates the efficiency being sought.
        #
        # Rather than overcomplicating DictionaryCache's API, we instead split the
        # state_group_cache into two halves - one for tracking non-member events,
        # and the other for tracking member_events.  This means that lazy loading
        # queries can be made in a cache-friendly manner by querying both caches
        # separately and then merging the result.  So for the example above, you
        # would query the members cache for a specific subset of state keys
        # (which DictionaryCache will handle efficiently and fine) and the non-members
        # cache for all state (which DictionaryCache will similarly handle fine)
        # and then just merge the results together.
        #
        # We size the non-members cache to be smaller than the members cache as the
        # vast majority of state in Matrix (today) is member events.

        self._state_group_cache: DictionaryCache[int, StateKey, str] = DictionaryCache(
            "*stateGroupCache*",
            # TODO: this hasn't been tuned yet
            50000,
        )
        self._state_group_members_cache: DictionaryCache[int, StateKey, str] = (
            DictionaryCache(
                "*stateGroupMembersCache*",
                500000,
            )
        )

        def get_max_state_group_txn(txn: Cursor) -> int:
            txn.execute("SELECT COALESCE(max(id), 0) FROM state_groups")
            return txn.fetchone()[0]  # type: ignore

        self._state_group_seq_gen = build_sequence_generator(
            db_conn,
            self.database_engine,
            get_max_state_group_txn,
            "state_group_id_seq",
            table="state_groups",
            id_column="id",
        )

    @cached(max_entries=10000, iterable=True)
    async def get_state_group_delta(self, state_group: int) -> _GetStateGroupDelta:
        """Given a state group try to return a previous group and a delta between
        the old and the new.

        Returns:
            _GetStateGroupDelta containing prev_group and delta_ids, where both may be None.
        """

        def _get_state_group_delta_txn(txn: LoggingTransaction) -> _GetStateGroupDelta:
            prev_group = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="state_group_edges",
                keyvalues={"state_group": state_group},
                retcol="prev_state_group",
                allow_none=True,
            )

            if not prev_group:
                return _GetStateGroupDelta(None, None)

            delta_ids = cast(
                List[Tuple[str, str, str]],
                self.db_pool.simple_select_list_txn(
                    txn,
                    table="state_groups_state",
                    keyvalues={"state_group": state_group},
                    retcols=("type", "state_key", "event_id"),
                ),
            )

            return _GetStateGroupDelta(
                prev_group,
                {
                    (event_type, state_key): event_id
                    for event_type, state_key, event_id in delta_ids
                },
            )

        return await self.db_pool.runInteraction(
            "get_state_group_delta", _get_state_group_delta_txn
        )

    @trace
    @tag_args
    @cancellable
    async def _get_state_groups_from_groups(
        self, groups: List[int], state_filter: StateFilter
    ) -> Dict[int, StateMap[str]]:
        """Returns the state groups for a given set of groups from the
        database, filtering on types of state events.

        Args:
            groups: list of state group IDs to query
            state_filter: The state filter used to fetch state
                from the database.
        Returns:
            Dict of state group to state map.
        """
        results: Dict[int, StateMap[str]] = {}

        chunks = [groups[i : i + 100] for i in range(0, len(groups), 100)]
        for chunk in chunks:
            res = await self.db_pool.runInteraction(
                "_get_state_groups_from_groups",
                self._get_state_groups_from_groups_txn,
                chunk,
                state_filter,
            )
            results.update(res)

        return results

    @trace
    @tag_args
    def _get_state_for_group_using_cache(
        self,
        cache: DictionaryCache[int, StateKey, str],
        group: int,
        state_filter: StateFilter,
    ) -> Tuple[MutableStateMap[str], bool]:
        """Checks if group is in cache. See `get_state_for_groups`

        Args:
            cache: the state group cache to use
            group: The state group to lookup
            state_filter: The state filter used to fetch state from the database.

        Returns:
             2-tuple (`state_dict`, `got_all`).
                `got_all` is a bool indicating if we successfully retrieved all
                requests state from the cache, if False we need to query the DB for the
                missing state.
        """
        # If we are asked explicitly for a subset of keys, we only ask for those
        # from the cache. This ensures that the `DictionaryCache` can make
        # better decisions about what to cache and what to expire.
        dict_keys = None
        if not state_filter.has_wildcards():
            dict_keys = state_filter.concrete_types()

        cache_entry = cache.get(group, dict_keys=dict_keys)
        state_dict_ids = cache_entry.value

        if cache_entry.full or state_filter.is_full():
            # Either we have everything or want everything, either way
            # `is_all` tells us whether we've gotten everything.
            return state_filter.filter_state(state_dict_ids), cache_entry.full

        # tracks whether any of our requested types are missing from the cache
        missing_types = False

        if state_filter.has_wildcards():
            # We don't know if we fetched all the state keys for the types in
            # the filter that are wildcards, so we have to assume that we may
            # have missed some.
            missing_types = True
        else:
            # There aren't any wild cards, so `concrete_types()` returns the
            # complete list of event types we're wanting.
            for key in state_filter.concrete_types():
                if key not in state_dict_ids and key not in cache_entry.known_absent:
                    missing_types = True
                    break

        return state_filter.filter_state(state_dict_ids), not missing_types

    @trace
    @tag_args
    @cancellable
    async def _get_state_for_groups(
        self, groups: Iterable[int], state_filter: Optional[StateFilter] = None
    ) -> Dict[int, MutableStateMap[str]]:
        """Gets the state at each of a list of state groups, optionally
        filtering by type/state_key

        Args:
            groups: list of state groups for which we want
                to get the state.
            state_filter: The state filter used to fetch state
                from the database.
        Returns:
            Dict of state group to state map.
        """
        if state_filter is None:
            state_filter = StateFilter.all()

        member_filter, non_member_filter = state_filter.get_member_split()

        # Now we look them up in the member and non-member caches
        non_member_state, incomplete_groups_nm = self._get_state_for_groups_using_cache(
            groups, self._state_group_cache, state_filter=non_member_filter
        )

        member_state, incomplete_groups_m = self._get_state_for_groups_using_cache(
            groups, self._state_group_members_cache, state_filter=member_filter
        )

        state = dict(non_member_state)
        for group in groups:
            state[group].update(member_state[group])

        # Now fetch any missing groups from the database

        incomplete_groups = incomplete_groups_m | incomplete_groups_nm

        if not incomplete_groups:
            return state

        cache_sequence_nm = self._state_group_cache.sequence
        cache_sequence_m = self._state_group_members_cache.sequence

        # Help the cache hit ratio by expanding the filter a bit
        db_state_filter = state_filter.return_expanded()

        group_to_state_dict = await self._get_state_groups_from_groups(
            list(incomplete_groups), state_filter=db_state_filter
        )

        # Now lets update the caches
        self._insert_into_cache(
            group_to_state_dict,
            db_state_filter,
            cache_seq_num_members=cache_sequence_m,
            cache_seq_num_non_members=cache_sequence_nm,
        )

        # And finally update the result dict, by filtering out any extra
        # stuff we pulled out of the database.
        for group, group_state_dict in group_to_state_dict.items():
            # We just replace any existing entries, as we will have loaded
            # everything we need from the database anyway.
            state[group] = state_filter.filter_state(group_state_dict)

        return state

    @trace
    @tag_args
    def _get_state_for_groups_using_cache(
        self,
        groups: Iterable[int],
        cache: DictionaryCache[int, StateKey, str],
        state_filter: StateFilter,
    ) -> Tuple[Dict[int, MutableStateMap[str]], Set[int]]:
        """Gets the state at each of a list of state groups, optionally
        filtering by type/state_key, querying from a specific cache.

        Args:
            groups: list of state groups for which we want to get the state.
            cache: the cache of group ids to state dicts which
                we will pass through - either the normal state cache or the
                specific members state cache.
            state_filter: The state filter used to fetch state from the
                database.

        Returns:
            Tuple of dict of state_group_id to state map of entries in the
            cache, and the state group ids either missing from the cache or
            incomplete.
        """
        results = {}
        incomplete_groups = set()
        for group in set(groups):
            state_dict_ids, got_all = self._get_state_for_group_using_cache(
                cache, group, state_filter
            )
            results[group] = state_dict_ids

            if not got_all:
                incomplete_groups.add(group)

        return results, incomplete_groups

    def _insert_into_cache(
        self,
        group_to_state_dict: Dict[int, StateMap[str]],
        state_filter: StateFilter,
        cache_seq_num_members: int,
        cache_seq_num_non_members: int,
    ) -> None:
        """Inserts results from querying the database into the relevant cache.

        Args:
            group_to_state_dict: The new entries pulled from database.
                Map from state group to state dict
            state_filter: The state filter used to fetch state
                from the database.
            cache_seq_num_members: Sequence number of member cache since
                last lookup in cache
            cache_seq_num_non_members: Sequence number of member cache since
                last lookup in cache
        """

        # We need to work out which types we've fetched from the DB for the
        # member vs non-member caches. This should be as accurate as possible,
        # but can be an underestimate (e.g. when we have wild cards)

        member_filter, non_member_filter = state_filter.get_member_split()
        if member_filter.is_full():
            # We fetched all member events
            member_types = None
        else:
            # `concrete_types()` will only return a subset when there are wild
            # cards in the filter, but that's fine.
            member_types = member_filter.concrete_types()

        if non_member_filter.is_full():
            # We fetched all non member events
            non_member_types = None
        else:
            non_member_types = non_member_filter.concrete_types()

        for group, group_state_dict in group_to_state_dict.items():
            state_dict_members = {}
            state_dict_non_members = {}

            for k, v in group_state_dict.items():
                if k[0] == EventTypes.Member:
                    state_dict_members[k] = v
                else:
                    state_dict_non_members[k] = v

            self._state_group_members_cache.update(
                cache_seq_num_members,
                key=group,
                value=state_dict_members,
                fetched_keys=member_types,
            )

            self._state_group_cache.update(
                cache_seq_num_non_members,
                key=group,
                value=state_dict_non_members,
                fetched_keys=non_member_types,
            )

    @trace
    @tag_args
    async def store_state_deltas_for_batched(
        self,
        events_and_context: List[Tuple[EventBase, UnpersistedEventContextBase]],
        room_id: str,
        prev_group: int,
    ) -> List[Tuple[EventBase, UnpersistedEventContext]]:
        """Generate and store state deltas for a group of events and contexts created to be
        batch persisted. Note that all the events must be in a linear chain (ie a <- b <- c).

        Args:
            events_and_context: the events to generate and store a state groups for
            and their associated contexts
            room_id: the id of the room the events were created for
            prev_group: the state group of the last event persisted before the batched events
            were created
        """

        def insert_deltas_group_txn(
            txn: LoggingTransaction,
            events_and_context: List[Tuple[EventBase, UnpersistedEventContext]],
            prev_group: int,
        ) -> List[Tuple[EventBase, UnpersistedEventContext]]:
            """Generate and store state groups for the provided events and contexts.

            Requires that we have the state as a delta from the last persisted state group.

            Returns:
                A list of state groups
            """
            is_in_db = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="state_groups",
                keyvalues={"id": prev_group},
                retcol="id",
                allow_none=True,
            )
            if not is_in_db:
                raise Exception(
                    "Trying to persist state with unpersisted prev_group: %r"
                    % (prev_group,)
                )

            num_state_groups = sum(
                1 for event, _ in events_and_context if event.is_state()
            )

            state_groups = self._state_group_seq_gen.get_next_mult_txn(
                txn, num_state_groups
            )

            sg_before = prev_group
            state_group_iter = iter(state_groups)
            for event, context in events_and_context:
                if not event.is_state():
                    context.state_group_after_event = sg_before
                    context.state_group_before_event = sg_before
                    continue

                sg_after = next(state_group_iter)
                context.state_group_after_event = sg_after
                context.state_group_before_event = sg_before
                context.state_delta_due_to_event = {
                    (event.type, event.state_key): event.event_id
                }
                sg_before = sg_after

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups",
                keys=("id", "room_id", "event_id"),
                values=[
                    (context.state_group_after_event, room_id, event.event_id)
                    for event, context in events_and_context
                    if event.is_state()
                ],
            )

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_group_edges",
                keys=("state_group", "prev_state_group"),
                values=[
                    (
                        context.state_group_after_event,
                        context.state_group_before_event,
                    )
                    for event, context in events_and_context
                    if event.is_state()
                ],
            )

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_state",
                keys=("state_group", "room_id", "type", "state_key", "event_id"),
                values=[
                    (
                        context.state_group_after_event,
                        room_id,
                        key[0],
                        key[1],
                        state_id,
                    )
                    for event, context in events_and_context
                    if context.state_delta_due_to_event is not None
                    for key, state_id in context.state_delta_due_to_event.items()
                ],
            )
            return events_and_context

        return await self.db_pool.runInteraction(
            "store_state_deltas_for_batched.insert_deltas_group",
            insert_deltas_group_txn,
            events_and_context,
            prev_group,
        )

    @trace
    @tag_args
    async def store_state_group(
        self,
        event_id: str,
        room_id: str,
        prev_group: Optional[int],
        delta_ids: Optional[StateMap[str]],
        current_state_ids: Optional[StateMap[str]],
    ) -> int:
        """Store a new set of state, returning a newly assigned state group.

        At least one of `current_state_ids` and `prev_group` must be provided. Whenever
        `prev_group` is not None, `delta_ids` must also not be None.

        Args:
            event_id: The event ID for which the state was calculated
            room_id
            prev_group: A previous state group for the room.
            delta_ids: The delta between state at `prev_group` and
                `current_state_ids`, if `prev_group` was given. Same format as
                `current_state_ids`.
            current_state_ids: The state to store. Map of (type, state_key)
                to event_id.

        Returns:
            The state group ID
        """

        if prev_group is None and current_state_ids is None:
            raise Exception("current_state_ids and prev_group can't both be None")

        if prev_group is not None and delta_ids is None:
            raise Exception("delta_ids is None when prev_group is not None")

        def insert_delta_group_txn(
            txn: LoggingTransaction, prev_group: int, delta_ids: StateMap[str]
        ) -> Optional[int]:
            """Try and persist the new group as a delta.

            Requires that we have the state as a delta from a previous state group.

            Returns:
                The state group if successfully created, or None if the state
                needs to be persisted as a full state.
            """
            is_in_db = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="state_groups",
                keyvalues={"id": prev_group},
                retcol="id",
                allow_none=True,
            )
            if not is_in_db:
                raise Exception(
                    "Trying to persist state with unpersisted prev_group: %r"
                    % (prev_group,)
                )

            # if the chain of state group deltas is going too long, we fall back to
            # persisting a complete state group.
            potential_hops = self._count_state_group_hops_txn(txn, prev_group)
            if potential_hops >= MAX_STATE_DELTA_HOPS:
                return None

            state_group = self._state_group_seq_gen.get_next_id_txn(txn)

            self.db_pool.simple_insert_txn(
                txn,
                table="state_groups",
                values={"id": state_group, "room_id": room_id, "event_id": event_id},
            )

            self.db_pool.simple_insert_txn(
                txn,
                table="state_group_edges",
                values={"state_group": state_group, "prev_state_group": prev_group},
            )

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_state",
                keys=("state_group", "room_id", "type", "state_key", "event_id"),
                values=[
                    (state_group, room_id, key[0], key[1], state_id)
                    for key, state_id in delta_ids.items()
                ],
            )

            return state_group

        def insert_full_state_txn(
            txn: LoggingTransaction, current_state_ids: StateMap[str]
        ) -> int:
            """Persist the full state, returning the new state group."""
            state_group = self._state_group_seq_gen.get_next_id_txn(txn)

            self.db_pool.simple_insert_txn(
                txn,
                table="state_groups",
                values={"id": state_group, "room_id": room_id, "event_id": event_id},
            )

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_state",
                keys=("state_group", "room_id", "type", "state_key", "event_id"),
                values=[
                    (state_group, room_id, key[0], key[1], state_id)
                    for key, state_id in current_state_ids.items()
                ],
            )

            # Prefill the state group caches with this group.
            # It's fine to use the sequence like this as the state group map
            # is immutable. (If the map wasn't immutable then this prefill could
            # race with another update)

            current_member_state_ids = {
                s: ev
                for (s, ev) in current_state_ids.items()
                if s[0] == EventTypes.Member
            }
            txn.call_after(
                self._state_group_members_cache.update,
                self._state_group_members_cache.sequence,
                key=state_group,
                value=current_member_state_ids,
            )

            current_non_member_state_ids = {
                s: ev
                for (s, ev) in current_state_ids.items()
                if s[0] != EventTypes.Member
            }
            txn.call_after(
                self._state_group_cache.update,
                self._state_group_cache.sequence,
                key=state_group,
                value=current_non_member_state_ids,
            )

            return state_group

        if prev_group is not None:
            state_group = await self.db_pool.runInteraction(
                "store_state_group.insert_delta_group",
                insert_delta_group_txn,
                prev_group,
                delta_ids,
            )
            if state_group is not None:
                return state_group

        # We're going to persist the state as a complete group rather than
        # a delta, so first we need to ensure we have loaded the state map
        # from the database.
        if current_state_ids is None:
            assert prev_group is not None
            assert delta_ids is not None
            groups = await self._get_state_for_groups([prev_group])
            current_state_ids = dict(groups[prev_group])
            current_state_ids.update(delta_ids)

        return await self.db_pool.runInteraction(
            "store_state_group.insert_full_state",
            insert_full_state_txn,
            current_state_ids,
        )

    async def purge_unreferenced_state_groups(
        self, room_id: str, state_groups_to_delete: Collection[int]
    ) -> None:
        """Deletes no longer referenced state groups and de-deltas any state
        groups that reference them.

        Args:
            room_id: The room the state groups belong to (must all be in the
                same room).
            state_groups_to_delete: Set of all state groups to delete.
        """

        await self.db_pool.runInteraction(
            "purge_unreferenced_state_groups",
            self._purge_unreferenced_state_groups,
            room_id,
            state_groups_to_delete,
        )

    def _purge_unreferenced_state_groups(
        self,
        txn: LoggingTransaction,
        room_id: str,
        state_groups_to_delete: Collection[int],
    ) -> None:
        logger.info(
            "[purge] found %i state groups to delete", len(state_groups_to_delete)
        )

        rows = cast(
            List[Tuple[int]],
            self.db_pool.simple_select_many_txn(
                txn,
                table="state_group_edges",
                column="prev_state_group",
                iterable=state_groups_to_delete,
                keyvalues={},
                retcols=("state_group",),
            ),
        )

        remaining_state_groups = {
            state_group
            for (state_group,) in rows
            if state_group not in state_groups_to_delete
        }

        logger.info(
            "[purge] de-delta-ing %i remaining state groups",
            len(remaining_state_groups),
        )

        # Now we turn the state groups that reference to-be-deleted state
        # groups to non delta versions.
        for sg in remaining_state_groups:
            logger.info("[purge] de-delta-ing remaining state group %s", sg)
            curr_state_by_group = self._get_state_groups_from_groups_txn(txn, [sg])
            curr_state = curr_state_by_group[sg]

            self.db_pool.simple_delete_txn(
                txn, table="state_groups_state", keyvalues={"state_group": sg}
            )

            self.db_pool.simple_delete_txn(
                txn, table="state_group_edges", keyvalues={"state_group": sg}
            )

            self.db_pool.simple_insert_many_txn(
                txn,
                table="state_groups_state",
                keys=("state_group", "room_id", "type", "state_key", "event_id"),
                values=[
                    (sg, room_id, key[0], key[1], state_id)
                    for key, state_id in curr_state.items()
                ],
            )

        logger.info("[purge] removing redundant state groups")
        txn.execute_batch(
            "DELETE FROM state_groups_state WHERE state_group = ?",
            [(sg,) for sg in state_groups_to_delete],
        )
        txn.execute_batch(
            "DELETE FROM state_groups WHERE id = ?",
            [(sg,) for sg in state_groups_to_delete],
        )

    @trace
    @tag_args
    async def get_previous_state_groups(
        self, state_groups: Iterable[int]
    ) -> Dict[int, int]:
        """Fetch the previous groups of the given state groups.

        Args:
            state_groups

        Returns:
            A mapping from state group to previous state group.
        """

        rows = cast(
            List[Tuple[int, int]],
            await self.db_pool.simple_select_many_batch(
                table="state_group_edges",
                column="prev_state_group",
                iterable=state_groups,
                keyvalues={},
                retcols=("state_group", "prev_state_group"),
                desc="get_previous_state_groups",
            ),
        )

        return dict(rows)

    async def purge_room_state(
        self, room_id: str, state_groups_to_delete: Collection[int]
    ) -> None:
        """Deletes all record of a room from state tables

        Args:
            room_id:
            state_groups_to_delete: State groups to delete
        """

        logger.info("[purge] Starting state purge")
        await self.db_pool.runInteraction(
            "purge_room_state",
            self._purge_room_state_txn,
            room_id,
            state_groups_to_delete,
        )
        logger.info("[purge] Done with state purge")

    def _purge_room_state_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        state_groups_to_delete: Collection[int],
    ) -> None:
        # first we have to delete the state groups states
        logger.info("[purge] removing %s from state_groups_state", room_id)

        self.db_pool.simple_delete_many_txn(
            txn,
            table="state_groups_state",
            column="state_group",
            values=state_groups_to_delete,
            keyvalues={},
        )

        # ... and the state group edges
        logger.info("[purge] removing %s from state_group_edges", room_id)

        self.db_pool.simple_delete_many_txn(
            txn,
            table="state_group_edges",
            column="state_group",
            values=state_groups_to_delete,
            keyvalues={},
        )

        # ... and the state groups
        logger.info("[purge] removing %s from state_groups", room_id)

        self.db_pool.simple_delete_many_txn(
            txn,
            table="state_groups",
            column="id",
            values=state_groups_to_delete,
            keyvalues={},
        )
