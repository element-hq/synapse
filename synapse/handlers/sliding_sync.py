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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import attr
from immutabledict import immutabledict

from synapse.api.constants import AccountDataTypes, Direction, EventTypes, Membership
from synapse.events import EventBase
from synapse.events.utils import strip_event
from synapse.handlers.relations import BundledAggregations
from synapse.storage.databases.main.stream import CurrentStateDeltaMembership
from synapse.types import (
    JsonDict,
    PersistedEventPosition,
    Requester,
    RoomStreamToken,
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
    # cases, TODO
    return membership != Membership.LEAVE or sender != user_id


# We can't freeze this class because we want to update it in place with the
# de-duplicated data.
@attr.s(slots=True, auto_attribs=True)
class RoomSyncConfig:
    """
    Holds the config for what data we should fetch for a room in the sync response.

    Attributes:
        timeline_limit: The maximum number of events to return in the timeline.
        required_state: The set of state events requested for the room. The
            values are close to `StateKey` but actually use a syntax where you can
            provide `*` wildcard and `$LAZY` for lazy room members as the `state_key` part
            of the tuple (type, state_key).
    """

    timeline_limit: int
    required_state: Set[Tuple[str, str]]


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

                sorted_room_info = await self.sort_rooms(
                    filtered_sync_room_map, to_token
                )

                ops: List[SlidingSyncResult.SlidingWindowList.Operation] = []
                if list_config.ranges:
                    for range in list_config.ranges:
                        sliced_room_ids = [
                            room_id
                            for room_id, _ in sorted_room_info[range[0] : range[1]]
                        ]

                        ops.append(
                            SlidingSyncResult.SlidingWindowList.Operation(
                                op=OperationType.SYNC,
                                range=range,
                                room_ids=sliced_room_ids,
                            )
                        )

                        # Take the superset of the `RoomSyncConfig` for each room
                        for room_id in sliced_room_ids:
                            if relevant_room_map.get(room_id) is not None:
                                # Take the highest timeline limit
                                if (
                                    relevant_room_map[room_id].timeline_limit
                                    < list_config.timeline_limit
                                ):
                                    relevant_room_map[room_id].timeline_limit = (
                                        list_config.timeline_limit
                                    )

                                # Union the required state
                                relevant_room_map[room_id].required_state.update(
                                    list_config.required_state
                                )
                            else:
                                relevant_room_map[room_id] = RoomSyncConfig(
                                    timeline_limit=list_config.timeline_limit,
                                    required_state=set(list_config.required_state),
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
                rooms_membership_for_user_at_to_token=sync_room_map[room_id],
                from_token=from_token,
                to_token=to_token,
            )

            rooms[room_id] = room_sync_result

        return SlidingSyncResult(
            next_pos=to_token,
            lists=lists,
            rooms=rooms,
            extensions={},
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
        prev_event_ids_in_from_to_range: List[str] = []
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
                prev_event_ids_in_from_to_range.append(
                    first_membership_change_after_to_token.prev_event_id
                )

        # 1) Fixup (more)
        #
        # 1b) 1c) Fetch the previous membership events that apply to the from/to range
        # and fixup our working list.
        prev_events_in_from_to_range = await self.store.get_events(
            prev_event_ids_in_from_to_range
        )
        for prev_event_in_from_to_range in prev_events_in_from_to_range.values():
            # These fields should be present for all persisted events
            assert (
                prev_event_in_from_to_range.internal_metadata.instance_name is not None
            )
            assert (
                prev_event_in_from_to_range.internal_metadata.stream_ordering
                is not None
            )

            # 1b) 1c) Update the membership with what we found
            sync_room_id_set[prev_event_in_from_to_range.room_id] = (
                _RoomMembershipForUser(
                    event_id=prev_event_in_from_to_range.event_id,
                    event_pos=PersistedEventPosition(
                        instance_name=prev_event_in_from_to_range.internal_metadata.instance_name,
                        stream=prev_event_in_from_to_range.internal_metadata.stream_ordering,
                    ),
                    membership=prev_event_in_from_to_range.membership,
                    sender=prev_event_in_from_to_range.sender,
                    newly_joined=False,
                )
            )

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
                    event_id=last_membership_change_in_from_to_range.event_id,
                    event_pos=last_membership_change_in_from_to_range.event_pos,
                    membership=last_membership_change_in_from_to_range.membership,
                    sender=last_membership_change_in_from_to_range.sender,
                    newly_joined=False,
                )

        # 3) Figure out `newly_joined`
        prev_event_ids_before_token_range: List[str] = []
        for possibly_newly_joined_room_id in possibly_newly_joined_room_ids:
            has_non_join_in_from_to_range = (
                has_non_join_event_by_room_id_in_from_to_range.get(
                    possibly_newly_joined_room_id, False
                )
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

                if prev_event_id is None:
                    # We found a `newly_joined` room (we are joining the room for the
                    # first time within the token range)
                    filtered_sync_room_id_set[room_id] = filtered_sync_room_id_set[
                        room_id
                    ].copy_and_replace(newly_joined=True)
                else:
                    # Last resort, we need to step back to the previous membership event
                    # just before the token range to see if we're joined then or not.
                    prev_event_ids_before_token_range.append(prev_event_id)

        # 3) more
        prev_events_before_token_range = await self.store.get_events(
            prev_event_ids_before_token_range
        )
        for prev_event_before_token_range in prev_events_before_token_range.values():
            if prev_event_before_token_range.membership != Membership.JOIN:
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
        #
        # TODO: Exclude partially stated rooms unless the `required_state` has
        # `["m.room.member", "$LAZY"]`

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
            for room_id in list(filtered_room_id_set):
                state_at_to_token = await self.storage_controllers.state.get_state_at(
                    room_id,
                    to_token,
                    state_filter=StateFilter.from_types(
                        [(EventTypes.RoomEncryption, "")]
                    ),
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
            for room_id in list(filtered_room_id_set):
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

        if filters.room_types:
            raise NotImplementedError()

        if filters.not_room_types:
            raise NotImplementedError()

        if filters.room_name_like:
            raise NotImplementedError()

        if filters.tags:
            raise NotImplementedError()

        if filters.not_tags:
            raise NotImplementedError()

        # Assemble a new sync room map but only with the `filtered_room_id_set`
        return {room_id: sync_room_map[room_id] for room_id in filtered_room_id_set}

    async def sort_rooms(
        self,
        sync_room_map: Dict[str, _RoomMembershipForUser],
        to_token: StreamToken,
    ) -> List[Tuple[str, _RoomMembershipForUser]]:
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
            # If they are fully-joined to the room, let's find the latest activity
            # at/before the `to_token`.
            if room_for_user.membership == Membership.JOIN:
                last_event_result = (
                    await self.store.get_last_event_pos_in_room_before_stream_ordering(
                        room_id, to_token.room_key
                    )
                )

                # If the room has no events at/before the `to_token`, this is probably a
                # mistake in the code that generates the `sync_room_map` since that should
                # only give us rooms that the user had membership in during the token range.
                assert last_event_result is not None

                _, event_pos = last_event_result

                last_activity_in_room_map[room_id] = event_pos.stream
            else:
                # Otherwise, if the user has left/been invited/knocked/been banned from
                # a room, they shouldn't see anything past that point.
                last_activity_in_room_map[room_id] = room_for_user.event_pos.stream

        return sorted(
            sync_room_map.items(),
            # Sort by the last activity (stream_ordering) in the room
            key=lambda room_info: last_activity_in_room_map[room_info[0]],
            # We want descending order
            reverse=True,
        )

    async def get_room_sync_data(
        self,
        user: UserID,
        room_id: str,
        room_sync_config: RoomSyncConfig,
        rooms_membership_for_user_at_to_token: _RoomMembershipForUser,
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
            rooms_membership_for_user_at_to_token: Membership information for the user
                in the room at the time of `to_token`.
            from_token: The point in the stream to sync from.
            to_token: The point in the stream to sync up to.
        """

        # Assemble the list of timeline events
        #
        # It would be nice to make the `rooms` response more uniform regardless of
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
            and rooms_membership_for_user_at_to_token.membership
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
            if rooms_membership_for_user_at_to_token.membership in (
                Membership.LEAVE,
                Membership.BAN,
            ):
                from_bound = (
                    rooms_membership_for_user_at_to_token.event_pos.to_room_stream_token()
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
                and not rooms_membership_for_user_at_to_token.newly_joined
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
                is_peeking=rooms_membership_for_user_at_to_token.membership
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
        stripped_state: List[JsonDict] = []
        if rooms_membership_for_user_at_to_token.membership in (
            Membership.INVITE,
            Membership.KNOCK,
        ):
            # This should never happen. If someone is invited/knocked on room, then
            # there should be an event for it.
            assert rooms_membership_for_user_at_to_token.event_id is not None

            invite_or_knock_event = await self.store.get_event(
                rooms_membership_for_user_at_to_token.event_id
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

        return SlidingSyncResult.RoomResult(
            # TODO: Dummy value
            name=None,
            # TODO: Dummy value
            avatar=None,
            # TODO: Dummy value
            heroes=None,
            # TODO: Since we can't determine whether we've already sent a room down this
            # Sliding Sync connection before (we plan to add this optimization in the
            # future), we're always returning the requested room state instead of
            # updates.
            initial=True,
            # TODO: Dummy value
            required_state=[],
            timeline_events=timeline_events,
            bundled_aggregations=bundled_aggregations,
            # TODO: Dummy value
            is_dm=False,
            stripped_state=stripped_state,
            prev_batch=prev_batch_token,
            limited=limited,
            # TODO: Dummy values
            joined_count=0,
            invited_count=0,
            # TODO: These are just dummy values. We could potentially just remove these
            # since notifications can only really be done correctly on the client anyway
            # (encrypted rooms).
            notification_count=0,
            highlight_count=0,
            num_live=num_live,
        )
