#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Paginated Sync (MSC4525): a dialect of Simplified Sliding Sync (MSC4186)
without lists, ranges, subscriptions or expanding timelines.

The server pages the client through whatever has changed, most recently active
rooms first: at most `page_size` rooms per response, at most `limit` new events
per room (with an explicit per-room gap beyond that), and `history` events for
rooms never sent on the connection. Rooms with updates that don't fit in the
page are reported in `pending` and delivered on subsequent requests - which is
exactly the NEVER/PREVIOUSLY/LIVE bookkeeping the sliding sync connection store
already does, so no new per-connection state is needed.

Everything except room selection is inherited from `SlidingSyncHandler`:
`get_room_sync_data` (timeline/state/heroes/bump_stamp per room), the
extensions, the connection store and the notifier integration are shared.
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, AbstractSet

from synapse.api.errors import SlidingSyncUnknownPosition
from synapse.handlers.sliding_sync import SlidingSyncHandler
from synapse.handlers.sliding_sync.extensions import SlidingSyncExtensionHandler
from synapse.logging.opentracing import log_kv, set_tag, start_active_span, trace
from synapse.types import Requester, SlidingSyncStreamToken, StrCollection, StreamToken
from synapse.types.handlers.paginated_sync import (
    PaginatedSyncConfig,
    PaginatedSyncResult,
)
from synapse.types.handlers.sliding_sync import (
    HaveSentRoomFlag,
    PerConnectionState,
    RoomSyncConfig,
    SlidingSyncResult,
)
from synapse.types.rest.client import SlidingSyncBody
from synapse.util.async_helpers import concurrently_execute

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# The fraction of each page reserved for the longest-deferred rooms whenever
# there are more rooms with updates than fit in the page. This guarantees that
# a quiet room's update cannot be starved indefinitely by busier rooms
# re-earning their place at the top of the most-recent-first ordering.
AGING_LANE_FRACTION = 4


class PaginatedSyncExtensionHandler(SlidingSyncExtensionHandler):
    """The sliding sync extensions without the `lists`/`rooms` scoping: with no
    lists and no subscriptions there is nothing to scope, so an enabled
    extension simply applies to the rooms in the response."""

    def find_relevant_room_ids_for_extension(
        self,
        requested_lists: StrCollection | None,
        requested_room_ids: StrCollection | None,
        actual_lists: Mapping[str, SlidingSyncResult.SlidingWindowList],
        actual_room_ids: AbstractSet[str],
    ) -> set[str]:
        return set(actual_room_ids)


