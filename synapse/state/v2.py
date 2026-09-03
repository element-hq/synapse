#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import hashlib
import heapq
import itertools
import json
import logging
from collections import ChainMap
from typing import (
    AbstractSet,
    Any,
    Awaitable,
    Callable,
    Generator,
    Iterable,
    Literal,
    MutableMapping,
    Protocol,
    Sequence,
    cast,
    overload,
)

from synapse import event_auth
from synapse.api.constants import CREATOR_POWER_LEVEL, EventTypes
from synapse.api.errors import AuthError
from synapse.api.room_versions import RoomVersion, StateResolutionVersions
from synapse.events import EventBase, is_creator
from synapse.storage.databases.main.event_federation import StateDifference
from synapse.types import MutableStateMap, StateKey, StateMap, StrCollection
from synapse.util.duration import Duration

logger = logging.getLogger(__name__)


class Clock(Protocol):
    # This is usually synapse.util.Clock, but it's replaced with a FakeClock in tests.
    # We only ever sleep(0) though, so that other async functions can make forward
    # progress without waiting for stateres to complete.
    async def sleep(self, duration: Duration) -> None: ...


class StateResolutionStore(Protocol):
    # This is usually synapse.state.StateResolutionStore, but it's replaced with a
    # TestStateResolutionStore in tests.
    def get_events(
        self, event_ids: StrCollection, allow_rejected: bool = False
    ) -> Awaitable[dict[str, EventBase]]: ...

    def get_auth_chain_difference(
        self,
        room_id: str,
        state_sets: list[set[str]],
        conflicted_state: set[str] | None,
        additional_backwards_reachable_conflicted_events: set[str] | None,
    ) -> Awaitable[StateDifference]: ...


# We want to await to the reactor occasionally during state res when dealing
# with large data sets, so that we don't exhaust the reactor. This is done by
# awaiting to reactor during loops every N iterations.
_AWAIT_AFTER_ITERATIONS = 100


__all__ = [
    "resolve_events_with_store",
]


