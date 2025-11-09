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


import logging
from itertools import chain
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Literal,
    Mapping,
    MutableMapping,
    cast,
)

import attr
from immutabledict import immutabledict
from typing_extensions import assert_never

from synapse.api.constants import (
    AccountDataTypes,
    EventContentFields,
    EventTypes,
    Membership,
)
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import StrippedStateEvent
from synapse.events.utils import parse_stripped_state_event
from synapse.logging.opentracing import start_active_span, trace
from synapse.storage.databases.main.state import (
    ROOM_UNKNOWN_SENTINEL,
    Sentinel as StateSentinel,
)
from synapse.storage.databases.main.stream import CurrentStateDeltaMembership
from synapse.storage.invite_rule import InviteRule
from synapse.storage.roommember import (
    RoomsForUser,
    RoomsForUserSlidingSync,
    RoomsForUserStateReset,
)
from synapse.types import (
    MutableStateMap,
    RoomStreamToken,
    StateMap,
    StrCollection,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    OperationType,
    PerConnectionState,
    RoomSyncConfig,
    SlidingSyncConfig,
    SlidingSyncResult,
)
from synapse.types.state import StateFilter
from synapse.util import MutableOverlayMapping
from synapse.util.sentinel import Sentinel

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


# Helper definition for the types that we might return. We do this to avoid
# copying data between types (which can be expensive for many rooms).
RoomsForUserType = RoomsForUserStateReset | RoomsForUser | RoomsForUserSlidingSync


@attr.s(auto_attribs=True, slots=True, frozen=True)
class SlidingSyncInterestedRooms:
    """The set of rooms and metadata a client is interested in based on their
    sliding sync request.

    Returned by `compute_interested_rooms`.

    Attributes:
        lists: A mapping from list name to the list result for the response
        relevant_room_map: A map from rooms that match the sync request to
            their room sync config.
        relevant_rooms_to_send_map: Subset of `relevant_room_map` that
            includes the rooms that *may* have relevant updates. Rooms not
            in this map will definitely not have room updates (though
            extensions may have updates in these rooms).
        newly_joined_rooms: The set of rooms that were joined in the token range
            and the user is still joined to at the end of this range.
        newly_left_rooms: The set of rooms that we left in the token range
            and are still "leave" at the end of this range.
        dm_room_ids: The set of rooms the user consider as direct-message (DM) rooms
    """

    lists: Mapping[str, SlidingSyncResult.SlidingWindowList]
    relevant_room_map: Mapping[str, RoomSyncConfig]
    relevant_rooms_to_send_map: Mapping[str, RoomSyncConfig]
    all_rooms: set[str]
    room_membership_for_user_map: Mapping[str, RoomsForUserType]

    newly_joined_rooms: AbstractSet[str]
    newly_left_rooms: AbstractSet[str]
    dm_room_ids: AbstractSet[str]

    @staticmethod
    def empty() -> "SlidingSyncInterestedRooms":
        return SlidingSyncInterestedRooms(
            lists={},
            relevant_room_map={},
            relevant_rooms_to_send_map={},
            all_rooms=set(),
            room_membership_for_user_map={},
            newly_joined_rooms=set(),
            newly_left_rooms=set(),
            dm_room_ids=set(),
        )


def filter_membership_for_sync(
    *,
    user_id: str,
    room_membership_for_user: RoomsForUserType,
    newly_left: bool,
) -> bool:
    """
    Returns True if the membership event should be included in the sync response,
    otherwise False.

    Attributes:
        user_id: The user ID that the membership applies to
        room_membership_for_user: Membership information for the user in the room
    """

    membership = room_membership_for_user.membership
    sender = room_membership_for_user.sender

    # We want to allow everything except rooms the user has left unless `newly_left`
    # because we want everything that's *still* relevant to the user. We include
    # `newly_left` rooms because the last event that the user should see is their own
    # leave event.
    #
    # A leave != kick. This logic includes kicks (leave events where the sender is not
    # the same user).
    #
    # When `sender=None`, it means that a state reset happened that removed the user
    # from the room without a corresponding leave event. We can just remove the rooms
    # since they are no longer relevant to the user but will still appear if they are
    # `newly_left`.
    return (
        # Anything except leave events
        membership != Membership.LEAVE
        # Unless...
        or newly_left
        # Allow kicks
        or (membership == Membership.LEAVE and sender not in (user_id, None))
    )


