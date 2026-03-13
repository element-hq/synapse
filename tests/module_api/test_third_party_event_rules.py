#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 The Matrix.org Foundation C.I.C.
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
from twisted.internet.testing import MemoryReactor

from synapse.module_api import EventBase, StateMap, UserID
from synapse.rest import admin, login, room, room_upgrade_rest_servlet
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.server import FakeChannel
from tests.unittest import HomeserverTestCase


class ThirdPartyEventRulesTestCase(HomeserverTestCase):
    servlets = [
        room.register_servlets,
        admin.register_servlets,
        login.register_servlets,
        room_upgrade_rest_servlet.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self._module_api = homeserver.get_module_api()
        self.user_id = self.register_user("user", "password")
        self.token = self.login("user", "password")

    def create_room(self, content: JsonDict) -> FakeChannel:
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/createRoom",
            content,
            access_token=self.token,
        )

        return channel

    def test_check_event_allowed_with_mentions(self) -> None:
        """Test that the check_event_allowed callback works with a message containing mentions."""

        async def check_event_allowed(
            event: EventBase,
            state_events: StateMap,
        ) -> tuple[bool, dict | None]:
            return True, None

        self._module_api.register_third_party_rules_callbacks(
            check_event_allowed=check_event_allowed
        )

        channel = self.create_room({})

        self.assertEqual(channel.code, 200)

        room_id = channel.json_body["room_id"]

        event_id = self.create_and_send_event(
            room_id,
            UserID.from_string(self.user_id),
            content={
                "body": "test",
                "msgtype": "m.text",
                "m.mentions": {},
            },
        )

        self.assertNotEquals(event_id, None)
