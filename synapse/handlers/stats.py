#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Sorunome
# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
import logging
from collections import Counter
from typing import (
    TYPE_CHECKING,
    Any,
    Counter as CounterType,
    Dict,
    Iterable,
    Optional,
    Tuple,
)

from synapse.api.constants import EventContentFields, EventTypes, Membership
from synapse.metrics import event_processing_positions
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class StatsHandler:
    """Handles keeping the *_stats tables updated with a simple time-series of
    information about the users, rooms and media on the server, such that admins
    have some idea of who is consuming their resources.

    Heavily derived from UserDirectoryHandler
    """

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.state = hs.get_state_handler()
        self.clock = hs.get_clock()
        self.notifier = hs.get_notifier()
        self.is_mine_id = hs.is_mine_id

        self.stats_enabled = hs.config.stats.stats_enabled

        # The current position in the current_state_delta stream
        self.pos: Optional[int] = None

        # Guard to ensure we only process deltas one at a time
        self._is_processing = False

        if self.stats_enabled and hs.config.worker.run_background_tasks:
            self.notifier.add_replication_callback(self.notify_new_event)

            # We kick this off so that we don't have to wait for a change before
            # we start populating stats
            self.clock.call_later(0, self.notify_new_event)

    def notify_new_event(self) -> None:
        """Called when there may be more deltas to process"""
        if not self.stats_enabled or self._is_processing:
            return

        self._is_processing = True

        async def process() -> None:
            try:
                await self._unsafe_process()
            finally:
                self._is_processing = False

        run_as_background_process("stats.notify_new_event", process)

    async def _unsafe_process(self) -> None:
        # If self.pos is None then means we haven't fetched it from DB
        if self.pos is None:
            self.pos = await self.store.get_stats_positions()
            room_max_stream_ordering = self.store.get_room_max_stream_ordering()
            if self.pos > room_max_stream_ordering:
                # apparently, we've processed more events than exist in the database!
                # this can happen if events are removed with history purge or similar.
                logger.warning(
                    "Event stream ordering appears to have gone backwards (%i -> %i): "
                    "rewinding stats processor",
                    self.pos,
                    room_max_stream_ordering,
                )
                self.pos = room_max_stream_ordering

        # Loop round handling deltas until we're up to date

        while True:
            # Be sure to read the max stream_ordering *before* checking if there are any outstanding
            # deltas, since there is otherwise a chance that we could miss updates which arrive
            # after we check the deltas.
            room_max_stream_ordering = self.store.get_room_max_stream_ordering()
            if self.pos == room_max_stream_ordering:
                break

            logger.debug(
                "Processing room stats %s->%s", self.pos, room_max_stream_ordering
            )
            (
                max_pos,
                deltas,
            ) = await self._storage_controllers.state.get_current_state_deltas(
                self.pos, room_max_stream_ordering
            )

            if deltas:
                logger.debug("Handling %d state deltas", len(deltas))
                room_deltas, user_deltas = await self._handle_deltas(deltas)
            else:
                room_deltas = {}
                user_deltas = {}

            logger.debug("room_deltas: %s", room_deltas)
            logger.debug("user_deltas: %s", user_deltas)

            # Always call this so that we update the stats position.
            await self.store.bulk_update_stats_delta(
                self.clock.time_msec(),
                updates={"room": room_deltas, "user": user_deltas},
                stream_id=max_pos,
            )

            logger.debug("Handled room stats to %s -> %s", self.pos, max_pos)

            event_processing_positions.labels("stats").set(max_pos)

            self.pos = max_pos

    async def _handle_deltas(
        self, deltas: Iterable[StateDelta]
    ) -> Tuple[Dict[str, CounterType[str]], Dict[str, CounterType[str]]]:
        """Called with the state deltas to process

        Returns:
            Two dicts: the room deltas and the user deltas,
            mapping from room/user ID to changes in the various fields.
        """

        room_to_stats_deltas: Dict[str, CounterType[str]] = {}
        user_to_stats_deltas: Dict[str, CounterType[str]] = {}

        room_to_state_updates: Dict[str, Dict[str, Any]] = {}

        for delta in deltas:
            logger.debug(
                "Handling: %r, %r %r, %s",
                delta.room_id,
                delta.event_type,
                delta.state_key,
                delta.event_id,
            )

            token = await self.store.get_earliest_token_for_stats("room", delta.room_id)

            # If the earliest token to begin from is larger than our current
            # stream ID, skip processing this delta.
            if token is not None and token >= delta.stream_id:
                logger.debug(
                    "Ignoring: %s as earlier than this room's initial ingestion event",
                    delta.event_id,
                )
                continue

            if delta.event_id is None and delta.prev_event_id is None:
                logger.error(
                    "event ID is None and so is the previous event ID. stream_id: %s",
                    delta.stream_id,
                )
                continue

            event_content: JsonDict = {}

            if delta.event_id is not None:
                event = await self.store.get_event(delta.event_id, allow_none=True)
                if event:
                    event_content = event.content or {}

            # All the values in this dict are deltas (RELATIVE changes)
            room_stats_delta = room_to_stats_deltas.setdefault(delta.room_id, Counter())

            room_state = room_to_state_updates.setdefault(delta.room_id, {})

            if delta.prev_event_id is None:
                # this state event doesn't overwrite another,
                # so it is a new effective/current state event
                room_stats_delta["current_state_events"] += 1

            if delta.event_type == EventTypes.Member:
                # we could use StateDeltasHandler._get_key_change here but it's
                # a bit inefficient given we're not testing for a specific
                # result; might as well just grab the prev_membership and
                # membership strings and compare them.
                # We take None rather than leave as a previous membership
                # in the absence of a previous event because we do not want to
                # reduce the leave count when a new-to-the-room user joins.
                prev_membership = None
                if delta.prev_event_id is not None:
                    prev_event = await self.store.get_event(
                        delta.prev_event_id, allow_none=True
                    )
                    if prev_event:
                        prev_event_content = prev_event.content
                        prev_membership = prev_event_content.get(
                            "membership", Membership.LEAVE
                        )

                membership = event_content.get("membership", Membership.LEAVE)

                if prev_membership is None:
                    logger.debug("No previous membership for this user.")
                elif membership == prev_membership:
                    pass  # noop
                elif prev_membership == Membership.JOIN:
                    room_stats_delta["joined_members"] -= 1
                elif prev_membership == Membership.INVITE:
                    room_stats_delta["invited_members"] -= 1
                elif prev_membership == Membership.LEAVE:
                    room_stats_delta["left_members"] -= 1
                elif prev_membership == Membership.BAN:
                    room_stats_delta["banned_members"] -= 1
                elif prev_membership == Membership.KNOCK:
                    room_stats_delta["knocked_members"] -= 1
                else:
                    raise ValueError(
                        "%r is not a valid prev_membership" % (prev_membership,)
                    )

                if membership == prev_membership:
                    pass  # noop
                elif membership == Membership.JOIN:
                    room_stats_delta["joined_members"] += 1
                elif membership == Membership.INVITE:
                    room_stats_delta["invited_members"] += 1
                elif membership == Membership.LEAVE:
                    room_stats_delta["left_members"] += 1
                elif membership == Membership.BAN:
                    room_stats_delta["banned_members"] += 1
                elif membership == Membership.KNOCK:
                    room_stats_delta["knocked_members"] += 1
                else:
                    raise ValueError("%r is not a valid membership" % (membership,))

                user_id = delta.state_key
                if self.is_mine_id(user_id):
                    # this accounts for transitions like leave → ban and so on.
                    has_changed_joinedness = (prev_membership == Membership.JOIN) != (
                        membership == Membership.JOIN
                    )

                    if has_changed_joinedness:
                        membership_delta = +1 if membership == Membership.JOIN else -1

                        user_to_stats_deltas.setdefault(user_id, Counter())[
                            "joined_rooms"
                        ] += membership_delta

                        room_stats_delta["local_users_in_room"] += membership_delta

            elif delta.event_type == EventTypes.Create:
                room_state["is_federatable"] = (
                    event_content.get(EventContentFields.FEDERATE, True) is True
                )
                room_type = event_content.get(EventContentFields.ROOM_TYPE)
                if isinstance(room_type, str):
                    room_state["room_type"] = room_type
            elif delta.event_type == EventTypes.JoinRules:
                room_state["join_rules"] = event_content.get("join_rule")
            elif delta.event_type == EventTypes.RoomHistoryVisibility:
                room_state["history_visibility"] = event_content.get(
                    "history_visibility"
                )
            elif delta.event_type == EventTypes.RoomEncryption:
                room_state["encryption"] = event_content.get(
                    EventContentFields.ENCRYPTION_ALGORITHM
                )
            elif delta.event_type == EventTypes.Name:
                room_state["name"] = event_content.get("name")
            elif delta.event_type == EventTypes.Topic:
                room_state["topic"] = event_content.get("topic")
            elif delta.event_type == EventTypes.RoomAvatar:
                room_state["avatar"] = event_content.get("url")
            elif delta.event_type == EventTypes.CanonicalAlias:
                room_state["canonical_alias"] = event_content.get("alias")
            elif delta.event_type == EventTypes.GuestAccess:
                room_state["guest_access"] = event_content.get(
                    EventContentFields.GUEST_ACCESS
                )

        for room_id, state in room_to_state_updates.items():
            logger.debug("Updating room_stats_state for %s: %s", room_id, state)
            await self.store.update_room_state(room_id, state)

        return room_to_stats_deltas, user_to_stats_deltas