async def resolve_events_with_store(
    clock: Clock,
    room_id: str,
    room_version: RoomVersion,
    state_sets: Sequence[StateMap[str]],
    event_map: dict[str, EventBase] | None,
    state_res_store: StateResolutionStore,
    conflict_cache: "ConflictCache | None" = None,
) -> StateMap[str]:
    """Resolves the state using the v2 state resolution algorithm

    Args:
        clock
        room_id: the room we are working in
        room_version: The room version
        state_sets: List of dicts of (type, state_key) -> event_id,
            which are the different state groups to resolve.
        event_map:
            a dict from event_id to event, for any events that we happen to
            have in flight (eg, those currently being persisted). This will be
            used as a starting point for finding the state we need; any missing
            events will be requested via state_res_store.

            If None, all events will be fetched via state_res_store.

        state_res_store:
        conflict_cache:
            if given, the resolution of the conflicted set is looked up here
            before it is computed, and stored here afterwards. See
            `_conflict_cache_key` for what the key covers.

    Returns:
        A map from (type, state_key) to event_id.
    """

    logger.debug("Computing conflicted state")

    # We use event_map as a cache, so if its None we need to initialize it
    if event_map is None:
        event_map = {}

    # First split up the un/conflicted state
    unconflicted_state, conflicted_state = _seperate(state_sets)

    if not conflicted_state:
        return unconflicted_state

    logger.debug("%d conflicted state entries", len(conflicted_state))
    logger.debug("Calculating auth chain difference")

    conflicted_set: set[str] | None = None
    if room_version.state_res == StateResolutionVersions.V2_1:
        # calculate the conflicted subgraph
        conflicted_set = set(itertools.chain.from_iterable(conflicted_state.values()))
    auth_diff = await _get_auth_chain_difference(
        room_id,
        state_sets,
        event_map,
        state_res_store,
        conflicted_set,
    )
    full_conflicted_set = set(
        itertools.chain(
            itertools.chain.from_iterable(conflicted_state.values()), auth_diff
        )
    )

    events = await state_res_store.get_events(
        [eid for eid in full_conflicted_set if eid not in event_map],
        allow_rejected=True,
    )
    event_map.update(events)

    # everything in the event map should be in the right room
    for event in event_map.values():
        if event.room_id != room_id:
            raise Exception(
                "Attempting to state-resolve for room %s with event %s which is in %s"
                % (
                    room_id,
                    event.event_id,
                    event.room_id,
                )
            )

    full_conflicted_set = {eid for eid in full_conflicted_set if eid in event_map}

    logger.debug("%d full_conflicted_set entries", len(full_conflicted_set))

    # Calculate the base state.
    #
    # v2 uses the unconflicted state as the base state, but v2.1 uses the empty
    # set.
    base_state: StateMap[str] = {}
    if room_version.state_res != StateResolutionVersions.V2_1:
        # `_iterative_auth_checks` reads the base state only at the auth types
        # of the events it checks, and `_mainline_sort` reads only the power
        # levels. Restricting the base state to those keys therefore changes
        # nothing, and it keeps keys that cannot affect the outcome out of the
        # cache key. The rest of the unconflicted state is layered back on
        # below.
        #
        # The keys come from `auth_types_for_event`, not from the events' own
        # auth events, because the checks read the resolved state at every auth
        # type rather than looking up the auth event ID.
        base_state_keys = {(EventTypes.PowerLevels, "")}
        for event_id in full_conflicted_set:
            base_state_keys.update(
                event_auth.auth_types_for_event(room_version, event_map[event_id])
            )

        base_state = {
            key: unconflicted_state[key]
            for key in base_state_keys
            if key in unconflicted_state
        }

    resolved_state: StateMap[str] | None = None
    if conflict_cache is not None:
        cache_key = _conflict_cache_key(
            room_id, room_version, full_conflicted_set, base_state
        )
        resolved_state = conflict_cache.get(cache_key)

    if resolved_state is None:
        resolved_state = await _resolve_conflicted_set(
            clock,
            room_id,
            room_version,
            full_conflicted_set,
            base_state,
            event_map,
            state_res_store,
        )
        if conflict_cache is not None:
            conflict_cache[cache_key] = resolved_state
    else:
        logger.debug(
            "Reusing the resolution of %d conflicted events",
            len(full_conflicted_set),
        )

    logger.debug("done")

    # We make sure that unconflicted state always still applies. A `ChainMap`
    # rather than a copy, because `resolved_state` may be the cached map and
    # must not be modified. The casts only satisfy `ChainMap`'s signature.
    # Nothing writes through the result.
    return ChainMap(
        cast(MutableMapping[StateKey, str], unconflicted_state),
        cast(MutableMapping[StateKey, str], resolved_state),
    )


class ConflictCache(Protocol):
    """What `resolve_events_with_store` needs from a `conflict_cache`.

    Satisfied by a plain `dict` and by `ExpiringCache`.

    Mainly used to allow unit tests to pass a `dict` rather than building a full
    cache.
    """

    def get(self, key: bytes) -> StateMap[str] | None: ...

    def __setitem__(self, key: bytes, value: StateMap[str]) -> None: ...


def _conflict_cache_key(
    room_id: str,
    room_version: RoomVersion,
    full_conflicted_set: AbstractSet[str],
    base_state: StateMap[str],
) -> bytes:
    """Fingerprint everything `_resolve_conflicted_set` depends on.

    Its result is a function of the room version, the events in the conflicted
    set and the base state it starts from. Two calls that agree on all three
    reach the same resolved state, however different their callers'
    unconflicted state is otherwise.

    A digest rather than the inputs themselves, so that a cache keyed on this
    doesn't hold onto thousands of event ID strings per entry.
    """

    # We use JSON as the serialization format for ease, we could use a
    # hand-rolled format but one has to be careful to ensure that you can't get
    # collisions (given in some room versions the room ID is arbitrary bytes
    # rather than a hash).
    key_material = json.dumps(
        {
            "room_id": room_id,
            "room_version": room_version.identifier,
            "conflicted_set": sorted(full_conflicted_set),
            "base_state": sorted(base_state.values()),
        }
    )
    return hashlib.sha256(key_material.encode("utf-8")).digest()


