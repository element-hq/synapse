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
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from immutabledict import immutabledict

from synapse.api.constants import AccountDataTypes, EventTypes, Membership
from synapse.events import EventBase
from synapse.storage.roommember import RoomsForUser
from synapse.types import (
    PersistedEventPosition,
    Requester,
    RoomStreamToken,
    StreamToken,
    UserID,
)
from synapse.types.handlers import OperationType, SlidingSyncConfig, SlidingSyncResult
from synapse.types.state import StateFilter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def convert_event_to_rooms_for_user(event: EventBase) -> RoomsForUser:
    """
    Quick helper to convert an event to a `RoomsForUser` object.
    """
    # These fields should be present for all persisted events
    assert event.internal_metadata.stream_ordering is not None
    assert event.internal_metadata.instance_name is not None

    return RoomsForUser(
        room_id=event.room_id,
        sender=event.sender,
        membership=event.membership,
        event_id=event.event_id,
        event_pos=PersistedEventPosition(
            event.internal_metadata.instance_name,
            event.internal_metadata.stream_ordering,
        ),
        room_version_id=event.room_version.identifier,
    )


def filter_membership_for_sync(*, membership: str, user_id: str, sender: str) -> bool:
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
    return membership != Membership.LEAVE or sender != user_id


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.clock = hs.get_clock()
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.auth_blocking = hs.get_auth_blocking()
        self.notifier = hs.get_notifier()
        self.event_sources = hs.get_event_sources()
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
                        ops.append(
                            SlidingSyncResult.SlidingWindowList.Operation(
                                op=OperationType.SYNC,
                                range=range,
                                room_ids=[
                                    room_id
                                    for room_id, _ in sorted_room_info[
                                        range[0] : range[1]
                                    ]
                                ],
                            )
                        )

                lists[list_key] = SlidingSyncResult.SlidingWindowList(
                    count=len(sorted_room_info),
                    ops=ops,
                )

        return SlidingSyncResult(
            next_pos=to_token,
            lists=lists,
            # TODO: Gather room data for rooms in lists and `sync_config.room_subscriptions`
            rooms={},
            extensions={},
        )

    async def get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken] = None,
    ) -> Dict[str, RoomsForUser]:
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
            room_for_user.room_id: room_for_user
            for room_for_user in room_for_user_list
            if filter_membership_for_sync(
                membership=room_for_user.membership,
                user_id=user_id,
                sender=room_for_user.sender,
            )
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
        # - 2) Add back newly_left rooms (> `from_token` and <= `to_token`)
        #
        # Below, we're doing two separate lookups for membership changes. We could
        # request everything for both fixups in one range, [`from_token.room_key`,
        # `membership_snapshot_token`), but we want to avoid raw `stream_ordering`
        # comparison without `instance_name` (which is flawed). We could refactor
        # `event.internal_metadata` to include `instance_name` but it might turn out a
        # little difficult and a bigger, broader Synapse change than we want to make.

        # 1) -----------------------------------------------------

        # 1) Fetch membership changes that fall in the range from `to_token` up to
        # `membership_snapshot_token`
        #
        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we don't need to do any "2)" fix-ups and can just straight-up
        # use the room list from the snapshot as a base (nothing has changed)
        membership_change_events_after_to_token = []
        if not membership_snapshot_token.is_before_or_eq(to_token.room_key):
            membership_change_events_after_to_token = (
                await self.store.get_membership_changes_for_user(
                    user_id,
                    from_key=to_token.room_key,
                    to_key=membership_snapshot_token,
                    excluded_rooms=self.rooms_to_exclude_globally,
                )
            )

        # 1) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        # We also need the first membership event after the `to_token` so we can step
        # backward to the previous membership that would apply to the from/to range.
        first_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        for event in membership_change_events_after_to_token:
            last_membership_change_by_room_id_after_to_token[event.room_id] = event
            # Only set if we haven't already set it
            first_membership_change_by_room_id_after_to_token.setdefault(
                event.room_id, event
            )

        # 1) Fixup
        for (
            last_membership_change_after_to_token
        ) in last_membership_change_by_room_id_after_to_token.values():
            room_id = last_membership_change_after_to_token.room_id

            # We want to find the first membership change after the `to_token` then step
            # backward to know the membership in the from/to range.
            first_membership_change_after_to_token = (
                first_membership_change_by_room_id_after_to_token.get(room_id)
            )
            assert first_membership_change_after_to_token is not None, (
                "If there was a `last_membership_change_after_to_token` that we're iterating over, "
                + "then there should be corresponding a first change. For example, even if there "
                + "is only one event after the `to_token`, the first and last event will be same event. "
                + "This is probably a mistake in assembling the `last_membership_change_by_room_id_after_to_token`"
                + "/`first_membership_change_by_room_id_after_to_token` dicts above."
            )
            # TODO: Instead of reading from `unsigned`, refactor this to use the
            # `current_state_delta_stream` table in the future. Probably a new
            # `get_membership_changes_for_user()` function that uses
            # `current_state_delta_stream` with a join to `room_memberships`. This would
            # help in state reset scenarios since `prev_content` is looking at the
            # current branch vs the current room state. This is all just data given to
            # the client so no real harm to data integrity, but we'd like to be nice to
            # the client. Since the `current_state_delta_stream` table is new, it
            # doesn't have all events in it. Since this is Sliding Sync, if we ever need
            # to, we can signal the client to throw all of their state away by sending
            # "operation: RESET".
            prev_content = first_membership_change_after_to_token.unsigned.get(
                "prev_content", {}
            )
            prev_membership = prev_content.get("membership", None)
            prev_sender = first_membership_change_after_to_token.unsigned.get(
                "prev_sender", None
            )

            # Check if the previous membership (membership that applies to the from/to
            # range) should be included in our `sync_room_id_set`
            should_prev_membership_be_included = (
                prev_membership is not None
                and prev_sender is not None
                and filter_membership_for_sync(
                    membership=prev_membership,
                    user_id=user_id,
                    sender=prev_sender,
                )
            )

            # Check if the last membership (membership that applies to our snapshot) was
            # already included in our `sync_room_id_set`
            was_last_membership_already_included = filter_membership_for_sync(
                membership=last_membership_change_after_to_token.membership,
                user_id=user_id,
                sender=last_membership_change_after_to_token.sender,
            )

            # 1a) Add back rooms that the user left after the `to_token`
            #
            # For example, if the last membership event after the `to_token` is a leave
            # event, then the room was excluded from `sync_room_id_set` when we first
            # crafted it above. We should add these rooms back as long as the user also
            # was part of the room before the `to_token`.
            if (
                not was_last_membership_already_included
                and should_prev_membership_be_included
            ):
                sync_room_id_set[room_id] = convert_event_to_rooms_for_user(
                    last_membership_change_after_to_token
                )
            # 1b) Remove rooms that the user joined (hasn't left) after the `to_token`
            #
            # For example, if the last membership event after the `to_token` is a "join"
            # event, then the room was included `sync_room_id_set` when we first crafted
            # it above. We should remove these rooms as long as the user also wasn't
            # part of the room before the `to_token`.
            elif (
                was_last_membership_already_included
                and not should_prev_membership_be_included
            ):
                del sync_room_id_set[room_id]

        # 2) -----------------------------------------------------
        # We fix-up newly_left rooms after the first fixup because it may have removed
        # some left rooms that we can figure out our newly_left in the following code

        # 2) Fetch membership changes that fall in the range from `from_token` up to `to_token`
        membership_change_events_in_from_to_range = []
        if from_token:
            membership_change_events_in_from_to_range = (
                await self.store.get_membership_changes_for_user(
                    user_id,
                    from_key=from_token.room_key,
                    to_key=to_token.room_key,
                    excluded_rooms=self.rooms_to_exclude_globally,
                )
            )

        # 2) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: Dict[str, EventBase] = {}
        for event in membership_change_events_in_from_to_range:
            last_membership_change_by_room_id_in_from_to_range[event.room_id] = event

        # 2) Fixup
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            room_id = last_membership_change_in_from_to_range.room_id

            # 2) Add back newly_left rooms (> `from_token` and <= `to_token`). We
            # include newly_left rooms because the last event that the user should see
            # is their own leave event
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                sync_room_id_set[room_id] = convert_event_to_rooms_for_user(
                    last_membership_change_in_from_to_range
                )

        return sync_room_id_set

    async def filter_rooms(
        self,
        user: UserID,
        sync_room_map: Dict[str, RoomsForUser],
        filters: SlidingSyncConfig.SlidingSyncList.Filters,
        to_token: StreamToken,
    ) -> Dict[str, RoomsForUser]:
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
        sync_room_map: Dict[str, RoomsForUser],
        to_token: StreamToken,
    ) -> List[Tuple[str, RoomsForUser]]:
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
