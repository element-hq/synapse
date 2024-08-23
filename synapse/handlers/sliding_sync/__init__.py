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
    AbstractSet,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import attr
from immutabledict import immutabledict
from prometheus_client import Histogram
from typing_extensions import assert_never

from synapse.api.constants import (
    AccountDataTypes,
    Direction,
    EventContentFields,
    EventTypes,
    Membership,
)
from synapse.events import EventBase, StrippedStateEvent
from synapse.events.utils import parse_stripped_state_event, strip_event
from synapse.handlers.relations import BundledAggregations
from synapse.handlers.sliding_sync.extensions import SlidingSyncExtensionHandler
from synapse.handlers.sliding_sync.store import SlidingSyncConnectionStore
from synapse.logging.opentracing import (
    SynapseTags,
    log_kv,
    set_tag,
    start_active_span,
    tag_args,
    trace,
)
from synapse.storage.databases.main.roommember import extract_heroes_from_room_summary
from synapse.storage.databases.main.state import (
    ROOM_UNKNOWN_SENTINEL,
    Sentinel as StateSentinel,
)
from synapse.storage.databases.main.stream import (
    CurrentStateDeltaMembership,
    PaginateFunction,
)
from synapse.storage.roommember import MemberSummary
from synapse.types import (
    JsonDict,
    MutableStateMap,
    PersistedEventPosition,
    Requester,
    RoomStreamToken,
    SlidingSyncStreamToken,
    StateMap,
    StrCollection,
    StreamKeyType,
    StreamToken,
    UserID,
)
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    MutablePerConnectionState,
    OperationType,
    PerConnectionState,
    RoomSyncConfig,
    SlidingSyncConfig,
    SlidingSyncResult,
    StateValues,
)
from synapse.types.state import StateFilter
from synapse.util.async_helpers import concurrently_execute
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


sync_processing_time = Histogram(
    "synapse_sliding_sync_processing_time",
    "Time taken to generate a sliding sync response, ignoring wait times.",
    ["initial"],
)


class Sentinel(enum.Enum):
    # defining a sentinel in this way allows mypy to correctly handle the
    # type of a dictionary lookup and subsequent type narrowing.
    UNSET_SENTINEL = object()