async def _resolve_conflicted_set(
    clock: Clock,
    room_id: str,
    room_version: RoomVersion,
    full_conflicted_set: set[str],
    base_state: StateMap[str],
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
) -> MutableStateMap[str]:
    """Apply the conflicted events to the base state.

    This is the expensive part of `resolve_events_with_store`: the reverse
    topological power sort, the two iterative auth check passes and the
    mainline sort.

    Args:
        clock
        room_id: the room we are working in
        room_version: the room version
        full_conflicted_set: the conflicted events plus the auth chain
            difference. All must be present in `event_map`.
        base_state: the state to apply the conflicted events to
        event_map: updated in place with the events fetched along the way
        state_res_store

    Returns:
        The base state with the conflicted events that passed auth applied
        over it. The unconflicted state is not merged in.
    """

    # Get and sort all the power events (kicks/bans/etc)
    power_events = (
        eid for eid in full_conflicted_set if _is_power_event(event_map[eid])
    )

    sorted_power_events = await _reverse_topological_power_sort(
        clock, room_id, power_events, event_map, state_res_store, full_conflicted_set
    )

    logger.debug("sorted %d power events", len(sorted_power_events))

    # Now sequentially auth each one
    resolved_state = await _iterative_auth_checks(
        clock,
        room_id,
        room_version,
        event_ids=sorted_power_events,
        base_state=base_state,
        event_map=event_map,
        state_res_store=state_res_store,
    )

    logger.debug("resolved power events")

    # OK, so we've now resolved the power events. Now sort the remaining
    # events using the mainline of the resolved power level.

    set_power_events = set(sorted_power_events)
    leftover_events = [
        ev_id for ev_id in full_conflicted_set if ev_id not in set_power_events
    ]

    logger.debug("sorting %d remaining events", len(leftover_events))

    pl = resolved_state.get((EventTypes.PowerLevels, ""), None)
    leftover_events = await _mainline_sort(
        clock, room_id, leftover_events, pl, event_map, state_res_store
    )

    logger.debug("resolving remaining events")

    resolved_state = await _iterative_auth_checks(
        clock,
        room_id,
        room_version,
        leftover_events,
        resolved_state,
        event_map,
        state_res_store,
    )

    logger.debug("resolved")

    return resolved_state


async def _get_power_level_for_sender(
    room_id: str,
    event_id: str,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
) -> int:
    """Return the power level of the sender of the given event according to
    their auth events.

    Args:
        room_id
        event_id
        event_map
        state_res_store

    Returns:
        The power level.
    """
    event = await _get_event(room_id, event_id, event_map, state_res_store)

    pl = None
    create = None
    for aid in event.auth_event_ids():
        aev = await _get_event(
            room_id, aid, event_map, state_res_store, allow_none=True
        )
        if aev and (aev.type, aev.state_key) == (EventTypes.PowerLevels, ""):
            pl = aev
        if aev and (aev.type, aev.state_key) == (EventTypes.Create, ""):
            create = aev

    if event.type != EventTypes.Create:
        # we should always have a create event
        assert create is not None

    if create and create.room_version.msc4289_creator_power_enabled:
        if is_creator(create, event.sender):
            return CREATOR_POWER_LEVEL

    if pl is None:
        # Couldn't find power level. Check if they're the creator of the room
        for aid in event.auth_event_ids():
            aev = await _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if aev and (aev.type, aev.state_key) == (EventTypes.Create, ""):
                creator = (
                    aev.sender
                    if event.room_version.implicit_room_creator
                    else aev.content.get("creator")
                )
                if not creator:
                    logger.warning(
                        "_get_power_level_for_sender: event %s has no PL in auth_events and "
                        "creator is missing from create event %s",
                        event_id,
                        aev.event_id,
                    )
                if creator == event.sender:
                    return 100
                break
        return 0

    level = pl.content.get("users", {}).get(event.sender)
    if level is None:
        level = pl.content.get("users_default", 0)

    if level is None:
        return 0
    else:
        return int(level)


