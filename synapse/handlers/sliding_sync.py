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
from typing import TYPE_CHECKING, AbstractSet, Dict, List, Optional

from immutabledict import immutabledict

from synapse.api.constants import Membership
from synapse.events import EventBase
from synapse.types import Requester, RoomStreamToken, StreamToken, UserID
from synapse.types.handlers import OperationType, SlidingSyncConfig, SlidingSyncResult

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


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
        """Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.
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
        """
        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        # Get all of the room IDs that the user should be able to see in the sync
        # response
        room_id_set = await self.get_sync_room_ids_for_user(
            sync_config.user,
            from_token=from_token,
            to_token=to_token,
        )

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        if sync_config.lists:
            for list_key, list_config in sync_config.lists.items():
                # TODO: Apply filters
                #
                # TODO: Exclude partially stated rooms unless the `required_state` has
                # `["m.room.member", "$LAZY"]`
                filtered_room_ids = room_id_set
                # TODO: Apply sorts
                sorted_room_ids = sorted(filtered_room_ids)

                ops: List[SlidingSyncResult.SlidingWindowList.Operation] = []
                if list_config.ranges:
                    for range in list_config.ranges:
                        ops.append(
                            SlidingSyncResult.SlidingWindowList.Operation(
                                op=OperationType.SYNC,
                                range=range,
                                room_ids=sorted_room_ids[range[0] : range[1]],
                            )
                        )

                lists[list_key] = SlidingSyncResult.SlidingWindowList(
                    count=len(sorted_room_ids),
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
    ) -> AbstractSet[str]:
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
            return set()

        # Our working list of rooms that can show up in the sync response
        sync_room_id_set = {
            room_for_user.room_id
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

        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we can just straight-up return the room list (nothing has
        # changed)
        if membership_snapshot_token.is_before_or_eq(to_token.room_key):
            return sync_room_id_set

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
                sync_room_id_set.add(room_id)
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
                sync_room_id_set.discard(room_id)

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
                sync_room_id_set.add(room_id)

        return sync_room_id_set
