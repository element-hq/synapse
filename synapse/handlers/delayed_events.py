import logging
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from twisted.internet.interfaces import IDelayedCall

from synapse.api.constants import EventTypes
from synapse.api.errors import ShadowBanError
from synapse.events import EventBase
from synapse.logging.opentracing import set_tag
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.databases.main.delayed_events import (
    Delay,
    DelayedEventDetails,
    DelayID,
    DeviceID,
    EventType,
    StateKey,
    Timestamp,
    UserLocalpart,
)
from synapse.types import (
    JsonDict,
    Requester,
    RoomID,
    StateMap,
    UserID,
    create_requester,
)
from synapse.util.events import generate_fake_event_id

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DelayedEventsHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._config = hs.config
        self._clock = hs.get_clock()
        self._request_ratelimiter = hs.get_request_ratelimiter()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._room_member_handler = hs.get_room_member_handler()

        self._next_delayed_event_call: Optional[IDelayedCall] = None

        # TODO: Looks like these callbacks are run in background. Find a foreground one
        hs.get_module_api().register_third_party_rules_callbacks(
            on_new_event=self.on_new_event,
        )

        async def _schedule_db_events() -> None:
            # TODO: Sync all state first, so that affected delayed state events will be cancelled

            # Delayed events that are already marked as processed on startup might not have been
            # sent properly on the last run of the server, so unmark them to send them again.
            # Caveats:
            # - This will double-send delayed events that successfully persisted, but failed to be
            #   removed from the DB table of delayed events.
            # - This will interfere with workers that are in the act of processing delayed events.
            # TODO: To avoid double-sending, scan the timeline to find which of these events were
            # already sent. To do so, must store delay_ids in sent events to retrieve them later.
            # TODO: To avoid interfering with workers, think of a way to distinguish between
            # events being processed by a worker vs ones that got lost after a server crash.
            await self._store.unprocess_delayed_events()

            events, next_send_ts = await self._store.process_timeout_delayed_events(
                self._get_current_ts()
            )

            if next_send_ts:
                self._schedule_next_at(next_send_ts)

            # Can send the events in background after having awaited on marking them as processed
            run_as_background_process(
                "_send_events",
                self._send_events,
                events,
            )

        self._initialized_from_db = run_as_background_process(
            "_schedule_db_events", _schedule_db_events
        )

    async def on_new_event(
        self, event: EventBase, _state_events: StateMap[EventBase]
    ) -> None:
        """
        Checks if a received event is a state event, and if so,
        cancels any delayed events that target the same state.
        """
        state_key = event.get_state_key()
        if state_key is not None:
            changed, next_send_ts = await self._store.cancel_delayed_state_events(
                room_id=event.room_id,
                event_type=event.type,
                state_key=state_key,
            )

            if changed:
                self._schedule_next_at_or_none(next_send_ts)

    async def add(
        self,
        requester: Requester,
        *,
        room_id: str,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
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
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db

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

        delay_id, changed = await self._store.add_delayed_event(
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

        if changed:
            self._schedule_next_at(Timestamp(creation_ts + delay))

        return delay_id

    async def cancel(self, requester: Requester, delay_id: str) -> None:
        """
        Cancels the scheduled delivery of the matching delayed event.

        Args:
            requester: The owner of the delayed event to act on.
            delay_id: The ID of the delayed event to act on.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db

        changed, next_send_ts = await self._store.cancel_delayed_event(
            delay_id=delay_id,
            user_localpart=requester.user.localpart,
        )

        if changed:
            self._schedule_next_at_or_none(next_send_ts)

    async def restart(self, requester: Requester, delay_id: str) -> None:
        """
        Restarts the scheduled delivery of the matching delayed event.

        Args:
            requester: The owner of the delayed event to act on.
            delay_id: The ID of the delayed event to act on.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db

        changed, next_send_ts = await self._store.restart_delayed_event(
            delay_id=delay_id,
            user_localpart=requester.user.localpart,
            current_ts=self._get_current_ts(),
        )

        if changed:
            self._schedule_next_at(next_send_ts)

    async def send(self, requester: Requester, delay_id: str) -> None:
        """
        Immediately sends the matching delayed event, instead of waiting for its scheduled delivery.

        Args:
            requester: The owner of the delayed event to act on.
            delay_id: The ID of the delayed event to act on.

        Raises:
            NotFoundError: if no matching delayed event could be found.
        """
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db

        event, changed, next_send_ts = await self._store.process_target_delayed_event(
            delay_id=delay_id,
            user_localpart=requester.user.localpart,
        )

        if changed:
            self._schedule_next_at_or_none(next_send_ts)

        await self._send_event(
            DelayID(delay_id),
            UserLocalpart(requester.user.localpart),
            *event,
        )

    async def _send_on_timeout(self) -> None:
        self._next_delayed_event_call = None

        events, next_send_ts = await self._store.process_timeout_delayed_events(
            self._get_current_ts()
        )

        if next_send_ts:
            self._schedule_next_at(next_send_ts)

        await self._send_events(events)

    async def _send_events(self, events: List[DelayedEventDetails]) -> None:
        sent_state: Set[Tuple[RoomID, EventType, StateKey]] = set()
        for (
            delay_id,
            user_localpart,
            room_id,
            event_type,
            state_key,
            origin_server_ts,
            content,
            device_id,
        ) in events:
            if state_key is not None:
                state_info = (room_id, event_type, state_key)
                if state_info in sent_state:
                    continue
            else:
                state_info = None
            try:
                # TODO: send in background if message event or non-conflicting state event
                await self._send_event(
                    delay_id,
                    user_localpart,
                    room_id,
                    event_type,
                    state_key,
                    origin_server_ts,
                    content,
                    device_id,
                )
                if state_info is not None:
                    # Note that removal from the DB is done by self.on_new_event
                    sent_state.add(state_info)
            except Exception:
                logger.exception("Failed to send delayed event")

    def _schedule_next_at_or_none(self, next_send_ts: Optional[Timestamp]) -> None:
        if next_send_ts is not None:
            self._schedule_next_at(next_send_ts)
        elif self._next_delayed_event_call is not None:
            self._next_delayed_event_call.cancel()
            self._next_delayed_event_call = None

    def _schedule_next_at(self, next_send_ts: Timestamp) -> None:
        return self._schedule_next(self._get_delay_until(next_send_ts))

    def _schedule_next(self, delay: Delay) -> None:
        delay_sec = delay / 1000 if delay > 0 else 0

        if self._next_delayed_event_call is None:
            self._next_delayed_event_call = self._clock.call_later(
                delay_sec,
                run_as_background_process,
                "_send_on_timeout",
                self._send_on_timeout,
            )
        else:
            self._next_delayed_event_call.reset(delay_sec)

    async def get_all_for_user(self, requester: Requester) -> List[JsonDict]:
        """Return all pending delayed events requested by the given user."""
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db
        return await self._store.get_all_delayed_events_for_user(
            requester.user.localpart
        )

    async def _send_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
        room_id: RoomID,
        event_type: EventType,
        state_key: Optional[StateKey],
        origin_server_ts: Timestamp,
        content: JsonDict,
        device_id: Optional[DeviceID],
        txn_id: Optional[str] = None,
    ) -> None:
        user_id = UserID(user_localpart, self._config.server.server_name)
        user_id_str = user_id.to_string()
        # Create a new requester from what data is currently available
        # TODO: Consider storing the requester in the DB at add time and deserialize it here
        requester = create_requester(
            user_id,
            is_guest=await self._store.is_guest(user_id_str),
            device_id=device_id,
        )

        try:
            if state_key is not None and event_type == EventTypes.Member:
                membership = content.get("membership")
                assert membership is not None
                event_id, _ = await self._room_member_handler.update_membership(
                    requester,
                    target=UserID.from_string(state_key),
                    room_id=room_id.to_string(),
                    action=membership,
                    content=content,
                    origin_server_ts=origin_server_ts,
                )
            else:
                event_dict: JsonDict = {
                    "type": event_type,
                    "content": content,
                    "room_id": room_id.to_string(),
                    "sender": user_id_str,
                    "origin_server_ts": origin_server_ts,
                }

                if state_key is not None:
                    event_dict["state_key"] = state_key

                (
                    event,
                    _,
                ) = await self._event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    event_dict,
                    txn_id=txn_id,
                )
                event_id = event.event_id
        except ShadowBanError:
            event_id = generate_fake_event_id()
        finally:
            # TODO: If this is a temporary error, retry. Otherwise, consider notifying clients of the failure
            try:
                await self._store.delete_processed_delayed_event(
                    delay_id, user_localpart
                )
            except Exception:
                logger.exception("Failed to delete processed delayed event")

        set_tag("event_id", event_id)

    def _get_current_ts(self) -> Timestamp:
        return Timestamp(self._clock.time_msec())

    def _get_delay_until(self, to_ts: Timestamp) -> Delay:
        return _get_delay_between(self._get_current_ts(), to_ts)


def _get_delay_between(from_ts: Timestamp, to_ts: Timestamp) -> Delay:
    return Delay(to_ts - from_ts)
