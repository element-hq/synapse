#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) The Matrix.org Foundation C.I.C. 2022
# Copyright 2014 - 2016 OpenMarket Ltd
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
from enum import Enum, auto
from typing import (
    Collection,
    Dict,
    Final,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import attr

from synapse.api.constants import (
    EventTypes,
    EventUnsignedContentFields,
    HistoryVisibility,
    Membership,
)
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.events.utils import clone_event, prune_event
from synapse.logging.opentracing import trace
from synapse.storage.controllers import StorageControllers
from synapse.storage.databases.main import DataStore
from synapse.types import RetentionPolicy, StateMap, StrCollection, get_domain_from_id
from synapse.types.state import StateFilter
from synapse.util import Clock

logger = logging.getLogger(__name__)
filtered_event_logger = logging.getLogger("synapse.visibility.filtered_event_debug")

VISIBILITY_PRIORITY = (
    HistoryVisibility.WORLD_READABLE,
    HistoryVisibility.SHARED,
    HistoryVisibility.INVITED,
    HistoryVisibility.JOINED,
)


MEMBERSHIP_PRIORITY = (
    Membership.JOIN,
    Membership.INVITE,
    Membership.KNOCK,
    Membership.LEAVE,
    Membership.BAN,
)

_HISTORY_VIS_KEY: Final[Tuple[str, str]] = (EventTypes.RoomHistoryVisibility, "")


@trace
async def filter_events_for_client(
    storage: StorageControllers,
    user_id: str,
    events: List[EventBase],
    is_peeking: bool = False,
    always_include_ids: FrozenSet[str] = frozenset(),
    filter_send_to_client: bool = True,
) -> List[EventBase]:
    """
    Check which events a user is allowed to see. If the user can see the event but its
    sender asked for their data to be erased, prune the content of the event.

    Args:
        storage
        user_id: user id to be checked
        events: sequence of events to be checked
        is_peeking: should be True if:
          * the user is not currently a member of the room, and:
          * the user has not been a member of the room since the given
            events
        always_include_ids: set of event ids to specifically include, if present
            in events (unless sender is ignored)
        filter_send_to_client: Whether we're checking an event that's going to be
            sent to a client. This might not always be the case since this function can
            also be called to check whether a user can see the state at a given point.

    Returns:
        The filtered events. The `unsigned` data is annotated with the membership state
        of `user_id` at each event.
    """
    # Filter out events that have been soft failed so that we don't relay them
    # to clients.
    events_before_filtering = events
    events = [e for e in events if not e.internal_metadata.is_soft_failed()]
    if len(events_before_filtering) != len(events):
        if filtered_event_logger.isEnabledFor(logging.DEBUG):
            filtered_event_logger.debug(
                "filter_events_for_client: Filtered out soft-failed events: Before=%s, After=%s",
                [event.event_id for event in events_before_filtering],
                [event.event_id for event in events],
            )

    types = (_HISTORY_VIS_KEY, (EventTypes.Member, user_id))

    # we exclude outliers at this point, and then handle them separately later
    event_id_to_state = await storage.state.get_state_for_events(
        frozenset(e.event_id for e in events if not e.internal_metadata.outlier),
        state_filter=StateFilter.from_types(types),
    )

    # Get the users who are ignored by the requesting user.
    ignore_list = await storage.main.ignored_users(user_id)

    erased_senders = await storage.main.are_users_erased(e.sender for e in events)

    if filter_send_to_client:
        room_ids = {e.room_id for e in events}
        retention_policies: Dict[str, RetentionPolicy] = {}

        for room_id in room_ids:
            retention_policies[
                room_id
            ] = await storage.main.get_retention_policy_for_room(room_id)

    def allowed(event: EventBase) -> Optional[EventBase]:
        state_after_event = event_id_to_state.get(event.event_id)
        filtered = _check_client_allowed_to_see_event(
            user_id=user_id,
            event=event,
            clock=storage.main.clock,
            filter_send_to_client=filter_send_to_client,
            sender_ignored=event.sender in ignore_list,
            always_include_ids=always_include_ids,
            retention_policy=retention_policies[event.room_id],
            state=state_after_event,
            is_peeking=is_peeking,
            sender_erased=erased_senders.get(event.sender, False),
        )
        if filtered is None:
            return None

        # Annotate the event with the user's membership after the event.
        #
        # Normally we just look in `state_after_event`, but if the event is an outlier
        # we won't have such a state. The only outliers that are returned here are the
        # user's own membership event, so we can just inspect that.

        user_membership_event: Optional[EventBase]
        if event.type == EventTypes.Member and event.state_key == user_id:
            user_membership_event = event
        elif state_after_event is not None:
            user_membership_event = state_after_event.get((EventTypes.Member, user_id))
        else:
            # unreachable!
            raise Exception("Missing state for event that is not user's own membership")

        user_membership = (
            user_membership_event.membership
            if user_membership_event
            else Membership.LEAVE
        )

        # Copy the event before updating the unsigned data: this shouldn't be persisted
        # to the cache!
        cloned = clone_event(filtered)
        cloned.unsigned[EventUnsignedContentFields.MEMBERSHIP] = user_membership

        return cloned

    # Check each event: gives an iterable of None or (a modified) EventBase.
    filtered_events = map(allowed, events)

    # Turn it into a list and remove None entries before returning.
    return [ev for ev in filtered_events if ev]


async def filter_event_for_clients_with_state(
    store: DataStore,
    user_ids: StrCollection,
    event: EventBase,
    context: EventContext,
    is_peeking: bool = False,
    filter_send_to_client: bool = True,
) -> StrCollection:
    """
    Checks to see if an event is visible to the users in the list at the time of
    the event.

    Note: This does *not* check if the sender of the event was erased.

    Args:
        store: databases
        user_ids: user_ids to be checked
        event: the event to be checked
        context: EventContext for the event to be checked
        is_peeking: Whether the users are peeking into the room, ie not
            currently joined
        filter_send_to_client: Whether we're checking an event that's going to be
            sent to a client. This might not always be the case since this function can
            also be called to check whether a user can see the state at a given point.

    Returns:
        Collection of user IDs for whom the event is visible
    """
    # None of the users should see the event if it is soft_failed
    if event.internal_metadata.is_soft_failed():
        return []

    # Fast path if we don't have any user IDs to check.
    if not user_ids:
        return ()

    # Make a set for all user IDs that haven't been filtered out by a check.
    allowed_user_ids = set(user_ids)

    # Only run some checks if these events aren't about to be sent to clients. This is
    # because, if this is not the case, we're probably only checking if the users can
    # see events in the room at that point in the DAG, and that shouldn't be decided
    # on those checks.
    if filter_send_to_client:
        ignored_by = await store.ignored_by(event.sender)
        retention_policy = await store.get_retention_policy_for_room(event.room_id)

        for user_id in user_ids:
            if (
                _check_filter_send_to_client(
                    event,
                    store.clock,
                    retention_policy,
                    sender_ignored=user_id in ignored_by,
                )
                == _CheckFilter.DENIED
            ):
                allowed_user_ids.discard(user_id)

    if event.internal_metadata.outlier:
        # Normally these can't be seen by clients, but we make an exception for
        # for out-of-band membership events (eg, incoming invites, or rejections of
        # said invite) for the user themselves.
        if event.type == EventTypes.Member and event.state_key in allowed_user_ids:
            logger.debug("Returning out-of-band-membership event %s", event)
            return {event.state_key}

        return set()

    # First we get just the history visibility in case its shared/world-readable
    # room.
    visibility_state_map = await _get_state_map(
        store, event, context, StateFilter.from_types([_HISTORY_VIS_KEY])
    )

    visibility = get_effective_room_visibility_from_state(visibility_state_map)
    if (
        _check_history_visibility(event, visibility, is_peeking=is_peeking)
        == _CheckVisibility.ALLOWED
    ):
        return allowed_user_ids

    # The history visibility isn't lax, so we now need to fetch the membership
    # events of all the users.

    filter_list = []
    for user_id in allowed_user_ids:
        filter_list.append((EventTypes.Member, user_id))
    filter_list.append((EventTypes.RoomHistoryVisibility, ""))

    state_filter = StateFilter.from_types(filter_list)
    state_map = await _get_state_map(store, event, context, state_filter)

    # Now we check whether the membership allows each user to see the event.
    return {
        user_id
        for user_id in allowed_user_ids
        if _check_membership(user_id, event, visibility, state_map, is_peeking).allowed
    }


async def _get_state_map(
    store: DataStore, event: EventBase, context: EventContext, state_filter: StateFilter
) -> StateMap[EventBase]:
    """Helper function for getting a `StateMap[EventBase]` from an `EventContext`"""
    state_map = await context.get_prev_state_ids(state_filter)

    # Use events rather than event ids as content from the events are needed in
    # _check_visibility
    event_map = await store.get_events(state_map.values(), get_prev_content=False)

    updated_state_map = {}
    for state_key, event_id in state_map.items():
        state_event = event_map.get(event_id)
        if state_event:
            updated_state_map[state_key] = state_event

    if event.is_state():
        current_state_key = (event.type, event.state_key)
        # Add current event to updated_state_map, we need to do this here as it
        # may not have been persisted to the db yet
        updated_state_map[current_state_key] = event

    return updated_state_map


def _check_client_allowed_to_see_event(
    user_id: str,
    event: EventBase,
    clock: Clock,
    filter_send_to_client: bool,
    is_peeking: bool,
    always_include_ids: FrozenSet[str],
    sender_ignored: bool,
    retention_policy: RetentionPolicy,
    state: Optional[StateMap[EventBase]],
    sender_erased: bool,
) -> Optional[EventBase]:
    """Check with the given user is allowed to see the given event

    See `filter_events_for_client` for details about args

    Args:
        user_id
        event
        clock
        filter_send_to_client
        is_peeking
        always_include_ids
        sender_ignored: Whether the user is ignoring the event sender
        retention_policy: The retention policy of the room
        state: The state at the event, unless its an outlier
        sender_erased: Whether the event sender has been marked as "erased"

    Returns:
        None if the user cannot see this event at all

        a redacted copy of the event if they can only see a redacted
        version

        the original event if they can see it as normal.
    """
    # Only run some checks if these events aren't about to be sent to clients. This is
    # because, if this is not the case, we're probably only checking if the users can
    # see events in the room at that point in the DAG, and that shouldn't be decided
    # on those checks.
    if filter_send_to_client:
        if (
            _check_filter_send_to_client(event, clock, retention_policy, sender_ignored)
            == _CheckFilter.DENIED
        ):
            filtered_event_logger.debug(
                "_check_client_allowed_to_see_event(event=%s): Filtered out event because `_check_filter_send_to_client` returned `_CheckFilter.DENIED`",
                event.event_id,
            )
            return None

    if event.event_id in always_include_ids:
        return event

    # we need to handle outliers separately, since we don't have the room state.
    if event.internal_metadata.outlier:
        # Normally these can't be seen by clients, but we make an exception for
        # for out-of-band membership events (eg, incoming invites, or rejections of
        # said invite) for the user themselves.
        if event.type == EventTypes.Member and event.state_key == user_id:
            logger.debug(
                "_check_client_allowed_to_see_event(event=%s): Returning out-of-band-membership event %s",
                event.event_id,
                event,
            )
            return event

        filtered_event_logger.debug(
            "_check_client_allowed_to_see_event(event=%s): Filtered out event because it's an outlier",
            event.event_id,
        )
        return None

    if state is None:
        raise Exception("Missing state for non-outlier event")

    # get the room_visibility at the time of the event.
    visibility = get_effective_room_visibility_from_state(state)

    # Check if the room has lax history visibility, allowing us to skip
    # membership checks.
    #
    # We can only do this check if the sender has *not* been erased, as if they
    # have we need to check the user's membership.
    if (
        not sender_erased
        and _check_history_visibility(event, visibility, is_peeking)
        == _CheckVisibility.ALLOWED
    ):
        return event

    membership_result = _check_membership(user_id, event, visibility, state, is_peeking)
    if not membership_result.allowed:
        filtered_event_logger.debug(
            "_check_client_allowed_to_see_event(event=%s): Filtered out event because the user can't see the event because of their membership, membership_result.allowed=%s membership_result.joined=%s",
            event.event_id,
            membership_result.allowed,
            membership_result.joined,
        )
        return None

    # If the sender has been erased and the user was not joined at the time, we
    # must only return the redacted form.
    if sender_erased and not membership_result.joined:
        filtered_event_logger.debug(
            "_check_client_allowed_to_see_event(event=%s): Returning pruned event because `sender_erased` and the user was not joined at the time",
            event.event_id,
        )
        event = prune_event(event)

    return event


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _CheckMembershipReturn:
    """Return value of `_check_membership`.

    Attributes:
        allowed: Whether the user should be allowed to see the event.
        joined: Whether the user was joined to the room at the event.
    """

    allowed: bool
    joined: bool


def _check_membership(
    user_id: str,
    event: EventBase,
    visibility: str,
    state: StateMap[EventBase],
    is_peeking: bool,
) -> _CheckMembershipReturn:
    """Check whether the user can see the event due to their membership"""
    # If the event is the user's own membership event, use the 'most joined'
    # membership
    membership = None
    if event.type == EventTypes.Member and event.state_key == user_id:
        membership = event.content.get("membership", None)
        if membership not in MEMBERSHIP_PRIORITY:
            membership = "leave"

        prev_content = event.unsigned.get("prev_content", {})
        prev_membership = prev_content.get("membership", None)
        if prev_membership not in MEMBERSHIP_PRIORITY:
            prev_membership = "leave"

        # Always allow the user to see their own leave events, otherwise
        # they won't see the room disappear if they reject the invite
        #
        # (Note this doesn't work for out-of-band invite rejections, which don't
        # have prev_state populated. They are handled above in the outlier code.)
        if membership == "leave" and (
            prev_membership == "join" or prev_membership == "invite"
        ):
            return _CheckMembershipReturn(True, False)

        new_priority = MEMBERSHIP_PRIORITY.index(membership)
        old_priority = MEMBERSHIP_PRIORITY.index(prev_membership)
        if old_priority < new_priority:
            membership = prev_membership

    # otherwise, get the user's membership at the time of the event.
    if membership is None:
        membership_event = state.get((EventTypes.Member, user_id), None)
        if membership_event:
            membership = membership_event.membership

    # if the user was a member of the room at the time of the event,
    # they can see it.
    if membership == Membership.JOIN:
        return _CheckMembershipReturn(True, True)

    # otherwise, it depends on the room visibility.

    if visibility == HistoryVisibility.JOINED:
        # we weren't a member at the time of the event, so we can't
        # see this event.
        return _CheckMembershipReturn(False, False)

    elif visibility == HistoryVisibility.INVITED:
        # user can also see the event if they were *invited* at the time
        # of the event.
        return _CheckMembershipReturn(membership == Membership.INVITE, False)

    elif visibility == HistoryVisibility.SHARED and is_peeking:
        # if the visibility is shared, users cannot see the event unless
        # they have *subsequently* joined the room (or were members at the
        # time, of course)
        #
        # XXX: if the user has subsequently joined and then left again,
        # ideally we would share history up to the point they left. But
        # we don't know when they left. We just treat it as though they
        # never joined, and restrict access.
        return _CheckMembershipReturn(False, False)

    # The visibility is either shared or world_readable, and the user was
    # not a member at the time. We allow it.
    return _CheckMembershipReturn(True, False)


class _CheckFilter(Enum):
    MAYBE_ALLOWED = auto()
    DENIED = auto()


def _check_filter_send_to_client(
    event: EventBase,
    clock: Clock,
    retention_policy: RetentionPolicy,
    sender_ignored: bool,
) -> _CheckFilter:
    """Apply checks for sending events to client

    Returns:
        True if might be allowed to be sent to clients, False if definitely not.
    """

    if event.type == EventTypes.Dummy:
        return _CheckFilter.DENIED

    if not event.is_state() and sender_ignored:
        return _CheckFilter.DENIED

    # Until MSC2261 has landed we can't redact malicious alias events, so for
    # now we temporarily filter out m.room.aliases entirely to mitigate
    # abuse, while we spec a better solution to advertising aliases
    # on rooms.
    if event.type == EventTypes.Aliases:
        return _CheckFilter.DENIED

    # Don't try to apply the room's retention policy if the event is a state
    # event, as MSC1763 states that retention is only considered for non-state
    # events.
    if not event.is_state():
        max_lifetime = retention_policy.max_lifetime

        if max_lifetime is not None:
            oldest_allowed_ts = clock.time_msec() - max_lifetime

            if event.origin_server_ts < oldest_allowed_ts:
                return _CheckFilter.DENIED

    return _CheckFilter.MAYBE_ALLOWED


class _CheckVisibility(Enum):
    ALLOWED = auto()
    MAYBE_DENIED = auto()


def _check_history_visibility(
    event: EventBase, visibility: str, is_peeking: bool
) -> _CheckVisibility:
    """Check if event is allowed to be seen due to lax history visibility.

    Returns:
        True if user can definitely see the event, False if maybe not.
    """
    # Always allow history visibility events on boundaries. This is done
    # by setting the effective visibility to the least restrictive
    # of the old vs new.
    if event.type == EventTypes.RoomHistoryVisibility:
        prev_content = event.unsigned.get("prev_content", {})
        prev_visibility = prev_content.get("history_visibility", None)

        if prev_visibility not in VISIBILITY_PRIORITY:
            prev_visibility = HistoryVisibility.SHARED

        new_priority = VISIBILITY_PRIORITY.index(visibility)
        old_priority = VISIBILITY_PRIORITY.index(prev_visibility)
        if old_priority < new_priority:
            visibility = prev_visibility

    if visibility == HistoryVisibility.SHARED and not is_peeking:
        return _CheckVisibility.ALLOWED
    elif visibility == HistoryVisibility.WORLD_READABLE:
        return _CheckVisibility.ALLOWED

    return _CheckVisibility.MAYBE_DENIED


def get_effective_room_visibility_from_state(state: StateMap[EventBase]) -> str:
    """Get the actual history vis, from a state map including the history_visibility event
    Handles missing and invalid history visibility events.
    """
    visibility_event = state.get(_HISTORY_VIS_KEY, None)
    if not visibility_event:
        return HistoryVisibility.SHARED

    visibility = visibility_event.content.get(
        "history_visibility", HistoryVisibility.SHARED
    )
    if visibility not in VISIBILITY_PRIORITY:
        visibility = HistoryVisibility.SHARED
    return visibility


async def filter_events_for_server(
    storage: StorageControllers,
    target_server_name: str,
    local_server_name: str,
    events: Sequence[EventBase],
    *,
    redact: bool,
    filter_out_erased_senders: bool,
    filter_out_remote_partial_state_events: bool,
) -> List[EventBase]:
    """Filter a list of events based on whether the target server is allowed to
    see them.

    For a fully stated room, the target server is allowed to see an event E if:
      - the state at E has world readable or shared history vis, OR
      - the state at E says that the target server is in the room.

    For a partially stated room, the target server is allowed to see E if:
      - E was created by this homeserver, AND:
          - the partial state at E has world readable or shared history vis, OR
          - the partial state at E says that the target server is in the room.

    TODO: state before or state after?

    Args:
        storage
        target_server_name
        local_server_name
        events
        redact: Controls what to do with events which have been filtered out.
            If True, include their redacted forms; if False, omit them entirely.
        filter_out_erased_senders: If true, also filter out events whose sender has been
            erased. This is used e.g. during pagination to decide whether to
            backfill or not.
        filter_out_remote_partial_state_events: If True, also filter out events in
            partial state rooms created by other homeservers.
    Returns
        The filtered events.
    """

    def is_sender_erased(event: EventBase, erased_senders: Mapping[str, bool]) -> bool:
        if erased_senders and erased_senders[event.sender]:
            logger.info("Sender of %s has been erased, redacting", event.event_id)
            return True
        return False

    def check_event_is_visible(
        visibility: str, memberships: StateMap[EventBase]
    ) -> bool:
        if visibility not in (HistoryVisibility.INVITED, HistoryVisibility.JOINED):
            return True

        # We now loop through all membership events looking for
        # membership states for the requesting server to determine
        # if the server is either in the room or has been invited
        # into the room.
        for ev in memberships.values():
            assert get_domain_from_id(ev.state_key) == target_server_name

            memtype = ev.membership
            if memtype == Membership.JOIN:
                return True
            elif memtype == Membership.INVITE:
                if visibility == HistoryVisibility.INVITED:
                    return True

        # server has no users in the room: redact
        return False

    if filter_out_erased_senders:
        erased_senders = await storage.main.are_users_erased(e.sender for e in events)
    else:
        # We don't want to check whether users are erased, which is equivalent
        # to no users having been erased.
        erased_senders = {}

    # Filter out non-local events when we are in the middle of a partial join, since our servers
    # list can be out of date and we could leak events to servers not in the room anymore.
    # This can also be true for local events but we consider it to be an acceptable risk.

    # We do this check as a first step and before retrieving membership events because
    # otherwise a room could be fully joined after we retrieve those, which would then bypass
    # this check but would base the filtering on an outdated view of the membership events.

    partial_state_invisible_event_ids: Set[str] = set()
    if filter_out_remote_partial_state_events:
        for e in events:
            sender_domain = get_domain_from_id(e.sender)
            if (
                sender_domain != local_server_name
                and await storage.main.is_partial_state_room(e.room_id)
            ):
                partial_state_invisible_event_ids.add(e.event_id)

    # Let's check to see if all the events have a history visibility
    # of "shared" or "world_readable". If that's the case then we don't
    # need to check membership (as we know the server is in the room).
    event_to_history_vis = await _event_to_history_vis(storage, events)

    # for any with restricted vis, we also need the memberships
    event_to_memberships = await _event_to_memberships(
        storage,
        [
            e
            for e in events
            if event_to_history_vis[e.event_id]
            not in (HistoryVisibility.SHARED, HistoryVisibility.WORLD_READABLE)
        ],
        target_server_name,
    )

    def include_event_in_output(e: EventBase) -> bool:
        erased = is_sender_erased(e, erased_senders)
        visible = check_event_is_visible(
            event_to_history_vis[e.event_id], event_to_memberships.get(e.event_id, {})
        )

        if e.event_id in partial_state_invisible_event_ids:
            visible = False

        return visible and not erased

    to_return = []
    for e in events:
        if include_event_in_output(e):
            to_return.append(e)
        elif redact:
            to_return.append(prune_event(e))

    return to_return


async def _event_to_history_vis(
    storage: StorageControllers, events: Collection[EventBase]
) -> Dict[str, str]:
    """Get the history visibility at each of the given events

    Returns a map from event id to history_visibility setting
    """

    # outliers get special treatment here. We don't have the state at that point in the
    # room (and attempting to look it up will raise an exception), so all we can really
    # do is assume that the requesting server is allowed to see the event. That's
    # equivalent to there not being a history_visibility event, so we just exclude
    # any outliers from the query.
    event_to_state_ids = await storage.state.get_state_ids_for_events(
        frozenset(e.event_id for e in events if not e.internal_metadata.is_outlier()),
        state_filter=StateFilter.from_types(types=(_HISTORY_VIS_KEY,)),
    )

    visibility_ids = {
        vis_event_id
        for vis_event_id in (
            state_ids.get(_HISTORY_VIS_KEY) for state_ids in event_to_state_ids.values()
        )
        if vis_event_id
    }
    vis_events = await storage.main.get_events(visibility_ids)

    result: Dict[str, str] = {}
    for event in events:
        vis = HistoryVisibility.SHARED
        state_ids = event_to_state_ids.get(event.event_id)

        # if we didn't find any state for this event, it's an outlier, and we assume
        # it's open
        visibility_id = None
        if state_ids:
            visibility_id = state_ids.get(_HISTORY_VIS_KEY)

        if visibility_id:
            vis_event = vis_events[visibility_id]
            vis = vis_event.content.get("history_visibility", HistoryVisibility.SHARED)
            assert isinstance(vis, str)

        result[event.event_id] = vis
    return result


async def _event_to_memberships(
    storage: StorageControllers, events: Collection[EventBase], server_name: str
) -> Dict[str, StateMap[EventBase]]:
    """Get the remote membership list at each of the given events

    Returns a map from event id to state map, which will contain only membership events
    for the given server.
    """

    if not events:
        return {}

    # for each event, get the event_ids of the membership state at those events.
    #
    # TODO: this means that we request the entire membership list. If there  are only
    #   one or two users on this server, and the room is huge, this is very wasteful
    #   (it means more db work, and churns the *stateGroupMembersCache*).
    #   It might be that we could extend StateFilter to specify "give me keys matching
    #   *:<server_name>", to avoid this.

    event_to_state_ids = await storage.state.get_state_ids_for_events(
        frozenset(e.event_id for e in events),
        state_filter=StateFilter.from_types(types=((EventTypes.Member, None),)),
    )

    # We only want to pull out member events that correspond to the
    # server's domain.
    #
    # event_to_state_ids contains lots of duplicates, so it turns out to be
    # cheaper to build a complete event_id => (type, state_key) dict, and then
    # filter out the ones we don't want
    #
    event_id_to_state_key = {
        event_id: key
        for key_to_eid in event_to_state_ids.values()
        for key, event_id in key_to_eid.items()
    }

    def include(state_key: str) -> bool:
        # we avoid using get_domain_from_id here for efficiency.
        idx = state_key.find(":")
        if idx == -1:
            return False
        return state_key[idx + 1 :] == server_name

    event_map = await storage.main.get_events(
        [
            e_id
            for e_id, (_, state_key) in event_id_to_state_key.items()
            if include(state_key)
        ]
    )

    return {
        e_id: {
            key: event_map[inner_e_id]
            for key, inner_e_id in key_to_eid.items()
            if inner_e_id in event_map
        }
        for e_id, key_to_eid in event_to_state_ids.items()
    }
