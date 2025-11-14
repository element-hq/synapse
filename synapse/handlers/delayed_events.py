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

import logging
from typing import TYPE_CHECKING

from twisted.internet.interfaces import IDelayedCall

from synapse.api.constants import EventTypes
from synapse.api.errors import ShadowBanError, SynapseError
from synapse.api.ratelimiting import Ratelimiter
from synapse.config.workers import MAIN_PROCESS_INSTANCE_NAME
from synapse.http.site import SynapseRequest
from synapse.logging.context import make_deferred_yieldable
from synapse.logging.opentracing import set_tag
from synapse.metrics import SERVER_NAME_LABEL, event_processing_positions
from synapse.replication.http.delayed_events import (
    ReplicationAddedDelayedEventRestServlet,
)
from synapse.storage.databases.main.delayed_events import (
    DelayedEventDetails,
    EventType,
    StateKey,
    Timestamp,
)
from synapse.storage.databases.main.state_deltas import StateDelta
from synapse.types import (
    JsonDict,
    Requester,
    RoomID,
    UserID,
    create_requester,
)
from synapse.util.events import generate_fake_event_id
from synapse.util.metrics import Measure
from synapse.util.sentinel import Sentinel

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DelayedEventsHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.server_name = hs.hostname
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._config = hs.config
        self._clock = hs.get_clock()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._room_member_handler = hs.get_room_member_handler()

        self._request_ratelimiter = hs.get_request_ratelimiter()

        # Ratelimiter for management of existing delayed events,
        # keyed by the sending user ID & device ID.
        self._delayed_event_mgmt_ratelimiter = Ratelimiter(
            store=self._store,
            clock=self._clock,
            cfg=self._config.ratelimiting.rc_delayed_event_mgmt,
        )

        self._next_delayed_event_call: IDelayedCall | None = None

        # The current position in the current_state_delta stream
        self._event_pos: int | None = None

        # Guard to ensure we only process event deltas one at a time
        self._event_processing = False

        if hs.config.worker.worker_app is None:
            self._repl_client = None

            async def _schedule_db_events() -> None:
                # We kick this off to pick up outstanding work from before the last restart.
                # Block until we're up to date.
                await self._unsafe_process_new_event()
                hs.get_notifier().add_replication_callback(self.notify_new_event)
                # Kick off again (without blocking) to catch any missed notifications
                # that may have fired before the callback was added.
                self._clock.call_later(
                    0,
                    self.notify_new_event,
                )

                # Delayed events that are already marked as processed on startup might not have been
                # sent properly on the last run of the server, so unmark them to send them again.
                # Caveat: this will double-send delayed events that successfully persisted, but failed
                # to be removed from the DB table of delayed events.
                # TODO: To avoid double-sending, scan the timeline to find which of these events were
                # already sent. To do so, must store delay_ids in sent events to retrieve them later.
                await self._store.unprocess_delayed_events()

                events, next_send_ts = await self._store.process_timeout_delayed_events(
                    self._get_current_ts()
                )

                if next_send_ts:
                    self._schedule_next_at(next_send_ts)

                # Can send the events in background after having awaited on marking them as processed
                self.hs.run_as_background_process(
                    "_send_events",
                    self._send_events,
                    events,
                )

            self._initialized_from_db = self.hs.run_as_background_process(
                "_schedule_db_events", _schedule_db_events
            )
        else:
            self._repl_client = ReplicationAddedDelayedEventRestServlet.make_client(hs)

    @property
    def _is_master(self) -> bool:
        return self._repl_client is None

    def notify_new_event(self) -> None:
        """
        Called when there may be more state event deltas to process,
        which should cancel pending delayed events for the same state.
        """
        if self._event_processing:
            return

        self._event_processing = True

        async def process() -> None:
            try:
                await self._unsafe_process_new_event()
            finally:
                self._event_processing = False

        self.hs.run_as_background_process("delayed_events.notify_new_event", process)

    async def _unsafe_process_new_event(self) -> None:
        # We purposefully fetch the current max room stream ordering before
        # doing anything else, as it could increment duing processing of state
        # deltas. We want to avoid updating `delayed_events_stream_pos` past
        # the stream ordering of the state deltas we've processed. Otherwise
        # we'll leave gaps in our processing.
        room_max_stream_ordering = self._store.get_room_max_stream_ordering()

        # Check that there are actually any delayed events to process. If not, bail early.
        delayed_events_count = await self._store.get_count_of_delayed_events()
        if delayed_events_count == 0:
            # There are no delayed events to process. Update the
            # `delayed_events_stream_pos` to the latest `events` stream pos and
            # exit early.
            self._event_pos = room_max_stream_ordering

            logger.debug(
                "No delayed events to process. Updating `delayed_events_stream_pos` to max stream ordering (%s)",
                room_max_stream_ordering,
            )

            await self._store.update_delayed_events_stream_pos(room_max_stream_ordering)

            event_processing_positions.labels(
                name="delayed_events", **{SERVER_NAME_LABEL: self.server_name}
            ).set(room_max_stream_ordering)

            return

        # If self._event_pos is None then means we haven't fetched it from the DB yet
        if self._event_pos is None:
            self._event_pos = await self._store.get_delayed_events_stream_pos()
            if self._event_pos > room_max_stream_ordering:
                # apparently, we've processed more events than exist in the database!
                # this can happen if events are removed with history purge or similar.
                logger.warning(
                    "Event stream ordering appears to have gone backwards (%i -> %i): "
                    "rewinding delayed events processor",
                    self._event_pos,
                    room_max_stream_ordering,
                )
                self._event_pos = room_max_stream_ordering

        # Loop round handling deltas until we're up to date
        while True:
            with Measure(
                self._clock, name="delayed_events_delta", server_name=self.server_name
            ):
                room_max_stream_ordering = self._store.get_room_max_stream_ordering()
                if self._event_pos >= room_max_stream_ordering:
                    return

                logger.debug(
                    "Processing delayed events %s->%s",
                    self._event_pos,
                    room_max_stream_ordering,
                )
                (
                    max_pos,
                    deltas,
                ) = await self._storage_controllers.state.get_current_state_deltas(
                    self._event_pos, room_max_stream_ordering
                )

                logger.debug(
                    "Handling %d state deltas for delayed events processing",
                    len(deltas),
                )
                await self._handle_state_deltas(deltas)

                self._event_pos = max_pos

                # Expose current event processing position to prometheus
                event_processing_positions.labels(
                    name="delayed_events", **{SERVER_NAME_LABEL: self.server_name}
                ).set(max_pos)

                await self._store.update_delayed_events_stream_pos(max_pos)

    async def _handle_state_deltas(self, deltas: list[StateDelta]) -> None:
        """
        Process current state deltas to cancel other users' pending delayed events
        that target the same state.
        """
        # Get the senders of each delta's state event (as sender information is
        # not currently stored in the `current_state_deltas` table).
        event_id_and_sender_dict = await self._store.get_senders_for_event_ids(
            [delta.event_id for delta in deltas if delta.event_id is not None]
        )

        # Note: No need to batch as `get_current_state_deltas` will only ever
        # return 100 rows at a time.
        for delta in deltas:
            logger.debug(
                "Handling: %r %r, %s", delta.event_type, delta.state_key, delta.event_id
            )

            # `delta.event_id` and `delta.sender` can be `None` in a few valid
            # cases (see the docstring of
            # `get_current_state_delta_membership_changes_for_user` for details).
            if delta.event_id is None:
                # TODO: Differentiate between this being caused by a state reset
                # which removed a user from a room, or the homeserver
                # purposefully having left the room. We can do so by checking
                # whether there are any local memberships still left in the
                # room. If so, then this is the result of a state reset.
                #
                # If it is a state reset, we should avoid cancelling new,
                # delayed state events due to old state resurfacing. So we
                # should skip and log a warning in this case.
                #
                # If the homeserver has left the room, then we should cancel all
                # delayed state events intended for this room, as there is no
                # need to try and send a delayed event into a room we've left.
                logger.warning(
                    "Skipping state delta (%r, %r) without corresponding event ID. "
                    "This can happen if the homeserver has left the room (in which "
                    "case this can be ignored), or if there has been a state reset "
                    "which has caused the sender to be kicked out of the room",
                    delta.event_type,
                    delta.state_key,
                )
                continue

            sender_str = event_id_and_sender_dict.get(
                delta.event_id, Sentinel.UNSET_SENTINEL
            )
            if sender_str is None:
                # An event exists, but the `sender` field was "null" and Synapse
                # incorrectly accepted the event. This is not expected.
                logger.error(
                    "Skipping state delta with event ID '%s' as 'sender' was None. "
                    "This is unexpected - please report it as a bug!",
                    delta.event_id,
                )
                continue
            if sender_str is Sentinel.UNSET_SENTINEL:
                # We have an event ID, but the event was not found in the
                # datastore. This can happen if a room, or its history, is
                # purged. State deltas related to the room are left behind, but
                # the event no longer exists.
                #
                # As we cannot get the sender of this event, we can't calculate
                # whether to cancel delayed events related to this one. So we skip.
                logger.debug(
                    "Skipping state delta with event ID '%s' - the room, or its history, may have been purged",
                    delta.event_id,
                )
                continue

            try:
                sender = UserID.from_string(sender_str)
            except SynapseError as e:
                logger.error(
                    "Skipping state delta with Matrix User ID '%s' that failed to parse: %s",
                    sender_str,
                    e,
                )
                continue

            next_send_ts = await self._store.cancel_delayed_state_events(
                room_id=delta.room_id,
                event_type=delta.event_type,
                state_key=delta.state_key,
                not_from_localpart=(
                    sender.localpart
                    if sender.domain == self._config.server.server_name
                    else ""
                ),
            )

            if self._next_send_ts_changed(next_send_ts):
                self._schedule_next_at_or_none(next_send_ts)

    async def add(
        self,
        requester: Requester,
        *,
        room_id: str,
        event_type: str,
        state_key: str | None,
        origin_server_ts: int | None,
        content: JsonDict,
        delay: int,
    ) -> str:
        """
        Creates a new delayed event and schedules its delivery.

        Args:
            requester: The requester of the delayed event, who will be its owner.
            room_id: The ID of the room where the event should be sent to.
            event_type: The type of event to be sent.
            state_key: The state key of the event to be sent, or None if it is not a state event.
            origin_server_ts: The custom timestamp to send the event with.
                If None, the timestamp will be the actual time when the event is sent.
            content: The content of the event to be sent.
            delay: How long (in milliseconds) to wait before automatically sending the event.

        Returns: The ID of the added delayed event.

        Raises:
            SynapseError: if the delayed event fails validation checks.
        """
        # Use standard request limiter for scheduling new delayed events.
        # TODO: Instead apply ratelimiting based on the scheduled send time.
        # See https://github.com/element-hq/synapse/issues/18021
        await self._request_ratelimiter.ratelimit(requester)

        self._event_creation_handler.validator.validate_builder(
            self._event_creation_handler.event_builder_factory.for_room_version(
                await self._store.get_room_version(room_id),
                {
                    "type": event_type,
                    "content": content,
                    "room_id": room_id,
                    "sender": str(requester.user),
                    **({"state_key": state_key} if state_key is not None else {}),
                },
            )
        )

        creation_ts = self._get_current_ts()

        delay_id, next_send_ts = await self._store.add_delayed_event(
            user_localpart=requester.user.localpart,
            device_id=requester.device_id,
            creation_ts=creation_ts,
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
            origin_server_ts=origin_server_ts,
            content=content,
            delay=delay,
        )

        if self._repl_client is not None:
            # NOTE: If this throws, the delayed event will remain in the DB and
            # will be picked up once the main worker gets another delayed event.
            await self._repl_client(
                instance_name=MAIN_PROCESS_INSTANCE_NAME,
                next_send_ts=next_send_ts,
            )
        elif self._next_send_ts_changed(next_send_ts):
            self._schedule_next_at(next_send_ts)

        return delay_id

    def on_added(self, next_send_ts: int) -> None:
        next_send_ts = Timestamp(next_send_ts)
        if self._next_send_ts_changed(next_send_ts):
            self._schedule_next_at(next_send_ts)

    async def cancel(self, request: SynapseRequest, delay_id: str) -> None:
        """
        Cancels the scheduled delivery of the matching delayed event.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        assert self._is_master
        await self._delayed_event_mgmt_ratelimiter.ratelimit(
            None, request.getClientAddress().host
        )
        await make_deferred_yieldable(self._initialized_from_db)

        next_send_ts = await self._store.cancel_delayed_event(delay_id)

        if self._next_send_ts_changed(next_send_ts):
            self._schedule_next_at_or_none(next_send_ts)

    async def restart(self, request: SynapseRequest, delay_id: str) -> None:
        """
        Restarts the scheduled delivery of the matching delayed event.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        assert self._is_master
        await self._delayed_event_mgmt_ratelimiter.ratelimit(
            None, request.getClientAddress().host
        )
        await make_deferred_yieldable(self._initialized_from_db)

        next_send_ts = await self._store.restart_delayed_event(
            delay_id, self._get_current_ts()
        )

        if self._next_send_ts_changed(next_send_ts):
            self._schedule_next_at(next_send_ts)

    async def send(self, request: SynapseRequest, delay_id: str) -> None:
        """
        Immediately sends the matching delayed event, instead of waiting for its scheduled delivery.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        assert self._is_master
        await self._delayed_event_mgmt_ratelimiter.ratelimit(
            None, request.getClientAddress().host
        )
        await make_deferred_yieldable(self._initialized_from_db)

        event, next_send_ts = await self._store.process_target_delayed_event(delay_id)

        if self._next_send_ts_changed(next_send_ts):
            self._schedule_next_at_or_none(next_send_ts)

        await self._send_event(event)

    async def _send_on_timeout(self) -> None:
        self._next_delayed_event_call = None

        events, next_send_ts = await self._store.process_timeout_delayed_events(
            self._get_current_ts()
        )

        if next_send_ts:
            self._schedule_next_at(next_send_ts)

        await self._send_events(events)

    async def _send_events(self, events: list[DelayedEventDetails]) -> None:
        sent_state: set[tuple[RoomID, EventType, StateKey]] = set()
        for event in events:
            if event.state_key is not None:
                state_info = (event.room_id, event.type, event.state_key)
                if state_info in sent_state:
                    continue
            else:
                state_info = None
            try:
                # TODO: send in background if message event or non-conflicting state event
                await self._send_event(event)
                if state_info is not None:
                    sent_state.add(state_info)
            except Exception:
                logger.exception("Failed to send delayed event")

            for room_id, event_type, state_key in sent_state:
                await self._store.delete_processed_delayed_state_events(
                    room_id=str(room_id),
                    event_type=event_type,
                    state_key=state_key,
                )

    def _schedule_next_at_or_none(self, next_send_ts: Timestamp | None) -> None:
        if next_send_ts is not None:
            self._schedule_next_at(next_send_ts)
        elif self._next_delayed_event_call is not None:
            self._next_delayed_event_call.cancel()
            self._next_delayed_event_call = None

    def _schedule_next_at(self, next_send_ts: Timestamp) -> None:
        delay = next_send_ts - self._get_current_ts()
        delay_sec = delay / 1000 if delay > 0 else 0

        if self._next_delayed_event_call is None:
            self._next_delayed_event_call = self._clock.call_later(
                delay_sec,
                self.hs.run_as_background_process,
                "_send_on_timeout",
                self._send_on_timeout,
            )
        else:
            self._next_delayed_event_call.reset(delay_sec)

    async def get_all_for_user(self, requester: Requester) -> list[JsonDict]:
        """Return all pending delayed events requested by the given user."""
        await self._delayed_event_mgmt_ratelimiter.ratelimit(
            requester,
            (requester.user.to_string(), requester.device_id),
        )
        return await self._store.get_all_delayed_events_for_user(
            requester.user.localpart
        )

    async def _send_event(
        self,
        event: DelayedEventDetails,
        txn_id: str | None = None,
    ) -> None:
        user_id = UserID(event.user_localpart, self._config.server.server_name)
        user_id_str = user_id.to_string()
        # Create a new requester from what data is currently available
        requester = create_requester(
            user_id,
            is_guest=await self._store.is_guest(user_id_str),
            device_id=event.device_id,
        )

        try:
            if event.state_key is not None and event.type == EventTypes.Member:
                membership = event.content.get("membership")
                assert membership is not None
                event_id, _ = await self._room_member_handler.update_membership(
                    requester,
                    target=UserID.from_string(event.state_key),
                    room_id=event.room_id.to_string(),
                    action=membership,
                    content=event.content,
                    origin_server_ts=event.origin_server_ts,
                )
            else:
                event_dict: JsonDict = {
                    "type": event.type,
                    "content": event.content,
                    "room_id": event.room_id.to_string(),
                    "sender": user_id_str,
                }

                if event.origin_server_ts is not None:
                    event_dict["origin_server_ts"] = event.origin_server_ts

                if event.state_key is not None:
                    event_dict["state_key"] = event.state_key

                (
                    sent_event,
                    _,
                ) = await self._event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    event_dict,
                    txn_id=txn_id,
                )
                event_id = sent_event.event_id
        except ShadowBanError:
            event_id = generate_fake_event_id()
        finally:
            # TODO: If this is a temporary error, retry. Otherwise, consider notifying clients of the failure
            try:
                await self._store.delete_processed_delayed_event(event.delay_id)
            except Exception:
                logger.exception("Failed to delete processed delayed event")

        set_tag("event_id", event_id)

    def _get_current_ts(self) -> Timestamp:
        return Timestamp(self._clock.time_msec())

    def _next_send_ts_changed(self, next_send_ts: Timestamp | None) -> bool:
        # The DB alone knows if the next send time changed after adding/modifying
        # a delayed event, but if we were to ever miss updating our delayed call's
        # firing time, we may miss other updates. So, keep track of changes to the
        # the next send time here instead of in the DB.
        cached_next_send_ts = (
            int(self._next_delayed_event_call.getTime() * 1000)
            if self._next_delayed_event_call is not None
            else None
        )
        return next_send_ts != cached_next_send_ts
