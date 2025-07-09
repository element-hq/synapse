#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016-2021 The Matrix.org Foundation C.I.C.
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
#

import logging
from typing import TYPE_CHECKING

from synapse.events import EventBase
from synapse.types.handlers.policy_server import RECOMMENDATION_OK
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class RoomPolicyHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._event_auth_handler = hs.get_event_auth_handler()
        self._federation_client = hs.get_federation_client()

    async def is_event_allowed(self, event: EventBase) -> bool:
        """Check if the given event is allowed in the room by the policy server.

        Note: This will *always* return True if the room's policy server is Synapse
        itself. This is because Synapse can't be a policy server (currently).

        If no policy server is configured in the room, this returns True. Similarly, if
        the policy server is invalid in any way (not joined, not a server, etc), this
        returns True.

        If a valid and contactable policy server is configured in the room, this returns
        True if that server suggests the event is not spammy, and False otherwise.

        Args:
            event: The event to check. This should be a fully-formed PDU.

        Returns:
            bool: True if the event is allowed in the room, False otherwise.
        """
        if event.type == "org.matrix.msc4284.policy" and event.state_key is not None:
            return True  # always allow policy server change events

        policy_event = await self._storage_controllers.state.get_current_state_event(
            event.room_id, "org.matrix.msc4284.policy", ""
        )
        if not policy_event:
            return True  # no policy server == default allow

        policy_server = policy_event.content.get("via", "")
        if policy_server is None or not isinstance(policy_server, str):
            return True  # no policy server == default allow

        if policy_server == self._hs.hostname:
            return True  # Synapse itself can't be a policy server (currently)

        try:
            parse_and_validate_server_name(policy_server)
        except ValueError:
            return True  # invalid policy server == default allow

        is_in_room = await self._event_auth_handler.is_host_in_room(
            event.room_id, policy_server
        )
        if not is_in_room:
            return True  # policy server not in room == default allow

        # At this point, the server appears valid and is in the room, so ask it to check
        # the event.
        recommendation = await self._federation_client.get_pdu_policy_recommendation(
            policy_server, event
        )
        if recommendation != RECOMMENDATION_OK:
            return False

        return True  # default allow
