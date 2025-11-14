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

from signedjson.key import decode_verify_key_bytes
from unpaddedbase64 import decode_base64

from synapse.api.errors import SynapseError
from synapse.crypto.keyring import VerifyJsonRequest
from synapse.events import EventBase
from synapse.types.handlers.policy_server import RECOMMENDATION_OK
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

POLICY_SERVER_EVENT_TYPE = "org.matrix.msc4284.policy"
POLICY_SERVER_KEY_ID = "ed25519:policy_server"


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
        if event.type == POLICY_SERVER_EVENT_TYPE and event.state_key is not None:
            return True  # always allow policy server change events

        policy_event = await self._storage_controllers.state.get_current_state_event(
            event.room_id, POLICY_SERVER_EVENT_TYPE, ""
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

        # Check if the event has been signed with the public key in the policy server state event.
        # If it is, we can save an HTTP hit.
        # We actually want to get the policy server state event BEFORE THE EVENT rather than
        # the current state value, else changing the public key will cause all of these checks to fail.
        # However, if we are checking outlier events (which we will due to is_event_allowed being called
        # near the edges at _check_sigs_and_hash) we won't know the state before the event, so the
        # only safe option is to use the current state
        public_key = policy_event.content.get("public_key", None)
        if public_key is not None and isinstance(public_key, str):
            valid = await self._verify_policy_server_signature(
                event, policy_server, public_key
            )
            if valid:
                return True
            # fallthrough to hit /check manually

        # At this point, the server appears valid and is in the room, so ask it to check
        # the event.
        recommendation = await self._federation_client.get_pdu_policy_recommendation(
            policy_server, event
        )
        if recommendation != RECOMMENDATION_OK:
            return False

        return True  # default allow

    async def _verify_policy_server_signature(
        self, event: EventBase, policy_server: str, public_key: str
    ) -> bool:
        # check the event is signed with this (via, public_key).
        verify_json_req = VerifyJsonRequest.from_event(policy_server, event, 0)
        try:
            key_bytes = decode_base64(public_key)
            verify_key = decode_verify_key_bytes(POLICY_SERVER_KEY_ID, key_bytes)
            # We would normally use KeyRing.verify_event_for_server but we can't here as we don't
            # want to fetch the server key, and instead want to use the public key in the state event.
            await self._hs.get_keyring().process_json(verify_key, verify_json_req)
            # if the event is correctly signed by the public key in the policy server state event = Allow
            return True
        except Exception as ex:
            logger.warning(
                "failed to verify event using public key in policy server event: %s", ex
            )
        return False

    async def ask_policy_server_to_sign_event(
        self, event: EventBase, verify: bool = False
    ) -> None:
        """Ask the policy server to sign this event. The signature is added to the event signatures block.

        Does nothing if there is no policy server state event in the room. If the policy server
        refuses to sign the event (as it's marked as spam) does nothing.

        Args:
            event: The event to sign
            verify: If True, verify that the signature is correctly signed by the public_key in the
            policy server state event.
        Raises:
            if verify=True and the policy server signed the event with an invalid signature. Does
            not raise if the policy server refuses to sign the event.
        """
        policy_event = await self._storage_controllers.state.get_current_state_event(
            event.room_id, POLICY_SERVER_EVENT_TYPE, ""
        )
        if not policy_event:
            return
        policy_server = policy_event.content.get("via", None)
        if policy_server is None or not isinstance(policy_server, str):
            return
        # Only ask to sign events if the policy state event has a public_key (so they can be subsequently verified)
        public_key = policy_event.content.get("public_key", None)
        if public_key is None or not isinstance(public_key, str):
            return

        # Ask the policy server to sign this event.
        # We set a smallish timeout here as we don't want to block event sending too long.
        signature = await self._federation_client.ask_policy_server_to_sign_event(
            policy_server,
            event,
            timeout=3000,
        )
        if (
            # the policy server returns {} if it refuses to sign the event.
            signature and len(signature) > 0
        ):
            event.signatures.update(signature)
            if verify:
                is_valid = await self._verify_policy_server_signature(
                    event, policy_server, public_key
                )
                if not is_valid:
                    raise SynapseError(
                        500,
                        f"policy server {policy_server} failed to sign event correctly",
                    )
