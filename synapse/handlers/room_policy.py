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

import attr
from signedjson.key import decode_verify_key_bytes
from unpaddedbase64 import decode_base64

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes, HttpResponseException, SynapseError
from synapse.crypto.keyring import VerifyJsonRequest
from synapse.events import EventBase
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

POLICY_SERVER_KEY_ID = "ed25519:policy_server"


@attr.s(slots=True, auto_attribs=True)
class PolicyServerInfo:
    # name of the server.
    server_name: str

    # the unpadded base64-encoded Ed25519 public key of the server.
    public_key: str


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
            return event.type in [EventTypes.RoomPolicy, "org.matrix.msc4284.policy"]
        return False

    async def _get_policy_server(self, room_id: str) -> PolicyServerInfo | None:
        """Get the policy server's name and Ed25519 public key for the room, if set.

        If the `m.room.policy` state event is invalid, then a policy server is not set. It
        can be invalid if:
        - The room doesn't have an `m.room.policy` state event with empty state key.
        - The policy state event is missing the `via` or `public_keys` field.
        - The policy state event's public keys is missing an `ed25519` key.
        - The via server is not a valid server name.
        - The via server is not in the room.
        - The via server is Synapse itself.

        TODO: Remove unstable MSC4284 support - https://github.com/element-hq/synapse/issues/19502
        This function also checks for the unstable `org.matrix.msc4284.policy` state event.

        Args:
            room_id: The room ID to get the policy server for.

        Returns:
            A tuple of policy server name and its Ed25519 public key (unpadded base64).
            Both values will be None if no policy server is configured or the configration
            is invalid.
        """
        policy_event = await self._storage_controllers.state.get_current_state_event(
            room_id, EventTypes.RoomPolicy, ""
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
            public_keys = policy_event.content.get("public_keys")
            if isinstance(public_keys, dict):
                ed25519_key = public_keys.get("ed25519")
                if isinstance(ed25519_key, str):
                    public_key = ed25519_key

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

        return PolicyServerInfo(policy_server, public_key)

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

        policy_server = await self._get_policy_server(event.room_id)
        if policy_server is None:
            return True  # no policy server configured, so allow

        # Check if the event has been signed with the public key in the policy server
        # state event. If it is, the event is valid according to the policy server and
        # we don't need to request a fresh signature.
        valid = await self._verify_policy_server_signature(
            event, policy_server.server_name, policy_server.public_key
        )
        if valid:
            return True  # valid signature == allow

        # We couldn't save the HTTP hit, so do that hit.
        try:
            await self.ask_policy_server_to_sign_event(event, verify=True)
        except Exception as ex:
            # We probably caught either a refusal to sign, an invalid signature, or
            # some other transient or network error. These are all rejection cases.
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
        if self._is_policy_server_state_event(event):
            # per spec, policy servers aren't asked to sign `m.room.policy` state events
            # with empty state keys
            return

        policy_server = await self._get_policy_server(event.room_id)
        if policy_server is None:
            return

        # Ask the policy server to sign this event.
        try:
            signature = await self._federation_client.ask_policy_server_to_sign_event(
                policy_server.server_name,
                event,
                # We set a smallish timeout here as we don't want to block event sending
                # too long.
                #
                # We were previously seeing regular timeouts with media
                # scanning/checking when the timeout was set to 3s. 30s was chosen based
                # on vibes and light real world testing.
                timeout=30000,
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

            # Note: if the policy server and event sender are the same server, the sender
            # might not have added policy server signatures to the event for whatever reason.
            # When this happens, we don't want to obliterate the event's existing signatures
            # because the event will fail authorization. This is why we add defaults rather
            # than simply `update` the signatures on the event.
            #
            # This situation can happen if the homeserver and policy server parts are
            # logically the same server, but run by different software. For example, Synapse
            # will not ask "itself" for a policy server signature, even if its server name
            # is the designated policy server, so it could send an event outwards that other
            # servers need to manually fetch signatures for. This is the code that allows
            # those events to continue working (because they're legally sent, even if missing
            # the policy server signature).
            event.signatures.setdefault(policy_server.server_name, {}).update(
                signature.get(policy_server.server_name, {})
            )
        except HttpResponseException as ex:
            # re-wrap HTTP errors as `SynapseError` so they can be proxied to clients directly
            raise ex.to_synapse_error() from ex

        if verify:
            is_valid = await self._verify_policy_server_signature(
                event, policy_server.server_name, policy_server.public_key
            )
            if not is_valid:
                raise SynapseError(
                    500,
                    f"policy server {policy_server.server_name} failed to sign event correctly",
                )