async def _get_auth_chain_difference(
    room_id: str,
    state_sets: Sequence[StateMap[str]],
    unpersisted_events: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    conflicted_state: set[str] | None,
) -> set[str]:
    """Compare the auth chains of each state set and return the set of events
    that only appear in some, but not all of the auth chains.

    Args:
        state_sets: The input state sets we are trying to resolve across.
        unpersisted_events: A map from event ID to EventBase containing all unpersisted
            events involved in this resolution.
        state_res_store: A way to retrieve events and extract graph information on the auth chains.
        conflicted_state: which event IDs are conflicted. Used in v2.1 for calculating the conflicted
            subgraph.

    Returns:
        The auth difference of the given state sets, as a set of event IDs. Also includes the
        conflicted subgraph if `conflicted_state` is set.
    """
    is_state_res_v21 = conflicted_state is not None
    num_conflicted_state = (
        len(conflicted_state) if conflicted_state is not None else None
    )

    # The `StateResolutionStore.get_auth_chain_difference` function assumes that
    # all events passed to it (and their auth chains) have been persisted
    # previously. We need to manually handle any other events that are yet to be
    # persisted.
    #
    # We do this in three steps:
    #   1. Compute the set of unpersisted events belonging to the auth difference.
    #   2. Replacing any unpersisted events in the state_sets with their auth events,
    #      recursively, until the state_sets contain only persisted events.
    #      Then we call `store.get_auth_chain_difference` as normal, which computes
    #      the set of persisted events belonging to the auth difference.
    #   3. Adding the results of 1 and 2 together.

    # Map from event ID in `unpersisted_events` to their auth event IDs, and their auth
    # event IDs if they appear in the `unpersisted_events`. This is the intersection of
    # the event's auth chain with the events in `unpersisted_events` *plus* their
    # auth event IDs.
    events_to_auth_chain: dict[str, set[str]] = {}
    # remember the forward links when doing the graph traversal, we'll need it for v2.1 checks
    # This is a map from an event to the set of events that contain it as an auth event.
    event_to_next_event: dict[str, set[str]] = {}
    for event in unpersisted_events.values():
        chain = {event.event_id}
        events_to_auth_chain[event.event_id] = chain

        to_search = [event]
        while to_search:
            next_event = to_search.pop()
            for auth_id in next_event.auth_event_ids():
                chain.add(auth_id)
                event_to_next_event.setdefault(auth_id, set()).add(next_event.event_id)
                auth_event = unpersisted_events.get(auth_id)
                if auth_event:
                    to_search.append(auth_event)

    # We now 1) calculate the auth chain difference for the unpersisted events
    # and 2) work out the state sets to pass to the store.
    #
    # Note: If there are no `unpersisted_events` (which is the common case), we can do a
    # much simpler calculation.
    additional_backwards_reachable_conflicted_events: set[str] = set()
    unpersisted_conflicted_events: set[str] = set()
    if unpersisted_events:
        # The list of state sets to pass to the store, where each state set is a set
        # of the event ids making up the state. This is similar to `state_sets`,
        # except that (a) we only have event ids, not the complete
        # ((type, state_key)->event_id) mappings; and (b) we have stripped out
        # unpersisted events and replaced them with the persisted events in
        # their auth chain.
        state_sets_ids: list[set[str]] = []

        # For each state set, the unpersisted event IDs reachable (by their auth
        # chain) from the events in that set.
        unpersisted_set_ids: list[set[str]] = []

        for state_set in state_sets:
            set_ids: set[str] = set()
            state_sets_ids.append(set_ids)

            unpersisted_ids: set[str] = set()
            unpersisted_set_ids.append(unpersisted_ids)

            for event_id in state_set.values():
                event_chain = events_to_auth_chain.get(event_id)
                if event_chain is not None:
                    # We have an unpersisted event. We add all the auth
                    # events that it references which are also unpersisted.
                    set_ids.update(
                        e for e in event_chain if e not in unpersisted_events
                    )

                    # We also add the full chain of unpersisted event IDs
                    # referenced by this state set, so that we can work out the
                    # auth chain difference of the unpersisted events.
                    unpersisted_ids.update(
                        e for e in event_chain if e in unpersisted_events
                    )
                else:
                    set_ids.add(event_id)
        if conflicted_state:
            for conflicted_event_id in conflicted_state:
                # presence in this map means it is unpersisted.
                event_chain = events_to_auth_chain.get(conflicted_event_id)
                if event_chain is not None:
                    unpersisted_conflicted_events.add(conflicted_event_id)
                    # tell the DB layer that we have some unpersisted conflicted events
                    additional_backwards_reachable_conflicted_events.update(
                        e for e in event_chain if e not in unpersisted_events
                    )
        # The auth chain difference of the unpersisted events of the state sets
        # is calculated by taking the difference between the union and
        # intersections.
        union = unpersisted_set_ids[0].union(*unpersisted_set_ids[1:])
        intersection = unpersisted_set_ids[0].intersection(*unpersisted_set_ids[1:])

        auth_difference_unpersisted_part: StrCollection = union - intersection
    else:
        auth_difference_unpersisted_part = ()
        state_sets_ids = [set(state_set.values()) for state_set in state_sets]

    if conflicted_state:
        # to ensure that conflicted state is a subset of state set IDs, we need to remove UNPERSISTED
        # conflicted state set ids as we removed them above.
        conflicted_state = conflicted_state - unpersisted_conflicted_events

    difference = await state_res_store.get_auth_chain_difference(
        room_id,
        state_sets_ids,
        conflicted_state,
        additional_backwards_reachable_conflicted_events,
    )
    difference.auth_difference.update(auth_difference_unpersisted_part)

    # if we're doing v2.1 we may need to add or expand the conflicted subgraph
    if (
        is_state_res_v21
        and difference.conflicted_subgraph is not None
        and unpersisted_events
    ):
        # we always include the conflicted events themselves in the subgraph.
        if conflicted_state:
            difference.conflicted_subgraph.update(conflicted_state)
        # we may need to expand the subgraph in the case where the subgraph starts in the DB and
        # ends in unpersisted events. To do this, we first need to see where the subgraph got up to,
        # which we can do by finding the intersection between the additional backwards reachable
        # conflicted events and the conflicted subgraph. Events in both sets mean A) some unpersisted
        # conflicted event could backwards reach it and B) some persisted conflicted event could forward
        # reach it.
        subgraph_frontier = difference.conflicted_subgraph.intersection(
            additional_backwards_reachable_conflicted_events
        )
        # we can now combine the 2 scenarios:
        #  - subgraph starts in DB and ends in unpersisted
        #  - subgraph starts in unpersisted and ends in unpersisted
        # by expanding the frontier into unpersisted events.
        # The frontier is currently all persisted events. We want to expand this into unpersisted
        # events. Mark every forwards reachable event from the frontier in the forwards_conflicted_set
        # but NOT the backwards conflicted set. This mirrors what the DB layer does but in reverse:
        # we supplied events which are backwards reachable to the DB and now the DB is providing
        # forwards reachable events from the DB.
        forwards_conflicted_set: set[str] = set()
        # we include unpersisted conflicted events here to process exclusive unpersisted subgraphs
        search_queue = subgraph_frontier.union(unpersisted_conflicted_events)
        while search_queue:
            frontier_event = search_queue.pop()
            next_event_ids = event_to_next_event.get(frontier_event, set())
            search_queue.update(next_event_ids)
            forwards_conflicted_set.add(frontier_event)

        # we've already calculated the backwards form as this is the auth chain for each
        # unpersisted conflicted event.
        backwards_conflicted_set: set[str] = set()
        for uce in unpersisted_conflicted_events:
            backwards_conflicted_set.update(events_to_auth_chain.get(uce, []))

        # the unpersisted conflicted subgraph is the intersection of the backwards/forwards sets
        conflicted_subgraph_unpersisted_part = backwards_conflicted_set.intersection(
            forwards_conflicted_set
        )
        # print(f"event_to_next_event={event_to_next_event}")
        # print(f"unpersisted_conflicted_events={unpersisted_conflicted_events}")
        # print(f"unperssited backwards_conflicted_set={backwards_conflicted_set}")
        # print(f"unperssited forwards_conflicted_set={forwards_conflicted_set}")
        difference.conflicted_subgraph.update(conflicted_subgraph_unpersisted_part)

    if difference.conflicted_subgraph:
        old_events = difference.auth_difference.union(
            conflicted_state if conflicted_state else set()
        )
        additional_events = difference.conflicted_subgraph.difference(old_events)

        logger.debug(
            "v2.1 %s additional events replayed=%d num_conflicts=%d conflicted_subgraph=%d auth_difference=%d",
            room_id,
            len(additional_events),
            num_conflicted_state,
            len(difference.conflicted_subgraph),
            len(difference.auth_difference),
        )
        # State res v2.1 includes the conflicted subgraph in the difference
        return difference.auth_difference.union(difference.conflicted_subgraph)

    return difference.auth_difference


