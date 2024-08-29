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


import enum
import logging
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Set,
    Union,
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
from synapse.events import StrippedStateEvent
from synapse.events.utils import parse_stripped_state_event
from synapse.logging.opentracing import start_active_span, trace
from synapse.storage.databases.main.state import (
    ROOM_UNKNOWN_SENTINEL,
    Sentinel as StateSentinel,
)
from synapse.storage.databases.main.stream import CurrentStateDeltaMembership
from synapse.types import (
    MutableStateMap,
    PersistedEventPosition,
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

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


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
    """

    lists: Mapping[str, SlidingSyncResult.SlidingWindowList]
    relevant_room_map: Mapping[str, RoomSyncConfig]
    relevant_rooms_to_send_map: Mapping[str, RoomSyncConfig]
    all_rooms: Set[str]
    room_membership_for_user_map: Mapping[str, "_RoomMembershipForUser"]


class Sentinel(enum.Enum):
    # defining a sentinel in this way allows mypy to correctly handle the
    # type of a dictionary lookup and subsequent type narrowing.
    UNSET_SENTINEL = object()


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _RoomMembershipForUser:
    """
    Attributes:
        room_id: The room ID of the membership event
        event_id: The event ID of the membership event
        event_pos: The stream position of the membership event
        membership: The membership state of the user in the room
        sender: The person who sent the membership event
        newly_joined: Whether the user newly joined the room during the given token
            range and is still joined to the room at the end of this range.
        newly_left: Whether the user newly left (or kicked) the room during the given
            token range and is still "leave" at the end of this range.
        is_dm: Whether this user considers this room as a direct-message (DM) room
    """

    room_id: str
    # Optional because state resets can affect room membership without a corresponding event.
    event_id: Optional[str]
    # Even during a state reset which removes the user from the room, we expect this to
    # be set because `current_state_delta_stream` will note the position that the reset
    # happened.
    event_pos: PersistedEventPosition
    # Even during a state reset which removes the user from the room, we expect this to
    # be set to `LEAVE` because we can make that assumption based on the situaton (see
    # `get_current_state_delta_membership_changes_for_user(...)`)
    membership: str
    # Optional because state resets can affect room membership without a corresponding event.
    sender: Optional[str]
    newly_joined: bool
    newly_left: bool
    is_dm: bool

    def copy_and_replace(self, **kwds: Any) -> "_RoomMembershipForUser":
        return attr.evolve(self, **kwds)


def filter_membership_for_sync(
    *, user_id: str, room_membership_for_user: _RoomMembershipForUser
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
    newly_left = room_membership_for_user.newly_left

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
        from_token: Optional[StreamToken],
    ) -> SlidingSyncInterestedRooms:
        """Fetch the set of rooms that match the request"""

        room_membership_for_user_map = (
            await self.get_room_membership_for_user_at_to_token(
                sync_config.user, to_token, from_token
            )
        )

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        # Keep track of the rooms that we can display and need to fetch more info about
        relevant_room_map: Dict[str, RoomSyncConfig] = {}
        # The set of room IDs of all rooms that could appear in any list. These
        # include rooms that are outside the list ranges.
        all_rooms: Set[str] = set()

        if sync_config.lists:
            with start_active_span("assemble_sliding_window_lists"):
                sync_room_map = await self.filter_rooms_relevant_for_sync(
                    user=sync_config.user,
                    room_membership_for_user_map=room_membership_for_user_map,
                )

                for list_key, list_config in sync_config.lists.items():
                    # Apply filters
                    filtered_sync_room_map = sync_room_map
                    if list_config.filters is not None:
                        filtered_sync_room_map = await self.filter_rooms(
                            sync_config.user,
                            sync_room_map,
                            list_config.filters,
                            to_token,
                        )

                    # Find which rooms are partially stated and may need to be filtered out
                    # depending on the `required_state` requested (see below).
                    partial_state_room_map = (
                        await self.store.is_partial_state_room_batched(
                            filtered_sync_room_map.keys()
                        )
                    )

                    # Since creating the `RoomSyncConfig` takes some work, let's just do it
                    # once and make a copy whenever we need it.
                    room_sync_config = RoomSyncConfig.from_room_config(list_config)

                    # Exclude partially-stated rooms if we must wait for the room to be
                    # fully-stated
                    if room_sync_config.must_await_full_state(self.is_mine_id):
                        filtered_sync_room_map = {
                            room_id: room
                            for room_id, room in filtered_sync_room_map.items()
                            if not partial_state_room_map.get(room_id)
                        }

                    all_rooms.update(filtered_sync_room_map)

                    # Sort the list
                    sorted_room_info = await self.sort_rooms(
                        filtered_sync_room_map, to_token
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
                partial_state_room_map = await self.store.is_partial_state_room_batched(
                    sync_config.room_subscriptions.keys()
                )

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
                        if partial_state_room_map.get(room_id):
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
        relevant_rooms_to_send_map: Dict[str, RoomSyncConfig] = relevant_room_map
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

        return SlidingSyncInterestedRooms(
            lists=lists,
            relevant_room_map=relevant_room_map,
            relevant_rooms_to_send_map=relevant_rooms_to_send_map,
            all_rooms=all_rooms,
            room_membership_for_user_map=room_membership_for_user_map,
        )

    @trace
    async def get_room_membership_for_user_at_to_token(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken],
    ) -> Dict[str, _RoomMembershipForUser]:
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
            A dictionary of room IDs that the user has had membership in along with
            membership information in that room at the time of `to_token`.
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
                # We will update these fields below to be accurate
                newly_joined=False,
                newly_left=False,
                is_dm=False,
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

        # Since we fetched the users room list at some point in time after the from/to
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`. In particular, we need to make these fixups:
        #
        # - 1a) Remove rooms that the user joined after the `to_token`
        # - 1b) Add back rooms that the user left after the `to_token`
        # - 1c) Update room membership events to the point in time of the `to_token`
        # - 2) Figure out which rooms are `newly_left` rooms (> `from_token` and <= `to_token`)
        # - 3) Figure out which rooms are `newly_joined` (> `from_token` and <= `to_token`)
        # - 4) Figure out which rooms are DM's

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
                        # We will update these fields below to be accurate
                        newly_joined=False,
                        newly_left=False,
                        is_dm=False,
                    )
                else:
                    # If we can't find the previous membership event, we shouldn't
                    # include the room in the sync response since we can't determine the
                    # exact membership state and shouldn't rely on the current snapshot.
                    sync_room_id_set.pop(room_id, None)

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

            # 2) Figure out newly_left rooms (> `from_token` and <= `to_token`).
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                # 2) Mark this room as `newly_left`

                # If we're seeing a membership change here, we should expect to already
                # have it in our snapshot but if a state reset happens, it wouldn't have
                # shown up in our snapshot but appear as a change here.
                existing_sync_entry = sync_room_id_set.get(room_id)
                if existing_sync_entry is not None:
                    # Normal expected case
                    sync_room_id_set[room_id] = existing_sync_entry.copy_and_replace(
                        newly_left=True
                    )
                else:
                    # State reset!
                    logger.warn(
                        "State reset detected for room_id %s with %s who is no longer in the room",
                        room_id,
                        user_id,
                    )
                    # Even though a state reset happened which removed the person from
                    # the room, we still add it the list so the user knows they left the
                    # room. Downstream code can check for a state reset by looking for
                    # `event_id=None and membership is not None`.
                    sync_room_id_set[room_id] = _RoomMembershipForUser(
                        room_id=room_id,
                        event_id=last_membership_change_in_from_to_range.event_id,
                        event_pos=last_membership_change_in_from_to_range.event_pos,
                        membership=last_membership_change_in_from_to_range.membership,
                        sender=last_membership_change_in_from_to_range.sender,
                        newly_joined=False,
                        newly_left=True,
                        is_dm=False,
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
                sync_room_id_set[room_id] = sync_room_id_set[room_id].copy_and_replace(
                    newly_joined=True
                )
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
                    sync_room_id_set[room_id] = sync_room_id_set[
                        room_id
                    ].copy_and_replace(newly_joined=True)
                # Last resort, we need to step back to the previous membership event
                # just before the token range to see if we're joined then or not.
                elif prev_membership != Membership.JOIN:
                    # We found a `newly_joined` room (we left before the token range
                    # and joined within the token range)
                    sync_room_id_set[room_id] = sync_room_id_set[
                        room_id
                    ].copy_and_replace(newly_joined=True)

        # 4) Figure out which rooms the user considers to be direct-message (DM) rooms
        #
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

        # 4) Fixup
        for room_id in sync_room_id_set:
            sync_room_id_set[room_id] = sync_room_id_set[room_id].copy_and_replace(
                is_dm=room_id in dm_room_id_set
            )

        return sync_room_id_set

    @trace
    async def filter_rooms_relevant_for_sync(
        self,
        user: UserID,
        room_membership_for_user_map: Dict[str, _RoomMembershipForUser],
    ) -> Dict[str, _RoomMembershipForUser]:
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
            )
        }

        return filtered_sync_room_map

    async def check_room_subscription_allowed_for_user(
        self,
        room_id: str,
        room_membership_for_user_map: Dict[str, _RoomMembershipForUser],
        to_token: StreamToken,
    ) -> Optional[_RoomMembershipForUser]:
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
        sync_room_map: Dict[str, _RoomMembershipForUser],
    ) -> Dict[str, Optional[StateMap[StrippedStateEvent]]]:
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
        room_id_to_stripped_state_map: Dict[
            str, Optional[StateMap[StrippedStateEvent]]
        ] = {}

        # Fetch what we haven't before
        room_ids_to_fetch = [
            room_id
            for room_id in room_ids
            if room_id not in room_id_to_stripped_state_map
        ]

        # Gather a list of event IDs we can grab stripped state from
        invite_or_knock_event_ids: List[str] = []
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

            stripped_state_map: Optional[MutableStateMap[StrippedStateEvent]] = None
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
        room_ids: Set[str],
        sync_room_map: Dict[str, _RoomMembershipForUser],
        to_token: StreamToken,
        room_id_to_stripped_state_map: Dict[
            str, Optional[StateMap[StrippedStateEvent]]
        ],
    ) -> Mapping[str, Union[Optional[str], StateSentinel]]:
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
        room_id_to_content: Dict[str, Union[Optional[str], StateSentinel]] = {}

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
        rooms_ids_without_stripped_state: Set[str] = set()
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
        room_id_to_stripped_state_map: Dict[
            str, Optional[StateMap[StrippedStateEvent]]
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
                        if sync_room_map[room_id].is_dm
                    }
                else:
                    # Only non-DM rooms please
                    filtered_room_id_set = {
                        room_id
                        for room_id in filtered_room_id_set
                        if not sync_room_map[room_id].is_dm
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

                    if (
                        filters.not_room_types is not None
                        and room_type in filters.not_room_types
                    ):
                        filtered_room_id_set.remove(room_id)

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

        if filters.tags is not None or filters.not_tags is not None:
            with start_active_span("filters.tags"):
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

        for room_id, room_for_user in sync_room_map.items():
            if room_for_user.membership != Membership.JOIN:
                # If the user has left/been invited/knocked/been banned from a
                # room, they shouldn't see anything past that point.
                #
                # FIXME: It's possible that people should see beyond this point
                # in invited/knocked cases if for example the room has
                # `invite`/`world_readable` history visibility, see
                # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
                last_activity_in_room_map[room_id] = room_for_user.event_pos.stream

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