class SlidingSyncRoomLists:
    """Handles calculating the room lists from sliding sync requests"""

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.rooms_to_exclude_globally = hs.config.server.rooms_to_exclude_from_sync
        self.is_mine_id = hs.is_mine_id

    async def compute_interested_rooms(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> SlidingSyncInterestedRooms:
        """Fetch the set of rooms that match the request"""
        has_lists = sync_config.lists is not None and len(sync_config.lists) > 0
        has_room_subscriptions = (
            sync_config.room_subscriptions is not None
            and len(sync_config.room_subscriptions) > 0
        )

        if not has_lists and not has_room_subscriptions:
            return SlidingSyncInterestedRooms.empty()

        if await self.store.have_finished_sliding_sync_background_jobs():
            return await self._compute_interested_rooms_new_tables(
                sync_config=sync_config,
                previous_connection_state=previous_connection_state,
                to_token=to_token,
                from_token=from_token,
            )
        else:
            # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
            # foreground update for
            # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
            # https://github.com/element-hq/synapse/issues/17623)
            return await self._compute_interested_rooms_fallback(
                sync_config=sync_config,
                previous_connection_state=previous_connection_state,
                to_token=to_token,
                from_token=from_token,
            )

    @trace
    async def _compute_interested_rooms_new_tables(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> SlidingSyncInterestedRooms:
        """Implementation of `compute_interested_rooms` using new sliding sync db tables."""
        user_id = sync_config.user.to_string()

        # Assemble sliding window lists
        lists: dict[str, SlidingSyncResult.SlidingWindowList] = {}
        # Keep track of the rooms that we can display and need to fetch more info about
        relevant_room_map: dict[str, RoomSyncConfig] = {}
        # The set of room IDs of all rooms that could appear in any list. These
        # include rooms that are outside the list ranges.
        all_rooms: set[str] = set()

        # Note: this won't include rooms the user has left themselves. We add back
        # `newly_left` rooms below. This is more efficient than fetching all rooms and
        # then filtering out the old left rooms.
        room_membership_for_user_map: MutableMapping[str, RoomsForUserSlidingSync] = (
            MutableOverlayMapping(
                await self.store.get_sliding_sync_rooms_for_user_from_membership_snapshots(
                    user_id
                )
            )
        )
        # To play nice with the rewind logic below, we need to go fetch the rooms the
        # user has left themselves but only if it changed after the `to_token`.
        #
        # If a leave happens *after* the token range, we may have still been joined (or
        # any non-self-leave which is relevant to sync) to the room before so we need to
        # include it in the list of potentially relevant rooms and apply our rewind
        # logic (outside of this function) to see if it's actually relevant.
        #
        # We do this separately from
        # `get_sliding_sync_rooms_for_user_from_membership_snapshots` as those results
        # are cached and the `to_token` isn't very cache friendly (people are constantly
        # requesting with new tokens) so we separate it out here.
        self_leave_room_membership_for_user_map = (
            await self.store.get_sliding_sync_self_leave_rooms_after_to_token(
                user_id, to_token
            )
        )
        if self_leave_room_membership_for_user_map:
            room_membership_for_user_map.update(self_leave_room_membership_for_user_map)

        # Remove invites from ignored users
        ignored_users = await self.store.ignored_users(user_id)
        invite_config = await self.store.get_invite_config_for_user(user_id)
        if ignored_users:
            # Make a copy so we don't run into an error: `dictionary changed size during
            # iteration`, when we remove items
            for room_id in list(room_membership_for_user_map.keys()):
                room_for_user_sliding_sync = room_membership_for_user_map[room_id]
                if (
                    room_for_user_sliding_sync.membership == Membership.INVITE
                    and room_for_user_sliding_sync.sender
                    and (
                        room_for_user_sliding_sync.sender in ignored_users
                        or invite_config.get_invite_rule(
                            room_for_user_sliding_sync.sender
                        )
                        == InviteRule.IGNORE
                    )
                ):
                    room_membership_for_user_map.pop(room_id, None)

        (
            newly_joined_room_ids,
            newly_left_room_map,
        ) = await self._get_newly_joined_and_left_rooms(
            user_id, from_token=from_token, to_token=to_token
        )

        changes = await self._get_rewind_changes_to_current_membership_to_token(
            sync_config.user, room_membership_for_user_map, to_token=to_token
        )
        if changes:
            for room_id, change in changes.items():
                if change is None:
                    # Remove rooms that the user joined after the `to_token`
                    room_membership_for_user_map.pop(room_id, None)
                    continue

                existing_room = room_membership_for_user_map.get(room_id)
                if existing_room is not None:
                    # Update room membership events to the point in time of the `to_token`
                    room_for_user = RoomsForUserSlidingSync(
                        room_id=room_id,
                        sender=change.sender,
                        membership=change.membership,
                        event_id=change.event_id,
                        event_pos=change.event_pos,
                        room_version_id=change.room_version_id,
                        # We keep the state of the room though
                        has_known_state=existing_room.has_known_state,
                        room_type=existing_room.room_type,
                        is_encrypted=existing_room.is_encrypted,
                    )
                    if filter_membership_for_sync(
                        user_id=user_id,
                        room_membership_for_user=room_for_user,
                        newly_left=room_id in newly_left_room_map,
                    ):
                        room_membership_for_user_map[room_id] = room_for_user
                    else:
                        room_membership_for_user_map.pop(room_id, None)

        # Add back `newly_left` rooms (rooms left in the from -> to token range).
        #
        # We do this because `get_sliding_sync_rooms_for_user_from_membership_snapshots(...)` doesn't include
        # rooms that the user left themselves as it's more efficient to add them back
        # here than to fetch all rooms and then filter out the old left rooms. The user
        # only leaves a room once in a blue moon so this barely needs to run.
        #
        missing_newly_left_rooms = (
            newly_left_room_map.keys() - room_membership_for_user_map.keys()
        )
        if missing_newly_left_rooms:
            for room_id in missing_newly_left_rooms:
                newly_left_room_for_user = newly_left_room_map[room_id]
                # This should be a given
                assert newly_left_room_for_user.membership == Membership.LEAVE

                # Add back `newly_left` rooms
                #
                # Check for membership and state in the Sliding Sync tables as it's just
                # another membership
                newly_left_room_for_user_sliding_sync = (
                    await self.store.get_sliding_sync_room_for_user(user_id, room_id)
                )
                # If the membership exists, it's just a normal user left the room on
                # their own
                if newly_left_room_for_user_sliding_sync is not None:
                    if filter_membership_for_sync(
                        user_id=user_id,
                        room_membership_for_user=newly_left_room_for_user_sliding_sync,
                        newly_left=room_id in newly_left_room_map,
                    ):
                        room_membership_for_user_map[room_id] = (
                            newly_left_room_for_user_sliding_sync
                        )
                    else:
                        room_membership_for_user_map.pop(room_id, None)

                    change = changes.get(room_id)
                    if change is not None:
                        # Update room membership events to the point in time of the `to_token`
                        room_for_user = RoomsForUserSlidingSync(
                            room_id=room_id,
                            sender=change.sender,
                            membership=change.membership,
                            event_id=change.event_id,
                            event_pos=change.event_pos,
                            room_version_id=change.room_version_id,
                            # We keep the state of the room though
                            has_known_state=newly_left_room_for_user_sliding_sync.has_known_state,
                            room_type=newly_left_room_for_user_sliding_sync.room_type,
                            is_encrypted=newly_left_room_for_user_sliding_sync.is_encrypted,
                        )
                        if filter_membership_for_sync(
                            user_id=user_id,
                            room_membership_for_user=room_for_user,
                            newly_left=room_id in newly_left_room_map,
                        ):
                            room_membership_for_user_map[room_id] = room_for_user
                        else:
                            room_membership_for_user_map.pop(room_id, None)

                # If we are `newly_left` from the room but can't find any membership,
                # then we have been "state reset" out of the room
                else:
                    # Get the state at the time. We can't read from the Sliding Sync
                    # tables because the user has no membership in the room according to
                    # the state (thanks to the state reset).
                    #
                    # Note: `room_type` never changes, so we can just get current room
                    # type
                    room_type = await self.store.get_room_type(room_id)
                    has_known_state = room_type is not ROOM_UNKNOWN_SENTINEL
                    if isinstance(room_type, StateSentinel):
                        room_type = None

                    # Get the encryption status at the time of the token
                    is_encrypted = await self.get_is_encrypted_for_room_at_token(
                        room_id,
                        newly_left_room_for_user.event_pos.to_room_stream_token(),
                    )

                    room_for_user = RoomsForUserSlidingSync(
                        room_id=room_id,
                        sender=newly_left_room_for_user.sender,
                        membership=newly_left_room_for_user.membership,
                        event_id=newly_left_room_for_user.event_id,
                        event_pos=newly_left_room_for_user.event_pos,
                        room_version_id=newly_left_room_for_user.room_version_id,
                        has_known_state=has_known_state,
                        room_type=room_type,
                        is_encrypted=is_encrypted,
                    )
                    if filter_membership_for_sync(
                        user_id=user_id,
                        room_membership_for_user=room_for_user,
                        newly_left=room_id in newly_left_room_map,
                    ):
                        room_membership_for_user_map[room_id] = room_for_user
                    else:
                        room_membership_for_user_map.pop(room_id, None)

        # Remove any rooms that we globally exclude from sync.
        for room_id in self.rooms_to_exclude_globally:
            room_membership_for_user_map.pop(room_id, None)

        dm_room_ids = await self._get_dm_rooms_for_user(user_id)

        if sync_config.lists:
            sync_room_map = room_membership_for_user_map
            with start_active_span("assemble_sliding_window_lists"):
                for list_key, list_config in sync_config.lists.items():
                    # Apply filters
                    filtered_sync_room_map = sync_room_map
                    if list_config.filters is not None:
                        filtered_sync_room_map = await self.filter_rooms_using_tables(
                            user_id,
                            sync_room_map,
                            previous_connection_state,
                            list_config.filters,
                            to_token,
                            dm_room_ids,
                        )

                    # Find which rooms are partially stated and may need to be filtered out
                    # depending on the `required_state` requested (see below).
                    partial_state_rooms = await self.store.get_partial_rooms()

                    # Since creating the `RoomSyncConfig` takes some work, let's just do it
                    # once.
                    room_sync_config = RoomSyncConfig.from_room_config(list_config)

                    # Exclude partially-stated rooms if we must wait for the room to be
                    # fully-stated
                    if room_sync_config.must_await_full_state(self.is_mine_id):
                        filtered_sync_room_map = {
                            room_id: room
                            for room_id, room in filtered_sync_room_map.items()
                            if room_id not in partial_state_rooms
                        }

                    all_rooms.update(filtered_sync_room_map)

                    ops: list[SlidingSyncResult.SlidingWindowList.Operation] = []

                    if list_config.ranges:
                        # Optimization: If we are asking for the full range, we don't
                        # need to sort the list.
                        if (
                            # We're looking for a single range that covers the entire list
                            len(list_config.ranges) == 1
                            # Range starts at 0
                            and list_config.ranges[0][0] == 0
                            # And the range extends to the end of the list or more. Each
                            # side is inclusive.
                            and list_config.ranges[0][1]
                            >= len(filtered_sync_room_map) - 1
                        ):
                            sorted_room_info: list[RoomsForUserType] = list(
                                filtered_sync_room_map.values()
                            )
                        else:
                            # Sort the list
                            sorted_room_info = await self.sort_rooms(
                                # Cast is safe because RoomsForUserSlidingSync is part
                                # of the `RoomsForUserType` union. Why can't it detect this?
                                cast(
                                    dict[str, RoomsForUserType], filtered_sync_room_map
                                ),
                                to_token,
                                # We only need to sort the rooms up to the end
                                # of the largest range. Both sides of range are
                                # inclusive so we `+ 1`.
                                limit=max(range[1] + 1 for range in list_config.ranges),
                            )

                        for range in list_config.ranges:
                            room_ids_in_list: list[str] = []

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

                                # Take the superset of the `RoomSyncConfig` for each room.
                                #
                                # Update our `relevant_room_map` with the room we're going
                                # to display and need to fetch more info about.
                                existing_room_sync_config = relevant_room_map.get(
                                    room_id
                                )
                                if existing_room_sync_config is not None:
                                    room_sync_config = existing_room_sync_config.combine_room_sync_config(
                                        room_sync_config
                                    )

                                relevant_room_map[room_id] = room_sync_config

                                room_ids_in_list.append(room_id)

                            ops.append(
                                SlidingSyncResult.SlidingWindowList.Operation(
                                    op=OperationType.SYNC,
                                    range=range,
                                    room_ids=room_ids_in_list,
                                )
                            )

                    lists[list_key] = SlidingSyncResult.SlidingWindowList(
                        count=len(filtered_sync_room_map),
                        ops=ops,
                    )

        if sync_config.room_subscriptions:
            with start_active_span("assemble_room_subscriptions"):
                # Find which rooms are partially stated and may need to be filtered out
                # depending on the `required_state` requested (see below).
                partial_state_rooms = await self.store.get_partial_rooms()

                # Fetch any rooms that we have not already fetched from the database.
                subscription_sliding_sync_rooms = (
                    await self.store.get_sliding_sync_room_for_user_batch(
                        user_id,
                        sync_config.room_subscriptions.keys()
                        - room_membership_for_user_map.keys(),
                    )
                )
                room_membership_for_user_map.update(subscription_sliding_sync_rooms)

                for (
                    room_id,
                    room_subscription,
                ) in sync_config.room_subscriptions.items():
                    # Check if we have a membership for the room, but didn't pull it out
                    # above. This could be e.g. a leave that we don't pull out by
                    # default.
                    current_room_entry = room_membership_for_user_map.get(room_id)
                    if not current_room_entry:
                        # TODO: Handle rooms the user isn't in.
                        continue

                    all_rooms.add(room_id)

                    # Take the superset of the `RoomSyncConfig` for each room.
                    room_sync_config = RoomSyncConfig.from_room_config(
                        room_subscription
                    )

                    # Exclude partially-stated rooms if we must wait for the room to be
                    # fully-stated
                    if room_sync_config.must_await_full_state(self.is_mine_id):
                        if room_id in partial_state_rooms:
                            continue

                    # Update our `relevant_room_map` with the room we're going to display
                    # and need to fetch more info about.
                    existing_room_sync_config = relevant_room_map.get(room_id)
                    if existing_room_sync_config is not None:
                        room_sync_config = (
                            existing_room_sync_config.combine_room_sync_config(
                                room_sync_config
                            )
                        )

                    relevant_room_map[room_id] = room_sync_config

        # Filtered subset of `relevant_room_map` for rooms that may have updates
        # (in the event stream)
        relevant_rooms_to_send_map = await self._filter_relevant_rooms_to_send(
            previous_connection_state, from_token, relevant_room_map
        )

        return SlidingSyncInterestedRooms(
            lists=lists,
            relevant_room_map=relevant_room_map,
            relevant_rooms_to_send_map=relevant_rooms_to_send_map,
            all_rooms=all_rooms,
            room_membership_for_user_map=room_membership_for_user_map,
            newly_joined_rooms=newly_joined_room_ids,
            newly_left_rooms=set(newly_left_room_map),
            dm_room_ids=dm_room_ids,
        )

    async def _compute_interested_rooms_fallback(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> SlidingSyncInterestedRooms:
        """Fallback code when the database background updates haven't completed yet."""

        (
            room_membership_for_user_map,
            newly_joined_room_ids,
            newly_left_room_ids,
        ) = await self.get_room_membership_for_user_at_to_token(
            sync_config.user, to_token, from_token
        )

        dm_room_ids = await self._get_dm_rooms_for_user(sync_config.user.to_string())

        # Assemble sliding window lists
        lists: dict[str, SlidingSyncResult.SlidingWindowList] = {}
        # Keep track of the rooms that we can display and need to fetch more info about
        relevant_room_map: dict[str, RoomSyncConfig] = {}
        # The set of room IDs of all rooms that could appear in any list. These
        # include rooms that are outside the list ranges.
        all_rooms: set[str] = set()

        if sync_config.lists:
            with start_active_span("assemble_sliding_window_lists"):
                sync_room_map = await self.filter_rooms_relevant_for_sync(
                    user=sync_config.user,
                    room_membership_for_user_map=room_membership_for_user_map,
                    newly_left_room_ids=newly_left_room_ids,
                )

                for list_key, list_config in sync_config.lists.items():
                    # Apply filters
                    filtered_sync_room_map = sync_room_map
                    if list_config.filters is not None:
                        filtered_sync_room_map = await self.filter_rooms(
                            sync_config.user,
                            sync_room_map,
                            previous_connection_state,
                            list_config.filters,
                            to_token,
                            dm_room_ids,
                        )

                    # Find which rooms are partially stated and may need to be filtered out
                    # depending on the `required_state` requested (see below).
                    partial_state_rooms = await self.store.get_partial_rooms()

                    # Since creating the `RoomSyncConfig` takes some work, let's just do it
                    # once.
                    room_sync_config = RoomSyncConfig.from_room_config(list_config)

                    # Exclude partially-stated rooms if we must wait for the room to be
                    # fully-stated
                    if room_sync_config.must_await_full_state(self.is_mine_id):
                        filtered_sync_room_map = {
                            room_id: room
                            for room_id, room in filtered_sync_room_map.items()
                            if room_id not in partial_state_rooms
                        }

                    all_rooms.update(filtered_sync_room_map)

                    # Sort the list
                    sorted_room_info = await self.sort_rooms(
                        filtered_sync_room_map, to_token
                    )

                    ops: list[SlidingSyncResult.SlidingWindowList.Operation] = []
                    if list_config.ranges:
                        for range in list_config.ranges:
                            room_ids_in_list: list[str] = []

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

                                # Take the superset of the `RoomSyncConfig` for each room.
                                #
                                # Update our `relevant_room_map` with the room we're going
                                # to display and need to fetch more info about.
                                existing_room_sync_config = relevant_room_map.get(
                                    room_id
                                )
                                if existing_room_sync_config is not None:
                                    room_sync_config = existing_room_sync_config.combine_room_sync_config(
                                        room_sync_config
                                    )

                                relevant_room_map[room_id] = room_sync_config

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

        if sync_config.room_subscriptions:
            with start_active_span("assemble_room_subscriptions"):
                # Find which rooms are partially stated and may need to be filtered out
                # depending on the `required_state` requested (see below).
                partial_state_rooms = await self.store.get_partial_rooms()

                for (
                    room_id,
                    room_subscription,
                ) in sync_config.room_subscriptions.items():
                    room_membership_for_user_at_to_token = (
                        await self.check_room_subscription_allowed_for_user(
                            room_id=room_id,
                            room_membership_for_user_map=room_membership_for_user_map,
                            to_token=to_token,
                        )
                    )

                    # Skip this room if the user isn't allowed to see it
                    if not room_membership_for_user_at_to_token:
                        continue

                    all_rooms.add(room_id)

                    room_membership_for_user_map[room_id] = (
                        room_membership_for_user_at_to_token
                    )

                    # Take the superset of the `RoomSyncConfig` for each room.
                    room_sync_config = RoomSyncConfig.from_room_config(
                        room_subscription
                    )

                    # Exclude partially-stated rooms if we must wait for the room to be
                    # fully-stated
                    if room_sync_config.must_await_full_state(self.is_mine_id):
                        if room_id in partial_state_rooms:
                            continue

                    all_rooms.add(room_id)

                    # Update our `relevant_room_map` with the room we're going to display
                    # and need to fetch more info about.
                    existing_room_sync_config = relevant_room_map.get(room_id)
                    if existing_room_sync_config is not None:
                        room_sync_config = (
                            existing_room_sync_config.combine_room_sync_config(
                                room_sync_config
                            )
                        )

                    relevant_room_map[room_id] = room_sync_config

        # Filtered subset of `relevant_room_map` for rooms that may have updates
        # (in the event stream)
        relevant_rooms_to_send_map = await self._filter_relevant_rooms_to_send(
            previous_connection_state, from_token, relevant_room_map
        )

        return SlidingSyncInterestedRooms(
            lists=lists,
            relevant_room_map=relevant_room_map,
            relevant_rooms_to_send_map=relevant_rooms_to_send_map,
            all_rooms=all_rooms,
            room_membership_for_user_map=room_membership_for_user_map,
            newly_joined_rooms=newly_joined_room_ids,
            newly_left_rooms=newly_left_room_ids,
            dm_room_ids=dm_room_ids,
        )

    async def _filter_relevant_rooms_to_send(
        self,
        previous_connection_state: PerConnectionState,
        from_token: StreamToken | None,
        relevant_room_map: dict[str, RoomSyncConfig],
    ) -> dict[str, RoomSyncConfig]:
        """Filters the `relevant_room_map` down to those rooms that may have
        updates we need to fetch and return."""

        # Filtered subset of `relevant_room_map` for rooms that may have updates
        # (in the event stream)
        relevant_rooms_to_send_map: dict[str, RoomSyncConfig] = relevant_room_map
        if relevant_room_map:
            with start_active_span("filter_relevant_rooms_to_send"):
                if from_token:
                    rooms_should_send = set()

                    # First we check if there are rooms that match a list/room
                    # subscription and have updates we need to send (i.e. either because
                    # we haven't sent the room down, or we have but there are missing
                    # updates).
                    for room_id, room_config in relevant_room_map.items():
                        prev_room_sync_config = (
                            previous_connection_state.room_configs.get(room_id)
                        )
                        if prev_room_sync_config is not None:
                            # Always include rooms whose timeline limit has increased.
                            # (see the "XXX: Odd behavior" described below)
                            if (
                                prev_room_sync_config.timeline_limit
                                < room_config.timeline_limit
                            ):
                                rooms_should_send.add(room_id)
                                continue

                        status = previous_connection_state.rooms.have_sent_room(room_id)
                        if (
                            # The room was never sent down before so the client needs to know
                            # about it regardless of any updates.
                            status.status == HaveSentRoomFlag.NEVER
                            # `PREVIOUSLY` literally means the "room was sent down before *AND*
                            # there are updates we haven't sent down" so we already know this
                            # room has updates.
                            or status.status == HaveSentRoomFlag.PREVIOUSLY
                        ):
                            rooms_should_send.add(room_id)
                        elif status.status == HaveSentRoomFlag.LIVE:
                            # We know that we've sent all updates up until `from_token`,
                            # so we just need to check if there have been updates since
                            # then.
                            pass
                        else:
                            assert_never(status.status)

                    # We only need to check for new events since any state changes
                    # will also come down as new events.
                    rooms_that_have_updates = (
                        self.store.get_rooms_that_might_have_updates(
                            relevant_room_map.keys(), from_token.room_key
                        )
                    )
                    rooms_should_send.update(rooms_that_have_updates)
                    relevant_rooms_to_send_map = {
                        room_id: room_sync_config
                        for room_id, room_sync_config in relevant_room_map.items()
                        if room_id in rooms_should_send
                    }

        return relevant_rooms_to_send_map

    @trace
    async def _get_rewind_changes_to_current_membership_to_token(
        self,
        user: UserID,
        rooms_for_user: Mapping[str, RoomsForUserType],
        to_token: StreamToken,
    ) -> Mapping[str, RoomsForUser | None]:
        """
        Takes the current set of rooms for a user (retrieved after the given
        token), and returns the changes needed to "rewind" it to match the set of
        memberships *at that token* (<= `to_token`).

        Args:
            user: User to fetch rooms for
            rooms_for_user: The set of rooms for the user after the `to_token`.
            to_token: The token to rewind to

        Returns:
            The changes to apply to rewind the the current memberships.
        """
        # If the user has never joined any rooms before, we can just return an empty list
        if not rooms_for_user:
            return {}

        user_id = user.to_string()

        # Get the `RoomStreamToken` that represents the spot we queried up to when we got
        # our membership snapshot from `get_rooms_for_local_user_where_membership_is()`.
        #
        # First, we need to get the max stream_ordering of each event persister instance
        # that we queried events from.
        instance_to_max_stream_ordering_map: dict[str, int] = {}
        for room_for_user in rooms_for_user.values():
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
        min_stream_pos = min(instance_to_max_stream_ordering_map.values())
        membership_snapshot_token = RoomStreamToken(
            # Minimum position in the `instance_map`
            stream=min_stream_pos,
            instance_map=immutabledict(
                {
                    instance_name: stream_pos
                    for instance_name, stream_pos in instance_to_max_stream_ordering_map.items()
                    if stream_pos > min_stream_pos
                }
            ),
        )

        # Since we fetched the users room list at some point in time after the
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`. In particular, we need to make these fixups:
        #
        # - a) Remove rooms that the user joined after the `to_token`
        # - b) Update room membership events to the point in time of the `to_token`

        # Fetch membership changes that fall in the range from `to_token` up to
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

        if not current_state_delta_membership_changes_after_to_token:
            # There have been no membership changes, so we can early return.
            return {}

        # Otherwise we're about to make changes to `rooms_for_user`, so we turn
        # it into a mutable dict.
        changes: dict[str, RoomsForUser | None] = {}

        # Assemble a list of the first membership event after the `to_token` so we can
        # step backward to the previous membership that would apply to the from/to
        # range.
        first_membership_change_by_room_id_after_to_token: dict[
            str, CurrentStateDeltaMembership
        ] = {}
        for membership_change in current_state_delta_membership_changes_after_to_token:
            # Only set if we haven't already set it
            first_membership_change_by_room_id_after_to_token.setdefault(
                membership_change.room_id, membership_change
            )

        # Since we fetched a snapshot of the users room list at some point in time after
        # the from/to tokens, we need to revert/rewind some membership changes to match
        # the point in time of the `to_token`.
        for (
            room_id,
            first_membership_change_after_to_token,
        ) in first_membership_change_by_room_id_after_to_token.items():
            # 1a) Remove rooms that the user joined after the `to_token`
            if first_membership_change_after_to_token.prev_event_id is None:
                changes[room_id] = None
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
                    and first_membership_change_after_to_token.prev_sender is not None
                ):
                    # We need to know the room version ID, which we normally we
                    # can get from the current membership, but if we don't have
                    # that then we need to query the DB.
                    current_membership = rooms_for_user.get(room_id)
                    if current_membership is not None:
                        room_version_id = current_membership.room_version_id
                    else:
                        room_version_id = await self.store.get_room_version_id(room_id)

                    changes[room_id] = RoomsForUser(
                        room_id=room_id,
                        event_id=first_membership_change_after_to_token.prev_event_id,
                        event_pos=first_membership_change_after_to_token.prev_event_pos,
                        membership=first_membership_change_after_to_token.prev_membership,
                        sender=first_membership_change_after_to_token.prev_sender,
                        room_version_id=room_version_id,
                    )
                else:
                    # If we can't find the previous membership event, we shouldn't
                    # include the room in the sync response since we can't determine the
                    # exact membership state and shouldn't rely on the current snapshot.
                    changes[room_id] = None

        return changes

    @trace
    async def get_room_membership_for_user_at_to_token(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> tuple[dict[str, RoomsForUserType], AbstractSet[str], AbstractSet[str]]:
        """
        Fetch room IDs that the user has had membership in (the full room list including
        long-lost left rooms that will be filtered, sorted, and sliced).

        We're looking for rooms where the user has had any sort of membership in the
        token range (> `from_token` and <= `to_token`)

        In order for bans/kicks to not show up, you need to `/forget` those rooms. This
        doesn't modify the event itself though and only adds the `forgotten` flag to the
        `room_memberships` table in Synapse. There isn't a way to tell when a room was
        forgotten at the moment so we can't factor it into the token range.

        Args:
            user: User to fetch rooms for
            to_token: The token to fetch rooms up to.
            from_token: The point in the stream to sync from.

        Returns:
            A 3-tuple of:
              - A dictionary of room IDs that the user has had membership in along with
                membership information in that room at the time of `to_token`.
              - Set of newly joined rooms
              - Set of newly left rooms
        """
        user_id = user.to_string()

        # First grab a current snapshot rooms for the user
        # (also handles forgotten rooms)
        room_for_user_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            # We want to fetch any kind of membership (joined and left rooms) in order
            # to get the `event_pos` of the latest room membership event for the
            # user.
            membership_list=Membership.LIST,
            excluded_rooms=self.rooms_to_exclude_globally,
        )

        # We filter out unknown room versions before we try and load any
        # metadata about the room. They shouldn't go down sync anyway, and their
        # metadata may be in a broken state.
        room_for_user_list = [
            room_for_user
            for room_for_user in room_for_user_list
            if room_for_user.room_version_id in KNOWN_ROOM_VERSIONS
        ]

        # Remove invites from ignored users
        ignored_users = await self.store.ignored_users(user_id)
        if ignored_users:
            room_for_user_list = [
                room_for_user
                for room_for_user in room_for_user_list
                if not (
                    room_for_user.membership == Membership.INVITE
                    and room_for_user.sender in ignored_users
                )
            ]

        (
            newly_joined_room_ids,
            newly_left_room_map,
        ) = await self._get_newly_joined_and_left_rooms_fallback(
            user_id, to_token=to_token, from_token=from_token
        )

        # If the user has never joined any rooms before, we can just return an empty
        # list. We also have to check the `newly_left_room_map` in case someone was
        # state reset out of all of the rooms they were in.
        if not room_for_user_list and not newly_left_room_map:
            return {}, set(), set()

        # Since we fetched the users room list at some point in time after the
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`.
        rooms_for_user: dict[str, RoomsForUserType] = {
            room.room_id: room for room in room_for_user_list
        }
        changes = await self._get_rewind_changes_to_current_membership_to_token(
            user, rooms_for_user, to_token
        )
        for room_id, change_room_for_user in changes.items():
            if change_room_for_user is None:
                rooms_for_user.pop(room_id, None)
            else:
                rooms_for_user[room_id] = change_room_for_user

        # Ensure we have entries for rooms that the user has been "state reset"
        # out of. These are rooms appear in the `newly_left_rooms` map but
        # aren't in the `rooms_for_user` map.
        for room_id, newly_left_room_for_user in newly_left_room_map.items():
            # If we already know about the room, it's not a state reset
            if room_id in rooms_for_user:
                continue

            # This should be true if it's a state reset
            assert newly_left_room_for_user.membership is Membership.LEAVE
            assert newly_left_room_for_user.event_id is None
            assert newly_left_room_for_user.sender is None

            rooms_for_user[room_id] = newly_left_room_for_user

        return rooms_for_user, newly_joined_room_ids, set(newly_left_room_map)

    @trace
    async def _get_newly_joined_and_left_rooms(
        self,
        user_id: str,
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> tuple[AbstractSet[str], Mapping[str, RoomsForUserStateReset]]:
        """Fetch the sets of rooms that the user newly joined or left in the
        given token range.

        Note: there may be rooms in the newly left rooms where the user was
        "state reset" out of the room, and so that room would not be part of the
        "current memberships" of the user.

        Returns:
            A 2-tuple of newly joined room IDs and a map of newly_left room
            IDs to the `RoomsForUserStateReset` entry.

            We're using `RoomsForUserStateReset` but that doesn't necessarily mean the
            user was state reset of the rooms. It's just that the `event_id`/`sender`
            are optional and we can't tell the difference between the server leaving the
            room when the user was the last person participating in the room and left or
            was state reset out of the room. To actually check for a state reset, you
            need to check if a membership still exists in the room.
        """

        newly_joined_room_ids: set[str] = set()
        newly_left_room_map: dict[str, RoomsForUserStateReset] = {}

        if not from_token:
            return newly_joined_room_ids, newly_left_room_map

        changes = await self.store.get_sliding_sync_membership_changes(
            user_id,
            from_key=from_token.room_key,
            to_key=to_token.room_key,
            excluded_room_ids=set(self.rooms_to_exclude_globally),
        )

        for room_id, entry in changes.items():
            if entry.membership == Membership.JOIN:
                newly_joined_room_ids.add(room_id)
            elif entry.membership == Membership.LEAVE:
                newly_left_room_map[room_id] = entry

        return newly_joined_room_ids, newly_left_room_map

    @trace
    async def _get_newly_joined_and_left_rooms_fallback(
        self,
        user_id: str,
        to_token: StreamToken,
        from_token: StreamToken | None,
    ) -> tuple[AbstractSet[str], Mapping[str, RoomsForUserStateReset]]:
        """Fetch the sets of rooms that the user newly joined or left in the
        given token range.

        Note: there may be rooms in the newly left rooms where the user was
        "state reset" out of the room, and so that room would not be part of the
        "current memberships" of the user.

        Returns:
            A 2-tuple of newly joined room IDs and a map of newly_left room
            IDs to the `RoomsForUserStateReset` entry.

            We're using `RoomsForUserStateReset` but that doesn't necessarily mean the
            user was state reset of the rooms. It's just that the `event_id`/`sender`
            are optional and we can't tell the difference between the server leaving the
            room when the user was the last person participating in the room and left or
            was state reset out of the room. To actually check for a state reset, you
            need to check if a membership still exists in the room.
        """
        newly_joined_room_ids: set[str] = set()
        newly_left_room_map: dict[str, RoomsForUserStateReset] = {}

        # We need to figure out the
        #
        # - 1) Figure out which rooms are `newly_left` rooms (> `from_token` and <= `to_token`)
        # - 2) Figure out which rooms are `newly_joined` (> `from_token` and <= `to_token`)

        # 1) Fetch membership changes that fall in the range from `from_token` up to `to_token`
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

        # 1) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: dict[
            str, CurrentStateDeltaMembership
        ] = {}
        # We also want to assemble a list of the first membership events during the token
        # range so we can step backward to the previous membership that would apply to
        # before the token range to see if we have `newly_joined` the room.
        first_membership_change_by_room_id_in_from_to_range: dict[
            str, CurrentStateDeltaMembership
        ] = {}
        # Keep track if the room has a non-join event in the token range so we can later
        # tell if it was a `newly_joined` room. If the last membership event in the
        # token range is a join and there is also some non-join in the range, we know
        # they `newly_joined`.
        has_non_join_event_by_room_id_in_from_to_range: dict[str, bool] = {}
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

        # 1) Fixup
        #
        # 2) We also want to assemble a list of possibly newly joined rooms. Someone
        # could have left and joined multiple times during the given range but we only
        # care about whether they are joined at the end of the token range so we are
        # working with the last membership even in the token range.
        possibly_newly_joined_room_ids = set()
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            room_id = last_membership_change_in_from_to_range.room_id

            # 2)
            if last_membership_change_in_from_to_range.membership == Membership.JOIN:
                possibly_newly_joined_room_ids.add(room_id)

            # 1) Figure out newly_left rooms (> `from_token` and <= `to_token`).
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                # 1) Mark this room as `newly_left`
                newly_left_room_map[room_id] = RoomsForUserStateReset(
                    room_id=room_id,
                    sender=last_membership_change_in_from_to_range.sender,
                    membership=Membership.LEAVE,
                    event_id=last_membership_change_in_from_to_range.event_id,
                    event_pos=last_membership_change_in_from_to_range.event_pos,
                    room_version_id=await self.store.get_room_version_id(room_id),
                )

        # 2) Figure out `newly_joined`
        for room_id in possibly_newly_joined_room_ids:
            has_non_join_in_from_to_range = (
                has_non_join_event_by_room_id_in_from_to_range.get(room_id, False)
            )
            # If the last membership event in the token range is a join and there is
            # also some non-join in the range, we know they `newly_joined`.
            if has_non_join_in_from_to_range:
                # We found a `newly_joined` room (we left and joined within the token range)
                newly_joined_room_ids.add(room_id)
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
                    newly_joined_room_ids.add(room_id)
                # Last resort, we need to step back to the previous membership event
                # just before the token range to see if we're joined then or not.
                elif prev_membership != Membership.JOIN:
                    # We found a `newly_joined` room (we left before the token range
                    # and joined within the token range)
                    newly_joined_room_ids.add(room_id)

        return newly_joined_room_ids, newly_left_room_map

    @trace
    async def _get_dm_rooms_for_user(
        self,
        user_id: str,
    ) -> AbstractSet[str]:
        """Get the set of DM rooms for the user."""

        # We're using global account data (`m.direct`) instead of checking for
        # `is_direct` on membership events because that property only appears for
        # the invitee membership event (doesn't show up for the inviter).
        #
        # We're unable to take `to_token` into account for global account data since
        # we only keep track of the latest account data for the user.
        dm_map = await self.store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.DIRECT
        )

        # Flatten out the map. Account data is set by the client so it needs to be
        # scrutinized.
        dm_room_id_set = set()
        if isinstance(dm_map, dict):
            for room_ids in dm_map.values():
                # Account data should be a list of room IDs. Ignore anything else
                if isinstance(room_ids, list):
                    for room_id in room_ids:
                        if isinstance(room_id, str):
                            dm_room_id_set.add(room_id)

        return dm_room_id_set

    @trace
    async def filter_rooms_relevant_for_sync(
        self,
        user: UserID,
        room_membership_for_user_map: dict[str, RoomsForUserType],
        newly_left_room_ids: AbstractSet[str],
    ) -> dict[str, RoomsForUserType]:
        """
        Filter room IDs that should/can be listed for this user in the sync response (the
        full room list that will be further filtered, sorted, and sliced).

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
            user: User that is syncing
            room_membership_for_user_map: Room membership for the user
            newly_left_room_ids: The set of room IDs we have newly left

        Returns:
            A dictionary of room IDs that should be listed in the sync response along
            with membership information in that room at the time of `to_token`.
        """
        user_id = user.to_string()

        # Filter rooms to only what we're interested to sync with
        filtered_sync_room_map = {
            room_id: room_membership_for_user
            for room_id, room_membership_for_user in room_membership_for_user_map.items()
            if filter_membership_for_sync(
                user_id=user_id,
                room_membership_for_user=room_membership_for_user,
                newly_left=room_id in newly_left_room_ids,
            )
        }

        return filtered_sync_room_map

    async def check_room_subscription_allowed_for_user(
        self,
        room_id: str,
        room_membership_for_user_map: dict[str, RoomsForUserType],
        to_token: StreamToken,
    ) -> RoomsForUserType | None:
        """
        Check whether the user is allowed to see the room based on whether they have
        ever had membership in the room or if the room is `world_readable`.

        Similar to `check_user_in_room_or_world_readable(...)`

        Args:
            room_id: Room to check
            room_membership_for_user_map: Room membership for the user at the time of
                the `to_token` (<= `to_token`).
            to_token: The token to fetch rooms up to.

        Returns:
            The room membership for the user if they are allowed to subscribe to the
            room else `None`.
        """

        # We can first check if they are already allowed to see the room based
        # on our previous work to assemble the `room_membership_for_user_map`.
        #
        # If they have had any membership in the room over time (up to the `to_token`),
        # let them subscribe and see what they can.
        existing_membership_for_user = room_membership_for_user_map.get(room_id)
        if existing_membership_for_user is not None:
            return existing_membership_for_user

        # TODO: Handle `world_readable` rooms
        return None

        # If the room is `world_readable`, it doesn't matter whether they can join,
        # everyone can see the room.
        # not_in_room_membership_for_user = _RoomMembershipForUser(
        #     room_id=room_id,
        #     event_id=None,
        #     event_pos=None,
        #     membership=None,
        #     sender=None,
        #     newly_joined=False,
        #     newly_left=False,
        #     is_dm=False,
        # )
        # room_state = await self.get_current_state_at(
        #     room_id=room_id,
        #     room_membership_for_user_at_to_token=not_in_room_membership_for_user,
        #     state_filter=StateFilter.from_types(
        #         [(EventTypes.RoomHistoryVisibility, "")]
        #     ),
        #     to_token=to_token,
        # )

        # visibility_event = room_state.get((EventTypes.RoomHistoryVisibility, ""))
        # if (
        #     visibility_event is not None
        #     and visibility_event.content.get("history_visibility")
        #     == HistoryVisibility.WORLD_READABLE
        # ):
        #     return not_in_room_membership_for_user

        # return None

    @trace
    async def _bulk_get_stripped_state_for_rooms_from_sync_room_map(
        self,
        room_ids: StrCollection,
        sync_room_map: dict[str, RoomsForUserType],
    ) -> dict[str, StateMap[StrippedStateEvent] | None]:
        """
        Fetch stripped state for a list of room IDs. Stripped state is only
        applicable to invite/knock rooms. Other rooms will have `None` as their
        stripped state.

        For invite rooms, we pull from `unsigned.invite_room_state`.
        For knock rooms, we pull from `unsigned.knock_room_state`.

        Args:
            room_ids: Room IDs to fetch stripped state for
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.

        Returns:
            Mapping from room_id to mapping of (type, state_key) to stripped state
            event.
        """
        room_id_to_stripped_state_map: dict[
            str, StateMap[StrippedStateEvent] | None
        ] = {}

        # Fetch what we haven't before
        room_ids_to_fetch = [
            room_id
            for room_id in room_ids
            if room_id not in room_id_to_stripped_state_map
        ]

        # Gather a list of event IDs we can grab stripped state from
        invite_or_knock_event_ids: list[str] = []
        for room_id in room_ids_to_fetch:
            if sync_room_map[room_id].membership in (
                Membership.INVITE,
                Membership.KNOCK,
            ):
                event_id = sync_room_map[room_id].event_id
                # If this is an invite/knock then there should be an event_id
                assert event_id is not None
                invite_or_knock_event_ids.append(event_id)
            else:
                room_id_to_stripped_state_map[room_id] = None

        invite_or_knock_events = await self.store.get_events(invite_or_knock_event_ids)
        for invite_or_knock_event in invite_or_knock_events.values():
            room_id = invite_or_knock_event.room_id
            membership = invite_or_knock_event.membership

            raw_stripped_state_events = None
            if membership == Membership.INVITE:
                invite_room_state = invite_or_knock_event.unsigned.get(
                    "invite_room_state"
                )
                raw_stripped_state_events = invite_room_state
            elif membership == Membership.KNOCK:
                knock_room_state = invite_or_knock_event.unsigned.get(
                    "knock_room_state"
                )
                raw_stripped_state_events = knock_room_state
            else:
                raise AssertionError(
                    f"Unexpected membership {membership} (this is a problem with Synapse itself)"
                )

            stripped_state_map: MutableStateMap[StrippedStateEvent] | None = None
            # Scrutinize unsigned things. `raw_stripped_state_events` should be a list
            # of stripped events
            if raw_stripped_state_events is not None:
                stripped_state_map = {}
                if isinstance(raw_stripped_state_events, list):
                    for raw_stripped_event in raw_stripped_state_events:
                        stripped_state_event = parse_stripped_state_event(
                            raw_stripped_event
                        )
                        if stripped_state_event is not None:
                            stripped_state_map[
                                (
                                    stripped_state_event.type,
                                    stripped_state_event.state_key,
                                )
                            ] = stripped_state_event

            room_id_to_stripped_state_map[room_id] = stripped_state_map

        return room_id_to_stripped_state_map

    @trace
    async def _bulk_get_partial_current_state_content_for_rooms(
        self,
        content_type: Literal[
            # `content.type` from `EventTypes.Create``
            "room_type",
            # `content.algorithm` from `EventTypes.RoomEncryption`
            "room_encryption",
        ],
        room_ids: set[str],
        sync_room_map: dict[str, RoomsForUserType],
        to_token: StreamToken,
        room_id_to_stripped_state_map: dict[str, StateMap[StrippedStateEvent] | None],
    ) -> Mapping[str, str | None | StateSentinel]:
        """
        Get the given state event content for a list of rooms. First we check the
        current state of the room, then fallback to stripped state if available, then
        historical state.

        Args:
            content_type: Which content to grab
            room_ids: Room IDs to fetch the given content field for.
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            to_token: We filter based on the state of the room at this token
            room_id_to_stripped_state_map: This does not need to be filled in before
                calling this function. Mapping from room_id to mapping of (type, state_key)
                to stripped state event. Modified in place when we fetch new rooms so we can
                save work next time this function is called.

        Returns:
            A mapping from room ID to the state event content if the room has
            the given state event (event_type, ""), otherwise `None`. Rooms unknown to
            this server will return `ROOM_UNKNOWN_SENTINEL`.
        """
        room_id_to_content: dict[str, str | None | StateSentinel] = {}

        # As a bulk shortcut, use the current state if the server is particpating in the
        # room (meaning we have current state). Ideally, for leave/ban rooms, we would
        # want the state at the time of the membership instead of current state to not
        # leak anything but we consider the create/encryption stripped state events to
        # not be a secret given they are often set at the start of the room and they are
        # normally handed out on invite/knock.
        #
        # Be mindful to only use this for non-sensitive details. For example, even
        # though the room name/avatar/topic are also stripped state, they seem a lot
        # more senstive to leak the current state value of.
        #
        # Since this function is cached, we need to make a mutable copy via
        # `dict(...)`.
        event_type = ""
        event_content_field = ""
        if content_type == "room_type":
            event_type = EventTypes.Create
            event_content_field = EventContentFields.ROOM_TYPE
            room_id_to_content = dict(await self.store.bulk_get_room_type(room_ids))
        elif content_type == "room_encryption":
            event_type = EventTypes.RoomEncryption
            event_content_field = EventContentFields.ENCRYPTION_ALGORITHM
            room_id_to_content = dict(
                await self.store.bulk_get_room_encryption(room_ids)
            )
        else:
            assert_never(content_type)

        room_ids_with_results = [
            room_id
            for room_id, content_field in room_id_to_content.items()
            if content_field is not ROOM_UNKNOWN_SENTINEL
        ]

        # We might not have current room state for remote invite/knocks if we are
        # the first person on our server to see the room. The best we can do is look
        # in the optional stripped state from the invite/knock event.
        room_ids_without_results = room_ids.difference(
            chain(
                room_ids_with_results,
                [
                    room_id
                    for room_id, stripped_state_map in room_id_to_stripped_state_map.items()
                    if stripped_state_map is not None
                ],
            )
        )
        room_id_to_stripped_state_map.update(
            await self._bulk_get_stripped_state_for_rooms_from_sync_room_map(
                room_ids_without_results, sync_room_map
            )
        )

        # Update our `room_id_to_content` map based on the stripped state
        # (applies to invite/knock rooms)
        rooms_ids_without_stripped_state: set[str] = set()
        for room_id in room_ids_without_results:
            stripped_state_map = room_id_to_stripped_state_map.get(
                room_id, Sentinel.UNSET_SENTINEL
            )
            assert stripped_state_map is not Sentinel.UNSET_SENTINEL, (
                f"Stripped state left unset for room {room_id}. "
                + "Make sure you're calling `_bulk_get_stripped_state_for_rooms_from_sync_room_map(...)` "
                + "with that room_id. (this is a problem with Synapse itself)"
            )

            # If there is some stripped state, we assume the remote server passed *all*
            # of the potential stripped state events for the room.
            if stripped_state_map is not None:
                create_stripped_event = stripped_state_map.get((EventTypes.Create, ""))
                stripped_event = stripped_state_map.get((event_type, ""))
                # Sanity check that we at-least have the create event
                if create_stripped_event is not None:
                    if stripped_event is not None:
                        room_id_to_content[room_id] = stripped_event.content.get(
                            event_content_field
                        )
                    else:
                        # Didn't see the state event we're looking for in the stripped
                        # state so we can assume relevant content field is `None`.
                        room_id_to_content[room_id] = None
            else:
                rooms_ids_without_stripped_state.add(room_id)

        # Last resort, we might not have current room state for rooms that the
        # server has left (no one local is in the room) but we can look at the
        # historical state.
        #
        # Update our `room_id_to_content` map based on the state at the time of
        # the membership event.
        for room_id in rooms_ids_without_stripped_state:
            # TODO: It would be nice to look this up in a bulk way (N+1 queries)
            #
            # TODO: `get_state_at(...)` doesn't take into account the "current state".
            room_state = await self.storage_controllers.state.get_state_at(
                room_id=room_id,
                stream_position=to_token.copy_and_replace(
                    StreamKeyType.ROOM,
                    sync_room_map[room_id].event_pos.to_room_stream_token(),
                ),
                state_filter=StateFilter.from_types(
                    [
                        (EventTypes.Create, ""),
                        (event_type, ""),
                    ]
                ),
                # Partially-stated rooms should have all state events except for
                # remote membership events so we don't need to wait at all because
                # we only want the create event and some non-member event.
                await_full_state=False,
            )
            # We can use the create event as a canary to tell whether the server has
            # seen the room before
            create_event = room_state.get((EventTypes.Create, ""))
            state_event = room_state.get((event_type, ""))

            if create_event is None:
                # Skip for unknown rooms
                continue

            if state_event is not None:
                room_id_to_content[room_id] = state_event.content.get(
                    event_content_field
                )
            else:
                # Didn't see the state event we're looking for in the stripped
                # state so we can assume relevant content field is `None`.
                room_id_to_content[room_id] = None

        return room_id_to_content

    @trace
    async def filter_rooms(
        self,
        user: UserID,
        sync_room_map: dict[str, RoomsForUserType],
        previous_connection_state: PerConnectionState,
        filters: SlidingSyncConfig.SlidingSyncList.Filters,
        to_token: StreamToken,
        dm_room_ids: AbstractSet[str],
    ) -> dict[str, RoomsForUserType]:
        """
        Filter rooms based on the sync request.

        Args:
            user: User to filter rooms for
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            filters: Filters to apply
            to_token: We filter based on the state of the room at this token
            dm_room_ids: Set of room IDs that are DMs for the user

        Returns:
            A filtered dictionary of room IDs along with membership information in the
            room at the time of `to_token`.
        """
        user_id = user.to_string()

        room_id_to_stripped_state_map: dict[
            str, StateMap[StrippedStateEvent] | None
        ] = {}

        filtered_room_id_set = set(sync_room_map.keys())

        # Filter for Direct-Message (DM) rooms
        if filters.is_dm is not None:
            with start_active_span("filters.is_dm"):
                if filters.is_dm:
                    # Only DM rooms please
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        if room_id in dm_room_ids
                    }
                else:
                    # Only non-DM rooms please
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        if room_id not in dm_room_ids
                    }

        if filters.spaces is not None:
            with start_active_span("filters.spaces"):
                raise NotImplementedError()

        # Filter for encrypted rooms
        if filters.is_encrypted is not None:
            with start_active_span("filters.is_encrypted"):
                room_id_to_encryption = (
                    await self._bulk_get_partial_current_state_content_for_rooms(
                        content_type="room_encryption",
                        room_ids=filtered_room_id_set,
                        to_token=to_token,
                        sync_room_map=sync_room_map,
                        room_id_to_stripped_state_map=room_id_to_stripped_state_map,
                    )
                )

                # Make a copy so we don't run into an error: `Set changed size during
                # iteration`, when we filter out and remove items
                for room_id in filtered_room_id_set.copy():
                    encryption = room_id_to_encryption.get(
                        room_id, ROOM_UNKNOWN_SENTINEL
                    )

                    # Just remove rooms if we can't determine their encryption status
                    if encryption is ROOM_UNKNOWN_SENTINEL:
                        filtered_room_id_set.remove(room_id)
                        continue

                    # If we're looking for encrypted rooms, filter out rooms that are not
                    # encrypted and vice versa
                    is_encrypted = encryption is not None
                    if (filters.is_encrypted and not is_encrypted) or (
                        not filters.is_encrypted and is_encrypted
                    ):
                        filtered_room_id_set.remove(room_id)

        # Filter for rooms that the user has been invited to
        if filters.is_invite is not None:
            with start_active_span("filters.is_invite"):
                # Make a copy so we don't run into an error: `Set changed size during
                # iteration`, when we filter out and remove items
                for room_id in filtered_room_id_set.copy():
                    room_for_user = sync_room_map[room_id]
                    # If we're looking for invite rooms, filter out rooms that the user is
                    # not invited to and vice versa
                    if (
                        filters.is_invite
                        and room_for_user.membership != Membership.INVITE
                    ) or (
                        not filters.is_invite
                        and room_for_user.membership == Membership.INVITE
                    ):
                        filtered_room_id_set.remove(room_id)

        # Filter by room type (space vs room, etc). A room must match one of the types
        # provided in the list. `None` is a valid type for rooms which do not have a
        # room type.
        if filters.room_types is not None or filters.not_room_types is not None:
            with start_active_span("filters.room_types"):
                room_id_to_type = (
                    await self._bulk_get_partial_current_state_content_for_rooms(
                        content_type="room_type",
                        room_ids=filtered_room_id_set,
                        to_token=to_token,
                        sync_room_map=sync_room_map,
                        room_id_to_stripped_state_map=room_id_to_stripped_state_map,
                    )
                )

                # Make a copy so we don't run into an error: `Set changed size during
                # iteration`, when we filter out and remove items
                for room_id in filtered_room_id_set.copy():
                    room_type = room_id_to_type.get(room_id, ROOM_UNKNOWN_SENTINEL)

                    # Just remove rooms if we can't determine their type
                    if room_type is ROOM_UNKNOWN_SENTINEL:
                        filtered_room_id_set.remove(room_id)
                        continue

                    if (
                        filters.room_types is not None
                        and room_type not in filters.room_types
                    ):
                        filtered_room_id_set.remove(room_id)
                        continue

                    if (
                        filters.not_room_types is not None
                        and room_type in filters.not_room_types
                    ):
                        filtered_room_id_set.remove(room_id)
                        continue

        if filters.room_name_like is not None:
            with start_active_span("filters.room_name_like"):
                # TODO: The room name is a bit more sensitive to leak than the
                # create/encryption event. Maybe we should consider a better way to fetch
                # historical state before implementing this.
                #
                # room_id_to_create_content = await self._bulk_get_partial_current_state_content_for_rooms(
                #     content_type="room_name",
                #     room_ids=filtered_room_id_set,
                #     to_token=to_token,
                #     sync_room_map=sync_room_map,
                #     room_id_to_stripped_state_map=room_id_to_stripped_state_map,
                # )
                raise NotImplementedError()

        # Filter by room tags according to the users account data
        if filters.tags is not None or filters.not_tags is not None:
            with start_active_span("filters.tags"):
                # Fetch the user tags for their rooms
                room_tags = await self.store.get_tags_for_user(user_id)
                room_id_to_tag_name_set: dict[str, set[str]] = {
                    room_id: set(tags.keys()) for room_id, tags in room_tags.items()
                }

                if filters.tags is not None:
                    tags_set = set(filters.tags)
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        # Remove rooms that don't have one of the tags in the filter
                        if room_id_to_tag_name_set.get(room_id, set()).intersection(
                            tags_set
                        )
                    }

                if filters.not_tags is not None:
                    not_tags_set = set(filters.not_tags)
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        # Remove rooms if they have any of the tags in the filter
                        if not room_id_to_tag_name_set.get(room_id, set()).intersection(
                            not_tags_set
                        )
                    }

        # Keep rooms if the user has been state reset out of it but we previously sent
        # down the connection before. We want to make sure that we send these down to
        # the client regardless of filters so they find out about the state reset.
        #
        # We don't always have access to the state in a room after being state reset if
        # no one else locally on the server is participating in the room so we patch
        # these back in manually.
        state_reset_out_of_room_id_set = {
            room_id
            for room_id in sync_room_map.keys()
            if sync_room_map[room_id].event_id is None
            and previous_connection_state.rooms.have_sent_room(room_id).status
            != HaveSentRoomFlag.NEVER
        }

        # Assemble a new sync room map but only with the `filtered_room_id_set`
        return {
            room_id: sync_room_map[room_id]
            for room_id in filtered_room_id_set | state_reset_out_of_room_id_set
        }

    @trace
    async def filter_rooms_using_tables(
        self,
        user_id: str,
        sync_room_map: Mapping[str, RoomsForUserSlidingSync],
        previous_connection_state: PerConnectionState,
        filters: SlidingSyncConfig.SlidingSyncList.Filters,
        to_token: StreamToken,
        dm_room_ids: AbstractSet[str],
    ) -> dict[str, RoomsForUserSlidingSync]:
        """
        Filter rooms based on the sync request.

        Args:
            user: User to filter rooms for
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            filters: Filters to apply
            to_token: We filter based on the state of the room at this token
            dm_room_ids: Set of room IDs which are DMs
            room_tags: Mapping of room ID to tags

        Returns:
            A filtered dictionary of room IDs along with membership information in the
            room at the time of `to_token`.
        """

        filtered_room_id_set = set(sync_room_map.keys())

        # Filter for Direct-Message (DM) rooms
        if filters.is_dm is not None:
            with start_active_span("filters.is_dm"):
                if filters.is_dm:
                    # Intersect with the DM room set
                    filtered_room_id_set &= dm_room_ids
                else:
                    # Remove DMs
                    filtered_room_id_set -= dm_room_ids

        if filters.spaces is not None:
            with start_active_span("filters.spaces"):
                raise NotImplementedError()

        # Filter for encrypted rooms
        if filters.is_encrypted is not None:
            filtered_room_id_set = {
                room_id
                for room_id in filtered_room_id_set
                # Remove rooms if we can't figure out what the encryption status is
                if sync_room_map[room_id].has_known_state
                # Or remove if it doesn't match the filter
                and sync_room_map[room_id].is_encrypted == filters.is_encrypted
            }

        # Filter for rooms that the user has been invited to
        if filters.is_invite is not None:
            with start_active_span("filters.is_invite"):
                # Make a copy so we don't run into an error: `Set changed size during
                # iteration`, when we filter out and remove items
                for room_id in filtered_room_id_set.copy():
                    room_for_user = sync_room_map[room_id]
                    # If we're looking for invite rooms, filter out rooms that the user is
                    # not invited to and vice versa
                    if (
                        filters.is_invite
                        and room_for_user.membership != Membership.INVITE
                    ) or (
                        not filters.is_invite
                        and room_for_user.membership == Membership.INVITE
                    ):
                        filtered_room_id_set.remove(room_id)

        # Filter by room type (space vs room, etc). A room must match one of the types
        # provided in the list. `None` is a valid type for rooms which do not have a
        # room type.
        if filters.room_types is not None or filters.not_room_types is not None:
            with start_active_span("filters.room_types"):
                # Make a copy so we don't run into an error: `Set changed size during
                # iteration`, when we filter out and remove items
                for room_id in filtered_room_id_set.copy():
                    # Remove rooms if we can't figure out what room type it is
                    if not sync_room_map[room_id].has_known_state:
                        filtered_room_id_set.remove(room_id)
                        continue

                    room_type = sync_room_map[room_id].room_type

                    if (
                        filters.room_types is not None
                        and room_type not in filters.room_types
                    ):
                        filtered_room_id_set.remove(room_id)
                        continue

                    if (
                        filters.not_room_types is not None
                        and room_type in filters.not_room_types
                    ):
                        filtered_room_id_set.remove(room_id)
                        continue

        if filters.room_name_like is not None:
            with start_active_span("filters.room_name_like"):
                # TODO: The room name is a bit more sensitive to leak than the
                # create/encryption event. Maybe we should consider a better way to fetch
                # historical state before implementing this.
                #
                # room_id_to_create_content = await self._bulk_get_partial_current_state_content_for_rooms(
                #     content_type="room_name",
                #     room_ids=filtered_room_id_set,
                #     to_token=to_token,
                #     sync_room_map=sync_room_map,
                #     room_id_to_stripped_state_map=room_id_to_stripped_state_map,
                # )
                raise NotImplementedError()

        # Filter by room tags according to the users account data
        if filters.tags is not None or filters.not_tags is not None:
            with start_active_span("filters.tags"):
                # Fetch the user tags for their rooms
                room_tags = await self.store.get_tags_for_user(user_id)
                room_id_to_tag_name_set: dict[str, set[str]] = {
                    room_id: set(tags.keys()) for room_id, tags in room_tags.items()
                }

                if filters.tags is not None:
                    tags_set = set(filters.tags)
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        # Remove rooms that don't have one of the tags in the filter
                        if room_id_to_tag_name_set.get(room_id, set()).intersection(
                            tags_set
                        )
                    }

                if filters.not_tags is not None:
                    not_tags_set = set(filters.not_tags)
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        # Remove rooms if they have any of the tags in the filter
                        if not room_id_to_tag_name_set.get(room_id, set()).intersection(
                            not_tags_set
                        )
                    }

        # Keep rooms if the user has been state reset out of it but we previously sent
        # down the connection before. We want to make sure that we send these down to
        # the client regardless of filters so they find out about the state reset.
        #
        # We don't always have access to the state in a room after being state reset if
        # no one else locally on the server is participating in the room so we patch
        # these back in manually.
        state_reset_out_of_room_id_set = {
            room_id
            for room_id in sync_room_map.keys()
            if sync_room_map[room_id].event_id is None
            and previous_connection_state.rooms.have_sent_room(room_id).status
            != HaveSentRoomFlag.NEVER
        }

        # Assemble a new sync room map but only with the `filtered_room_id_set`
        return {
            room_id: sync_room_map[room_id]
            for room_id in filtered_room_id_set | state_reset_out_of_room_id_set
        }

    @trace
    async def sort_rooms(
        self,
        sync_room_map: dict[str, RoomsForUserType],
        to_token: StreamToken,
        limit: int | None = None,
    ) -> list[RoomsForUserType]:
        """
        Sort by `stream_ordering` of the last event that the user should see in the
        room. `stream_ordering` is unique so we get a stable sort.

        If `limit` is specified then sort may return fewer entries, but will
        always return at least the top N rooms. This is useful as we don't always
        need to sort the full list, but are just interested in the top N.

        Args:
            sync_room_map: Dictionary of room IDs to sort along with membership
                information in the room at the time of `to_token`.
            to_token: We sort based on the events in the room at this token (<= `to_token`)
            limit: The number of rooms that we need to return from the top of the list.

        Returns:
            A sorted list of room IDs by `stream_ordering` along with membership information.
        """

        # Assemble a map of room ID to the `stream_ordering` of the last activity that the
        # user should see in the room (<= `to_token`)
        last_activity_in_room_map: dict[str, int] = {}

        # Same as above, except for positions that we know are in the event
        # stream cache.
        cached_positions: dict[str, int] = {}

        earliest_cache_position = (
            self.store._events_stream_cache.get_earliest_known_position()
        )

        for room_id, room_for_user in sync_room_map.items():
            if room_for_user.membership == Membership.JOIN:
                # For joined rooms check the stream change cache.
                cached_position = (
                    self.store._events_stream_cache.get_max_pos_of_last_change(room_id)
                )
                if cached_position is not None:
                    cached_positions[room_id] = cached_position
            else:
                # If the user has left/been invited/knocked/been banned from a
                # room, they shouldn't see anything past that point.
                #
                # FIXME: It's possible that people should see beyond this point
                # in invited/knocked cases if for example the room has
                # `invite`/`world_readable` history visibility, see
                # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
                last_activity_in_room_map[room_id] = room_for_user.event_pos.stream

                # If the stream position is in range of the stream change cache
                # we can include it.
                if room_for_user.event_pos.stream > earliest_cache_position:
                    cached_positions[room_id] = room_for_user.event_pos.stream

        # If we are only asked for the top N rooms, and we have enough from
        # looking in the stream change cache, then we can return early. This
        # is because the cache must include all entries above
        # `.get_earliest_known_position()`.
        if limit is not None and len(cached_positions) >= limit:
            # ... but first we need to handle the case where the cached max
            # position is greater than the to_token, in which case we do
            # actually query the DB. This should happen rarely, so can do it in
            # a loop.
            for room_id, position in list(cached_positions.items()):
                if position > to_token.room_key.stream:
                    result = await self.store.get_last_event_pos_in_room_before_stream_ordering(
                        room_id, to_token.room_key
                    )
                    if (
                        result is not None
                        and result[1].stream > earliest_cache_position
                    ):
                        # We have a stream position in the cached range.
                        cached_positions[room_id] = result[1].stream
                    else:
                        # No position in the range, so we remove the entry.
                        cached_positions.pop(room_id)

        if limit is not None and len(cached_positions) >= limit:
            return sorted(
                (
                    room
                    for room in sync_room_map.values()
                    if room.room_id in cached_positions
                ),
                # Sort by the last activity (stream_ordering) in the room
                key=lambda room_info: cached_positions[room_info.room_id],
                # We want descending order
                reverse=True,
            )

        # For fully-joined rooms, we find the latest activity at/before the
        # `to_token`.
        joined_room_positions = (
            await self.store.bulk_get_last_event_pos_in_room_before_stream_ordering(
                [
                    room_id
                    for room_id, room_for_user in sync_room_map.items()
                    if room_for_user.membership == Membership.JOIN
                ],
                to_token.room_key,
            )
        )

        last_activity_in_room_map.update(joined_room_positions)

        return sorted(
            sync_room_map.values(),
            # Sort by the last activity (stream_ordering) in the room
            key=lambda room_info: last_activity_in_room_map[room_info.room_id],
            # We want descending order
            reverse=True,
        )

    async def get_is_encrypted_for_room_at_token(
        self, room_id: str, to_token: RoomStreamToken
    ) -> bool:
        """Get if the room is encrypted at the time."""

        # Fetch the current encryption state
        state_ids = await self.store.get_partial_filtered_current_state_ids(
            room_id, StateFilter.from_types([(EventTypes.RoomEncryption, "")])
        )
        encryption_event_id = state_ids.get((EventTypes.RoomEncryption, ""))

        # Now roll back the state by looking at the state deltas between
        # to_token and now.
        deltas = await self.store.get_current_state_deltas_for_room(
            room_id,
            from_token=to_token,
            to_token=self.store.get_room_max_token(),
        )

        for delta in deltas:
            if delta.event_type != EventTypes.RoomEncryption:
                continue

            # Found the first change, we look at the previous event ID to get
            # the state at the to token.

            if delta.prev_event_id is None:
                # There is no prev event, so no encryption state event, so room is not encrypted
                return False

            encryption_event_id = delta.prev_event_id
            break

        # We didn't find an encryption state, room isn't encrypted
        if encryption_event_id is None:
            return False

        # We found encryption state, check if content has a non-null algorithm
        encrypted_event = await self.store.get_event(encryption_event_id)
        algorithm = encrypted_event.content.get(EventContentFields.ENCRYPTION_ALGORITHM)

        return algorithm is not None