def _seperate(
    state_sets: Iterable[StateMap[str]],
) -> tuple[StateMap[str], StateMap[set[str]]]:
    """Return the unconflicted and conflicted state. This is different than in
    the original algorithm, as this defines a key to be conflicted if one of
    the state sets doesn't have that key.

    Args:
        state_sets

    Returns:
        A tuple of unconflicted and conflicted state. The conflicted state dict
        is a map from type/state_key to set of event IDs
    """
    unconflicted_state = {}
    conflicted_state = {}

    for key in set(itertools.chain.from_iterable(state_sets)):
        event_ids = {state_set.get(key) for state_set in state_sets}
        if len(event_ids) == 1:
            unconflicted_state[key] = event_ids.pop()
        else:
            event_ids.discard(None)
            conflicted_state[key] = event_ids

    # mypy doesn't understand that discarding None above means that conflicted
    # state is StateMap[set[str]], not StateMap[set[str | None]].
    return unconflicted_state, conflicted_state  # type: ignore[return-value]


def _is_power_event(event: EventBase) -> bool:
    """Return whether or not the event is a "power event", as defined by the
    v2 state resolution algorithm

    Args:
        event

    Returns:
        True if the event is a power event.
    """
    if (event.type, event.state_key) in (
        (EventTypes.PowerLevels, ""),
        (EventTypes.JoinRules, ""),
        (EventTypes.Create, ""),
    ):
        return True

    if event.type == EventTypes.Member:
        if event.membership in ("leave", "ban"):
            return event.sender != event.state_key

    return False


