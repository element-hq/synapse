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
from typing import Optional
from unittest import mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.events import EventBase, make_event_from_dict
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.types.handlers.policy_server import RECOMMENDATION_OK, RECOMMENDATION_SPAM
from synapse.util import Clock

from tests import unittest
from tests.test_utils import event_injection


class RoomPolicyTestCase(unittest.FederatingHomeserverTestCase):
    """Tests room policy handler."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # mock out the federation transport client
        self.mock_federation_transport_client = mock.Mock(
            spec=["get_policy_recommendation_for_pdu"]
        )
        self.mock_federation_transport_client.get_policy_recommendation_for_pdu = (
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
            timeout: Optional[int] = None,
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

    def _add_policy_server_to_room(self) -> None:
        # Inject a member event into the room
        policy_user_id = f"@policy:{self.OTHER_SERVER_NAME}"
        self.get_success(
            event_injection.inject_member_event(
                self.hs, self.room_id, policy_user_id, "join"
            )
        )
        self.helper.send_state(
            self.room_id,
            "org.matrix.msc4284.policy",
            {
                "via": self.OTHER_SERVER_NAME,
            },
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

    def test_not_spammy_event_is_not_spam(self) -> None:
        self._add_policy_server_to_room()

        ok = self.get_success(self.handler.is_event_allowed(self.not_spammy_event))
        self.assertEqual(ok, True)
        self.assertEqual(self.call_count, 1)
