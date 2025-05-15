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
        policy_event = await self._storage_controllers.state.get_current_state_event(
            event.room_id, "org.matrix.msc4284.policy", ""
        )
        if not policy_event:
            logger.info("Allowing %s due to no policy event", event.event_id)
            return True  # no policy server == default allow

        policy_server = policy_event.content.get("via", "")
        if policy_server is None or not isinstance(policy_server, str):
            logger.info("Allowing %s due to missing via", event.event_id)
            return True  # no policy server == default allow

        if policy_server == self._hs.hostname:
            logger.info("Allowing %s due to self policy server", event.event_id)
            return True  # Synapse itself can't be a policy server (currently)

        try:
            parse_and_validate_server_name(policy_server)
        except ValueError:
            logger.info("Allowing %s due to invalid policy server name", event.event_id)
            return True  # invalid policy server == default allow

        is_in_room = await self._event_auth_handler.is_host_in_room(
            event.room_id, policy_server
        )
        if not is_in_room:
            logger.info("Allowing %s due to policy server not in room", event.event_id)
            return True  # policy server not in room == default allow

        # At this point, the server appears valid and is in the room, so ask it to check
        # the event.
        recommendation = await self._federation_client.get_pdu_policy_recommendation(
            policy_server, event
        )
        if recommendation != RECOMMENDATION_OK:
            logger.info("Denying %s due to policy server", event.event_id)
            return False

        logger.info("Allowing %s due to policy server saying so", event.event_id)
        return True  # default allow
