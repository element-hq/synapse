#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
import random
from typing import TYPE_CHECKING, Iterable, List, Optional

from synapse.api.constants import EduTypes, EventTypes, Membership, PresenceState
from synapse.api.errors import AuthError, SynapseError
from synapse.events import EventBase
from synapse.events.utils import SerializeEventConfig
from synapse.handlers.presence import format_user_presence_state
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.streams.config import PaginationConfig
from synapse.types import JsonDict, Requester, UserID
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class EventStreamHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.hs = hs

        self.notifier = hs.get_notifier()
        self.state = hs.get_state_handler()
        self._server_notices_sender = hs.get_server_notices_sender()
        self._event_serializer = hs.get_event_client_serializer()

    async def get_stream(
        self,
        requester: Requester,
        pagin_config: PaginationConfig,
        timeout: int = 0,
        as_client_event: bool = True,
        affect_presence: bool = True,
        room_id: Optional[str] = None,
    ) -> JsonDict:
        """Fetches the events stream for a given user."""

        if room_id:
            blocked = await self.store.is_room_blocked(room_id)
            if blocked:
                raise SynapseError(403, "This room has been blocked on this server")

        # send any outstanding server notices to the user.
        await self._server_notices_sender.on_user_syncing(requester.user.to_string())

        presence_handler = self.hs.get_presence_handler()

        context = await presence_handler.user_syncing(
            requester.user.to_string(),
            requester.device_id,
            affect_presence=affect_presence,
            presence_state=PresenceState.ONLINE,
        )
        with context:
            if timeout:
                # If they've set a timeout set a minimum limit.
                timeout = max(timeout, 500)

                # Add some randomness to this value to try and mitigate against
                # thundering herds on restart.
                timeout = random.randint(int(timeout * 0.9), int(timeout * 1.1))

            stream_result = await self.notifier.get_events_for(
                requester.user,
                pagin_config,
                timeout,
                is_guest=requester.is_guest,
                explicit_room_id=room_id,
            )
            events = stream_result.events

            time_now = self.clock.time_msec()

            # When the user joins a new room, or another user joins a currently
            # joined room, we need to send down presence for those users.
            to_add: List[JsonDict] = []
            for event in events:
                if not isinstance(event, EventBase):
                    continue
                if event.type == EventTypes.Member:
                    if event.membership != Membership.JOIN:
                        continue
                    # Send down presence.
                    if event.state_key == requester.user.to_string():
                        # Send down presence for everyone in the room.
                        users: Iterable[str] = await self.store.get_users_in_room(
                            event.room_id
                        )
                    else:
                        users = [event.state_key]

                    states = await presence_handler.get_states(users)
                    to_add.extend(
                        {
                            "type": EduTypes.PRESENCE,
                            "content": format_user_presence_state(state, time_now),
                        }
                        for state in states
                    )

            events.extend(to_add)

            chunks = await self._event_serializer.serialize_events(
                events,
                time_now,
                config=SerializeEventConfig(
                    as_client_event=as_client_event, requester=requester
                ),
            )

            chunk = {
                "chunk": chunks,
                "start": await stream_result.start_token.to_string(self.store),
                "end": await stream_result.end_token.to_string(self.store),
            }

            return chunk


class EventHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()

    async def get_event(
        self,
        user: UserID,
        room_id: Optional[str],
        event_id: str,
        show_redacted: bool = False,
    ) -> Optional[EventBase]:
        """Retrieve a single specified event.

        Args:
            user: The local user requesting the event
            room_id: The expected room id. We'll return None if the
                event's room does not match.
            event_id: The event ID to obtain.
            show_redacted: Should the full content of redacted events be returned?
        Returns:
            An event, or None if there is no event matching this ID.
        Raises:
            AuthError: if the user does not have the rights to inspect this event.
        """
        redact_behaviour = (
            EventRedactBehaviour.as_is if show_redacted else EventRedactBehaviour.redact
        )
        event = await self.store.get_event(
            event_id,
            check_room_id=room_id,
            redact_behaviour=redact_behaviour,
            allow_none=True,
        )

        if not event:
            return None

        is_user_in_room = await self.store.check_local_user_in_room(
            user_id=user.to_string(), room_id=event.room_id
        )
        # The user is peeking if they aren't in the room already
        is_peeking = not is_user_in_room

        filtered = await filter_events_for_client(
            self._storage_controllers,
            user.to_string(),
            [event],
            is_peeking=is_peeking,
        )

        if not filtered:
            raise AuthError(403, "You don't have permission to access that event.")

        return event