class PaginatedSyncHandler(SlidingSyncHandler):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        # History is `/messages`'s job in paginated sync; never re-send
        # historical events because a room's effective limit grew.
        self.expanded_timeline_on_limit_increase = False

        # `required_state` is immutable for the life of a connection, so there
        # are no per-room request configs to remember or diff.
        self.track_room_configs = False

        # Extensions lose their scoping fields.
        self.extensions = PaginatedSyncExtensionHandler(hs)

    async def wait_for_paginated_sync_for_user(
        self,
        requester: Requester,
        sync_config: PaginatedSyncConfig,
        from_token: SlidingSyncStreamToken | None = None,
        timeout_ms: int = 0,
    ) -> tuple[PaginatedSyncResult, bool]:
        """
        Get the paginated sync for a client if we have new data for it now,
        otherwise wait for new data to arrive on the server (mirrors
        `SlidingSyncHandler.wait_for_sync_for_user`).

        Returns:
            The `PaginatedSyncResult` and whether we waited for new activity
            before responding.
        """
        did_wait = False

        await self.auth_blocking.check_auth_blocking(requester=requester)

        if from_token is not None:
            # Bound tokens "from the future" and wait for this worker to catch
            # up, exactly as sliding sync does.
            from_token = SlidingSyncStreamToken(
                stream_token=await self.event_sources.bound_future_token(
                    from_token.stream_token
                ),
                connection_position=from_token.connection_position,
            )
            before_wait_ts = self.clock.time_msec()
            if not await self.notifier.wait_for_stream_token(from_token.stream_token):
                logger.warning(
                    "Timed out waiting for worker to catch up. Returning empty response"
                )
                return PaginatedSyncResult.empty(from_token), did_wait

            after_wait_ts = self.clock.time_msec()
            if after_wait_ts - before_wait_ts > 1_000:
                timeout_ms -= after_wait_ts - before_wait_ts
                timeout_ms = max(timeout_ms, 0)

        # Always compute a response first; if it has anything in it (including
        # a pending backlog) we return immediately, which is what makes the
        # server ignore `timeout` while the client is draining.
        now_token = self.event_sources.get_current_token()
        result = await self.current_paginated_sync_for_user(
            sync_config,
            from_token=from_token,
            to_token=now_token,
        )

        if result or timeout_ms == 0 or from_token is None:
            return result, did_wait

        async def current_sync_callback(
            before_token: StreamToken, after_token: StreamToken
        ) -> PaginatedSyncResult:
            return await self.current_paginated_sync_for_user(
                sync_config,
                from_token=from_token,
                to_token=after_token,
            )

        result = await self.notifier.wait_for_events(
            sync_config.user.to_string(),
            timeout_ms,
            current_sync_callback,
            from_token=now_token,
        )
        did_wait = True

        return result, did_wait

    @trace
    async def current_paginated_sync_for_user(
        self,
        sync_config: PaginatedSyncConfig,
        to_token: StreamToken,
        from_token: SlidingSyncStreamToken | None = None,
    ) -> PaginatedSyncResult:
        """
        Generate a paginated sync response for the token range (> `from_token`
        and <= `to_token`).
        """
        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            raise NotImplementedError()

        page_size = sync_config.page_size
        limit = sync_config.limit
        history = sync_config.history if sync_config.history is not None else limit

        # There is no M_UNKNOWN_POS in this API: a `pos` the server doesn't
        # recognise (expired, forged, another device's) is treated as absent -
        # nothing is trusted from the token, the connection starts afresh and
        # rooms come down as never-sent. The client has no error path.
        try:
            previous_connection_state = (
                await self.connection_store.get_and_clear_connection_positions(
                    sync_config, from_token
                )
            )
        except SlidingSyncUnknownPosition:
            logger.info(
                "Unrecognised paginated sync pos for %s; starting the connection afresh",
                user_id,
            )
            from_token = None
            previous_connection_state = PerConnectionState(last_used_ts=None)

        # Whether the new sliding sync tables are usable (c.f. SlidingSyncBase);
        # needed both here and for update detection below.
        use_new_tables = await self.store.have_finished_sliding_sync_background_jobs()

        # Reuse the sliding sync membership machinery wholesale: with no lists
        # and no subscriptions it assembles the full membership map (with
        # rewinds, newly-left add-back and state-reset handling) and the
        # newly-joined/newly-left/DM sets, without computing any list windows.
        if use_new_tables:
            interested_rooms = (
                await self.room_lists._compute_interested_rooms_new_tables(
                    sync_config=sync_config,  # type: ignore[arg-type]
                    previous_connection_state=previous_connection_state,
                    from_token=from_token.stream_token if from_token else None,
                    to_token=to_token,
                )
            )
        else:
            interested_rooms = await self.room_lists._compute_interested_rooms_fallback(
                sync_config=sync_config,  # type: ignore[arg-type]
                previous_connection_state=previous_connection_state,
                from_token=from_token.stream_token if from_token else None,
                to_token=to_token,
            )

        sync_room_map = dict(interested_rooms.room_membership_for_user_map)
        newly_joined_rooms = interested_rooms.newly_joined_rooms
        newly_left_rooms = interested_rooms.newly_left_rooms
        dm_room_ids = interested_rooms.dm_room_ids

        # The room config is the same for every room; only the timeline limit
        # varies (`history` for never-sent rooms, `limit` otherwise).
        room_params = SlidingSyncBody.CommonRoomParameters(
            required_state=sync_config.required_state,
            timeline_limit=limit,
        )
        base_room_config = RoomSyncConfig.from_room_config(room_params)

        # Exclude partially-stated rooms if we'd have to wait for them.
        if base_room_config.must_await_full_state(self.is_mine_id):
            partial_state_rooms = await self.store.get_partial_rooms()
            if partial_state_rooms:
                sync_room_map = {
                    room_id: room
                    for room_id, room in sync_room_map.items()
                    if room_id not in partial_state_rooms
                }

        # Work out which rooms have updates the connection hasn't received:
        #  - rooms never sent on this connection (all of them, on initial sync)
        #  - rooms previously deferred (sent before, known-undelivered updates)
        #  - live rooms with events since the client's token
        never_room_ids: set[str] = set()
        previously_room_ids: set[str] = set()
        if from_token is None:
            never_room_ids = set(sync_room_map)
            candidates = set(never_room_ids)
        else:
            live_room_ids: list[str] = []
            for room_id in sync_room_map:
                room_status = previous_connection_state.rooms.have_sent_room(room_id)
                if room_status.status == HaveSentRoomFlag.NEVER:
                    never_room_ids.add(room_id)
                elif room_status.status == HaveSentRoomFlag.PREVIOUSLY:
                    previously_room_ids.add(room_id)
                else:
                    live_room_ids.append(room_id)

            if use_new_tables:
                updated_room_ids = await (
                    self.store.get_rooms_that_have_updates_since_sliding_sync_table(
                        room_ids=live_room_ids,
                        from_key=from_token.stream_token.room_key,
                    )
                )
            else:
                updated_event_map = await self.store.get_room_events_stream_for_rooms(
                    room_ids=live_room_ids,
                    from_key=to_token.room_key,
                    to_key=from_token.stream_token.room_key,
                    limit=1,
                )
                updated_room_ids = set(updated_event_map)

            candidates = never_room_ids | previously_room_ids | set(updated_room_ids)
            # Membership transitions must always be delivered.
            candidates.update(newly_joined_rooms & sync_room_map.keys())
            candidates.update(newly_left_rooms & sync_room_map.keys())

            # Rooms with undelivered receipt or room account data changes must
            # also wake into the page, else a read receipt in an otherwise
            # quiet room is deferred until someone speaks. The room comes down
            # as an (often empty, filtered-out) entry and the extension
            # delivers the data. Only streams the client has enabled can have
            # anything to deliver.
            extensions_body = sync_config.extensions
            if (
                extensions_body is not None
                and extensions_body.receipts is not None
                and extensions_body.receipts.enabled
            ):
                # Rooms already recorded as having undelivered receipts, plus
                # rooms with new receipt activity in the token range.
                candidates.update(
                    room_id
                    for room_id, receipt_status in previous_connection_state.receipts._statuses.items()
                    if receipt_status.status == HaveSentRoomFlag.PREVIOUSLY
                    and room_id in sync_room_map
                )
                candidates.update(
                    await self.store.get_rooms_with_receipts_between(
                        [
                            room_id
                            for room_id in sync_room_map
                            if room_id not in candidates
                        ],
                        from_key=from_token.stream_token.receipt_key,
                        to_key=to_token.receipt_key,
                    )
                )
            if (
                extensions_body is not None
                and extensions_body.account_data is not None
                and extensions_body.account_data.enabled
            ):
                candidates.update(
                    room_id
                    for room_id, account_data_status in previous_connection_state.account_data._statuses.items()
                    if account_data_status.status == HaveSentRoomFlag.PREVIOUSLY
                    and room_id in sync_room_map
                )
                updated_account_data = (
                    await self.store.get_updated_room_account_data_for_user(
                        user_id, from_token.stream_token.account_data_key
                    )
                )
                updated_tags = await self.store.get_updated_tags(
                    user_id, from_token.stream_token.account_data_key
                )
                candidates.update(
                    (updated_account_data.keys() | updated_tags.keys())
                    & sync_room_map.keys()
                )

        # Page: most recently active rooms first. When the page overflows, a
        # slice of it is reserved for the longest-deferred rooms so that
        # nothing is starved by busier rooms perpetually sorting first.
        page_room_ids: list[str] = []
        if candidates:
            aged_room_ids: list[str] = []
            if len(candidates) > page_size and previously_room_ids:
                aging_lane_size = max(1, page_size // AGING_LANE_FRACTION)

                def last_sent_stream_pos(room_id: str) -> int:
                    room_status = previous_connection_state.rooms.have_sent_room(
                        room_id
                    )
                    assert room_status.last_token is not None
                    return room_status.last_token.stream

                aged_room_ids = sorted(
                    previously_room_ids, key=last_sent_stream_pos
                )[:aging_lane_size]

            sorted_room_infos = await self.room_lists.sort_rooms(
                {room_id: sync_room_map[room_id] for room_id in candidates},
                to_token,
                limit=page_size,
            )

            page_room_ids = list(aged_room_ids)
            for room_info in sorted_room_infos:
                if len(page_room_ids) >= page_size:
                    break
                if room_info.room_id not in page_room_ids:
                    page_room_ids.append(room_info.room_id)

        pending = len(candidates) - len(page_room_ids)

        log_kv(
            {
                "paginated_sync.candidates": len(candidates),
                "paginated_sync.page": len(page_room_ids),
                "paginated_sync.pending": pending,
            }
        )

        # Per-room configs: `history` for rooms the connection has never seen,
        # `limit` for everything else.
        relevant_rooms_to_send_map: dict[str, RoomSyncConfig] = {}
        for room_id in page_room_ids:
            if room_id in never_room_ids:
                timeline_limit = history
            else:
                timeline_limit = limit
            relevant_rooms_to_send_map[room_id] = RoomSyncConfig(
                timeline_limit=timeline_limit,
                required_state_map=base_room_config.required_state_map,
            )

        # Fetch room data, exactly as sliding sync does.
        rooms: dict[str, SlidingSyncResult.RoomResult] = {}
        new_connection_state = previous_connection_state.get_mutable()

        async def handle_room(room_id: str) -> None:
            room_sync_result = await self.get_room_sync_data(
                sync_config=sync_config,  # type: ignore[arg-type]
                previous_connection_state=previous_connection_state,
                new_connection_state=new_connection_state,
                room_id=room_id,
                room_sync_config=relevant_rooms_to_send_map[room_id],
                room_membership_for_user_at_to_token=sync_room_map[room_id],
                from_token=from_token,
                to_token=to_token,
                newly_joined=room_id in newly_joined_rooms,
                newly_left=room_id in newly_left_rooms,
                is_dm=room_id in dm_room_ids,
            )

            # Filter out empty room results during incremental sync
            if room_sync_result or not from_token:
                rooms[room_id] = room_sync_result

        if relevant_rooms_to_send_map:
            with start_active_span("paginated_sync.generate_room_entries"):
                await concurrently_execute(handle_room, relevant_rooms_to_send_map, 20)

        extensions = await self.extensions.get_extensions_response(
            sync_config=sync_config,  # type: ignore[arg-type]
            actual_lists={},
            previous_connection_state=previous_connection_state,
            new_connection_state=new_connection_state,
            all_interested_room_ids=set(sync_room_map),
            actual_room_ids=set(relevant_rooms_to_send_map.keys()),
            actual_room_response_map=rooms,
            from_token=from_token,
            to_token=to_token,
        )

        # Record what was and wasn't delivered. Candidates that didn't fit in
        # the page are downgraded LIVE -> PREVIOUSLY (never-sent rooms stay
        # NEVER), which is the entire paging cursor.
        if from_token:
            unsent_room_ids = candidates - set(page_room_ids)
            if unsent_room_ids:
                new_connection_state.rooms.record_unsent_rooms(
                    unsent_room_ids, from_token.stream_token.room_key
                )

        new_connection_state.rooms.record_sent_rooms(relevant_rooms_to_send_map.keys())

        connection_position = await self.connection_store.record_new_state(
            sync_config=sync_config,
            from_token=from_token,
            new_connection_state=new_connection_state,
        )

        result = PaginatedSyncResult(
            next_pos=SlidingSyncStreamToken(to_token, connection_position),
            rooms=rooms,
            extensions=extensions,
            pending=pending,
            total_rooms=len(sync_room_map),
        )

        set_tag("paginated_sync.result", bool(result))
        return result
