#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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
from typing import TYPE_CHECKING, Any, Dict, Final, List, Optional, Set, Tuple

import attr
from immutabledict import immutabledict

from synapse.api.constants import (
    AccountDataTypes,
    Direction,
    EventContentFields,
    EventTypes,
    Membership,
)
from synapse.events import EventBase
from synapse.events.utils import strip_event
from synapse.handlers.relations import BundledAggregations
from synapse.logging.opentracing import trace
from synapse.storage.databases.main.stream import CurrentStateDeltaMembership
from synapse.types import (
    JsonDict,
    PersistedEventPosition,
    Requester,
    RoomStreamToken,
    StateMap,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.types.handlers import OperationType, SlidingSyncConfig, SlidingSyncResult
from synapse.types.state import StateFilter
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# The event types that clients should consider as new activity.
DEFAULT_BUMP_EVENT_TYPES = {
    EventTypes.Message,
    EventTypes.Encrypted,
    EventTypes.Sticker,
    EventTypes.CallInvite,
    EventTypes.PollStart,
    EventTypes.LiveLocationShareStart,
}


def filter_membership_for_sync(
    *, membership: str, user_id: str, sender: Optional[str]
) -> bool:
    """
    Returns True if the membership event should be included in the sync response,
    otherwise False.

    Attributes:
        membership: The membership state of the user in the room.
        user_id: The user ID that the membership applies to
        sender: The person who sent the membership event
    """

    # Everything except `Membership.LEAVE` because we want everything that's *still*
    # relevant to the user. There are few more things to include in the sync response
    # (newly_left) but those are handled separately.
    #
    # This logic includes kicks (leave events where the sender is not the same user) and
    # can be read as "anything that isn't a leave or a leave with a different sender".
    #
    # When `sender=None` and `membership=Membership.LEAVE`, it means that a state reset
    # happened that removed the user from the room, or the user was the last person
    # locally to leave the room which caused the server to leave the room. In both
    # cases, we can just remove the rooms since they are no longer relevant to the user.
    # They could still be added back later if they are `newly_left`.
    return membership != Membership.LEAVE or sender not in (user_id, None)


# We can't freeze this class because we want to update it in place with the
# de-duplicated data.
@attr.s(slots=True, auto_attribs=True)
class RoomSyncConfig:
    """
    Holds the config for what data we should fetch for a room in the sync response.

    Attributes:
        timeline_limit: The maximum number of events to return in the timeline.

        required_state_map: Map from state event type to state_keys requested for the
            room. The values are close to `StateKey` but actually use a syntax where you
            can provide `*` wildcard and `$LAZY` for lazy-loading room members.
    """

    timeline_limit: int
    required_state_map: Dict[str, Set[str]]

    @classmethod
    def from_room_config(
        cls,
        room_params: SlidingSyncConfig.CommonRoomParameters,
    ) -> "RoomSyncConfig":
        """
        Create a `RoomSyncConfig` from a `SlidingSyncList`/`RoomSubscription` config.

        Args:
            room_params: `SlidingSyncConfig.SlidingSyncList` or `SlidingSyncConfig.RoomSubscription`
        """
        required_state_map: Dict[str, Set[str]] = {}
        for (
            state_type,
            state_key,
        ) in room_params.required_state:
            # If we already have a wildcard for this specific `state_key`, we don't need
            # to add it since the wildcard already covers it.
            if state_key in required_state_map.get(StateValues.WILDCARD, set()):
                continue

            # If we already have a wildcard `state_key` for this `state_type`, we don't need
            # to add anything else
            if StateValues.WILDCARD in required_state_map.get(state_type, set()):
                continue

            # If we're getting wildcards for the `state_type` and `state_key`, that's
            # all that matters so get rid of any other entries
            if state_type == StateValues.WILDCARD and state_key == StateValues.WILDCARD:
                required_state_map = {StateValues.WILDCARD: {StateValues.WILDCARD}}
                # We can break, since we don't need to add anything else
                break

            # If we're getting a wildcard for the `state_type`, get rid of any other
            # entries with the same `state_key`, since the wildcard will cover it already.
            elif state_type == StateValues.WILDCARD:
                # Get rid of any entries that match the `state_key`
                #
                # Make a copy so we don't run into an error: `dictionary changed size
                # during iteration`, when we remove items
                for (
                    existing_state_type,
                    existing_state_key_set,
                ) in list(required_state_map.items()):
                    # Make a copy so we don't run into an error: `Set changed size during
                    # iteration`, when we filter out and remove items
                    for existing_state_key in existing_state_key_set.copy():
                        if existing_state_key == state_key:
                            existing_state_key_set.remove(state_key)

                    # If we've the left the `set()` empty, remove it from the map
                    if existing_state_key_set == set():
                        required_state_map.pop(existing_state_type, None)

            # If we're getting a wildcard `state_key`, get rid of any other state_keys
            # for this `state_type` since the wildcard will cover it already.
            if state_key == StateValues.WILDCARD:
                required_state_map[state_type] = {state_key}
            # Otherwise, just add it to the set
            else:
                if required_state_map.get(state_type) is None:
                    required_state_map[state_type] = {state_key}
                else:
                    required_state_map[state_type].add(state_key)

        return cls(
            timeline_limit=room_params.timeline_limit,
            required_state_map=required_state_map,
        )

    def deep_copy(self) -> "RoomSyncConfig":
        required_state_map: Dict[str, Set[str]] = {
            state_type: state_key_set.copy()
            for state_type, state_key_set in self.required_state_map.items()
        }

        return RoomSyncConfig(
            timeline_limit=self.timeline_limit,
            required_state_map=required_state_map,
        )

    def combine_room_sync_config(
        self, other_room_sync_config: "RoomSyncConfig"
    ) -> None:
        """
        Combine this `RoomSyncConfig` with another `RoomSyncConfig` and take the
        superset union of the two.
        """
        # Take the highest timeline limit
        if self.timeline_limit < other_room_sync_config.timeline_limit:
            self.timeline_limit = other_room_sync_config.timeline_limit

        # Union the required state
        for (
            state_type,
            state_key_set,
        ) in other_room_sync_config.required_state_map.items():
            # If we already have a wildcard for everything, we don't need to add
            # anything else
            if StateValues.WILDCARD in self.required_state_map.get(
                StateValues.WILDCARD, set()
            ):
                break

            # If we already have a wildcard `state_key` for this `state_type`, we don't need
            # to add anything else
            if StateValues.WILDCARD in self.required_state_map.get(state_type, set()):
                continue

            # If we're getting wildcards for the `state_type` and `state_key`, that's
            # all that matters so get rid of any other entries
            if (
                state_type == StateValues.WILDCARD
                and StateValues.WILDCARD in state_key_set
            ):
                self.required_state_map = {state_type: {StateValues.WILDCARD}}
                # We can break, since we don't need to add anything else
                break

            for state_key in state_key_set:
                # If we already have a wildcard for this specific `state_key`, we don't need
                # to add it since the wildcard already covers it.
                if state_key in self.required_state_map.get(
                    StateValues.WILDCARD, set()
                ):
                    continue

                # If we're getting a wildcard for the `state_type`, get rid of any other
                # entries with the same `state_key`, since the wildcard will cover it already.
                if state_type == StateValues.WILDCARD:
                    # Get rid of any entries that match the `state_key`
                    #
                    # Make a copy so we don't run into an error: `dictionary changed size
                    # during iteration`, when we remove items
                    for existing_state_type, existing_state_key_set in list(
                        self.required_state_map.items()
                    ):
                        # Make a copy so we don't run into an error: `Set changed size during
                        # iteration`, when we filter out and remove items
                        for existing_state_key in existing_state_key_set.copy():
                            if existing_state_key == state_key:
                                existing_state_key_set.remove(state_key)

                        # If we've the left the `set()` empty, remove it from the map
                        if existing_state_key_set == set():
                            self.required_state_map.pop(existing_state_type, None)

                # If we're getting a wildcard `state_key`, get rid of any other state_keys
                # for this `state_type` since the wildcard will cover it already.
                if state_key == StateValues.WILDCARD:
                    self.required_state_map[state_type] = {state_key}
                    break
                # Otherwise, just add it to the set
                else:
                    if self.required_state_map.get(state_type) is None:
                        self.required_state_map[state_type] = {state_key}
                    else:
                        self.required_state_map[state_type].add(state_key)


class StateValues:
    """
    Understood values of the (type, state_key) tuple in `required_state`.
    """

    # Include all state events of the given type
    WILDCARD: Final = "*"
    # Lazy-load room membership events (include room membership events for any event
    # `sender` in the timeline). We only give special meaning to this value when it's a
    # `state_key`.
    LAZY: Final = "$LAZY"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _RoomMembershipForUser:
    """
    Attributes:
        event_id: The event ID of the membership event
        event_pos: The stream position of the membership event
        membership: The membership state of the user in the room
        sender: The person who sent the membership event
        newly_joined: Whether the user newly joined the room during the given token
            range
    """

    room_id: str
    event_id: Optional[str]
    event_pos: PersistedEventPosition
    membership: str
    sender: Optional[str]
    newly_joined: bool

    def copy_and_replace(self, **kwds: Any) -> "_RoomMembershipForUser":
        return attr.evolve(self, **kwds)


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.auth_blocking = hs.get_auth_blocking()
        self.notifier = hs.get_notifier()
        self.event_sources = hs.get_event_sources()
        self.relations_handler = hs.get_relations_handler()
        self.rooms_to_exclude_globally = hs.config.server.rooms_to_exclude_from_sync

    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SlidingSyncConfig,
        from_token: Optional[StreamToken] = None,
        timeout_ms: int = 0,
    ) -> SlidingSyncResult:
        """
        Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.

        Args:
            requester: The user making the request
            sync_config: Sync configuration
            from_token: The point in the stream to sync from. Token of the end of the
                previous batch. May be `None` if this is the initial sync request.
            timeout_ms: The time in milliseconds to wait for new data to arrive. If 0,
                we will immediately but there might not be any new data so we just return an
                empty response.
        """
        # If the user is not part of the mau group, then check that limits have
        # not been exceeded (if not part of the group by this point, almost certain
        # auth_blocking will occur)
        await self.auth_blocking.check_auth_blocking(requester=requester)

        # TODO: If the To-Device extension is enabled and we have a `from_token`, delete
        # any to-device messages before that token (since we now know that the device
        # has received them). (see sync v2 for how to do this)

        # If we're working with a user-provided token, we need to make sure to wait for
        # this worker to catch up with the token so we don't skip past any incoming
        # events or future events if the user is nefariously, manually modifying the
        # token.
        if from_token is not None:
            # We need to make sure this worker has caught up with the token. If
            # this returns false, it means we timed out waiting, and we should
            # just return an empty response.
            before_wait_ts = self.clock.time_msec()
            if not await self.notifier.wait_for_stream_token(from_token):
                logger.warning(
                    "Timed out waiting for worker to catch up. Returning empty response"
                )
                return SlidingSyncResult.empty(from_token)

            # If we've spent significant time waiting to catch up, take it off
            # the timeout.
            after_wait_ts = self.clock.time_msec()
            if after_wait_ts - before_wait_ts > 1_000:
                timeout_ms -= after_wait_ts - before_wait_ts
                timeout_ms = max(timeout_ms, 0)

        # We're going to respond immediately if the timeout is 0 or if this is an
        # initial sync (without a `from_token`) so we can avoid calling
        # `notifier.wait_for_events()`.
        if timeout_ms == 0 or from_token is None:
            now_token = self.event_sources.get_current_token()
            result = await self.current_sync_for_user(
                sync_config,
                from_token=from_token,
                to_token=now_token,
            )
        else:
            # Otherwise, we wait for something to happen and report it to the user.
            async def current_sync_callback(
                before_token: StreamToken, after_token: StreamToken
            ) -> SlidingSyncResult:
                return await self.current_sync_for_user(
                    sync_config,
                    from_token=from_token,
                    to_token=after_token,
                )

            result = await self.notifier.wait_for_events(
                sync_config.user.to_string(),
                timeout_ms,
                current_sync_callback,
                from_token=from_token,
            )

        return result

    async def current_sync_for_user(
        self,
        sync_config: SlidingSyncConfig,
        to_token: StreamToken,
        from_token: Optional[StreamToken] = None,
    ) -> SlidingSyncResult:
        """
        Generates the response body of a Sliding Sync result, represented as a
        `SlidingSyncResult`.

        We fetch data according to the token range (> `from_token` and <= `to_token`).

        Args:
            sync_config: Sync configuration
            to_token: The point in the stream to sync up to.
            from_token: The point in the stream to sync from. Token of the end of the
                previous batch. May be `None` if this is the initial sync request.
        """
        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        # Keep track of the rooms that we're going to display and need to fetch more
        # info about
        relevant_room_map: Dict[str, RoomSyncConfig] = {}
        if sync_config.lists:
            # Get all of the room IDs that the user should be able to see in the sync
            # response
            sync_room_map = await self.get_sync_room_ids_for_user(
                sync_config.user,
                from_token=from_token,
                to_token=to_token,
            )

            for list_key, list_config in sync_config.lists.items():
                # Apply filters
                filtered_sync_room_map = sync_room_map
                if list_config.filters is not None:
                    filtered_sync_room_map = await self.filter_rooms(
                        sync_config.user, sync_room_map, list_config.filters, to_token
                    )

                # Sort the list
                sorted_room_info = await self.sort_rooms(
                    filtered_sync_room_map, to_token
                )

                # Find which rooms are partially stated and may need to be filtered out
                # depending on the `required_state` requested (see below).
                partial_state_room_map = await self.store.is_partial_state_room_batched(
                    filtered_sync_room_map.keys()
                )

                # Since creating the `RoomSyncConfig` takes some work, let's just do it
                # once and make a copy whenever we need it.
                room_sync_config = RoomSyncConfig.from_room_config(list_config)
                membership_state_keys = room_sync_config.required_state_map.get(
                    EventTypes.Member
                )
                # Also see `StateFilter.must_await_full_state(...)` for comparison
                lazy_loading = (
                    membership_state_keys is not None
                    and len(membership_state_keys) == 1
                    and StateValues.LAZY in membership_state_keys
                )

                ops: List[SlidingSyncResult.SlidingWindowList.Operation] = []
                if list_config.ranges:
                    for range in list_config.ranges:
                        room_ids_in_list: List[str] = []

                        # We're going to loop through the sorted list of rooms starting
                        # at the range start index and keep adding rooms until we fill
                        # up the range or run out of rooms.
                        #
                        # Both sides of range are inclusive so we `+ 1`
                        max_num_rooms = range[1] - range[0] + 1
                        for room_membership in sorted_room_info[range[0] :]:
                            room_id = room_membership.room_id

                            if len(room_ids_in_list) >= max_num_rooms:
                                break

                            # Exclude partially-stated rooms unless the `required_state`
                            # only has `["m.room.member", "$LAZY"]` for membership
                            # (lazy-loading room members).
                            if partial_state_room_map.get(room_id) and not lazy_loading:
                                continue

                            # Take the superset of the `RoomSyncConfig` for each room.
                            #
                            # Update our `relevant_room_map` with the room we're going
                            # to display and need to fetch more info about.
                            existing_room_sync_config = relevant_room_map.get(room_id)
                            if existing_room_sync_config is not None:
                                existing_room_sync_config.combine_room_sync_config(
                                    room_sync_config
                                )
                            else:
                                # Make a copy so if we modify it later, it doesn't
                                # affect all references.
                                relevant_room_map[room_id] = (
                                    room_sync_config.deep_copy()
                                )

                            room_ids_in_list.append(room_id)

                        ops.append(
                            SlidingSyncResult.SlidingWindowList.Operation(
                                op=OperationType.SYNC,
                                range=range,
                                room_ids=room_ids_in_list,
                            )
                        )

                lists[list_key] = SlidingSyncResult.SlidingWindowList(
                    count=len(sorted_room_info),
                    ops=ops,
                )

        # TODO: if (sync_config.room_subscriptions):

        # Fetch room data
        rooms: Dict[str, SlidingSyncResult.RoomResult] = {}
        for room_id, room_sync_config in relevant_room_map.items():
            room_sync_result = await self.get_room_sync_data(
                user=sync_config.user,
                room_id=room_id,
                room_sync_config=room_sync_config,
                room_membership_for_user_at_to_token=sync_room_map[room_id],
                from_token=from_token,
                to_token=to_token,
            )

            rooms[room_id] = room_sync_result

        extensions = await self.get_extensions_response(
            sync_config=sync_config, to_token=to_token
        )

        return SlidingSyncResult(
            next_pos=to_token,
            lists=lists,
            rooms=rooms,
            extensions=extensions,
        )

    async def get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken] = None,
    ) -> Dict[str, _RoomMembershipForUser]:
        """
        Fetch room IDs that should be listed for this user in the sync response (the
        full room list that will be filtered, sorted, and sliced).

        We're looking for rooms where the user has the following state in the token
        range (> `from_token` and <= `to_token`):

        - `invite`, `join`, `knock`, `ban` membership events
        - Kicks (`leave` membership events where `sender` is different from the
          `user_id`/`state_key`)
        - `newly_left` (rooms that were left during the given token range)
        - In order for bans/kicks to not show up in sync, you need to `/forget` those
          rooms. This doesn't modify the event itself though and only adds the
          `forgotten` flag to the `room_memberships` table in Synapse. There isn't a way
          to tell when a room was forgotten at the moment so we can't factor it into the
          from/to range.

        Args:
            user: User to fetch rooms for
            to_token: The token to fetch rooms up to.
            from_token: The point in the stream to sync from.

        Returns:
            A dictionary of room IDs that should be listed in the sync response along
            with membership information in that room at the time of `to_token`.
        """
        user_id = user.to_string()

        # First grab a current snapshot rooms for the user
        # (also handles forgotten rooms)
        room_for_user_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            # We want to fetch any kind of membership (joined and left rooms) in order
            # to get the `event_pos` of the latest room membership event for the
            # user.
            #
            # We will filter out the rooms that don't belong below (see
            # `filter_membership_for_sync`)
            membership_list=Membership.LIST,
            excluded_rooms=self.rooms_to_exclude_globally,
        )

        # If the user has never joined any rooms before, we can just return an empty list
        if not room_for_user_list:
            return {}

        # Our working list of rooms that can show up in the sync response
        sync_room_id_set = {
            # Note: The `room_for_user` we're assigning here will need to be fixed up
            # (below) because they are potentially from the current snapshot time
            # instead from the time of the `to_token`.
            room_for_user.room_id: _RoomMembershipForUser(
                room_id=room_for_user.room_id,
                event_id=room_for_user.event_id,
                event_pos=room_for_user.event_pos,
                membership=room_for_user.membership,
                sender=room_for_user.sender,
                newly_joined=False,
            )
            for room_for_user in room_for_user_list
        }

        # Get the `RoomStreamToken` that represents the spot we queried up to when we got
        # our membership snapshot from `get_rooms_for_local_user_where_membership_is()`.
        #
        # First, we need to get the max stream_ordering of each event persister instance
        # that we queried events from.
        instance_to_max_stream_ordering_map: Dict[str, int] = {}
        for room_for_user in room_for_user_list:
            instance_name = room_for_user.event_pos.instance_name
            stream_ordering = room_for_user.event_pos.stream

            current_instance_max_stream_ordering = (
                instance_to_max_stream_ordering_map.get(instance_name)
            )
            if (
                current_instance_max_stream_ordering is None
                or stream_ordering > current_instance_max_stream_ordering
            ):
                instance_to_max_stream_ordering_map[instance_name] = stream_ordering

        # Then assemble the `RoomStreamToken`
        membership_snapshot_token = RoomStreamToken(
            # Minimum position in the `instance_map`
            stream=min(instance_to_max_stream_ordering_map.values()),
            instance_map=immutabledict(instance_to_max_stream_ordering_map),
        )

        # Since we fetched the users room list at some point in time after the from/to
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`. In particular, we need to make these fixups:
        #
        # - 1a) Remove rooms that the user joined after the `to_token`
        # - 1b) Add back rooms that the user left after the `to_token`
        # - 1c) Update room membership events to the point in time of the `to_token`
        # - 2) Add back newly_left rooms (> `from_token` and <= `to_token`)
        # - 3) Figure out which rooms are `newly_joined`

        # 1) -----------------------------------------------------

        # 1) Fetch membership changes that fall in the range from `to_token` up to
        # `membership_snapshot_token`
        #
        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we don't need to do any "2)" fix-ups and can just straight-up
        # use the room list from the snapshot as a base (nothing has changed)
        current_state_delta_membership_changes_after_to_token = []
        if not membership_snapshot_token.is_before_or_eq(to_token.room_key):
            current_state_delta_membership_changes_after_to_token = (
                await self.store.get_current_state_delta_membership_changes_for_user(
                    user_id,
                    from_key=to_token.room_key,
                    to_key=membership_snapshot_token,
                    excluded_room_ids=self.rooms_to_exclude_globally,
                )
            )

        # 1) Assemble a list of the first membership event after the `to_token` so we can
        # step backward to the previous membership that would apply to the from/to
        # range.
        first_membership_change_by_room_id_after_to_token: Dict[
            str, CurrentStateDeltaMembership
        ] = {}
        for membership_change in current_state_delta_membership_changes_after_to_token:
            # Only set if we haven't already set it
            first_membership_change_by_room_id_after_to_token.setdefault(
                membership_change.room_id, membership_change
            )

        # 1) Fixup
        #
        # Since we fetched a snapshot of the users room list at some point in time after
        # the from/to tokens, we need to revert/rewind some membership changes to match
        # the point in time of the `to_token`.
        for (
            room_id,
            first_membership_change_after_to_token,
        ) in first_membership_change_by_room_id_after_to_token.items():
            # 1a) Remove rooms that the user joined after the `to_token`
            if first_membership_change_after_to_token.prev_event_id is None:
                sync_room_id_set.pop(room_id, None)
            # 1b) 1c) From the first membership event after the `to_token`, step backward to the
            # previous membership that would apply to the from/to range.
            else:
                # We don't expect these fields to be `None` if we have a `prev_event_id`
                # but we're being defensive since it's possible that the prev event was
                # culled from the database.
                if (
                    first_membership_change_after_to_token.prev_event_pos is not None
                    and first_membership_change_after_to_token.prev_membership
                    is not None
                ):
                    sync_room_id_set[room_id] = _RoomMembershipForUser(
                        room_id=room_id,
                        event_id=first_membership_change_after_to_token.prev_event_id,
                        event_pos=first_membership_change_after_to_token.prev_event_pos,
                        membership=first_membership_change_after_to_token.prev_membership,
                        sender=first_membership_change_after_to_token.prev_sender,
                        newly_joined=False,
                    )
                else:
                    # If we can't find the previous membership event, we shouldn't
                    # include the room in the sync response since we can't determine the
                    # exact membership state and shouldn't rely on the current snapshot.
                    sync_room_id_set.pop(room_id, None)

        # Filter the rooms that that we have updated room membership events to the point
        # in time of the `to_token` (from the "1)" fixups)
        filtered_sync_room_id_set = {
            room_id: room_membership_for_user
            for room_id, room_membership_for_user in sync_room_id_set.items()
            if filter_membership_for_sync(
                membership=room_membership_for_user.membership,
                user_id=user_id,
                sender=room_membership_for_user.sender,
            )
        }

        # 2) -----------------------------------------------------
        # We fix-up newly_left rooms after the first fixup because it may have removed
        # some left rooms that we can figure out are newly_left in the following code

        # 2) Fetch membership changes that fall in the range from `from_token` up to `to_token`
        current_state_delta_membership_changes_in_from_to_range = []
        if from_token:
            current_state_delta_membership_changes_in_from_to_range = (
                await self.store.get_current_state_delta_membership_changes_for_user(
                    user_id,
                    from_key=from_token.room_key,
                    to_key=to_token.room_key,
                    excluded_room_ids=self.rooms_to_exclude_globally,
                )
            )

        # 2) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: Dict[
            str, CurrentStateDeltaMembership
        ] = {}
        # We also want to assemble a list of the first membership events during the token
        # range so we can step backward to the previous membership that would apply to
        # before the token range to see if we have `newly_joined` the room.
        first_membership_change_by_room_id_in_from_to_range: Dict[
            str, CurrentStateDeltaMembership
        ] = {}
        # Keep track if the room has a non-join event in the token range so we can later
        # tell if it was a `newly_joined` room. If the last membership event in the
        # token range is a join and there is also some non-join in the range, we know
        # they `newly_joined`.
        has_non_join_event_by_room_id_in_from_to_range: Dict[str, bool] = {}
        for (
            membership_change
        ) in current_state_delta_membership_changes_in_from_to_range:
            room_id = membership_change.room_id

            last_membership_change_by_room_id_in_from_to_range[room_id] = (
                membership_change
            )
            # Only set if we haven't already set it
            first_membership_change_by_room_id_in_from_to_range.setdefault(
                room_id, membership_change
            )

            if membership_change.membership != Membership.JOIN:
                has_non_join_event_by_room_id_in_from_to_range[room_id] = True

        # 2) Fixup
        #
        # 3) We also want to assemble a list of possibly newly joined rooms. Someone
        # could have left and joined multiple times during the given range but we only
        # care about whether they are joined at the end of the token range so we are
        # working with the last membership even in the token range.
        possibly_newly_joined_room_ids = set()
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            room_id = last_membership_change_in_from_to_range.room_id

            # 3)
            if last_membership_change_in_from_to_range.membership == Membership.JOIN:
                possibly_newly_joined_room_ids.add(room_id)

            # 2) Add back newly_left rooms (> `from_token` and <= `to_token`). We
            # include newly_left rooms because the last event that the user should see
            # is their own leave event
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                filtered_sync_room_id_set[room_id] = _RoomMembershipForUser(
                    room_id=room_id,
                    event_id=last_membership_change_in_from_to_range.event_id,
                    event_pos=last_membership_change_in_from_to_range.event_pos,
                    membership=last_membership_change_in_from_to_range.membership,
                    sender=last_membership_change_in_from_to_range.sender,
                    newly_joined=False,
                )

        # 3) Figure out `newly_joined`
        for room_id in possibly_newly_joined_room_ids:
            has_non_join_in_from_to_range = (
                has_non_join_event_by_room_id_in_from_to_range.get(room_id, False)
            )
            # If the last membership event in the token range is a join and there is
            # also some non-join in the range, we know they `newly_joined`.
            if has_non_join_in_from_to_range:
                # We found a `newly_joined` room (we left and joined within the token range)
                filtered_sync_room_id_set[room_id] = filtered_sync_room_id_set[
                    room_id
                ].copy_and_replace(newly_joined=True)
            else:
                prev_event_id = first_membership_change_by_room_id_in_from_to_range[
                    room_id
                ].prev_event_id
                prev_membership = first_membership_change_by_room_id_in_from_to_range[
                    room_id
                ].prev_membership

                if prev_event_id is None:
                    # We found a `newly_joined` room (we are joining the room for the
                    # first time within the token range)
                    filtered_sync_room_id_set[room_id] = filtered_sync_room_id_set[
                        room_id
                    ].copy_and_replace(newly_joined=True)
                # Last resort, we need to step back to the previous membership event
                # just before the token range to see if we're joined then or not.
                elif prev_membership != Membership.JOIN:
                    # We found a `newly_joined` room (we left before the token range
                    # and joined within the token range)
                    filtered_sync_room_id_set[room_id] = filtered_sync_room_id_set[
                        room_id
                    ].copy_and_replace(newly_joined=True)

        return filtered_sync_room_id_set

    async def filter_rooms(
        self,
        user: UserID,
        sync_room_map: Dict[str, _RoomMembershipForUser],
        filters: SlidingSyncConfig.SlidingSyncList.Filters,
        to_token: StreamToken,
    ) -> Dict[str, _RoomMembershipForUser]:
        """
        Filter rooms based on the sync request.

        Args:
            user: User to filter rooms for
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            filters: Filters to apply
            to_token: We filter based on the state of the room at this token

        Returns:
            A filtered dictionary of room IDs along with membership information in the
            room at the time of `to_token`.
        """
        user_id = user.to_string()

        # TODO: Apply filters

        filtered_room_id_set = set(sync_room_map.keys())

        # Filter for Direct-Message (DM) rooms
        if filters.is_dm is not None:
            # We're using global account data (`m.direct`) instead of checking for
            # `is_direct` on membership events because that property only appears for
            # the invitee membership event (doesn't show up for the inviter). Account
            # data is set by the client so it needs to be scrutinized.
            #
            # We're unable to take `to_token` into account for global account data since
            # we only keep track of the latest account data for the user.
            dm_map = await self.store.get_global_account_data_by_type_for_user(
                user_id, AccountDataTypes.DIRECT
            )

            # Flatten out the map
            dm_room_id_set = set()
            if isinstance(dm_map, dict):
                for room_ids in dm_map.values():
                    # Account data should be a list of room IDs. Ignore anything else
                    if isinstance(room_ids, list):
                        for room_id in room_ids:
                            if isinstance(room_id, str):
                                dm_room_id_set.add(room_id)

            if filters.is_dm:
                # Only DM rooms please
                filtered_room_id_set = filtered_room_id_set.intersection(dm_room_id_set)
            else:
                # Only non-DM rooms please
                filtered_room_id_set = filtered_room_id_set.difference(dm_room_id_set)

        if filters.spaces:
            raise NotImplementedError()

        # Filter for encrypted rooms
        if filters.is_encrypted is not None:
            # Make a copy so we don't run into an error: `Set changed size during
            # iteration`, when we filter out and remove items
            for room_id in filtered_room_id_set.copy():
                state_at_to_token = await self.storage_controllers.state.get_state_at(
                    room_id,
                    to_token,
                    state_filter=StateFilter.from_types(
                        [(EventTypes.RoomEncryption, "")]
                    ),
                    # Partially-stated rooms should have all state events except for the
                    # membership events so we don't need to wait because we only care
                    # about retrieving the `EventTypes.RoomEncryption` state event here.
                    # Plus we don't want to block the whole sync waiting for this one
                    # room.
                    await_full_state=False,
                )
                is_encrypted = state_at_to_token.get((EventTypes.RoomEncryption, ""))

                # If we're looking for encrypted rooms, filter out rooms that are not
                # encrypted and vice versa
                if (filters.is_encrypted and not is_encrypted) or (
                    not filters.is_encrypted and is_encrypted
                ):
                    filtered_room_id_set.remove(room_id)

        # Filter for rooms that the user has been invited to
        if filters.is_invite is not None:
            # Make a copy so we don't run into an error: `Set changed size during
            # iteration`, when we filter out and remove items
            for room_id in filtered_room_id_set.copy():
                room_for_user = sync_room_map[room_id]
                # If we're looking for invite rooms, filter out rooms that the user is
                # not invited to and vice versa
                if (
                    filters.is_invite and room_for_user.membership != Membership.INVITE
                ) or (
                    not filters.is_invite
                    and room_for_user.membership == Membership.INVITE
                ):
                    filtered_room_id_set.remove(room_id)

        # Filter by room type (space vs room, etc). A room must match one of the types
        # provided in the list. `None` is a valid type for rooms which do not have a
        # room type.
        if filters.room_types is not None or filters.not_room_types is not None:
            # Make a copy so we don't run into an error: `Set changed size during
            # iteration`, when we filter out and remove items
            for room_id in filtered_room_id_set.copy():
                create_event = await self.store.get_create_event_for_room(room_id)
                room_type = create_event.content.get(EventContentFields.ROOM_TYPE)
                if (
                    filters.room_types is not None
                    and room_type not in filters.room_types
                ):
                    filtered_room_id_set.remove(room_id)

                if (
                    filters.not_room_types is not None
                    and room_type in filters.not_room_types
                ):
                    filtered_room_id_set.remove(room_id)

        if filters.room_name_like:
            raise NotImplementedError()

        if filters.tags:
            raise NotImplementedError()

        if filters.not_tags:
            raise NotImplementedError()

        # Assemble a new sync room map but only with the `filtered_room_id_set`
        return {room_id: sync_room_map[room_id] for room_id in filtered_room_id_set}

    @trace
    async def sort_rooms(
        self,
        sync_room_map: Dict[str, _RoomMembershipForUser],
        to_token: StreamToken,
    ) -> List[_RoomMembershipForUser]:
        """
        Sort by `stream_ordering` of the last event that the user should see in the
        room. `stream_ordering` is unique so we get a stable sort.

        Args:
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            to_token: We sort based on the events in the room at this token (<= `to_token`)

        Returns:
            A sorted list of room IDs by `stream_ordering` along with membership information.
        """

        # Assemble a map of room ID to the `stream_ordering` of the last activity that the
        # user should see in the room (<= `to_token`)
        last_activity_in_room_map: Dict[str, int] = {}
        to_fetch = []
        for room_id, room_for_user in sync_room_map.items():
            # If they are fully-joined to the room, let's find the latest activity
            # at/before the `to_token`.
            if room_for_user.membership == Membership.JOIN:
                stream = self.store._events_stream_cache._entity_to_key.get(room_id)
                if stream is not None:
                    if stream <= to_token.room_key.stream:
                        last_activity_in_room_map[room_id] = stream
                        continue

                to_fetch.append(room_id)
            else:
                # Otherwise, if the user has left/been invited/knocked/been banned from
                # a room, they shouldn't see anything past that point.
                #
                # FIXME: It's possible that people should see beyond this point in
                # invited/knocked cases if for example the room has
                # `invite`/`world_readable` history visibility, see
                # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
                last_activity_in_room_map[room_id] = room_for_user.event_pos.stream

        ordering_map = await self.store.get_max_stream_ordering_in_rooms(to_fetch)
        for room_id, stream_pos in ordering_map.items():
            if stream_pos is None:
                continue

            if stream_pos.persisted_after(to_token.room_key):
                continue

            last_activity_in_room_map[room_id] = stream_pos.stream

        for room_id in sync_room_map.keys() - last_activity_in_room_map.keys():
            # TODO: Handle better
            last_activity_in_room_map[room_id] = sync_room_map[room_id].event_pos.stream

        return sorted(
            sync_room_map.values(),
            # Sort by the last activity (stream_ordering) in the room
            key=lambda room_info: last_activity_in_room_map[room_info.room_id],
            # We want descending order
            reverse=True,
        )

    async def get_room_sync_data(
        self,
        user: UserID,
        room_id: str,
        room_sync_config: RoomSyncConfig,
        room_membership_for_user_at_to_token: _RoomMembershipForUser,
        from_token: Optional[StreamToken],
        to_token: StreamToken,
    ) -> SlidingSyncResult.RoomResult:
        """
        Fetch room data for the sync response.

        We fetch data according to the token range (> `from_token` and <= `to_token`).

        Args:
            user: User to fetch data for
            room_id: The room ID to fetch data for
            room_sync_config: Config for what data we should fetch for a room in the
                sync response.
            room_membership_for_user_at_to_token: Membership information for the user
                in the room at the time of `to_token`.
            from_token: The point in the stream to sync from.
            to_token: The point in the stream to sync up to.
        """

        # Assemble the list of timeline events
        #
        # FIXME: It would be nice to make the `rooms` response more uniform regardless of
        # membership. Currently, we have to make all of these optional because
        # `invite`/`knock` rooms only have `stripped_state`. See
        # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
        timeline_events: Optional[List[EventBase]] = None
        bundled_aggregations: Optional[Dict[str, BundledAggregations]] = None
        limited: Optional[bool] = None
        prev_batch_token: Optional[StreamToken] = None
        num_live: Optional[int] = None
        if (
            room_sync_config.timeline_limit > 0
            # No timeline for invite/knock rooms (just `stripped_state`)
            and room_membership_for_user_at_to_token.membership
            not in (Membership.INVITE, Membership.KNOCK)
        ):
            limited = False
            # We want to start off using the `to_token` (vs `from_token`) because we look
            # backwards from the `to_token` up to the `timeline_limit` and we might not
            # reach the `from_token` before we hit the limit. We will update the room stream
            # position once we've fetched the events to point to the earliest event fetched.
            prev_batch_token = to_token

            # We're going to paginate backwards from the `to_token`
            from_bound = to_token.room_key
            # People shouldn't see past their leave/ban event
            if room_membership_for_user_at_to_token.membership in (
                Membership.LEAVE,
                Membership.BAN,
            ):
                from_bound = (
                    room_membership_for_user_at_to_token.event_pos.to_room_stream_token()
                )

            # Determine whether we should limit the timeline to the token range.
            #
            # We should return historical messages (before token range) in the
            # following cases because we want clients to be able to show a basic
            # screen of information:
            #  - Initial sync (because no `from_token` to limit us anyway)
            #  - When users `newly_joined`
            #  - TODO: For an incremental sync where we haven't sent it down this
            #    connection before
            to_bound = (
                from_token.room_key
                if from_token is not None
                and not room_membership_for_user_at_to_token.newly_joined
                else None
            )

            timeline_events, new_room_key = await self.store.paginate_room_events(
                room_id=room_id,
                from_key=from_bound,
                to_key=to_bound,
                direction=Direction.BACKWARDS,
                # We add one so we can determine if there are enough events to saturate
                # the limit or not (see `limited`)
                limit=room_sync_config.timeline_limit + 1,
                event_filter=None,
            )

            # We want to return the events in ascending order (the last event is the
            # most recent).
            timeline_events.reverse()

            # Determine our `limited` status based on the timeline. We do this before
            # filtering the events so we can accurately determine if there is more to
            # paginate even if we filter out some/all events.
            if len(timeline_events) > room_sync_config.timeline_limit:
                limited = True
                # Get rid of that extra "+ 1" event because we only used it to determine
                # if we hit the limit or not
                timeline_events = timeline_events[-room_sync_config.timeline_limit :]
                assert timeline_events[0].internal_metadata.stream_ordering
                new_room_key = RoomStreamToken(
                    stream=timeline_events[0].internal_metadata.stream_ordering - 1
                )

            # Make sure we don't expose any events that the client shouldn't see
            timeline_events = await filter_events_for_client(
                self.storage_controllers,
                user.to_string(),
                timeline_events,
                is_peeking=room_membership_for_user_at_to_token.membership
                != Membership.JOIN,
                filter_send_to_client=True,
            )
            # TODO: Filter out `EventTypes.CallInvite` in public rooms,
            # see https://github.com/element-hq/synapse/issues/17359

            # TODO: Handle timeline gaps (`get_timeline_gaps()`)

            # Determine how many "live" events we have (events within the given token range).
            #
            # This is mostly useful to determine whether a given @mention event should
            # make a noise or not. Clients cannot rely solely on the absence of
            # `initial: true` to determine live events because if a room not in the
            # sliding window bumps into the window because of an @mention it will have
            # `initial: true` yet contain a single live event (with potentially other
            # old events in the timeline)
            num_live = 0
            if from_token is not None:
                for timeline_event in reversed(timeline_events):
                    # This fields should be present for all persisted events
                    assert timeline_event.internal_metadata.stream_ordering is not None
                    assert timeline_event.internal_metadata.instance_name is not None

                    persisted_position = PersistedEventPosition(
                        instance_name=timeline_event.internal_metadata.instance_name,
                        stream=timeline_event.internal_metadata.stream_ordering,
                    )
                    if persisted_position.persisted_after(from_token.room_key):
                        num_live += 1
                    else:
                        # Since we're iterating over the timeline events in
                        # reverse-chronological order, we can break once we hit an event
                        # that's not live. In the future, we could potentially optimize
                        # this more with a binary search (bisect).
                        break

            # If the timeline is `limited=True`, the client does not have all events
            # necessary to calculate aggregations themselves.
            if limited:
                bundled_aggregations = (
                    await self.relations_handler.get_bundled_aggregations(
                        timeline_events, user.to_string()
                    )
                )

            # Update the `prev_batch_token` to point to the position that allows us to
            # keep paginating backwards from the oldest event we return in the timeline.
            prev_batch_token = prev_batch_token.copy_and_replace(
                StreamKeyType.ROOM, new_room_key
            )

        # Figure out any stripped state events for invite/knocks. This allows the
        # potential joiner to identify the room.
        stripped_state: Optional[List[JsonDict]] = None
        if room_membership_for_user_at_to_token.membership in (
            Membership.INVITE,
            Membership.KNOCK,
        ):
            # This should never happen. If someone is invited/knocked on room, then
            # there should be an event for it.
            assert room_membership_for_user_at_to_token.event_id is not None

            invite_or_knock_event = await self.store.get_event(
                room_membership_for_user_at_to_token.event_id
            )

            stripped_state = []
            if invite_or_knock_event.membership == Membership.INVITE:
                stripped_state.extend(
                    invite_or_knock_event.unsigned.get("invite_room_state", [])
                )
            elif invite_or_knock_event.membership == Membership.KNOCK:
                stripped_state.extend(
                    invite_or_knock_event.unsigned.get("knock_room_state", [])
                )

            stripped_state.append(strip_event(invite_or_knock_event))

        # TODO: Handle state resets. For example, if we see
        # `room_membership_for_user_at_to_token.membership = Membership.LEAVE` but
        # `required_state` doesn't include it, we should indicate to the client that a
        # state reset happened. Perhaps we should indicate this by setting `initial:
        # True` and empty `required_state`.

        # TODO: Since we can't determine whether we've already sent a room down this
        # Sliding Sync connection before (we plan to add this optimization in the
        # future), we're always returning the requested room state instead of
        # updates.
        initial = True

        # Fetch the `required_state` for the room
        #
        # No `required_state` for invite/knock rooms (just `stripped_state`)
        #
        # FIXME: It would be nice to make the `rooms` response more uniform regardless
        # of membership. Currently, we have to make this optional because
        # `invite`/`knock` rooms only have `stripped_state`. See
        # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
        #
        # Calculate the `StateFilter` based on the `required_state` for the room
        room_state: Optional[StateMap[EventBase]] = None
        required_room_state: Optional[StateMap[EventBase]] = None
        if room_membership_for_user_at_to_token.membership not in (
            Membership.INVITE,
            Membership.KNOCK,
        ):
            required_state_filter = StateFilter.none()
            # If we have a double wildcard ("*", "*") in the `required_state`, we need
            # to fetch all state for the room
            #
            # Note: MSC3575 describes different behavior to how we're handling things
            # here but since it's not wrong to return more state than requested
            # (`required_state` is just the minimum requested), it doesn't matter if we
            # include more than client wanted. This complexity is also under scrutiny,
            # see
            # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1185109050
            #
            # > One unique exception is when you request all state events via ["*", "*"]. When used,
            # > all state events are returned by default, and additional entries FILTER OUT the returned set
            # > of state events. These additional entries cannot use '*' themselves.
            # > For example, ["*", "*"], ["m.room.member", "@alice:example.com"] will _exclude_ every m.room.member
            # > event _except_ for @alice:example.com, and include every other state event.
            # > In addition, ["*", "*"], ["m.space.child", "*"] is an error, the m.space.child filter is not
            # > required as it would have been returned anyway.
            # >
            # > -- MSC3575 (https://github.com/matrix-org/matrix-spec-proposals/pull/3575)
            if StateValues.WILDCARD in room_sync_config.required_state_map.get(
                StateValues.WILDCARD, set()
            ):
                required_state_filter = StateFilter.all()
            # TODO: `StateFilter` currently doesn't support wildcard event types. We're
            # currently working around this by returning all state to the client but it
            # would be nice to fetch less from the database and return just what the
            # client wanted.
            elif (
                room_sync_config.required_state_map.get(StateValues.WILDCARD)
                is not None
            ):
                required_state_filter = StateFilter.all()
            else:
                required_state_types: List[Tuple[str, Optional[str]]] = []
                for (
                    state_type,
                    state_key_set,
                ) in room_sync_config.required_state_map.items():
                    for state_key in state_key_set:
                        if state_key == StateValues.WILDCARD:
                            # `None` is a wildcard in the `StateFilter`
                            required_state_types.append((state_type, None))
                        # We need to fetch all relevant people when we're lazy-loading membership
                        elif (
                            state_type == EventTypes.Member
                            and state_key == StateValues.LAZY
                        ):
                            # Everyone in the timeline is relevant
                            timeline_membership: Set[str] = set()
                            if timeline_events is not None:
                                for timeline_event in timeline_events:
                                    timeline_membership.add(timeline_event.sender)

                            for user_id in timeline_membership:
                                required_state_types.append(
                                    (EventTypes.Member, user_id)
                                )

                            # FIXME: We probably also care about invite, ban, kick, targets, etc
                            # but the spec only mentions "senders".
                        else:
                            required_state_types.append((state_type, state_key))

                required_state_filter = StateFilter.from_types(required_state_types)

            # We need this base set of info for the response so let's just fetch it along
            # with the `required_state` for the room
            META_ROOM_STATE = [(EventTypes.Name, ""), (EventTypes.RoomAvatar, "")]
            state_filter = StateFilter(
                types=StateFilter.from_types(
                    chain(META_ROOM_STATE, required_state_filter.to_types())
                ).types,
                include_others=required_state_filter.include_others,
            )

            # We can return all of the state that was requested if this was the first
            # time we've sent the room down this connection.
            if initial:
                # People shouldn't see past their leave/ban event
                if room_membership_for_user_at_to_token.membership in (
                    Membership.LEAVE,
                    Membership.BAN,
                ):
                    room_state = await self.storage_controllers.state.get_state_at(
                        room_id,
                        stream_position=to_token.copy_and_replace(
                            StreamKeyType.ROOM,
                            room_membership_for_user_at_to_token.event_pos.to_room_stream_token(),
                        ),
                        state_filter=state_filter,
                        # Partially-stated rooms should have all state events except for
                        # remote membership events. Since we've already excluded
                        # partially-stated rooms unless `required_state` only has
                        # `["m.room.member", "$LAZY"]` for membership, we should be able to
                        # retrieve everything requested. When we're lazy-loading, if there
                        # are some remote senders in the timeline, we should also have their
                        # membership event because we had to auth that timeline event. Plus
                        # we don't want to block the whole sync waiting for this one room.
                        await_full_state=False,
                    )
                # Otherwise, we can get the latest current state in the room
                else:
                    room_state = await self.storage_controllers.state.get_current_state(
                        room_id,
                        state_filter,
                        # Partially-stated rooms should have all state events except for
                        # remote membership events. Since we've already excluded
                        # partially-stated rooms unless `required_state` only has
                        # `["m.room.member", "$LAZY"]` for membership, we should be able to
                        # retrieve everything requested. When we're lazy-loading, if there
                        # are some remote senders in the timeline, we should also have their
                        # membership event because we had to auth that timeline event. Plus
                        # we don't want to block the whole sync waiting for this one room.
                        await_full_state=False,
                    )
                    # TODO: Query `current_state_delta_stream` and reverse/rewind back to the `to_token`
            else:
                # TODO: Once we can figure out if we've sent a room down this connection before,
                # we can return updates instead of the full required state.
                raise NotImplementedError()

            if required_state_filter != StateFilter.none():
                required_room_state = required_state_filter.filter_state(room_state)

        # Find the room name and avatar from the state
        room_name: Optional[str] = None
        room_avatar: Optional[str] = None
        if room_state is not None:
            name_event = room_state.get((EventTypes.Name, ""))
            if name_event is not None:
                room_name = name_event.content.get("name")

            avatar_event = room_state.get((EventTypes.RoomAvatar, ""))
            if avatar_event is not None:
                room_avatar = avatar_event.content.get("url")
        elif stripped_state is not None:
            for event in stripped_state:
                if event["type"] == EventTypes.Name:
                    room_name = event.get("content", {}).get("name")
                elif event["type"] == EventTypes.RoomAvatar:
                    room_avatar = event.get("content", {}).get("url")

                # Found everything so we can stop looking
                if room_name is not None and room_avatar is not None:
                    break

        # Figure out the last bump event in the room
        last_bump_event_stream_ordering = None
        if timeline_events:
            for e in reversed(timeline_events):
                if e.type in DEFAULT_BUMP_EVENT_TYPES:
                    last_bump_event_stream_ordering = (
                        e.internal_metadata.stream_ordering
                    )
                    break

        if last_bump_event_stream_ordering is None:
            last_bump_event_result = (
                await self.store.get_last_event_pos_in_room_before_stream_ordering(
                    room_id, to_token.room_key, event_types=DEFAULT_BUMP_EVENT_TYPES
                )
            )
            if last_bump_event_result is not None:
                last_bump_event_stream_ordering = last_bump_event_result[1].stream

        # By default, just choose the membership event position
        bump_stamp = room_membership_for_user_at_to_token.event_pos.stream
        # But if we found a bump event, use that instead
        if last_bump_event_stream_ordering is not None:
            bump_stamp = last_bump_event_stream_ordering

        return SlidingSyncResult.RoomResult(
            name=room_name,
            avatar=room_avatar,
            # TODO: Dummy value
            heroes=None,
            # TODO: Dummy value
            is_dm=False,
            initial=initial,
            required_state=(
                list(required_room_state.values()) if required_room_state else None
            ),
            timeline_events=timeline_events,
            bundled_aggregations=bundled_aggregations,
            stripped_state=stripped_state,
            prev_batch=prev_batch_token,
            limited=limited,
            num_live=num_live,
            bump_stamp=bump_stamp,
            # TODO: Dummy values
            joined_count=0,
            invited_count=0,
            # TODO: These are just dummy values. We could potentially just remove these
            # since notifications can only really be done correctly on the client anyway
            # (encrypted rooms).
            notification_count=0,
            highlight_count=0,
        )

    async def get_extensions_response(
        self,
        sync_config: SlidingSyncConfig,
        to_token: StreamToken,
    ) -> SlidingSyncResult.Extensions:
        """Handle extension requests.

        Args:
            sync_config: Sync configuration
            to_token: The point in the stream to sync up to.
        """

        if sync_config.extensions is None:
            return SlidingSyncResult.Extensions()

        to_device_response = None
        if sync_config.extensions.to_device:
            to_device_response = await self.get_to_device_extensions_response(
                sync_config=sync_config,
                to_device_request=sync_config.extensions.to_device,
                to_token=to_token,
            )

        return SlidingSyncResult.Extensions(to_device=to_device_response)

    async def get_to_device_extensions_response(
        self,
        sync_config: SlidingSyncConfig,
        to_device_request: SlidingSyncConfig.Extensions.ToDeviceExtension,
        to_token: StreamToken,
    ) -> SlidingSyncResult.Extensions.ToDeviceExtension:
        """Handle to-device extension (MSC3885)

        Args:
            sync_config: Sync configuration
            to_device_request: The to-device extension from the request
            to_token: The point in the stream to sync up to.
        """

        user_id = sync_config.user.to_string()
        device_id = sync_config.device_id

        # Check that this request has a valid device ID (not all requests have
        # to belong to a device, and so device_id is None), and that the
        # extension is enabled.
        if device_id is None or not to_device_request.enabled:
            return SlidingSyncResult.Extensions.ToDeviceExtension(
                next_batch=f"{to_token.to_device_key}",
                events=[],
            )

        since_stream_id = 0
        if to_device_request.since is not None:
            # We've already validated this is an int.
            since_stream_id = int(to_device_request.since)

            if to_token.to_device_key < since_stream_id:
                # The since token is ahead of our current token, so we return an
                # empty response.
                logger.warning(
                    "Got to-device.since from the future. since token: %r is ahead of our current to_device stream position: %r",
                    since_stream_id,
                    to_token.to_device_key,
                )
                return SlidingSyncResult.Extensions.ToDeviceExtension(
                    next_batch=to_device_request.since,
                    events=[],
                )

            # Delete everything before the given since token, as we know the
            # device must have received them.
            deleted = await self.store.delete_messages_for_device(
                user_id=user_id,
                device_id=device_id,
                up_to_stream_id=since_stream_id,
            )

            logger.debug(
                "Deleted %d to-device messages up to %d for %s",
                deleted,
                since_stream_id,
                user_id,
            )

        messages, stream_id = await self.store.get_messages_for_device(
            user_id=user_id,
            device_id=device_id,
            from_stream_id=since_stream_id,
            to_stream_id=to_token.to_device_key,
            limit=min(to_device_request.limit, 100),  # Limit to at most 100 events
        )

        return SlidingSyncResult.Extensions.ToDeviceExtension(
            next_batch=f"{stream_id}",
            events=messages,
        )