# The event types that clients should consider as new activity.
DEFAULT_BUMP_EVENT_TYPES = {
    EventTypes.Create,
    EventTypes.Message,
    EventTypes.Encrypted,
    EventTypes.Sticker,
    EventTypes.CallInvite,
    EventTypes.PollStart,
    EventTypes.LiveLocationShareStart,
}


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
        self.is_mine_id = hs.is_mine_id

        self.connection_store = SlidingSyncConnectionStore(self.store)
        self.extensions = SlidingSyncExtensionHandler(hs)

    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SlidingSyncConfig,
        from_token: Optional[SlidingSyncStreamToken] = None,
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

        # If we're working with a user-provided token, we need to make sure to wait for
        # this worker to catch up with the token so we don't skip past any incoming
        # events or future events if the user is nefariously, manually modifying the
        # token.
        if from_token is not None:
            # We need to make sure this worker has caught up with the token. If
            # this returns false, it means we timed out waiting, and we should
            # just return an empty response.
            before_wait_ts = self.clock.time_msec()
            if not await self.notifier.wait_for_stream_token(from_token.stream_token):
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
                from_token=from_token.stream_token,
            )

        return result

    @trace
    async def current_sync_for_user(
        self,
        sync_config: SlidingSyncConfig,
        to_token: StreamToken,
        from_token: Optional[SlidingSyncStreamToken] = None,
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
        start_time_s = self.clock.time()

        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        # Get the per-connection state (if any).
        #
        # Raises an exception if there is a `connection_position` that we don't
        # recognize. If we don't do this and the client asks for the full range
        # of rooms, we end up sending down all rooms and their state from
        # scratch (which can be very slow). By expiring the connection we allow
        # the client a chance to do an initial request with a smaller range of
        # rooms to get them some results sooner but will end up taking the same
        # amount of time (more with round-trips and re-processing) in the end to
        # get everything again.
        previous_connection_state = (
            await self.connection_store.get_per_connection_state(
                sync_config, from_token
            )
        )
        new_connection_state = previous_connection_state.get_mutable()

        # Get all of the room IDs that the user should be able to see in the sync
        # response
        has_lists = sync_config.lists is not None and len(sync_config.lists) > 0
        has_room_subscriptions = (
            sync_config.room_subscriptions is not None
            and len(sync_config.room_subscriptions) > 0
        )
        if has_lists or has_room_subscriptions:
            room_membership_for_user_map = (
                await self.get_room_membership_for_user_at_to_token(
                    user=sync_config.user,
                    to_token=to_token,
                    from_token=from_token.stream_token if from_token else None,
                )
            )

        lists_to_rooms: Mapping[str, AbstractSet[str]] = {}
        if previous_connection_state is not None:
            lists_to_rooms = previous_connection_state.list_to_rooms

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        # Keep track of the rooms that we can display and need to fetch more info about
        relevant_room_map: Dict[str, RoomSyncConfig] = {}
        # The set of room IDs of all rooms that could appear in any list. These
        # include rooms that are outside the list ranges.
        all_rooms: Set[str] = set()
        if has_lists and sync_config.lists is not None:
            with start_active_span("assemble_sliding_window_lists"):
                sync_room_map = await self.filter_rooms_relevant_for_sync(
                    user=sync_config.user,
                    room_membership_for_user_map=room_membership_for_user_map,
                )

                for list_key, list_config in sync_config.lists.items():
                    # Apply filters
                    previous_found_rooms = lists_to_rooms.get(list_key)
                    if previous_found_rooms:
                        filtered_sync_room_map = {
                            room_id: sync_room_map[room_id]
                            for room_id in previous_found_rooms
                        }

                        # TODO: Record changes to the list.
                    else:
                        filtered_sync_room_map = sync_room_map
                        if list_config.filters is not None:
                            filtered_sync_room_map = await self.filter_rooms(
                                sync_config.user,
                                sync_room_map,
                                list_config.filters,
                                to_token,
                            )

                        new_connection_state.list_to_rooms[list_key] = set(
                            filtered_sync_room_map.keys()
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

        # Handle room subscriptions
        if has_room_subscriptions and sync_config.room_subscriptions is not None:
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

        # Fetch room data
        rooms: Dict[str, SlidingSyncResult.RoomResult] = {}

        # Filter out rooms that haven't received updates and we've sent down
        # previously.
        # Keep track of the rooms that we're going to display and need to fetch more info about
        relevant_rooms_to_send_map = relevant_room_map
        with start_active_span("filter_relevant_rooms_to_send"):
            if from_token:
                rooms_should_send = set()

                # First we check if there are rooms that match a list/room
                # subscription and have updates we need to send (i.e. either because
                # we haven't sent the room down, or we have but there are missing
                # updates).
                for room_id, room_config in relevant_room_map.items():
                    prev_room_sync_config = previous_connection_state.room_configs.get(
                        room_id
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
                rooms_that_have_updates = self.store.get_rooms_that_might_have_updates(
                    relevant_room_map.keys(), from_token.stream_token.room_key
                )
                rooms_should_send.update(rooms_that_have_updates)
                relevant_rooms_to_send_map = {
                    room_id: room_sync_config
                    for room_id, room_sync_config in relevant_room_map.items()
                    if room_id in rooms_should_send
                }

        @trace
        @tag_args
        async def handle_room(room_id: str) -> None:
            room_sync_result = await self.get_room_sync_data(
                sync_config=sync_config,
                previous_connection_state=previous_connection_state,
                new_connection_state=new_connection_state,
                room_id=room_id,
                room_sync_config=relevant_rooms_to_send_map[room_id],
                room_membership_for_user_at_to_token=room_membership_for_user_map[
                    room_id
                ],
                from_token=from_token,
                to_token=to_token,
            )

            # Filter out empty room results during incremental sync
            if room_sync_result or not from_token:
                rooms[room_id] = room_sync_result

        if relevant_rooms_to_send_map:
            with start_active_span("sliding_sync.generate_room_entries"):
                await concurrently_execute(handle_room, relevant_rooms_to_send_map, 10)

        extensions = await self.extensions.get_extensions_response(
            sync_config=sync_config,
            actual_lists=lists,
            previous_connection_state=previous_connection_state,
            new_connection_state=new_connection_state,
            # We're purposely using `relevant_room_map` instead of
            # `relevant_rooms_to_send_map` here. This needs to be all room_ids we could
            # send regardless of whether they have an event update or not. The
            # extensions care about more than just normal events in the rooms (like
            # account data, read receipts, typing indicators, to-device messages, etc).
            actual_room_ids=set(relevant_room_map.keys()),
            actual_room_response_map=rooms,
            from_token=from_token,
            to_token=to_token,
        )

        if has_lists or has_room_subscriptions:
            # We now calculate if any rooms outside the range have had updates,
            # which we are not sending down.
            #
            # We *must* record rooms that have had updates, but it is also fine
            # to record rooms as having updates even if there might not actually
            # be anything new for the user (e.g. due to event filters, events
            # having happened after the user left, etc).
            unsent_room_ids = []
            if from_token:
                # The set of rooms that the client (may) care about, but aren't
                # in any list range (or subscribed to).
                missing_rooms = all_rooms - relevant_room_map.keys()

                # We now just go and try fetching any events in the above rooms
                # to see if anything has happened since the `from_token`.
                #
                # TODO: Replace this with something faster. When we land the
                # sliding sync tables that record the most recent event
                # positions we can use that.
                missing_event_map_by_room = (
                    await self.store.get_room_events_stream_for_rooms(
                        room_ids=missing_rooms,
                        from_key=to_token.room_key,
                        to_key=from_token.stream_token.room_key,
                        limit=1,
                    )
                )
                unsent_room_ids = list(missing_event_map_by_room)

                new_connection_state.rooms.record_unsent_rooms(
                    unsent_room_ids, from_token.stream_token.room_key
                )

            new_connection_state.rooms.record_sent_rooms(
                relevant_rooms_to_send_map.keys()
            )

            connection_position = await self.connection_store.record_new_state(
                sync_config=sync_config,
                from_token=from_token,
                new_connection_state=new_connection_state,
            )
        elif from_token:
            connection_position = from_token.connection_position
        else:
            # Initial sync without a `from_token` starts at `0`
            connection_position = 0

        sliding_sync_result = SlidingSyncResult(
            next_pos=SlidingSyncStreamToken(to_token, connection_position),
            lists=lists,
            rooms=rooms,
            extensions=extensions,
        )

        # Make it easy to find traces for syncs that aren't empty
        set_tag(SynapseTags.RESULT_PREFIX + "result", bool(sliding_sync_result))
        set_tag(SynapseTags.FUNC_ARG_PREFIX + "sync_config.user", user_id)

        end_time_s = self.clock.time()
        sync_processing_time.labels(from_token is not None).observe(
            end_time_s - start_time_s
        )

        return sliding_sync_result

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

    @trace
    async def get_current_state_ids_at(
        self,
        room_id: str,
        room_membership_for_user_at_to_token: _RoomMembershipForUser,
        state_filter: StateFilter,
        to_token: StreamToken,
    ) -> StateMap[str]:
        """
        Get current state IDs for the user in the room according to their membership. This
        will be the current state at the time of their LEAVE/BAN, otherwise will be the
        current state <= to_token.

        Args:
            room_id: The room ID to fetch data for
            room_membership_for_user_at_token: Membership information for the user
                in the room at the time of `to_token`.
            to_token: The point in the stream to sync up to.
        """
        state_ids: StateMap[str]
        # People shouldn't see past their leave/ban event
        if room_membership_for_user_at_to_token.membership in (
            Membership.LEAVE,
            Membership.BAN,
        ):
            # TODO: `get_state_ids_at(...)` doesn't take into account the "current
            # state". Maybe we need to use
            # `get_forward_extremities_for_room_at_stream_ordering(...)` to "Fetch the
            # current state at the time."
            state_ids = await self.storage_controllers.state.get_state_ids_at(
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
            state_ids = await self.storage_controllers.state.get_current_state_ids(
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

        return state_ids

    @trace
    async def get_current_state_at(
        self,
        room_id: str,
        room_membership_for_user_at_to_token: _RoomMembershipForUser,
        state_filter: StateFilter,
        to_token: StreamToken,
    ) -> StateMap[EventBase]:
        """
        Get current state for the user in the room according to their membership. This
        will be the current state at the time of their LEAVE/BAN, otherwise will be the
        current state <= to_token.

        Args:
            room_id: The room ID to fetch data for
            room_membership_for_user_at_token: Membership information for the user
                in the room at the time of `to_token`.
            to_token: The point in the stream to sync up to.
        """
        state_ids = await self.get_current_state_ids_at(
            room_id=room_id,
            room_membership_for_user_at_to_token=room_membership_for_user_at_to_token,
            state_filter=state_filter,
            to_token=to_token,
        )

        event_map = await self.store.get_events(list(state_ids.values()))

        state_map = {}
        for key, event_id in state_ids.items():
            event = event_map.get(event_id)
            if event:
                state_map[key] = event

        return state_map

    async def get_room_sync_data(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        room_id: str,
        room_sync_config: RoomSyncConfig,
        room_membership_for_user_at_to_token: _RoomMembershipForUser,
        from_token: Optional[SlidingSyncStreamToken],
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
        user = sync_config.user

        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "membership",
            room_membership_for_user_at_to_token.membership,
        )
        set_tag(
            SynapseTags.FUNC_ARG_PREFIX + "timeline_limit",
            room_sync_config.timeline_limit,
        )

        # Determine whether we should limit the timeline to the token range.
        #
        # We should return historical messages (before token range) in the
        # following cases because we want clients to be able to show a basic
        # screen of information:
        #
        #  - Initial sync (because no `from_token` to limit us anyway)
        #  - When users `newly_joined`
        #  - For an incremental sync where we haven't sent it down this
        #    connection before
        #
        # Relevant spec issue:
        # https://github.com/matrix-org/matrix-spec/issues/1917
        #
        # XXX: Odd behavior - We also check if the `timeline_limit` has increased, if so
        # we ignore the from bound for the timeline to send down a larger chunk of
        # history and set `unstable_expanded_timeline` to true. This is only being added
        # to match the behavior of the Sliding Sync proxy as we expect the ElementX
        # client to feel a certain way and be able to trickle in a full page of timeline
        # messages to fill up the screen. This is a bit different to the behavior of the
        # Sliding Sync proxy (which sets initial=true, but then doesn't send down the
        # full state again), but existing apps, e.g. ElementX, just need `limited` set.
        # We don't explicitly set `limited` but this will be the case for any room that
        # has more history than we're trying to pull out. Using
        # `unstable_expanded_timeline` allows us to avoid contaminating what `initial`
        # or `limited` mean for clients that interpret them correctly. In future this
        # behavior is almost certainly going to change.
        #
        # TODO: Also handle changes to `required_state`
        from_bound = None
        initial = True
        ignore_timeline_bound = False
        if from_token and not room_membership_for_user_at_to_token.newly_joined:
            room_status = previous_connection_state.rooms.have_sent_room(room_id)
            if room_status.status == HaveSentRoomFlag.LIVE:
                from_bound = from_token.stream_token.room_key
                initial = False
            elif room_status.status == HaveSentRoomFlag.PREVIOUSLY:
                assert room_status.last_token is not None
                from_bound = room_status.last_token
                initial = False
            elif room_status.status == HaveSentRoomFlag.NEVER:
                from_bound = None
                initial = True
            else:
                assert_never(room_status.status)

            log_kv({"sliding_sync.room_status": room_status})

            prev_room_sync_config = previous_connection_state.room_configs.get(room_id)
            if prev_room_sync_config is not None:
                # Check if the timeline limit has increased, if so ignore the
                # timeline bound and record the change (see "XXX: Odd behavior"
                # above).
                if (
                    prev_room_sync_config.timeline_limit
                    < room_sync_config.timeline_limit
                ):
                    ignore_timeline_bound = True

                # TODO: Check for changes in `required_state``

        log_kv(
            {
                "sliding_sync.from_bound": from_bound,
                "sliding_sync.initial": initial,
                "sliding_sync.ignore_timeline_bound": ignore_timeline_bound,
            }
        )

        # Assemble the list of timeline events
        #
        # FIXME: It would be nice to make the `rooms` response more uniform regardless of
        # membership. Currently, we have to make all of these optional because
        # `invite`/`knock` rooms only have `stripped_state`. See
        # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1653045932
        timeline_events: List[EventBase] = []
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
            to_bound = to_token.room_key
            # People shouldn't see past their leave/ban event
            if room_membership_for_user_at_to_token.membership in (
                Membership.LEAVE,
                Membership.BAN,
            ):
                to_bound = (
                    room_membership_for_user_at_to_token.event_pos.to_room_stream_token()
                )

            timeline_from_bound = from_bound
            if ignore_timeline_bound:
                timeline_from_bound = None

            # For initial `/sync` (and other historical scenarios mentioned above), we
            # want to view a historical section of the timeline; to fetch events by
            # `topological_ordering` (best representation of the room DAG as others were
            # seeing it at the time). This also aligns with the order that `/messages`
            # returns events in.
            #
            # For incremental `/sync`, we want to get all updates for rooms since
            # the last `/sync` (regardless if those updates arrived late or happened
            # a while ago in the past); to fetch events by `stream_ordering` (in the
            # order they were received by the server).
            #
            # Relevant spec issue: https://github.com/matrix-org/matrix-spec/issues/1917
            #
            # FIXME: Using workaround for mypy,
            # https://github.com/python/mypy/issues/10740#issuecomment-1997047277 and
            # https://github.com/python/mypy/issues/17479
            paginate_room_events_by_topological_ordering: PaginateFunction = (
                self.store.paginate_room_events_by_topological_ordering
            )
            paginate_room_events_by_stream_ordering: PaginateFunction = (
                self.store.paginate_room_events_by_stream_ordering
            )
            pagination_method: PaginateFunction = (
                # Use `topographical_ordering` for historical events
                paginate_room_events_by_topological_ordering
                if timeline_from_bound is None
                # Use `stream_ordering` for updates
                else paginate_room_events_by_stream_ordering
            )
            timeline_events, new_room_key = await pagination_method(
                room_id=room_id,
                # The bounds are reversed so we can paginate backwards
                # (from newer to older events) starting at to_bound.
                # This ensures we fill the `limit` with the newest events first,
                from_key=to_bound,
                to_key=timeline_from_bound,
                direction=Direction.BACKWARDS,
                # We add one so we can determine if there are enough events to saturate
                # the limit or not (see `limited`)
                limit=room_sync_config.timeline_limit + 1,
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
                    if persisted_position.persisted_after(
                        from_token.stream_token.room_key
                    ):
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
        # `room_membership_for_user_at_to_token.event_id=None and
        # room_membership_for_user_at_to_token.membership is not None`, we should
        # indicate to the client that a state reset happened. Perhaps we should indicate
        # this by setting `initial: True` and empty `required_state`.

        # Check whether the room has a name set
        name_state_ids = await self.get_current_state_ids_at(
            room_id=room_id,
            room_membership_for_user_at_to_token=room_membership_for_user_at_to_token,
            state_filter=StateFilter.from_types([(EventTypes.Name, "")]),
            to_token=to_token,
        )
        name_event_id = name_state_ids.get((EventTypes.Name, ""))

        room_membership_summary: Mapping[str, MemberSummary]
        empty_membership_summary = MemberSummary([], 0)
        if room_membership_for_user_at_to_token.membership in (
            Membership.LEAVE,
            Membership.BAN,
        ):
            # TODO: Figure out how to get the membership summary for left/banned rooms
            room_membership_summary = {}
        else:
            room_membership_summary = await self.store.get_room_summary(room_id)
            # TODO: Reverse/rewind back to the `to_token`

        # `heroes` are required if the room name is not set.
        #
        # Note: When you're the first one on your server to be invited to a new room
        # over federation, we only have access to some stripped state in
        # `event.unsigned.invite_room_state` which currently doesn't include `heroes`,
        # see https://github.com/matrix-org/matrix-spec/issues/380. This means that
        # clients won't be able to calculate the room name when necessary and just a
        # pitfall we have to deal with until that spec issue is resolved.
        hero_user_ids: List[str] = []
        # TODO: Should we also check for `EventTypes.CanonicalAlias`
        # (`m.room.canonical_alias`) as a fallback for the room name? see
        # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1671260153
        if name_event_id is None:
            hero_user_ids = extract_heroes_from_room_summary(
                room_membership_summary, me=user.to_string()
            )

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
        required_state_filter = StateFilter.none()
        if room_membership_for_user_at_to_token.membership not in (
            Membership.INVITE,
            Membership.KNOCK,
        ):
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
                set_tag(
                    SynapseTags.FUNC_ARG_PREFIX + "required_state_wildcard",
                    True,
                )
                required_state_filter = StateFilter.all()
            # TODO: `StateFilter` currently doesn't support wildcard event types. We're
            # currently working around this by returning all state to the client but it
            # would be nice to fetch less from the database and return just what the
            # client wanted.
            elif (
                room_sync_config.required_state_map.get(StateValues.WILDCARD)
                is not None
            ):
                set_tag(
                    SynapseTags.FUNC_ARG_PREFIX + "required_state_wildcard_event_type",
                    True,
                )
                required_state_filter = StateFilter.all()
            else:
                required_state_types: List[Tuple[str, Optional[str]]] = []
                for (
                    state_type,
                    state_key_set,
                ) in room_sync_config.required_state_map.items():
                    num_wild_state_keys = 0
                    lazy_load_room_members = False
                    num_others = 0
                    for state_key in state_key_set:
                        if state_key == StateValues.WILDCARD:
                            num_wild_state_keys += 1
                            # `None` is a wildcard in the `StateFilter`
                            required_state_types.append((state_type, None))
                        # We need to fetch all relevant people when we're lazy-loading membership
                        elif (
                            state_type == EventTypes.Member
                            and state_key == StateValues.LAZY
                        ):
                            lazy_load_room_members = True
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
                        elif state_key == StateValues.ME:
                            num_others += 1
                            required_state_types.append((state_type, user.to_string()))
                        else:
                            num_others += 1
                            required_state_types.append((state_type, state_key))

                    set_tag(
                        SynapseTags.FUNC_ARG_PREFIX
                        + "required_state_wildcard_state_key_count",
                        num_wild_state_keys,
                    )
                    set_tag(
                        SynapseTags.FUNC_ARG_PREFIX + "required_state_lazy",
                        lazy_load_room_members,
                    )
                    set_tag(
                        SynapseTags.FUNC_ARG_PREFIX + "required_state_other_count",
                        num_others,
                    )

                required_state_filter = StateFilter.from_types(required_state_types)

        # We need this base set of info for the response so let's just fetch it along
        # with the `required_state` for the room
        meta_room_state = [(EventTypes.Name, ""), (EventTypes.RoomAvatar, "")] + [
            (EventTypes.Member, hero_user_id) for hero_user_id in hero_user_ids
        ]
        state_filter = StateFilter.all()
        if required_state_filter != StateFilter.all():
            state_filter = StateFilter(
                types=StateFilter.from_types(
                    chain(meta_room_state, required_state_filter.to_types())
                ).types,
                include_others=required_state_filter.include_others,
            )

        # We can return all of the state that was requested if this was the first
        # time we've sent the room down this connection.
        room_state: StateMap[EventBase] = {}
        if initial:
            room_state = await self.get_current_state_at(
                room_id=room_id,
                room_membership_for_user_at_to_token=room_membership_for_user_at_to_token,
                state_filter=state_filter,
                to_token=to_token,
            )
        else:
            assert from_bound is not None

            # TODO: Limit the number of state events we're about to send down
            # the room, if its too many we should change this to an
            # `initial=True`?
            deltas = await self.store.get_current_state_deltas_for_room(
                room_id=room_id,
                from_token=from_bound,
                to_token=to_token.room_key,
            )
            # TODO: Filter room state before fetching events
            # TODO: Handle state resets where event_id is None
            events = await self.store.get_events(
                [d.event_id for d in deltas if d.event_id]
            )
            room_state = {(s.type, s.state_key): s for s in events.values()}

        required_room_state: StateMap[EventBase] = {}
        if required_state_filter != StateFilter.none():
            required_room_state = required_state_filter.filter_state(room_state)

        # Find the room name and avatar from the state
        room_name: Optional[str] = None
        # TODO: Should we also check for `EventTypes.CanonicalAlias`
        # (`m.room.canonical_alias`) as a fallback for the room name? see
        # https://github.com/matrix-org/matrix-spec-proposals/pull/3575#discussion_r1671260153
        name_event = room_state.get((EventTypes.Name, ""))
        if name_event is not None:
            room_name = name_event.content.get("name")

        room_avatar: Optional[str] = None
        avatar_event = room_state.get((EventTypes.RoomAvatar, ""))
        if avatar_event is not None:
            room_avatar = avatar_event.content.get("url")

        # Assemble heroes: extract the info from the state we just fetched
        heroes: List[SlidingSyncResult.RoomResult.StrippedHero] = []
        for hero_user_id in hero_user_ids:
            member_event = room_state.get((EventTypes.Member, hero_user_id))
            if member_event is not None:
                heroes.append(
                    SlidingSyncResult.RoomResult.StrippedHero(
                        user_id=hero_user_id,
                        display_name=member_event.content.get("displayname"),
                        avatar_url=member_event.content.get("avatar_url"),
                    )
                )

        # Figure out the last bump event in the room
        last_bump_event_result = (
            await self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id, to_token.room_key, event_types=DEFAULT_BUMP_EVENT_TYPES
            )
        )

        # By default, just choose the membership event position
        bump_stamp = room_membership_for_user_at_to_token.event_pos.stream
        # But if we found a bump event, use that instead
        if last_bump_event_result is not None:
            _, new_bump_event_pos = last_bump_event_result

            # If we've just joined a remote room, then the last bump event may
            # have been backfilled (and so have a negative stream ordering).
            # These negative stream orderings can't sensibly be compared, so
            # instead we use the membership event position.
            if new_bump_event_pos.stream > 0:
                bump_stamp = new_bump_event_pos.stream

        unstable_expanded_timeline = False
        prev_room_sync_config = previous_connection_state.room_configs.get(room_id)
        # Record the `room_sync_config` if we're `ignore_timeline_bound` (which means
        # that the `timeline_limit` has increased)
        if ignore_timeline_bound:
            # FIXME: We signal the fact that we're sending down more events to
            # the client by setting `unstable_expanded_timeline` to true (see
            # "XXX: Odd behavior" above).
            unstable_expanded_timeline = True

            new_connection_state.room_configs[room_id] = RoomSyncConfig(
                timeline_limit=room_sync_config.timeline_limit,
                required_state_map=room_sync_config.required_state_map,
            )
        elif prev_room_sync_config is not None:
            # If the result is `limited` then we need to record that the
            # `timeline_limit` has been reduced, as when/if the client later requests
            # more timeline then we have more data to send.
            #
            # Otherwise (when not `limited`) we don't need to record that the
            # `timeline_limit` has been reduced, as the *effective* `timeline_limit`
            # (i.e. the amount of timeline we have previously sent to the client) is at
            # least the previous `timeline_limit`.
            #
            # This is to handle the case where the `timeline_limit` e.g. goes from 10 to
            # 5 to 10 again (without any timeline gaps), where there's no point sending
            # down the initial historical chunk events when the `timeline_limit` is
            # increased as the client already has the 10 previous events. However, if
            # client has a gap in the timeline (i.e. `limited` is True), then we *do*
            # need to record the reduced timeline.
            #
            # TODO: Handle timeline gaps (`get_timeline_gaps()`) - This is separate from
            # the gaps we might see on the client because a response was `limited` we're
            # talking about above.
            if (
                limited
                and prev_room_sync_config.timeline_limit
                > room_sync_config.timeline_limit
            ):
                new_connection_state.room_configs[room_id] = RoomSyncConfig(
                    timeline_limit=room_sync_config.timeline_limit,
                    required_state_map=room_sync_config.required_state_map,
                )

            # TODO: Record changes in required_state.

        else:
            new_connection_state.room_configs[room_id] = room_sync_config

        set_tag(SynapseTags.RESULT_PREFIX + "initial", initial)

        return SlidingSyncResult.RoomResult(
            name=room_name,
            avatar=room_avatar,
            heroes=heroes,
            is_dm=room_membership_for_user_at_to_token.is_dm,
            initial=initial,
            required_state=list(required_room_state.values()),
            timeline_events=timeline_events,
            bundled_aggregations=bundled_aggregations,
            stripped_state=stripped_state,
            prev_batch=prev_batch_token,
            limited=limited,
            unstable_expanded_timeline=unstable_expanded_timeline,
            num_live=num_live,
            bump_stamp=bump_stamp,
            joined_count=room_membership_summary.get(
                Membership.JOIN, empty_membership_summary
            ).count,
            invited_count=room_membership_summary.get(
                Membership.INVITE, empty_membership_summary
            ).count,
            # TODO: These are just dummy values. We could potentially just remove these
            # since notifications can only really be done correctly on the client anyway
            # (encrypted rooms).
            notification_count=0,
            highlight_count=0,
        )
