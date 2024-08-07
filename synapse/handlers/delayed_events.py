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
from enum import Enum
from http import HTTPStatus
from typing import TYPE_CHECKING, Dict, Optional

import attr

from twisted.internet.interfaces import IDelayedCall

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, ShadowBanError, SynapseError
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
from synapse.types import JsonDict, Requester, RoomID, UserID, create_requester
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


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
        self.store = hs.get_datastores().main
        self.config = hs.config
        self.clock = hs.get_clock()
        self.request_ratelimiter = hs.get_request_ratelimiter()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.room_member_handler = hs.get_room_member_handler()

        self._delayed_calls: Dict[_DelayedCallKey, IDelayedCall] = {}

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
            max_delay = self.config.experimental.msc4140_max_delay
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

        await self.request_ratelimiter.ratelimit(requester)

        # TODO: Validate that the event is valid before scheduling it

        user_localpart = UserLocalpart(requester.user.localpart)
        delay_id = await self.store.add(
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

        await self.request_ratelimiter.ratelimit(requester)

        if enum_action == _UpdateDelayedEventAction.CANCEL:
            for removed_timeout_delay_id in await self.store.remove(
                delay_id, user_localpart
            ):
                self._unschedule(removed_timeout_delay_id, user_localpart)

        elif enum_action == _UpdateDelayedEventAction.RESTART:
            delay = await self.store.restart(
                delay_id,
                user_localpart,
                self._get_current_ts(),
            )

            self._unschedule(delay_id, user_localpart)
            self._schedule(delay_id, user_localpart, delay)

        elif enum_action == _UpdateDelayedEventAction.SEND:
            args, removed_timeout_delay_ids = await self.store.pop_event(
                delay_id, user_localpart
            )

            for timeout_delay_id in removed_timeout_delay_ids:
                self._unschedule(timeout_delay_id, user_localpart)
            await self._send_event(user_localpart, *args)

    async def _send_on_timeout(
        self, delay_id: DelayID, user_localpart: UserLocalpart
    ) -> None:
        del self._delayed_calls[_DelayedCallKey(delay_id, user_localpart)]

        args, removed_timeout_delay_ids = await self.store.pop_event(
            delay_id, user_localpart
        )

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
            self.clock.call_later(
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
        self.clock.cancel_call_later(delayed_call)

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
        user_id = UserID(user_localpart, self.config.server.server_name)
        user_id_str = user_id.to_string()
        requester = create_requester(
            user_id,
            is_guest=await self.store.is_guest(user_id_str),
        )

        try:
            if state_key is not None and event_type == EventTypes.Member:
                membership = content.get("membership", None)
                event_id, _ = await self.room_member_handler.update_membership(
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
                ) = await self.event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    event_dict,
                    txn_id=txn_id,
                )
                event_id = event.event_id
        except ShadowBanError:
            event_id = "$" + random_string(43)

        set_tag("event_id", event_id)

    def _get_current_ts(self) -> Timestamp:
        return Timestamp(self.clock.time_msec())