async def _add_event_and_auth_chain_to_graph(
    graph: dict[str, set[str]],
    room_id: str,
    event_id: str,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    full_conflicted_set: set[str],
) -> None:
    """Helper function for _reverse_topological_power_sort that add the event
    and its auth chain (that is in the auth diff) to the graph

    Args:
        graph: A map from event ID to the events auth event IDs
        room_id: the room we are working in
        event_id: Event to add to the graph
        event_map
        state_res_store
        full_conflicted_set: Set of event IDs that are in the full conflicted set.
    """

    state = [event_id]
    while state:
        eid = state.pop()
        graph.setdefault(eid, set())

        event = await _get_event(room_id, eid, event_map, state_res_store)
        for aid in event.auth_event_ids():
            if aid in full_conflicted_set:
                if aid not in graph:
                    state.append(aid)

                graph.setdefault(eid, set()).add(aid)


async def _reverse_topological_power_sort(
    clock: Clock,
    room_id: str,
    event_ids: Iterable[str],
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    full_conflicted_set: set[str],
) -> list[str]:
    """Returns a list of the event_ids sorted by reverse topological ordering,
    and then by power level and origin_server_ts

    Args:
        clock
        room_id: the room we are working in
        event_ids: The events to sort
        event_map
        state_res_store
        full_conflicted_set: Set of event IDs that are in the full conflicted set.

    Returns:
        The sorted list
    """

    graph: dict[str, set[str]] = {}
    for idx, event_id in enumerate(event_ids, start=1):
        await _add_event_and_auth_chain_to_graph(
            graph, room_id, event_id, event_map, state_res_store, full_conflicted_set
        )

        # We await occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

    event_to_pl = {}
    for idx, event_id in enumerate(graph, start=1):
        pl = await _get_power_level_for_sender(
            room_id, event_id, event_map, state_res_store
        )
        event_to_pl[event_id] = pl

        # We await occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

    def _get_power_order(event_id: str) -> tuple[int, int, str]:
        ev = event_map[event_id]
        pl = event_to_pl[event_id]

        return -pl, ev.origin_server_ts, event_id

    # Note: graph is modified during the sort
    it = lexicographical_topological_sort(graph, key=_get_power_order)
    sorted_events = list(it)

    return sorted_events


