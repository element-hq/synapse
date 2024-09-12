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
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Set, Tuple

from prometheus_client import Histogram
from typing_extensions import assert_never

from synapse.api.constants import Direction, EventTypes, Membership
from synapse.events import EventBase
from synapse.events.utils import strip_event
from synapse.handlers.relations import BundledAggregations
from synapse.handlers.sliding_sync.extensions import SlidingSyncExtensionHandler
from synapse.handlers.sliding_sync.room_lists import (
    RoomsForUserType,
    SlidingSyncRoomLists,
)
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
from synapse.storage.databases.main.stream import PaginateFunction
from synapse.storage.roommember import (
    MemberSummary,
)
from synapse.types import (
    JsonDict,
    MutableStateMap,
    PersistedEventPosition,
    Requester,
    SlidingSyncStreamToken,
    StateMap,
    StreamKeyType,
    StreamToken,
)
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    MutablePerConnectionState,
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
        self.room_lists = SlidingSyncRoomLists(hs)

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
            await self.connection_store.get_and_clear_connection_positions(
                sync_config, from_token
            )
        )

        # Get all of the room IDs that the user should be able to see in the sync
        # response
        has_lists = sync_config.lists is not None and len(sync_config.lists) > 0
        has_room_subscriptions = (
            sync_config.room_subscriptions is not None
            and len(sync_config.room_subscriptions) > 0
        )

        interested_rooms = await self.room_lists.compute_interested_rooms(
            sync_config=sync_config,
            previous_connection_state=previous_connection_state,
            from_token=from_token.stream_token if from_token else None,
            to_token=to_token,
        )

        lists = interested_rooms.lists
        relevant_room_map = interested_rooms.relevant_room_map
        all_rooms = interested_rooms.all_rooms
        room_membership_for_user_map = interested_rooms.room_membership_for_user_map
        relevant_rooms_to_send_map = interested_rooms.relevant_rooms_to_send_map

        # Fetch room data
        rooms: Dict[str, SlidingSyncResult.RoomResult] = {}

        new_connection_state = previous_connection_state.get_mutable()

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
                newly_joined=room_id in interested_rooms.newly_joined_rooms,
                is_dm=room_id in interested_rooms.dm_room_ids,
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
    async def get_current_state_ids_at(
        self,
        room_id: str,
        room_membership_for_user_at_to_token: RoomsForUserType,
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
        room_membership_for_user_at_to_token: RoomsForUserType,
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

    @trace
    async def get_room_sync_data(
        self,
        sync_config: SlidingSyncConfig,
        previous_connection_state: "PerConnectionState",
        new_connection_state: "MutablePerConnectionState",
        room_id: str,
        room_sync_config: RoomSyncConfig,
        room_membership_for_user_at_to_token: RoomsForUserType,
        from_token: Optional[SlidingSyncStreamToken],
        to_token: StreamToken,
        newly_joined: bool,
        is_dm: bool,
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
            newly_joined: If the user has newly joined the room
            is_dm: Whether the room is a DM room
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
        if from_token and not newly_joined:
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
                to_bound = room_membership_for_user_at_to_token.event_pos.to_room_stream_token()

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
            timeline_events, new_room_key, limited = await pagination_method(
                room_id=room_id,
                # The bounds are reversed so we can paginate backwards
                # (from newer to older events) starting at to_bound.
                # This ensures we fill the `limit` with the newest events first,
                from_key=to_bound,
                to_key=timeline_from_bound,
                direction=Direction.BACKWARDS,
                limit=room_sync_config.timeline_limit,
            )

            # We want to return the events in ascending order (the last event is the
            # most recent).
            timeline_events.reverse()

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

        # Get the changes to current state in the token range from the
        # `current_state_delta_stream` table.
        #
        # For incremental syncs, we can do this first to determine if something relevant
        # has changed and strategically avoid fetching other costly things.
        room_state_delta_id_map: MutableStateMap[str] = {}
        name_event_id: Optional[str] = None
        membership_changed = False
        name_changed = False
        avatar_changed = False
        if initial:
            # Check whether the room has a name set
            name_state_ids = await self.get_current_state_ids_at(
                room_id=room_id,
                room_membership_for_user_at_to_token=room_membership_for_user_at_to_token,
                state_filter=StateFilter.from_types([(EventTypes.Name, "")]),
                to_token=to_token,
            )
            name_event_id = name_state_ids.get((EventTypes.Name, ""))
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
            for delta in deltas:
                # TODO: Handle state resets where event_id is None
                if delta.event_id is not None:
                    room_state_delta_id_map[(delta.event_type, delta.state_key)] = (
                        delta.event_id
                    )

                if delta.event_type == EventTypes.Member:
                    membership_changed = True
                elif delta.event_type == EventTypes.Name and delta.state_key == "":
                    name_changed = True
                elif (
                    delta.event_type == EventTypes.RoomAvatar and delta.state_key == ""
                ):
                    avatar_changed = True

        # We only need the room summary for calculating heroes, however if we do
        # fetch it then we can use it to calculate `joined_count` and
        # `invited_count`.
        room_membership_summary: Optional[Mapping[str, MemberSummary]] = None

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
        #
        # We need to fetch the `heroes` if the room name is not set. But we only need to
        # get them on initial syncs (or the first time we send down the room) or if the
        # membership has changed which may change the heroes.
        if name_event_id is None and (initial or (not initial and membership_changed)):
            # We need the room summary to extract the heroes from
            if room_membership_for_user_at_to_token.membership != Membership.JOIN:
                # TODO: Figure out how to get the membership summary for left/banned rooms
                # For invite/knock rooms we don't include the information.
                room_membership_summary = {}
            else:
                room_membership_summary = await self.store.get_room_summary(room_id)
                # TODO: Reverse/rewind back to the `to_token`

            hero_user_ids = extract_heroes_from_room_summary(
                room_membership_summary, me=user.to_string()
            )

        # Fetch the membership counts for rooms we're joined to.
        #
        # Similarly to other metadata, we only need to calculate the member
        # counts if this is an initial sync or the memberships have changed.
        joined_count: Optional[int] = None
        invited_count: Optional[int] = None
        if (
            initial or membership_changed
        ) and room_membership_for_user_at_to_token.membership == Membership.JOIN:
            # If we have the room summary (because we calculated heroes above)
            # then we can simply pull the counts from there.
            if room_membership_summary is not None:
                empty_membership_summary = MemberSummary([], 0)

                joined_count = room_membership_summary.get(
                    Membership.JOIN, empty_membership_summary
                ).count

                invited_count = room_membership_summary.get(
                    Membership.INVITE, empty_membership_summary
                ).count
            else:
                member_counts = await self.store.get_member_counts(room_id)
                joined_count = member_counts.get(Membership.JOIN, 0)
                invited_count = member_counts.get(Membership.INVITE, 0)

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
                num_wild_state_keys = 0
                lazy_load_room_members = False
                num_others = 0
                for (
                    state_type,
                    state_key_set,
                ) in room_sync_config.required_state_map.items():
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
        hero_room_state = [
            (EventTypes.Member, hero_user_id) for hero_user_id in hero_user_ids
        ]
        meta_room_state = list(hero_room_state)
        if initial or name_changed:
            meta_room_state.append((EventTypes.Name, ""))
        if initial or avatar_changed:
            meta_room_state.append((EventTypes.RoomAvatar, ""))

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

            events = await self.store.get_events(
                state_filter.filter_state(room_state_delta_id_map).values()
            )
            room_state = {(s.type, s.state_key): s for s in events.values()}

            # If the membership changed and we have to get heroes, get the remaining
            # heroes from the state
            if hero_user_ids:
                hero_membership_state = await self.get_current_state_at(
                    room_id=room_id,
                    room_membership_for_user_at_to_token=room_membership_for_user_at_to_token,
                    state_filter=StateFilter.from_types(hero_room_state),
                    to_token=to_token,
                )
                room_state.update(hero_membership_state)

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
        #
        # By default, just choose the membership event position for any non-join membership
        bump_stamp = room_membership_for_user_at_to_token.event_pos.stream
        # If we're joined to the room, we need to find the last bump event before the
        # `to_token`
        if room_membership_for_user_at_to_token.membership == Membership.JOIN:
            # Try and get a bump stamp, if not we just fall back to the
            # membership token.
            new_bump_stamp = await self._get_bump_stamp(
                room_id, to_token, timeline_events
            )
            if new_bump_stamp is not None:
                bump_stamp = new_bump_stamp

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
            is_dm=is_dm,
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
            joined_count=joined_count,
            invited_count=invited_count,
            # TODO: These are just dummy values. We could potentially just remove these
            # since notifications can only really be done correctly on the client anyway
            # (encrypted rooms).
            notification_count=0,
            highlight_count=0,
        )

    @trace
    async def _get_bump_stamp(
        self, room_id: str, to_token: StreamToken, timeline: List[EventBase]
    ) -> Optional[int]:
        """Get a bump stamp for the room, if we have a bump event

        Args:
            room_id
            to_token: The upper bound of token to return
            timeline: The list of events we have fetched.
        """

        # First check the timeline events we're returning to see if one of
        # those matches. We iterate backwards and take the stream ordering
        # of the first event that matches the bump event types.
        for timeline_event in reversed(timeline):
            if timeline_event.type in SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES:
                new_bump_stamp = timeline_event.internal_metadata.stream_ordering

                # All persisted events have a stream ordering
                assert new_bump_stamp is not None

                # If we've just joined a remote room, then the last bump event may
                # have been backfilled (and so have a negative stream ordering).
                # These negative stream orderings can't sensibly be compared, so
                # instead we use the membership event position.
                if new_bump_stamp > 0:
                    return new_bump_stamp

        # We can quickly query for the latest bump event in the room using the
        # sliding sync tables.
        latest_room_bump_stamp = await self.store.get_latest_bump_stamp_for_room(
            room_id
        )

        min_to_token_position = to_token.room_key.stream

        # If we can rely on the new sliding sync tables and the `bump_stamp` is
        # `None`, just fallback to the membership event position. This can happen
        # when we've just joined a remote room and all the events are backfilled.
        if (
            # FIXME: The background job check can be removed once we bump
            # `SCHEMA_COMPAT_VERSION` and run the foreground update for
            # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots`
            # (tracked by https://github.com/element-hq/synapse/issues/17623)
            await self.store.have_finished_sliding_sync_background_jobs()
            and latest_room_bump_stamp is None
        ):
            return None

        # The `bump_stamp` stored in the database might be ahead of our token. Since
        # `bump_stamp` is only a `stream_ordering` position, we can't be 100% sure
        # that's before the `to_token` in all scenarios. The only scenario we can be
        # sure of is if the `bump_stamp` is totally before the minimum position from
        # the token.
        #
        # We don't need to check if the background update has finished, as if the
        # returned bump stamp is not None then it must be up to date.
        elif (
            latest_room_bump_stamp is not None
            and latest_room_bump_stamp < min_to_token_position
        ):
            if latest_room_bump_stamp > 0:
                return latest_room_bump_stamp
            else:
                return None

        # Otherwise, if it's within or after the `to_token`, we need to find the
        # last bump event before the `to_token`.
        else:
            last_bump_event_result = (
                await self.store.get_last_event_pos_in_room_before_stream_ordering(
                    room_id,
                    to_token.room_key,
                    event_types=SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES,
                )
            )
            if last_bump_event_result is not None:
                _, new_bump_event_pos = last_bump_event_result

                # If we've just joined a remote room, then the last bump event may
                # have been backfilled (and so have a negative stream ordering).
                # These negative stream orderings can't sensibly be compared, so
                # instead we use the membership event position.
                if new_bump_event_pos.stream > 0:
                    return new_bump_event_pos.stream

            return None
