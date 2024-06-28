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
from http import HTTPStatus
from typing import TYPE_CHECKING, Dict, List, Optional

from twisted.internet.interfaces import IDelayedCall

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, NotFoundError, ShadowBanError, SynapseError
from synapse.logging.opentracing import set_tag
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.databases.main.futures import (
    EventType,
    FutureID,
    FutureTokenType,
    StateKey,
    Timeout,
    Timestamp,
)
from synapse.types import JsonDict, Requester, RoomID, UserID, create_requester
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class FuturesHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.config = hs.config
        self.clock = hs.get_clock()
        self.request_ratelimiter = hs.get_request_ratelimiter()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.room_member_handler = hs.get_room_member_handler()

        self._hostname = hs.hostname

        self._futures: Dict[FutureID, IDelayedCall] = {}

        async def _schedule_db_futures() -> None:
            all_future_timestamps = await self.store.get_all_future_timestamps()

            # Get the time after awaiting to increase accuracy.
            # For even more accuracy, could get the time on each loop iteration.
            current_ts = self._get_current_ts()

            for future_id, ts in all_future_timestamps:
                timeout = _get_timeout_between(current_ts, ts)
                if timeout > 0:
                    self._schedule_future(future_id, timeout)
                else:
                    logger.info("Scheduling timeout for future_id %s now", future_id)
                    run_as_background_process(
                        "_send_future",
                        self._send_future,
                        future_id,
                    )

        self._initialized_from_db = run_as_background_process(
            "_schedule_db_futures", _schedule_db_futures
        )

    async def add_future(
        self,
        requester: Requester,
        *,
        room_id: str,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        timeout: Optional[int],
        group_id: Optional[str],
        is_custom_endpoint: bool = False,  # TODO: Remove this eventually
    ) -> JsonDict:
        """Creates a new future, and if it is a timeout future, schedules it to be sent.

        Params:
            requester: The initial requester of the future.
            room_id: The room where the future event should be sent.
            event_type: The event type of the future event.
            state_key: The state key of the future event, or None if it is not a state event.
            origin_server_ts: The custom timestamp to send the future event with.
                If None, the timestamp will be the actual time when the future is sent.
            content: The content of the future event.
            timeout: How long (in milliseconds) to wait before automatically sending the future.
                If None, the future will never be automatically sent.
            group_id: The future group this future belongs to.
                If None, the future will be added to a new group with an auto-generated ID.

        Returns:
            A mapping of tokens that can be used in requests to send, cancel, or refresh the future.
        """
        if timeout is not None:
            max_timeout = self.config.experimental.msc4140_max_future_timeout_duration
            if timeout > max_timeout:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    f"'{'future_timeout' if not is_custom_endpoint else 'timeout'}' is too large",
                    Codes.INVALID_PARAM,
                    {
                        "max_timeout_duration": max_timeout,
                    },
                )
        else:
            if group_id is None:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "At least one of 'future_timeout' and 'future_group_id' is required",
                    Codes.MISSING_PARAM,
                )

        await self.request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db

        if (
            timeout is None
            and group_id is not None
            and not await self.store.has_group_id(requester.user, group_id)
        ):
            raise SynapseError(
                HTTPStatus.CONFLICT,
                # TODO: Think of a better wording for this
                "The given 'future_group_id' does not exist and cannot be created with an action future",
                Codes.INVALID_PARAM,
            )

        # TODO: For state event timeout futures, track state at request time & cancel future on state change!
        (
            future_id,
            group_id,
            send_token,
            cancel_token,
            refresh_token,
        ) = await self.store.add_future(
            user_id=requester.user,
            group_id=group_id,
            room_id=RoomID.from_string(room_id),
            event_type=event_type,
            state_key=state_key,
            origin_server_ts=origin_server_ts,
            content=content,
            timeout=timeout,
            timestamp=timeout + self.clock.time_msec() if timeout is not None else None,
        )

        if timeout is not None:
            self._schedule_future(future_id, Timeout(timeout))

        ret = {
            "future_group_id": group_id,
            "send_token": send_token,
            "cancel_token": cancel_token,
        }
        # TODO: type hint for non-None refresh_token return value when timeout argument is non-None
        if refresh_token is not None:
            ret["refresh_token"] = refresh_token
        return ret

    async def use_future_token(self, token: str) -> None:
        """Executes the appropriate action for the given token.

        Params:
            token: The token of the future to act on.
        """
        await self._initialized_from_db

        future_id, future_token_type = await self.store.get_future_by_token(token)
        if future_token_type == FutureTokenType.SEND:
            await self._send_future(future_id)
        elif future_token_type == FutureTokenType.CANCEL:
            await self._cancel_future(future_id)
        elif future_token_type == FutureTokenType.REFRESH:
            await self._refresh_future(future_id)

    async def _send_future(self, future_id: FutureID) -> None:
        await self._initialized_from_db

        args = await self.store.pop_future_event(future_id)

        self._unschedule_future(future_id)
        await self._send_event(*args)

    async def _send_future_on_timeout(self, future_id: FutureID) -> None:
        await self._initialized_from_db

        try:
            args = await self.store.pop_future_event(future_id)
        except NotFoundError:
            logger.warning(
                "future_id %s missing from DB on timeout, so it must have been cancelled/sent",
                future_id,
            )
        else:
            await self._send_event(*args)

    async def _cancel_future(self, future_id: FutureID) -> None:
        await self._initialized_from_db

        await self.store.remove_future(future_id)

        self._unschedule_future(future_id)

    async def _refresh_future(self, future_id: FutureID) -> None:
        await self._initialized_from_db

        timeout = await self.store.update_future_timestamp(
            future_id,
            self._get_current_ts(),
        )

        self._unschedule_future(future_id)
        self._schedule_future(future_id, timeout)

    def _schedule_future(
        self,
        future_id: FutureID,
        timeout: Timeout,
    ) -> None:
        """NOTE: Should not be called with a future_id that isn't in the DB, or with a negative timeout."""
        delay_sec = timeout / 1000

        logger.info(
            "Scheduling timeout for future_id %d in %.3fs", future_id, delay_sec
        )

        self._futures[future_id] = self.clock.call_later(
            delay_sec,
            run_as_background_process,
            "_send_future_on_timeout",
            self._send_future_on_timeout,
            future_id,
        )

    def _unschedule_future(self, future_id: FutureID) -> None:
        try:
            delayed_call = self._futures.pop(future_id)
            self.clock.cancel_call_later(delayed_call)
        except KeyError:
            logger.debug("future_id %s was not mapped to a delayed call", future_id)

    async def get_all_futures_for_user(self, requester: Requester) -> List[JsonDict]:
        """Return all pending futures requested by the given user."""
        await self.request_ratelimiter.ratelimit(requester)
        await self._initialized_from_db
        return await self.store.get_all_futures_for_user(requester.user)

    # TODO: Consider getting a list of all timeout futures that were in this one's group, and cancel their delayed calls
    async def _send_event(
        self,
        user_localpart: str,
        room_id: RoomID,
        event_type: EventType,
        state_key: Optional[StateKey],
        origin_server_ts: Optional[Timestamp],
        content: JsonDict,
        # TODO: support guests
        # is_guest: bool,
        txn_id: Optional[str] = None,
    ) -> None:
        user_id = UserID(user_localpart, self._hostname)
        requester = create_requester(
            user_id,
            # is_guest=is_guest,
        )

        try:
            if state_key is not None and event_type == EventTypes.Member:
                membership = content.get("membership", None)
                event_id, _ = await self.room_member_handler.update_membership(
                    requester,
                    target=UserID.from_string(state_key),
                    room_id=str(room_id),
                    action=membership,
                    content=content,
                    origin_server_ts=origin_server_ts,
                )
            else:
                event_dict: JsonDict = {
                    "type": event_type,
                    "content": content,
                    "room_id": room_id.to_string(),
                    "sender": user_id.to_string(),
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


def _get_timeout_between(from_ts: Timestamp, to_ts: Timestamp) -> Timeout:
    return Timeout(to_ts - from_ts)