async def _iterative_auth_checks(
    clock: Clock,
    room_id: str,
    room_version: RoomVersion,
    event_ids: list[str],
    base_state: StateMap[str],
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
) -> MutableStateMap[str]:
    """Sequentially apply auth checks to each event in given list, updating the
    state as it goes along.

    Args:
        clock
        room_id
        room_version
        event_ids: Ordered list of events to apply auth checks to
        base_state: The set of state to start with
        event_map
        state_res_store

    Returns:
        Returns the final updated state
    """
    resolved_state = dict(base_state)

    for idx, event_id in enumerate(event_ids, start=1):
        event = event_map[event_id]

        auth_events = {}
        for aid in event.auth_event_ids():
            ev = await _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )

            if not ev:
                logger.warning(
                    "auth_event id %s for event %s is missing", aid, event_id
                )
            else:
                if ev.rejected_reason is None:
                    auth_events[(ev.type, ev.state_key)] = ev

        for key in event_auth.auth_types_for_event(room_version, event):
            if key in resolved_state:
                ev_id = resolved_state[key]
                ev = await _get_event(room_id, ev_id, event_map, state_res_store)

                if ev.rejected_reason is None:
                    auth_events[key] = event_map[ev_id]

        if event.rejected_reason is not None:
            # Do not admit previously rejected events into state.
            # TODO: This isn't spec compliant. Events that were previously rejected due
            #       to failing auth checks at their state, but pass auth checks during
            #       state resolution should be accepted. Synapse does not handle the
            #       change of rejection status well, so we preserve the previous
            #       rejection status for now.
            #
            #       Note that events rejected for non-state reasons, such as having the
            #       wrong auth events, should remain rejected.
            #
            #       https://spec.matrix.org/v1.2/rooms/v9/#rejected-events
            #       https://github.com/matrix-org/synapse/issues/13797
            continue

        try:
            event_auth.check_state_dependent_auth_rules(
                event,
                auth_events.values(),
            )

            resolved_state[(event.type, event.state_key)] = event_id
        except AuthError:
            pass

        # We await occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

    return resolved_state


async def _mainline_sort(
    clock: Clock,
    room_id: str,
    event_ids: list[str],
    resolved_power_event_id: str | None,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
) -> list[str]:
    """Returns a sorted list of event_ids sorted by mainline ordering based on
    the given event resolved_power_event_id

    Args:
        clock
        room_id: room we're working in
        event_ids: Events to sort
        resolved_power_event_id: The final resolved power level event ID
        event_map
        state_res_store

    Returns:
        The sorted list
    """
    if not event_ids:
        # It's possible for there to be no event IDs here to sort, so we can
        # skip calculating the mainline in that case.
        return []

    mainline = []
    pl = resolved_power_event_id
    idx = 0
    while pl:
        mainline.append(pl)
        pl_ev = await _get_event(room_id, pl, event_map, state_res_store)
        auth_events = pl_ev.auth_event_ids()
        pl = None
        for aid in auth_events:
            ev = await _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if ev and (ev.type, ev.state_key) == (EventTypes.PowerLevels, ""):
                pl = aid
                break

        # We await occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx != 0 and idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

        idx += 1

    mainline_map = {ev_id: i + 1 for i, ev_id in enumerate(reversed(mainline))}

    event_ids = list(event_ids)

    order_map = {}
    for idx, ev_id in enumerate(event_ids, start=1):
        depth = await _get_mainline_depth_for_event(
            clock, event_map[ev_id], mainline_map, event_map, state_res_store
        )
        order_map[ev_id] = (depth, event_map[ev_id].origin_server_ts, ev_id)

        # We await occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

    event_ids.sort(key=lambda ev_id: order_map[ev_id])

    return event_ids


