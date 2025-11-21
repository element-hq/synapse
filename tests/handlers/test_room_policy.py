#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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
from unittest import mock

import signedjson
from signedjson.key import encode_verify_key_base64, get_verify_key

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import SynapseError
from synapse.crypto.event_signing import compute_event_signature
from synapse.events import EventBase, make_event_from_dict
from synapse.handlers.room_policy import POLICY_SERVER_KEY_ID
from synapse.rest import admin
from synapse.rest.client import filter, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.types.handlers.policy_server import RECOMMENDATION_OK, RECOMMENDATION_SPAM
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils import event_injection


class RoomPolicyTestCase(unittest.FederatingHomeserverTestCase):
    """Tests room policy handler."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        filter.register_servlets,
        sync.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # mock out the federation transport client
        self.mock_federation_transport_client = mock.Mock(
            spec=[
                "get_policy_recommendation_for_pdu",
                "ask_policy_server_to_sign_event",
            ]
        )
        self.mock_federation_transport_client.get_policy_recommendation_for_pdu = (
            mock.AsyncMock()
        )
        self.mock_federation_transport_client.ask_policy_server_to_sign_event = (
            mock.AsyncMock()
        )
        return super().setup_test_homeserver(
            federation_transport_client=self.mock_federation_transport_client
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.hs = hs
        self.handler = hs.get_room_policy_handler()
        main_store = self.hs.get_datastores().main

        # Create a room
        self.creator = self.register_user("creator", "test1234")
        self.creator_token = self.login("creator", "test1234")
        self.room_id = self.helper.create_room_as(
            room_creator=self.creator, tok=self.creator_token
        )
        room_version = self.get_success(main_store.get_room_version(self.room_id))
        self.room_version = room_version
        self.signing_key = signedjson.key.generate_signing_key("policy_server")

        # Create some sample events
        self.spammy_event = make_event_from_dict(
            room_version=room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is a spammy event.",
                },
            },
        )
        self.not_spammy_event = make_event_from_dict(
            room_version=room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@not_spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is a NOT spammy event.",
                },
            },
        )

        # Prepare the policy server mock to decide spam vs not spam on those events
        self.call_count = 0

        async def get_policy_recommendation_for_pdu(
            destination: str,
            pdu: EventBase,
            timeout: int | None = None,
        ) -> JsonDict:
            self.call_count += 1
            self.assertEqual(destination, self.OTHER_SERVER_NAME)
            if pdu.event_id == self.spammy_event.event_id:
                return {"recommendation": RECOMMENDATION_SPAM}
            elif pdu.event_id == self.not_spammy_event.event_id:
                return {"recommendation": RECOMMENDATION_OK}
            else:
                self.fail("Unexpected event ID")

        self.mock_federation_transport_client.get_policy_recommendation_for_pdu.side_effect = get_policy_recommendation_for_pdu

        # Mock policy server actions on signing events
        async def policy_server_signs_event(
            destination: str, pdu: EventBase, timeout: int | None = None
        ) -> JsonDict | None:
            sigs = compute_event_signature(
                pdu.room_version,
                pdu.get_dict(),
                self.OTHER_SERVER_NAME,
                self.signing_key,
            )
            return sigs

        async def policy_server_signs_event_with_wrong_key(
            destination: str, pdu: EventBase, timeout: int | None = None
        ) -> JsonDict | None:
            sk = signedjson.key.generate_signing_key("policy_server")
            sigs = compute_event_signature(
                pdu.room_version,
                pdu.get_dict(),
                self.OTHER_SERVER_NAME,
                sk,
            )
            return sigs

        async def policy_server_refuses_to_sign_event(
            destination: str, pdu: EventBase, timeout: int | None = None
        ) -> JsonDict | None:
            return {}

        async def policy_server_event_sign_error(
            destination: str, pdu: EventBase, timeout: int | None = None
        ) -> JsonDict | None:
            return None

        self.policy_server_signs_event = policy_server_signs_event
        self.policy_server_refuses_to_sign_event = policy_server_refuses_to_sign_event
        self.policy_server_event_sign_error = policy_server_event_sign_error
        self.policy_server_signs_event_with_wrong_key = (
            policy_server_signs_event_with_wrong_key
        )

    def _add_policy_server_to_room(self, public_key: str | None = None) -> None:
        # Inject a member event into the room
        policy_user_id = f"@policy:{self.OTHER_SERVER_NAME}"
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room_id, policy_user_id, "join"
            )
        )
        content = {
            "via": self.OTHER_SERVER_NAME,
        }
        if public_key is not None:
            content["public_key"] = public_key
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            content,
            tok=self.creator_token,
            state_key="",
        )

    def test_no_policy_event_set(self) -> None:
        # We don't need to modify the room state at all - we're testing the default
        # case where a room doesn't use a policy server.
        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_empty_policy_event_set(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                # empty content (no `via`)
            },
            tok=self.creator_token,
            state_key="",
        )

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_nonstring_policy_event_set(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                "via": 42,  # should be a server name
            },
            tok=self.creator_token,
            state_key="",
        )

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_self_policy_event_set(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                # We ignore events when the policy server is ourselves (for now?)
                "via": (UserID.from_string(self.creator)).domain,
            },
            tok=self.creator_token,
            state_key="",
        )

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_invalid_server_policy_event_set(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                "via": "|this| is *not* a (valid) server name.com",
            },
            tok=self.creator_token,
            state_key="",
        )

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_not_in_room_policy_event_set(self) -> None:
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                "via": f"x.{self.OTHER_SERVER_NAME}",
            },
            tok=self.creator_token,
            state_key="",
        )

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 0)

    def test_spammy_event_is_spam(self) -> None:
        self._add_policy_server_to_room()

        ok = self.get_success(self.handler.is_event_allowed(self.spammy_event))
        self.assertEqual(ok, False)
        self.assertEqual(self.call_count, 1)

    def test_signed_event_is_not_spam(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is a signed event.",
                },
            },
        )

        # We're going to sign the event and check it marks the event as not-spam, without hitting the
        # policy server
        sigs = compute_event_signature(
            event.room_version,
            event.get_dict(),
            self.OTHER_SERVER_NAME,
            self.signing_key,
        )
        event.signatures.update(sigs)

        ok = self.get_success(self.handler.is_event_allowed(event))
        self.assertEqual(ok, True)
        # Make sure we did not make an HTTP hit to get_policy_recommendation_for_pdu
        self.assertEqual(self.call_count, 0)

    def test_ask_policy_server_to_sign_event_ok(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is another signed event.",
                },
            },
        )
        self.mock_federation_transport_client.ask_policy_server_to_sign_event.side_effect = self.policy_server_signs_event
        self.get_success(
            self.handler.ask_policy_server_to_sign_event(event, verify=True)
        )
        self.assertEqual(len(event.signatures), 1)

    def test_ask_policy_server_to_sign_event_refuses(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is spam and is refused.",
                },
            },
        )
        self.mock_federation_transport_client.ask_policy_server_to_sign_event.side_effect = self.policy_server_refuses_to_sign_event
        self.get_success(
            self.handler.ask_policy_server_to_sign_event(event, verify=True)
        )
        self.assertEqual(len(event.signatures), 0)

    def test_ask_policy_server_to_sign_event_cannot_reach(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is spam and is refused.",
                },
            },
        )
        self.mock_federation_transport_client.ask_policy_server_to_sign_event.side_effect = self.policy_server_event_sign_error
        self.get_success(
            self.handler.ask_policy_server_to_sign_event(event, verify=True)
        )
        self.assertEqual(len(event.signatures), 0)

    def test_ask_policy_server_to_sign_event_wrong_sig(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        self.mock_federation_transport_client.ask_policy_server_to_sign_event.side_effect = self.policy_server_signs_event_with_wrong_key
        unverified_event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is signed but with the wrong key.",
                },
            },
        )
        # verify=False so it passes
        self.get_success(
            self.handler.ask_policy_server_to_sign_event(unverified_event, verify=False)
        )
        self.assertEqual(len(unverified_event.signatures), 1)

        verified_event = make_event_from_dict(
            room_version=self.room_version,
            internal_metadata_dict={},
            event_dict={
                "room_id": self.room_id,
                "type": "m.room.message",
                "sender": "@spammy:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "This is signed but with the wrong key.",
                },
            },
        )
        # verify=True so it fails
        self.get_failure(
            self.handler.ask_policy_server_to_sign_event(verified_event, verify=True),
            SynapseError,
        )

    def test_policy_server_signatures_end_to_end(self) -> None:
        verify_key_str = encode_verify_key_base64(get_verify_key(self.signing_key))
        self._add_policy_server_to_room(public_key=verify_key_str)
        self.mock_federation_transport_client.ask_policy_server_to_sign_event.side_effect = self.policy_server_signs_event
        # Send an event and ensure we get a policy server signature on it.
        resp = self.helper.send_event(
            self.room_id,
            "m.room.message",
            {"body": "honk", "msgtype": "m.text"},
            tok=self.creator_token,
        )
        ev = self._fetch_federation_event(resp["event_id"])
        assert ev is not None
        sig = (
            ev.get("signatures", {})
            .get(self.OTHER_SERVER_NAME, {})
            .get(POLICY_SERVER_KEY_ID, None)
        )
        self.assertNotEquals(
            sig,
            None,
            f"event did not include policy server signature, signature block = {ev.get('signatures', None)}",
        )

    def _fetch_federation_event(self, event_id: str) -> JsonDict | None:
        # Request federation events to see the signatures
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/user/%s/filter" % (self.creator),
            {"event_format": "federation"},
            self.creator_token,
        )
        self.assertEqual(channel.code, 200)
        filter_id = channel.json_body["filter_id"]
        # Note: we could use `/context`, but given we don't test that neutral events are
        # delivered over `/sync` anywhere else, might as well implicitly test it here.
        channel = self.make_request(
            "GET",
            "/sync?filter=%s" % filter_id,
            access_token=self.creator_token,
        )
        self.assertEqual(channel.code, 200, channel.result)

        for ev in channel.json_body["rooms"]["join"][self.room_id]["timeline"][
            "events"
        ]:
            if ev["event_id"] == event_id:
                return ev
        return None
