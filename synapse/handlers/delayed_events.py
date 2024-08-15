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
#

import logging
from contextlib import asynccontextmanager
from enum import Enum
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    AsyncContextManager,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

import attr

from twisted.internet.interfaces import IDelayedCall

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, NotFoundError, ShadowBanError, SynapseError
from synapse.events import EventBase
from synapse.logging.opentracing import set_tag
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.databases.main.delayed_events import (
    Delay,
    DelayID,
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
from synapse.util.async_helpers import Linearizer, ReadWriteLock
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_STATE_LOCK_KEY = "STATE_LOCK_KEY"


class _UpdateDelayedEventAction(Enum):
    CANCEL = "cancel"
    RESTART = "restart"
    SEND = "send"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _DelayedCallKey:
    delay_id: DelayID
    user_localpart: UserLocalpart

    def __str__(self) -> str:
        return f"{self.user_localpart}:{self.delay_id}"


class DelayedEventsHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._config = hs.config
        self._clock = hs.get_clock()
        self._request_ratelimiter = hs.get_request_ratelimiter()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._room_member_handler = hs.get_room_member_handler()

        self._delayed_calls: Dict[_DelayedCallKey, IDelayedCall] = {}
        # This is for making delayed event actions atomic
        self._linearizer = Linearizer("delayed_events_handler")
        # This is to prevent running actions on delayed events removed due to state changes
        self._state_lock = ReadWriteLock()

        async def _schedule_db_events() -> None:
            # TODO: Sync all state first, so that affected delayed state events will be cancelled
            events, remaining_timeout_delays = await self._store.process_all_delays(
                self._get_current_ts()
            )
            sent_state: Set[Tuple[RoomID, EventType, StateKey]] = set()
            for (
                user_localpart,
                room_id,
                event_type,
                state_key,
                timestamp,
                content,
            ) in events:
                if state_key is not None:
                    state_info = (room_id, event_type, state_key)
                    if state_info in sent_state:
                        continue
                else:
                    state_info = None
                try:
                    await self._send_event(
                        user_localpart,
                        room_id,
                        event_type,
                        state_key,
                        timestamp,
                        content,
                    )
                    if state_info is not None:
                        sent_state.add(state_info)
                except Exception:
                    logger.exception("Failed to send delayed event on startup")
            sent_state.clear()

            for delay_id, user_localpart, relative_delay in remaining_timeout_delays:
                self._schedule(delay_id, user_localpart, relative_delay)

            hs.get_module_api().register_third_party_rules_callbacks(
                on_new_event=self.on_new_event,
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
            async with self._get_state_context():
                for (
                    removed_timeout_delay_id,
                    removed_timeout_delay_user_localpart,
                ) in await self._store.remove_state_events(
                    event.room_id,
                    event.type,
                    state_key,
                ):
                    self._unschedule(
                        removed_timeout_delay_id,
                        removed_timeout_delay_user_localpart,
                    )

    async def add(
        self,
        requester: Requester,
        *,
        room_id: str,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        delay: Optional[int],
        parent_id: Optional[str],
    ) -> str:
        """
        Creates a new delayed event.

        Params:
            requester: The requester of the delayed event, who will be its owner.
            room_id: The room where the event should be sent.
            event_type: The type of event to be sent.
            state_key: The state key of the event to be sent, or None if it is not a state event.
            origin_server_ts: The custom timestamp to send the event with.
                If None, the timestamp will be the actual time when the event is sent.
            content: The content of the event to be sent.
            delay: How long (in milliseconds) to wait before automatically sending the event.
                If None, the event won't be automatically sent (allowed only when parent_id is set).
            parent_id: The ID of the delayed event this one is grouped with.
                May only refer to a delayed event that has no parent itself.

        Returns:
            The ID of the added delayed event.
        """
        if delay is not None:
            max_delay = self._config.experimental.msc4140_max_delay
            if delay > max_delay:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "The requested delay exceeds the allowed maximum.",
                    Codes.UNKNOWN,
                    {
                        "org.matrix.msc4140.errcode": "M_MAX_DELAY_EXCEEDED",
                        "org.matrix.msc4140.max_delay": max_delay,
                    },
                )
        # Callers should ensure that at least one of these are set
        assert delay or parent_id

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

        user_localpart = UserLocalpart(requester.user.localpart)
        delay_id = await self._store.add(
            user_localpart=user_localpart,
            current_ts=self._get_current_ts(),
            room_id=RoomID.from_string(room_id),
            event_type=event_type,
            state_key=state_key,
            origin_server_ts=origin_server_ts,
            content=content,
            delay=delay,
            parent_id=parent_id,
        )

        if delay is not None:
            self._schedule(delay_id, user_localpart, Delay(delay))

        return delay_id

    async def update(self, requester: Requester, delay_id: str, action: str) -> None:
        """
        Executes the appropriate action for the matching delayed event.

        Params:
            delay_id: The ID of the delayed event to act on.
            action: What to do with the delayed event.

        Raises:
            SynapseError if the provided action is unknown, or is unsupported for the target delayed event.
            NotFoundError if no matching delayed event could be found.
        """
        try:
            enum_action = _UpdateDelayedEventAction(action)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "'action' is not one of "
                + ", ".join(f"'{m.value}'" for m in _UpdateDelayedEventAction),
                Codes.INVALID_PARAM,
            )

        delay_id = DelayID(delay_id)
        user_localpart = UserLocalpart(requester.user.localpart)

        await self._request_ratelimiter.ratelimit(requester)

        async with self._get_delay_context(delay_id, user_localpart):
            if enum_action == _UpdateDelayedEventAction.CANCEL:
                for removed_timeout_delay_id in await self._store.remove(
                    delay_id, user_localpart
                ):
                    self._unschedule(removed_timeout_delay_id, user_localpart)

            elif enum_action == _UpdateDelayedEventAction.RESTART:
                delay = await self._store.restart(
                    delay_id,
                    user_localpart,
                    self._get_current_ts(),
                )

                self._unschedule(delay_id, user_localpart)
                self._schedule(delay_id, user_localpart, delay)

            elif enum_action == _UpdateDelayedEventAction.SEND:
                args, removed_timeout_delay_ids = await self._store.pop_event(
                    delay_id, user_localpart
                )

                for timeout_delay_id in removed_timeout_delay_ids:
                    self._unschedule(timeout_delay_id, user_localpart)
                await self._send_event(user_localpart, *args)

    async def _send_on_timeout(
        self, delay_id: DelayID, user_localpart: UserLocalpart
    ) -> None:
        del self._delayed_calls[_DelayedCallKey(delay_id, user_localpart)]

        async with self._get_delay_context(delay_id, user_localpart):
            try:
                args, removed_timeout_delay_ids = await self._store.pop_event(
                    delay_id, user_localpart
                )
            except NotFoundError:
                logger.debug(
                    "delay_id %s for local user %s was removed after it timed out, but before it was sent on timeout",
                    delay_id,
                    user_localpart,
                )
                return

            removed_timeout_delay_ids.remove(delay_id)
            for timeout_delay_id in removed_timeout_delay_ids:
                self._unschedule(timeout_delay_id, user_localpart)
            await self._send_event(user_localpart, *args)

    def _schedule(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
        delay: Delay,
    ) -> None:
        """NOTE: Should not be called with a delay_id that isn't in the DB, or with a negative delay."""
        delay_sec = delay / 1000

        logger.info(
            "Scheduling delayed event %s for local user %s to be sent in %.3fs",
            delay_id,
            user_localpart,
            delay_sec,
        )

        self._delayed_calls[_DelayedCallKey(delay_id, user_localpart)] = (
            self._clock.call_later(
                delay_sec,
                run_as_background_process,
                "_send_on_timeout",
                self._send_on_timeout,
                delay_id,
                user_localpart,
            )
        )

    def _unschedule(self, delay_id: DelayID, user_localpart: UserLocalpart) -> None:
        delayed_call = self._delayed_calls.pop(
            _DelayedCallKey(delay_id, user_localpart)
        )
        self._clock.cancel_call_later(delayed_call)

    async def get_all_for_user(self, requester: Requester) -> List[JsonDict]:
        """Return all pending delayed events requested by the given user."""
        await self._request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db
        return await self._store.get_all_for_user(
            UserLocalpart(requester.user.localpart)
        )

    async def _send_event(
        self,
        user_localpart: UserLocalpart,
        room_id: RoomID,
        event_type: EventType,
        state_key: Optional[StateKey],
        origin_server_ts: Optional[Timestamp],
        content: JsonDict,
        txn_id: Optional[str] = None,
    ) -> None:
        user_id = UserID(user_localpart, self._config.server.server_name)
        user_id_str = user_id.to_string()
        requester = create_requester(
            user_id,
            is_guest=await self._store.is_guest(user_id_str),
        )

        try:
            if state_key is not None and event_type == EventTypes.Member:
                membership = content.get("membership", None)
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
                }

                if state_key is not None:
                    event_dict["state_key"] = state_key

                if origin_server_ts is not None:
                    event_dict["origin_server_ts"] = origin_server_ts

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
            event_id = "$" + random_string(43)

        set_tag("event_id", event_id)

    def _get_current_ts(self) -> Timestamp:
        return Timestamp(self._clock.time_msec())

    @asynccontextmanager
    async def _get_delay_context(
        self, delay_id: DelayID, user_localpart: UserLocalpart
    ) -> AsyncIterator[None]:
        await self._initialized_from_db
        # TODO: Use parenthesized context manager once the minimum supported Python version is 3.10
        async with self._state_lock.read(_STATE_LOCK_KEY), self._linearizer.queue(
            _DelayedCallKey(delay_id, user_localpart)
        ):
            yield

    def _get_state_context(self) -> AsyncContextManager:
        return self._state_lock.write(_STATE_LOCK_KEY)