async def _get_mainline_depth_for_event(
    clock: Clock,
    event: EventBase,
    mainline_map: dict[str, int],
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
) -> int:
    """Get the mainline depths for the given event based on the mainline map

    Args:
        event
        mainline_map: Map from event_id to mainline depth for events in the mainline.
        event_map
        state_res_store

    Returns:
        The mainline depth
    """

    room_id = event.room_id
    tmp_event: EventBase | None = event

    # We do an iterative search, replacing `event with the power level in its
    # auth events (if any)
    idx = 0
    while tmp_event:
        depth = mainline_map.get(tmp_event.event_id)
        if depth is not None:
            return depth

        auth_events = tmp_event.auth_event_ids()
        tmp_event = None

        for aid in auth_events:
            aev = await _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if aev and (aev.type, aev.state_key) == (EventTypes.PowerLevels, ""):
                tmp_event = aev
                break

        idx += 1

        if idx % _AWAIT_AFTER_ITERATIONS == 0:
            await clock.sleep(Duration(seconds=0))

    # Didn't find a power level auth event, so we just return 0
    return 0


@overload
async def _get_event(
    room_id: str,
    event_id: str,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    allow_none: Literal[False] = False,
) -> EventBase: ...


@overload
async def _get_event(
    room_id: str,
    event_id: str,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    allow_none: Literal[True],
) -> EventBase | None: ...


async def _get_event(
    room_id: str,
    event_id: str,
    event_map: dict[str, EventBase],
    state_res_store: StateResolutionStore,
    allow_none: bool = False,
) -> EventBase | None:
    """Helper function to look up event in event_map, falling back to looking
    it up in the store

    Args:
        room_id
        event_id
        event_map
        state_res_store
        allow_none: if the event is not found, return None rather than raising
            an exception

    Returns:
        The event, or none if the event does not exist (and allow_none is True).
    """
    if event_id not in event_map:
        events = await state_res_store.get_events([event_id], allow_rejected=True)
        event_map.update(events)
    event = event_map.get(event_id)

    if event is None:
        if allow_none:
            return None
        raise Exception("Unknown event %s" % (event_id,))

    if event.room_id != room_id:
        raise Exception(
            "In state res for room %s, event %s is in %s"
            % (room_id, event_id, event.room_id)
        )
    return event


def lexicographical_topological_sort(
    graph: dict[str, set[str]], key: Callable[[str], Any]
) -> Generator[str, None, None]:
    """Performs a lexicographic reverse topological sort on the graph.

    This returns a reverse topological sort (i.e. if node A references B then B
    appears before A in the sort), with ties broken lexicographically based on
    return value of the `key` function.

    NOTE: `graph` is modified during the sort.

    Args:
        graph: A representation of the graph where each node is a key in the
            dict and its value are the nodes edges.
        key: A function that takes a node and returns a value that is comparable
            and used to order nodes

    Yields:
        The next node in the topological sort
    """

    # Note, this is basically Kahn's algorithm except we look at nodes with no
    # outgoing edges, c.f.
    # https://en.wikipedia.org/wiki/Topological_sorting#Kahn's_algorithm
    outdegree_map = graph
    reverse_graph: dict[str, set[str]] = {}

    # Lists of nodes with zero out degree. Is actually a tuple of
    # `(key(node), node)` so that sorting does the right thing
    zero_outdegree = []

    for node, edges in graph.items():
        if len(edges) == 0:
            zero_outdegree.append((key(node), node))

        reverse_graph.setdefault(node, set())
        for edge in edges:
            reverse_graph.setdefault(edge, set()).add(node)

    # heapq is a built in implementation of a sorted queue.
    heapq.heapify(zero_outdegree)

    while zero_outdegree:
        _, node = heapq.heappop(zero_outdegree)

        for parent in reverse_graph[node]:
            out = outdegree_map[parent]
            out.discard(node)
            if len(out) == 0:
                heapq.heappush(zero_outdegree, (key(parent), parent))

        yield node
