import logging
from enum import Enum
from typing import TYPE_CHECKING, AbstractSet, Dict, Final, List, Optional, Tuple

import attr
from immutabledict import immutabledict

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


# Everything except `Membership.LEAVE` because we want everything that's *still*
# relevant to the user.
MEMBERSHIP_TO_DISPLAY_IN_SYNC = (
    Membership.INVITE,
    Membership.JOIN,
    Membership.KNOCK,
    Membership.BAN,
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

    return (
        membership in MEMBERSHIP_TO_DISPLAY_IN_SYNC
        # Include kicks
        or (membership == Membership.LEAVE and sender != user_id)
    )


class SlidingSyncConfig(SlidingSyncBody):
    """
    Inherit from `SlidingSyncBody` since we need all of the same fields and add a few
    extra fields that we need in the handler
    """

    user: UserID
    device_id: Optional[str]

    # Pydantic config
    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False
        # Allow custom types like `UserID` to be used in the model
        arbitrary_types_allowed = True


class OperationType(Enum):
    """
    Represents the operation types in a Sliding Sync window.

    Attributes:
        SYNC: Sets a range of entries. Clients SHOULD discard what they previous knew about
            entries in this range.
        INSERT: Sets a single entry. If the position is not empty then clients MUST move
            entries to the left or the right depending on where the closest empty space is.
        DELETE: Remove a single entry. Often comes before an INSERT to allow entries to move
            places.
        INVALIDATE: Remove a range of entries. Clients MAY persist the invalidated range for
            offline support, but they should be treated as empty when additional operations
            which concern indexes in the range arrive from the server.
    """

    SYNC: Final = "SYNC"
    INSERT: Final = "INSERT"
    DELETE: Final = "DELETE"
    INVALIDATE: Final = "INVALIDATE"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingSyncResult:
    """
    The Sliding Sync result to be serialized to JSON for a response.

    Attributes:
        next_pos: The next position token in the sliding window to request (next_batch).
        lists: Sliding window API. A map of list key to list results.
        rooms: Room subscription API. A map of room ID to room subscription to room results.
        extensions: Extensions API. A map of extension key to extension results.
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
    rooms: Dict[str, RoomResult]
    extensions: JsonMapping

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        return bool(self.lists or self.rooms or self.extensions)

    @staticmethod
    def empty(next_pos: StreamToken) -> "SlidingSyncResult":
        "Return a new empty result"
        return SlidingSyncResult(
            next_pos=next_pos,
            lists={},
            rooms={},
            extensions={},
        )


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs_config = hs.config
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

        # If the we're working with a user-provided token, we need to make sure to wait
        # for this worker to catch up with the token.
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

        We're looking for rooms that the user has not left (`invite`, `knock`, `join`,
        and `ban`), or kicks (`leave` where the `sender` is different from the
        `state_key`), or newly_left rooms that are > `from_token` and <= `to_token`.
        """
        user_id = user.to_string()

        # First grab a current snapshot rooms for the user
        room_for_user_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            # We want to fetch any kind of membership (joined and left rooms) in order
            # to get the `event_pos` of the latest room membership event for the
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
            if filter_membership_for_sync(
                membership=room_for_user.membership,
                user_id=user_id,
                sender=room_for_user.sender,
            )
        }

        # Get the `RoomStreamToken` that represents the spot we queried up to when we got
        # our membership snapshot from `get_rooms_for_local_user_where_membership_is()`.
        #
        # First we need to get the max stream_ordering of each event persister instance
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
            stream=min(
                stream_ordering
                for stream_ordering in instance_to_max_stream_ordering_map.values()
            ),
            instance_map=immutabledict(instance_to_max_stream_ordering_map),
        )

        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we can just straight-up return the room list (nothing has
        # changed)
        if membership_snapshot_token.is_before_or_eq(to_token.room_key):
            return sync_room_id_set

        # We assume the `from_token` is before or at-least equal to the `to_token`
        assert from_token is None or from_token.room_key.is_before_or_eq(
            to_token.room_key
        ), f"{from_token.room_key if from_token else None} < {to_token.room_key}"

        # We assume the `from_token`/`to_token` is before the `max_stream_ordering_from_room_list`
        assert from_token is None or from_token.room_key.is_before_or_eq(
            membership_snapshot_token
        ), f"{from_token.room_key if from_token else None} < {membership_snapshot_token}"
        assert to_token.room_key.is_before_or_eq(
            membership_snapshot_token
        ), f"{to_token.room_key} < {membership_snapshot_token}"

        # Since we fetched the users room list at some point in time after the from/to
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`. In particular, we need to make these fixups:
        #
        # - 1) Add back newly_left rooms (> `from_token` and <= `to_token`)
        # - 2a) Remove rooms that the user joined after the `to_token`
        # - 2b) Add back rooms that the user left after the `to_token`
        #
        # We're doing two separate lookups for membership changes. We could request
        # everything for both fixups in one range, [`from_token.room_key`,
        # `membership_snapshot_token`), but we want to avoid raw `stream_ordering`
        # comparison without `instance_name` (which is flawed). We could refactor
        # `event.internal_metadata` to include `instance_name` but it might turn out a
        # little difficult and a bigger, broader Synapse change than we want to make.

        # 1) -----------------------------------------------------

        # 1) Fetch membership changes that fall in the range from `from_token` up to `to_token`
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

        # 1) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: Dict[str, EventBase] = {}
        for event in membership_change_events_in_from_to_range:
            assert event.internal_metadata.stream_ordering
            last_membership_change_by_room_id_in_from_to_range[event.room_id] = event

        # 1) Fixup
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            room_id = last_membership_change_in_from_to_range.room_id

            # 1) Add back newly_left rooms (> `from_token` and <= `to_token`). We
            # include newly_left rooms because the last event that the user should see
            # is their own leave event
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                sync_room_id_set.add(room_id)

        # 2) -----------------------------------------------------

        # 2) Fetch membership changes that fall in the range from `to_token` up to
        # `membership_snapshot_token`
        membership_change_events_after_to_token = (
            await self.store.get_membership_changes_for_user(
                user_id,
                from_key=to_token.room_key,
                to_key=membership_snapshot_token,
                excluded_rooms=self.rooms_to_exclude_globally,
            )
        )

        # 2) Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        # We also need the first membership event after the `to_token` so we can step
        # backward to the previous membership that would apply to the from/to range.
        first_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        for event in membership_change_events_after_to_token:
            assert event.internal_metadata.stream_ordering

            last_membership_change_by_room_id_after_to_token[event.room_id] = event
            # Only set if we haven't already set it
            first_membership_change_by_room_id_after_to_token.setdefault(
                event.room_id, event
            )

        # 2) Fixup
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

            # Check if the last membership (membership that applies to our snapshot) was
            # already included in our `sync_room_id_set`
            was_last_membership_already_included = filter_membership_for_sync(
                membership=last_membership_change_after_to_token.membership,
                user_id=user_id,
                sender=last_membership_change_after_to_token.sender,
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

            # 2a) Add back rooms that the user left after the `to_token`
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
            # 2b) Remove rooms that the user joined (hasn't left) after the `to_token`
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

        return sync_room_id_set
