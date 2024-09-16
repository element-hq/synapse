#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from itertools import chain
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

from synapse.api.constants import EventTypes, Membership
from synapse.events import EventBase
from synapse.logging.opentracing import tag_args, trace
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.storage.roommember import ProfileInfo
from synapse.storage.util.partial_state_events_tracker import (
    PartialCurrentStateTracker,
    PartialStateEventsTracker,
)
from synapse.synapse_rust.acl import ServerAclEvaluator
from synapse.types import MutableStateMap, StateMap, StreamToken, get_domain_from_id
from synapse.types.state import StateFilter
from synapse.util.async_helpers import Linearizer
from synapse.util.caches import intern_string
from synapse.util.caches.descriptors import cached
from synapse.util.cancellation import cancellable
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.state import _StateCacheEntry
    from synapse.storage.databases import Databases


logger = logging.getLogger(__name__)


class StateStorageController:
    """High level interface to fetching state for an event, or the current state
    in a room.
    """

    def __init__(self, hs: "HomeServer", stores: "Databases"):
        self._is_mine_id = hs.is_mine_id
        self._clock = hs.get_clock()
        self.stores = stores
        self._partial_state_events_tracker = PartialStateEventsTracker(stores.main)
        self._partial_state_room_tracker = PartialCurrentStateTracker(stores.main)

        # Used by `_get_joined_hosts` to ensure only one thing mutates the cache
        # at a time. Keyed by room_id.
        self._joined_host_linearizer = Linearizer("_JoinedHostsCache")

    def notify_event_un_partial_stated(self, event_id: str) -> None:
        self._partial_state_events_tracker.notify_un_partial_stated(event_id)

    def notify_room_un_partial_stated(self, room_id: str) -> None:
        """Notify that the room no longer has any partial state.

        Must be called after `DataStore.clear_partial_state_room`
        """
        self._partial_state_room_tracker.notify_un_partial_stated(room_id)

    @trace
    @tag_args
    async def get_state_group_delta(
        self, state_group: int
    ) -> Tuple[Optional[int], Optional[StateMap[str]]]:
        """Given a state group try to return a previous group and a delta between
        the old and the new.

        Args:
            state_group: The state group used to retrieve state deltas.

        Returns:
            A tuple of the previous group and a state map of the event IDs which
            make up the delta between the old and new state groups.
        """

        state_group_delta = await self.stores.state.get_state_group_delta(state_group)
        return state_group_delta.prev_group, state_group_delta.delta_ids

    @trace
    @tag_args
    async def get_state_groups_ids(
        self, _room_id: str, event_ids: Collection[str], await_full_state: bool = True
    ) -> Dict[int, MutableStateMap[str]]:
        """Get the event IDs of all the state for the state groups for the given events

        Args:
            _room_id: id of the room for these events
            event_ids: ids of the events
            await_full_state: if `True`, will block if we do not yet have complete
               state at these events.

        Returns:
            dict of state_group_id -> (dict of (type, state_key) -> event id)

        Raises:
            RuntimeError if we don't have a state group for one or more of the events
               (ie they are outliers or unknown)
        """
        if not event_ids:
            return {}

        event_to_groups = await self.get_state_group_for_events(
            event_ids, await_full_state=await_full_state
        )

        groups = set(event_to_groups.values())
        group_to_state = await self.stores.state._get_state_for_groups(groups)

        return group_to_state

    @trace
    @tag_args
    async def get_state_ids_for_group(
        self, state_group: int, state_filter: Optional[StateFilter] = None
    ) -> StateMap[str]:
        """Get the event IDs of all the state in the given state group

        Args:
            state_group: A state group for which we want to get the state IDs.
            state_filter: specifies the type of state event to fetch from DB, example: EventTypes.JoinRules

        Returns:
            Resolves to a map of (type, state_key) -> event_id
        """
        group_to_state = await self.get_state_for_groups((state_group,), state_filter)

        return group_to_state[state_group]

    @trace
    @tag_args
    async def get_state_groups(
        self, room_id: str, event_ids: Collection[str]
    ) -> Dict[int, List[EventBase]]:
        """Get the state groups for the given list of event_ids

        Args:
            room_id: ID of the room for these events.
            event_ids: The event IDs to retrieve state for.

        Returns:
            dict of state_group_id -> list of state events.
        """
        if not event_ids:
            return {}

        group_to_ids = await self.get_state_groups_ids(room_id, event_ids)

        state_event_map = await self.stores.main.get_events(
            [
                ev_id
                for group_ids in group_to_ids.values()
                for ev_id in group_ids.values()
            ],
            get_prev_content=False,
        )

        return {
            group: [
                state_event_map[v]
                for v in event_id_map.values()
                if v in state_event_map
            ]
            for group, event_id_map in group_to_ids.items()
        }

    @trace
    @tag_args
    async def _get_state_groups_from_groups(
        self, groups: List[int], state_filter: StateFilter
    ) -> Dict[int, StateMap[str]]:
        """Returns the state groups for a given set of groups, filtering on
        types of state events.

        Args:
            groups: list of state group IDs to query
            state_filter: The state filter used to fetch state
                from the database.

        Returns:
            Dict of state group to state map.
        """

        return await self.stores.state._get_state_groups_from_groups(
            groups, state_filter
        )

    @trace
    @tag_args
    async def get_state_for_events(
        self, event_ids: Collection[str], state_filter: Optional[StateFilter] = None
    ) -> Dict[str, StateMap[EventBase]]:
        """Given a list of event_ids and type tuples, return a list of state
        dicts for each event.

        Args:
            event_ids: The events to fetch the state of.
            state_filter: The state filter used to fetch state.

        Returns:
            A dict of (event_id) -> (type, state_key) -> [state_events]

        Raises:
            RuntimeError if we don't have a state group for one or more of the events
               (ie they are outliers or unknown)
        """
        await_full_state = True
        if state_filter and not state_filter.must_await_full_state(self._is_mine_id):
            await_full_state = False

        event_to_groups = await self.get_state_group_for_events(
            event_ids, await_full_state=await_full_state
        )

        groups = set(event_to_groups.values())
        group_to_state = await self.stores.state._get_state_for_groups(
            groups, state_filter or StateFilter.all()
        )

        state_event_map = await self.stores.main.get_events(
            [ev_id for sd in group_to_state.values() for ev_id in sd.values()],
            get_prev_content=False,
        )

        event_to_state = {
            event_id: {
                k: state_event_map[v]
                for k, v in group_to_state[group].items()
                if v in state_event_map
            }
            for event_id, group in event_to_groups.items()
        }

        return {event: event_to_state[event] for event in event_ids}

    @trace
    @tag_args
    @cancellable
    async def get_state_ids_for_events(
        self,
        event_ids: Collection[str],
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> Dict[str, StateMap[str]]:
        """
        Get the room states after each of a list of events.

        For each event in `event_ids`, the result contains a map from state tuple
        to the event_ids of the state event (as opposed to the events themselves).

        Args:
            event_ids: events whose state should be returned
            state_filter: The state filter used to fetch state from the database.
            await_full_state: if `True`, will block if we do not yet have complete state
                at these events and `state_filter` is not satisfied by partial state.
                Defaults to `True`.

        Returns:
            A dict from event_id -> (type, state_key) -> event_id

        Raises:
            RuntimeError if we don't have a state group for one or more of the events
                (ie they are outliers or unknown)
        """
        if (
            await_full_state
            and state_filter
            and not state_filter.must_await_full_state(self._is_mine_id)
        ):
            # Full state is not required if the state filter is restrictive enough.
            await_full_state = False

        event_to_groups = await self.get_state_group_for_events(
            event_ids, await_full_state=await_full_state
        )

        groups = set(event_to_groups.values())
        group_to_state = await self.stores.state._get_state_for_groups(
            groups, state_filter or StateFilter.all()
        )

        event_to_state = {
            event_id: group_to_state[group]
            for event_id, group in event_to_groups.items()
        }

        return {event: event_to_state[event] for event in event_ids}

    @trace
    @tag_args
    async def get_state_for_event(
        self, event_id: str, state_filter: Optional[StateFilter] = None
    ) -> StateMap[EventBase]:
        """
        Get the state dict corresponding to a particular event

        Args:
            event_id: event whose state should be returned
            state_filter: The state filter used to fetch state from the database.

        Returns:
            A dict from (type, state_key) -> state_event

        Raises:
            RuntimeError if we don't have a state group for the event (ie it is an
                outlier or is unknown)
        """
        state_map = await self.get_state_for_events(
            [event_id], state_filter or StateFilter.all()
        )
        return state_map[event_id]

    @trace
    @tag_args
    async def get_state_ids_for_event(
        self,
        event_id: str,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> StateMap[str]:
        """
        Get the state dict corresponding to the state after a particular event

        Args:
            event_id: event whose state should be returned
            state_filter: The state filter used to fetch state from the database.
            await_full_state: if `True`, will block if we do not yet have complete state
                at the event and `state_filter` is not satisfied by partial state.
                Defaults to `True`.

        Returns:
            A dict from (type, state_key) -> state_event_id

        Raises:
            RuntimeError if we don't have a state group for the event (ie it is an
                outlier or is unknown)
        """
        state_map = await self.get_state_ids_for_events(
            [event_id],
            state_filter or StateFilter.all(),
            await_full_state=await_full_state,
        )
        return state_map[event_id]

    async def get_state_after_event(
        self,
        event_id: str,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> StateMap[str]:
        """
        Get the room state after the given event

        Args:
            event_id: event of interest
            state_filter: The state filter used to fetch state from the database.
            await_full_state: if `True`, will block if we do not yet have complete state
                at the event and `state_filter` is not satisfied by partial state.
                Defaults to `True`.
        """
        state_ids = await self.get_state_ids_for_event(
            event_id,
            state_filter=state_filter or StateFilter.all(),
            await_full_state=await_full_state,
        )

        # using get_metadata_for_events here (instead of get_event) sidesteps an issue
        # with redactions: if `event_id` is a redaction event, and we don't have the
        # original (possibly because it got purged), get_event will refuse to return
        # the redaction event, which isn't terribly helpful here.
        #
        # (To be fair, in that case we could assume it's *not* a state event, and
        # therefore we don't need to worry about it. But still, it seems cleaner just
        # to pull the metadata.)
        m = (await self.stores.main.get_metadata_for_events([event_id]))[event_id]
        if m.state_key is not None and m.rejection_reason is None:
            state_ids = dict(state_ids)
            state_ids[(m.event_type, m.state_key)] = event_id

        return state_ids

    async def get_state_ids_at(
        self,
        room_id: str,
        stream_position: StreamToken,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> StateMap[str]:
        """Get the room state at a particular stream position

        Args:
            room_id: room for which to get state
            stream_position: point at which to get state
            state_filter: The state filter used to fetch state from the database.
            await_full_state: if `True`, will block if we do not yet have complete state
                at the last event in the room before `stream_position` and
                `state_filter` is not satisfied by partial state. Defaults to `True`.
        """
        # FIXME: This gets the state at the latest event before the stream ordering,
        # which might not be the same as the "current state" of the room at the time
        # of the stream token if there were multiple forward extremities at the time.
        last_event_id = (
            await self.stores.main.get_last_event_id_in_room_before_stream_ordering(
                room_id,
                end_token=stream_position.room_key,
            )
        )

        # FIXME: This will return incorrect results when there are timeline gaps. For
        # example, when you try to get a point in the room we haven't backfilled before.

        if last_event_id:
            state = await self.get_state_after_event(
                last_event_id,
                state_filter=state_filter or StateFilter.all(),
                await_full_state=await_full_state,
            )

        else:
            # no events in this room - so presumably no state
            state = {}

            # (erikj) This should be rarely hit, but we've had some reports that
            # we get more state down gappy syncs than we should, so let's add
            # some logging.
            logger.info(
                "Failed to find any events in room %s at %s",
                room_id,
                stream_position.room_key,
            )
        return state

    @trace
    @tag_args
    async def get_state_at(
        self,
        room_id: str,
        stream_position: StreamToken,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> StateMap[EventBase]:
        """Same as `get_state_ids_at` but also fetches the events"""
        state_map_ids = await self.get_state_ids_at(
            room_id, stream_position, state_filter, await_full_state
        )

        event_map = await self.stores.main.get_events(list(state_map_ids.values()))

        state_map = {}
        for key, event_id in state_map_ids.items():
            event = event_map.get(event_id)
            if event:
                state_map[key] = event

        return state_map

    @trace
    @tag_args
    async def get_state_for_groups(
        self, groups: Iterable[int], state_filter: Optional[StateFilter] = None
    ) -> Dict[int, MutableStateMap[str]]:
        """Gets the state at each of a list of state groups, optionally
        filtering by type/state_key

        Args:
            groups: list of state groups for which we want to get the state.
            state_filter: The state filter used to fetch state.
                from the database.

        Returns:
            Dict of state group to state map.
        """
        return await self.stores.state._get_state_for_groups(
            groups, state_filter or StateFilter.all()
        )

    @trace
    @tag_args
    @cancellable
    async def get_state_group_for_events(
        self,
        event_ids: Collection[str],
        await_full_state: bool = True,
    ) -> Mapping[str, int]:
        """Returns mapping event_id -> state_group

        Args:
            event_ids: events to get state groups for
            await_full_state: if true, will block if we do not yet have complete
               state at these events.

        Raises:
            RuntimeError if we don't have a state group for one or more of the events
               (ie. they are outliers or unknown)
        """
        if await_full_state:
            await self._partial_state_events_tracker.await_full_state(event_ids)

        return await self.stores.main._get_state_group_for_events(event_ids)

    async def store_state_group(
        self,
        event_id: str,
        room_id: str,
        prev_group: Optional[int],
        delta_ids: Optional[StateMap[str]],
        current_state_ids: Optional[StateMap[str]],
    ) -> int:
        """Store a new set of state, returning a newly assigned state group.

        Args:
            event_id: The event ID for which the state was calculated.
            room_id: ID of the room for which the state was calculated.
            prev_group: A previous state group for the room, optional.
            delta_ids: The delta between state at `prev_group` and
                `current_state_ids`, if `prev_group` was given. Same format as
                `current_state_ids`.
            current_state_ids: The state to store. Map of (type, state_key)
                to event_id.

        Returns:
            The state group ID
        """
        return await self.stores.state.store_state_group(
            event_id, room_id, prev_group, delta_ids, current_state_ids
        )

    @trace
    @tag_args
    @cancellable
    async def get_current_state_ids(
        self,
        room_id: str,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
        on_invalidate: Optional[Callable[[], None]] = None,
    ) -> StateMap[str]:
        """Get the current state event ids for a room based on the
        current_state_events table.

        If a state filter is given (that is not `StateFilter.all()`) the query
        result is *not* cached.

        Args:
            room_id: The room to get the state IDs of. state_filter: The state
            filter used to fetch state from the
                database.
            await_full_state: if true, will block if we do not yet have complete
               state for the room.
            on_invalidate: Callback for when the `get_current_state_ids` cache
                for the room gets invalidated.

        Returns:
            The current state of the room.
        """
        if await_full_state and (
            not state_filter or state_filter.must_await_full_state(self._is_mine_id)
        ):
            await self._partial_state_room_tracker.await_full_state(room_id)

        if state_filter and not state_filter.is_full():
            return await self.stores.main.get_partial_filtered_current_state_ids(
                room_id, state_filter
            )
        else:
            return await self.stores.main.get_partial_current_state_ids(
                room_id, on_invalidate=on_invalidate
            )

    @trace
    @tag_args
    async def get_canonical_alias_for_room(self, room_id: str) -> Optional[str]:
        """Get canonical alias for room, if any

        Args:
            room_id: The room ID

        Returns:
            The canonical alias, if any
        """

        state = await self.get_current_state_ids(
            room_id, StateFilter.from_types([(EventTypes.CanonicalAlias, "")])
        )

        event_id = state.get((EventTypes.CanonicalAlias, ""))
        if not event_id:
            return None

        event = await self.stores.main.get_event(event_id, allow_none=True)
        if not event:
            return None

        return event.content.get("alias")

    @cached()
    async def get_server_acl_for_room(
        self, room_id: str
    ) -> Optional[ServerAclEvaluator]:
        """Get the server ACL evaluator for room, if any

        This does up-front parsing of the content to ignore bad data and pre-compile
        regular expressions.

        Args:
            room_id: The room ID

        Returns:
            The server ACL evaluator, if any
        """

        acl_event = await self.get_current_state_event(
            room_id, EventTypes.ServerACL, ""
        )

        if not acl_event:
            return None

        return server_acl_evaluator_from_event(acl_event)

    @trace
    @tag_args
    async def get_current_state_deltas(
        self, prev_stream_id: int, max_stream_id: int
    ) -> Tuple[int, List[StateDelta]]:
        """Fetch a list of room state changes since the given stream id

        Args:
            prev_stream_id: point to get changes since (exclusive)
            max_stream_id: the point that we know has been correctly persisted
               - ie, an upper limit to return changes from.

        Returns:
            A tuple consisting of:
               - the stream id which these results go up to
               - list of current_state_delta_stream rows. If it is empty, we are
                 up to date.
        """
        # FIXME(faster_joins): what do we do here?
        #   https://github.com/matrix-org/synapse/issues/13008

        return await self.stores.main.get_partial_current_state_deltas(
            prev_stream_id, max_stream_id
        )

    @trace
    @tag_args
    async def get_current_state(
        self,
        room_id: str,
        state_filter: Optional[StateFilter] = None,
        await_full_state: bool = True,
    ) -> StateMap[EventBase]:
        """Same as `get_current_state_ids` but also fetches the events"""
        state_map_ids = await self.get_current_state_ids(
            room_id, state_filter, await_full_state
        )

        event_map = await self.stores.main.get_events(list(state_map_ids.values()))

        state_map = {}
        for key, event_id in state_map_ids.items():
            event = event_map.get(event_id)
            if event:
                state_map[key] = event

        return state_map

    @trace
    @tag_args
    async def get_current_state_event(
        self, room_id: str, event_type: str, state_key: str
    ) -> Optional[EventBase]:
        """Get the current state event for the given type/state_key."""

        key = (event_type, state_key)
        state_map = await self.get_current_state(
            room_id, StateFilter.from_types((key,))
        )
        return state_map.get(key)

    @trace
    @tag_args
    async def get_current_hosts_in_room(self, room_id: str) -> AbstractSet[str]:
        """Get current hosts in room based on current state.

        Blocks until we have full state for the given room. This only happens for rooms
        with partial state.
        """

        await self._partial_state_room_tracker.await_full_state(room_id)

        return await self.stores.main.get_current_hosts_in_room(room_id)

    @trace
    @tag_args
    async def get_current_hosts_in_room_ordered(self, room_id: str) -> Tuple[str, ...]:
        """Get current hosts in room based on current state.

        Blocks until we have full state for the given room. This only happens for rooms
        with partial state.

        Returns:
            A list of hosts in the room, sorted by longest in the room first. (aka.
            sorted by join with the lowest depth first).
        """

        await self._partial_state_room_tracker.await_full_state(room_id)

        return await self.stores.main.get_current_hosts_in_room_ordered(room_id)

    @trace
    @tag_args
    async def get_current_hosts_in_room_or_partial_state_approximation(
        self, room_id: str
    ) -> Collection[str]:
        """Get approximation of current hosts in room based on current state.

        For rooms with full state, this is equivalent to `get_current_hosts_in_room`,
        with the same order of results.

        For rooms with partial state, no blocking occurs. Instead, the list of hosts
        in the room at the time of joining is combined with the list of hosts which
        joined the room afterwards. The returned list may include hosts that are not
        actually in the room and exclude hosts that are in the room, since we may
        calculate state incorrectly during the partial state phase. The order of results
        is arbitrary for rooms with partial state.
        """
        # We have to read this list first to mitigate races with un-partial stating.
        hosts_at_join = await self.stores.main.get_partial_state_servers_at_join(
            room_id
        )
        if hosts_at_join is None:
            hosts_at_join = frozenset()

        hosts_from_state = await self.stores.main.get_current_hosts_in_room(room_id)

        hosts = set(hosts_at_join)
        hosts.update(hosts_from_state)

        return hosts

    @trace
    @tag_args
    async def get_users_in_room_with_profiles(
        self, room_id: str
    ) -> Mapping[str, ProfileInfo]:
        """
        Get the current users in the room with their profiles.
        If the room is currently partial-stated, this will block until the room has
        full state.
        """
        await self._partial_state_room_tracker.await_full_state(room_id)

        return await self.stores.main.get_users_in_room_with_profiles(room_id)

    async def get_joined_hosts(
        self, room_id: str, state_entry: "_StateCacheEntry"
    ) -> FrozenSet[str]:
        state_group: Union[object, int] = state_entry.state_group
        if not state_group:
            # If state_group is None it means it has yet to be assigned a
            # state group, i.e. we need to make sure that calls with a state_group
            # of None don't hit previous cached calls with a None state_group.
            # To do this we set the state_group to a new object as object() != object()
            state_group = object()

        assert state_group is not None
        with Measure(self._clock, "get_joined_hosts"):
            return await self._get_joined_hosts(
                room_id, state_group, state_entry=state_entry
            )

    @cached(num_args=2, max_entries=10000, iterable=True)
    async def _get_joined_hosts(
        self,
        room_id: str,
        state_group: Union[object, int],
        state_entry: "_StateCacheEntry",
    ) -> FrozenSet[str]:
        # We don't use `state_group`, it's there so that we can cache based on
        # it. However, its important that its never None, since two
        # current_state's with a state_group of None are likely to be different.
        #
        # The `state_group` must match the `state_entry.state_group` (if not None).
        assert state_group is not None
        assert state_entry.state_group is None or state_entry.state_group == state_group

        # We use a secondary cache of previous work to allow us to build up the
        # joined hosts for the given state group based on previous state groups.
        #
        # We cache one object per room containing the results of the last state
        # group we got joined hosts for. The idea is that generally
        # `get_joined_hosts` is called with the "current" state group for the
        # room, and so consecutive calls will be for consecutive state groups
        # which point to the previous state group.
        cache = await self.stores.main._get_joined_hosts_cache(room_id)

        # If the state group in the cache matches, we already have the data we need.
        if state_entry.state_group == cache.state_group:
            return frozenset(cache.hosts_to_joined_users)

        # Since we'll mutate the cache we need to lock.
        async with self._joined_host_linearizer.queue(room_id):
            if state_entry.state_group == cache.state_group:
                # Same state group, so nothing to do. We've already checked for
                # this above, but the cache may have changed while waiting on
                # the lock.
                pass
            elif state_entry.prev_group == cache.state_group:
                # The cached work is for the previous state group, so we work out
                # the delta.
                assert state_entry.delta_ids is not None
                for (typ, state_key), event_id in state_entry.delta_ids.items():
                    if typ != EventTypes.Member:
                        continue

                    host = intern_string(get_domain_from_id(state_key))
                    user_id = state_key
                    known_joins = cache.hosts_to_joined_users.setdefault(host, set())

                    event = await self.stores.main.get_event(event_id)
                    if event.membership == Membership.JOIN:
                        known_joins.add(user_id)
                    else:
                        known_joins.discard(user_id)

                        if not known_joins:
                            cache.hosts_to_joined_users.pop(host, None)
            else:
                # The cache doesn't match the state group or prev state group,
                # so we calculate the result from first principles.
                #
                # We need to fetch all hosts joined to the room according to `state` by
                # inspecting all join memberships in `state`. However, if the `state` is
                # relatively recent then many of its events are likely to be held in
                # the current state of the room, which is easily available and likely
                # cached.
                #
                # We therefore compute the set of `state` events not in the
                # current state and only fetch those.
                current_memberships = (
                    await self.stores.main._get_approximate_current_memberships_in_room(
                        room_id
                    )
                )
                unknown_state_events = {}
                joined_users_in_current_state = []

                state = await state_entry.get_state(
                    self, StateFilter.from_types([(EventTypes.Member, None)])
                )

                for (type, state_key), event_id in state.items():
                    if event_id not in current_memberships:
                        unknown_state_events[type, state_key] = event_id
                    elif current_memberships[event_id] == Membership.JOIN:
                        joined_users_in_current_state.append(state_key)

                joined_user_ids = await self.stores.main.get_joined_user_ids_from_state(
                    room_id, unknown_state_events
                )

                cache.hosts_to_joined_users = {}
                for user_id in chain(joined_user_ids, joined_users_in_current_state):
                    host = intern_string(get_domain_from_id(user_id))
                    cache.hosts_to_joined_users.setdefault(host, set()).add(user_id)

            if state_entry.state_group:
                cache.state_group = state_entry.state_group
            else:
                cache.state_group = object()

        return frozenset(cache.hosts_to_joined_users)


def server_acl_evaluator_from_event(acl_event: EventBase) -> "ServerAclEvaluator":
    """
    Create a ServerAclEvaluator from a m.room.server_acl event's content.

    This does up-front parsing of the content to ignore bad data. It then creates
    the ServerAclEvaluator which will pre-compile regular expressions from the globs.
    """

    # first of all, parse if literal IPs are blocked.
    allow_ip_literals = acl_event.content.get("allow_ip_literals", True)
    if not isinstance(allow_ip_literals, bool):
        logger.warning("Ignoring non-bool allow_ip_literals flag")
        allow_ip_literals = True

    # next, parse the deny list by ignoring any non-strings.
    deny = acl_event.content.get("deny", [])
    if not isinstance(deny, (list, tuple)):
        logger.warning("Ignoring non-list deny ACL %s", deny)
        deny = []
    else:
        deny = [s for s in deny if isinstance(s, str)]

    # then the allow list.
    allow = acl_event.content.get("allow", [])
    if not isinstance(allow, (list, tuple)):
        logger.warning("Ignoring non-list allow ACL %s", allow)
        allow = []
    else:
        allow = [s for s in allow if isinstance(s, str)]

    return ServerAclEvaluator(allow_ip_literals, allow, deny)
