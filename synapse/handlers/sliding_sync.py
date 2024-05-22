import logging
from typing import TYPE_CHECKING, AbstractSet, Dict, Final, List, Optional, Tuple

import attr

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import Extra
else:
    from pydantic import Extra

from synapse.api.constants import Membership
from synapse.events import EventBase
from synapse.rest.client.models import SlidingSyncBody
from synapse.types import JsonMapping, Requester, RoomStreamToken, StreamToken, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# Everything except `Membership.LEAVE`
MEMBERSHIP_TO_DISPLAY_IN_SYNC = (
    Membership.INVITE,
    Membership.JOIN,
    Membership.KNOCK,
    Membership.BAN,
)


class SlidingSyncConfig(SlidingSyncBody):
    user: UserID
    device_id: str

    # Pydantic config
    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False
        # Allow custom types like `UserID` to be used in the model
        arbitrary_types_allowed = True


class OperationType:
    """Represents the operation types in a Sliding Sync window."""

    SYNC: Final = "SYNC"
    INSERT: Final = "INSERT"
    DELETE: Final = "DELETE"
    INVALIDATE: Final = "INVALIDATE"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingSyncResult:
    """
    Attributes:
        next_pos: The next position token in the sliding window to request (next_batch).
        lists: Sliding window API. A map of list key to list results.
        rooms: Room subscription API. A map of room ID to room subscription to room results.
        extensions: TODO
    """

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class RoomResult:
        """

        Attributes:
            name: Room name or calculated room name.
            avatar: Room avatar
            heroes: List of stripped membership events (containing `user_id` and optionally
                `avatar_url` and `displayname`) for the users used to calculate the room name.
            initial: Flag which is set when this is the first time the server is sending this
                data on this connection. Clients can use this flag to replace or update
                their local state. When there is an update, servers MUST omit this flag
                entirely and NOT send "initial":false as this is wasteful on bandwidth. The
                absence of this flag means 'false'.
            required_state: The current state of the room
            timeline: Latest events in the room. The last event is the most recent
            is_dm: Flag to specify whether the room is a direct-message room (most likely
                between two people).
            invite_state: Stripped state events. Same as `rooms.invite.$room_id.invite_state`
                in sync v2, absent on joined/left rooms
            prev_batch: A token that can be passed as a start parameter to the
                `/rooms/<room_id>/messages` API to retrieve earlier messages.
            limited: True if their are more events than fit between the given position and now.
                Sync again to get more.
            joined_count: The number of users with membership of join, including the client's
                own user ID. (same as sync `v2 m.joined_member_count`)
            invited_count: The number of users with membership of invite. (same as sync v2
                `m.invited_member_count`)
            notification_count: The total number of unread notifications for this room. (same
                as sync v2)
            highlight_count: The number of unread notifications for this room with the highlight
                flag set. (same as sync v2)
            num_live: The number of timeline events which have just occurred and are not historical.
                The last N events are 'live' and should be treated as such. This is mostly
                useful to determine whether a given @mention event should make a noise or not.
                Clients cannot rely solely on the absence of `initial: true` to determine live
                events because if a room not in the sliding window bumps into the window because
                of an @mention it will have `initial: true` yet contain a single live event
                (with potentially other old events in the timeline).
        """

        name: str
        avatar: Optional[str]
        heroes: Optional[List[EventBase]]
        initial: bool
        required_state: List[EventBase]
        timeline: List[EventBase]
        is_dm: bool
        invite_state: List[EventBase]
        prev_batch: StreamToken
        limited: bool
        joined_count: int
        invited_count: int
        notification_count: int
        highlight_count: int
        num_live: int

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class SlidingWindowList:
        """
        Attributes:
            count: The total number of entries in the list. Always present if this list
                is.
            ops: The sliding list operations to perform.
        """

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class Operation:
            """
            Attributes:
                op: The operation type to perform.
                range: Which index positions are affected by this operation. These are
                    both inclusive.
                room_ids: Which room IDs are affected by this operation. These IDs match
                    up to the positions in the `range`, so the last room ID in this list
                    matches the 9th index. The room data is held in a separate object.
            """

            op: OperationType
            range: Tuple[int, int]
            room_ids: List[str]

        count: int
        ops: List[Operation]

    next_pos: StreamToken
    lists: Dict[str, SlidingWindowList]
    rooms: List[RoomResult]
    extensions: JsonMapping

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        return bool(self.lists or self.rooms or self.extensions)


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs_config = hs.config
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
        timeout: int = 0,
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

        if timeout == 0 or from_token is None:
            now_token = self.event_sources.get_current_token()
            return await self.current_sync_for_user(
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
                sync_config.user,
                timeout,
                current_sync_callback,
                from_token=from_token,
            )

    async def current_sync_for_user(
        self,
        sync_config: SlidingSyncConfig,
        from_token: Optional[StreamToken] = None,
        to_token: StreamToken = None,
    ) -> SlidingSyncResult:
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

        logger.info("Sliding sync rooms for user %s: %s", user_id, room_id_set)

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        for list_key, list_config in sync_config.lists.items():
            # TODO: Apply filters
            filtered_room_ids = room_id_set
            # TODO: Apply sorts
            sorted_room_ids = sorted(filtered_room_ids)

            ops: List[SlidingSyncResult.SlidingWindowList.Operation] = []
            for range in list_config.ranges:
                ops.append(
                    {
                        "op": OperationType.SYNC,
                        "range": range,
                        "room_ids": sorted_room_ids[range[0] : range[1]],
                    }
                )

            lists[list_key] = SlidingSyncResult.SlidingWindowList(
                count=len(sorted_room_ids),
                ops=ops,
            )

        # TODO: sync_config.room_subscriptions

        return SlidingSyncResult(
            next_pos=to_token,
            lists=lists,
            rooms=[],
            extensions={},
        )

    async def get_sync_room_ids_for_user(
        self,
        user: UserID,
        from_token: Optional[StreamToken] = None,
        to_token: StreamToken = None,
    ) -> AbstractSet[str]:
        """
        Fetch room IDs that should be listed for this user in the sync response.

        We're looking for rooms that the user has not left (`invite`, `knock`, `join`,
        and `ban`) or newly_left rooms that are > `from_token` and <= `to_token`.
        """
        user_id = user.to_string()

        # First grab a current snapshot rooms for the user
        room_for_user_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            # We want to fetch any kind of membership (joined and left rooms) in order
            # to get the `stream_ordering` of the latest room membership event for the
            # user.
            #
            # We will filter out the rooms that the user has left below (see
            # `MEMBERSHIP_TO_DISPLAY_IN_SYNC`)
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
            if room_for_user.membership in MEMBERSHIP_TO_DISPLAY_IN_SYNC
        }

        # Find the stream_ordering of the latest room membership event which will mark
        # the spot we queried up to.
        max_stream_ordering_from_room_list = max(
            room_for_user.stream_ordering for room_for_user in room_for_user_list
        )

        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we can just straight-up return the room list (nothing has
        # changed)
        if max_stream_ordering_from_room_list <= to_token.room_key.stream:
            return sync_room_id_set

        # We assume the `from_token` is before or at-least equal to the `to_token`
        assert (
            from_token is None or from_token.room_key.stream <= to_token.room_key.stream
        ), f"{from_token.room_key.stream if from_token else None} <= {to_token.room_key.stream}"

        # We assume the `from_token`/`to_token` is before the `max_stream_ordering_from_room_list`
        assert (
            from_token is None
            or from_token.room_key.stream < max_stream_ordering_from_room_list
        ), f"{from_token.room_key.stream if from_token else None} < {max_stream_ordering_from_room_list}"
        assert (
            to_token.room_key.stream < max_stream_ordering_from_room_list
        ), f"{to_token.room_key.stream} < {max_stream_ordering_from_room_list}"

        # Since we fetched the users room list at some point in time after the from/to
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`.
        #
        # - 1) Add back newly_left rooms (> `from_token` and <= `to_token`)
        # - 2a) Remove rooms that the user joined after the `to_token`
        # - 2b) Add back rooms that the user left after the `to_token`
        membership_change_events = await self.store.get_membership_changes_for_user(
            user_id,
            # Start from the `from_token` if given, otherwise from the `to_token` so we
            # can still do the 2) fixups.
            from_key=from_token.room_key if from_token else to_token.room_key,
            # Fetch up to our membership snapshot
            to_key=RoomStreamToken(stream=max_stream_ordering_from_room_list),
            excluded_rooms=self.rooms_to_exclude_globally,
        )

        # Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: Dict[str, EventBase] = {}
        last_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        # We also need the first membership event after the `to_token` so we can step
        # backward to the previous membership that would apply to the from/to range.
        first_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        for event in membership_change_events:
            assert event.internal_metadata.stream_ordering

            if (
                event.internal_metadata.stream_ordering > from_token.room_key.stream
                and event.internal_metadata.stream_ordering <= to_token.room_key.stream
            ):
                last_membership_change_by_room_id_in_from_to_range[event.room_id] = (
                    event
                )
            elif (
                event.internal_metadata.stream_ordering > to_token.room_key.stream
                and event.internal_metadata.stream_ordering
                <= max_stream_ordering_from_room_list
            ):
                last_membership_change_by_room_id_after_to_token[event.room_id] = event
                # Only set if we haven't already set it
                first_membership_change_by_room_id_after_to_token.setdefault(
                    event.room_id, event
                )
            else:
                raise AssertionError(
                    "Membership event with stream_ordering=%s should fall in the given ranges above"
                    + " (%d > x <= %d) or (%d > x <= %d). We shouldn't be fetching extra membership"
                    + " events that aren't used.",
                    event.internal_metadata.stream_ordering,
                    from_token.room_key.stream,
                    to_token.room_key.stream,
                    to_token.room_key.stream,
                    max_stream_ordering_from_room_list,
                )

        # 1)
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            # 1) Add back newly_left rooms (> `from_token` and <= `to_token`). We
            # include newly_left rooms because the last event that the user should see
            # is their own leave event
            if event.membership == Membership.LEAVE:
                sync_room_id_set.add(last_membership_change_in_from_to_range.room_id)

        # 2)
        for (
            last_membership_change_after_to_token
        ) in last_membership_change_by_room_id_after_to_token.values():
            room_id = last_membership_change_after_to_token.room_id

            # We want to find the first membership change after the `to_token` then step
            # backward to know the membership in the from/to range.
            first_membership_change_after_to_token = (
                first_membership_change_by_room_id_after_to_token.get(room_id)
            )
            prev_content = first_membership_change_after_to_token.unsigned.get(
                "prev_content", {}
            )
            prev_membership = prev_content.get("membership", None)

            # 2a) Add back rooms that the user left after the `to_token`
            #
            # If the last membership event after the `to_token` is a leave event, then
            # the room was excluded from the
            # `get_rooms_for_local_user_where_membership_is()` results. We should add
            # these rooms back as long as the user was part of the room before the
            # `to_token`.
            if (
                last_membership_change_after_to_token.membership == Membership.LEAVE
                and prev_membership != None
                and prev_membership != Membership.LEAVE
            ):
                sync_room_id_set.add(room_id)
            # 2b) Remove rooms that the user joined (hasn't left) after the `to_token`
            #
            # If the last membership event after the `to_token` is a "join" event, then
            # the room was included in the `get_rooms_for_local_user_where_membership_is()`
            # results. We should remove these rooms as long as the user wasn't part of
            # the room before the `to_token`.
            elif (
                last_membership_change_after_to_token.membership != Membership.LEAVE
                and (prev_membership == None or prev_membership == Membership.LEAVE)
            ):
                sync_room_id_set.discard(room_id)

        return sync_room_id_set
