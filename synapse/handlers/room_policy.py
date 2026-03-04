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

from synapse.api.errors import Codes, SynapseError
from synapse.crypto.keyring import VerifyJsonRequest
from synapse.events import EventBase
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

POLICY_SERVER_EVENT_TYPE = "m.room.policy"
POLICY_SERVER_KEY_ID = "ed25519:policy_server"


class RoomPolicyHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._event_auth_handler = hs.get_event_auth_handler()
        self._federation_client = hs.get_federation_client()

    def _is_policy_server_state_event(self, event: EventBase) -> bool:
        state_key = event.get_state_key()
        if state_key is not None and state_key == "":
            # TODO: Remove unstable MSC4284 support
            # https://github.com/element-hq/synapse/issues/19502
            # Note: we can probably drop this whole function when we remove unstable support
            return event.type in [POLICY_SERVER_EVENT_TYPE, "org.matrix.msc4284.policy"]
        return False

    async def _get_policy_server(self, room_id: str) -> tuple[str, str] | None:
        """Get the policy server name for a room.

        Args:
            room_id: The room ID to get the policy server for.

        Returns:
            The policy server name, or None if no policy server is configured or the
            configuration is invalid.
        """
        policy_event = await self._storage_controllers.state.get_current_state_event(
            room_id, POLICY_SERVER_EVENT_TYPE, ""
        )
        public_key = None
        if not policy_event:
            # TODO: Remove unstable MSC4284 support
            # https://github.com/element-hq/synapse/issues/19502
            policy_event = (
                await self._storage_controllers.state.get_current_state_event(
                    room_id, "org.matrix.msc4284.policy", ""
                )
            )
            if not policy_event:
                return None  # neither stable or unstable configured

            # Unstable configured, grab its public key
            public_key = policy_event.content.get("public_key", None)
        else:
            # Stable configured, grab its public key
            public_keys = policy_event.content.get("public_keys", None)
            if public_keys is not None:
                public_key = public_keys.get("ed25519", None)

        if public_key is None or not isinstance(public_key, str):
            return None  # no public key means no policy server

        policy_server = policy_event.content.get("via", "")
        if policy_server is None or not isinstance(policy_server, str):
            return None  # no policy server

        if policy_server == self._hs.hostname:
            return None  # Synapse itself can't be a policy server (currently)

        try:
            parse_and_validate_server_name(policy_server)
        except ValueError:
            return None  # invalid policy server

        is_in_room = await self._event_auth_handler.is_host_in_room(
            room_id, policy_server
        )
        if not is_in_room:
            return None  # policy server not in room

        return policy_server, public_key

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
        if self._is_policy_server_state_event(event):
            return True  # always allow policy server change events

        tup = await self._get_policy_server(event.room_id)
        if tup is None:
            return True  # no policy server configured, so allow
        policy_server, public_key = tup

        # Check if the event has been signed with the public key in the policy server state event.
        # If it is, we can save an HTTP hit to get a fresh signature.
        valid = await self._verify_policy_server_signature(
            event, policy_server, public_key
        )
        if valid:
            return True  # valid signature == allow

        # We couldn't save the HTTP hit, so do that hit.
        try:
            await self.ask_policy_server_to_sign_event(event, verify=True)
        except SynapseError as ex:
            # We probably caught either a refusal to sign, an invalid signature, or
            # some other transient error. These are all rejection cases.
            logger.warning("Failed to get a signature from the policy server: %s", ex)
            return False

        return True  # passed all verifications and checks, so allow

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
        refuses to sign the event (as it's marked as spam), an error is raised.

        Args:
            event: The event to sign.
            verify: If True, verify that the signature is correctly signed by the policy server's
                defined public key.
        Raises:
            When the policy server refuses to sign the event, or when verify is True and the
            signature is invalid.
        """
        tup = await self._get_policy_server(event.room_id)
        if tup is None:
            return
        policy_server, public_key = tup

        # Ask the policy server to sign this event.
        # We set a smallish timeout here as we don't want to block event sending too long.
        # Note: we expect that http errors will fall through to calling code.
        signature = await self._federation_client.ask_policy_server_to_sign_event(
            policy_server,
            event,
            timeout=3000,
        )
        # TODO: We can *probably* remove this when we remove unstable MSC4284 support.
        # The server *should* be returning either a signature or an error, but there could
        # also be implementation bugs. Whoever reads this when removing unstable MSC4284
        # stuff, make a decision on whether to remove this bit.
        # https://github.com/element-hq/synapse/issues/19502
        if not signature or len(signature) == 0:
            raise SynapseError(
                403,
                "This event has been rejected as probable spam by the policy server",
                Codes.FORBIDDEN,
            )
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
