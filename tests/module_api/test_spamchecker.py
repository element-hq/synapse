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
from typing import Literal

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.rest import admin, login, room, room_upgrade_rest_servlet
from synapse.server import HomeServer
from synapse.types import Codes, JsonDict
from synapse.util.clock import Clock

from tests.server import FakeChannel
from tests.unittest import HomeserverTestCase


class SpamCheckerTestCase(HomeserverTestCase):
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

    def test_user_may_create_room(self) -> None:
        """Test that the user_may_create_room callback is called when a user
        creates a room, and that it receives the correct parameters.
        """

        async def user_may_create_room(
            user_id: str, room_config: JsonDict
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_room_config = room_config
            self.last_user_id = user_id
            return "NOT_SPAM"

        self._module_api.register_spam_checker_callbacks(
            user_may_create_room=user_may_create_room
        )

        expected_room_config = {"foo": "baa"}
        channel = self.create_room(expected_room_config)

        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_user_id, self.user_id)
        self.assertEqual(self.last_room_config, expected_room_config)

    def test_user_may_create_room_with_initial_state(self) -> None:
        """Test that the user_may_create_room callback is called when a user
        creates a room with some initial state events, and that it receives the correct parameters.
        """

        async def user_may_create_room(
            user_id: str, room_config: JsonDict
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_room_config = room_config
            self.last_user_id = user_id
            return "NOT_SPAM"

        self._module_api.register_spam_checker_callbacks(
            user_may_create_room=user_may_create_room
        )

        expected_room_config = {
            "foo": "baa",
            "initial_state": [
                {
                    "type": EventTypes.Topic,
                    "content": {EventContentFields.TOPIC: "foo"},
                }
            ],
        }
        channel = self.create_room(expected_room_config)

        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_user_id, self.user_id)
        self.assertEqual(self.last_room_config, expected_room_config)

    def test_user_may_create_room_on_upgrade(self) -> None:
        """Test that the user_may_create_room callback is called when a room is upgraded."""

        # First, create a room to upgrade.
        channel = self.create_room({EventContentFields.TOPIC: "foo"})

        self.assertEqual(channel.code, 200)
        room_id = channel.json_body["room_id"]

        async def user_may_create_room(
            user_id: str, room_config: JsonDict
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_room_config = room_config
            self.last_user_id = user_id
            return "NOT_SPAM"

        # Register the callback for spam checking.
        self._module_api.register_spam_checker_callbacks(
            user_may_create_room=user_may_create_room
        )

        # Now upgrade the room.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{room_id}/upgrade",
            # This will upgrade a room to the same version, but that's fine.
            content={"new_version": DEFAULT_ROOM_VERSION},
            access_token=self.token,
        )

        # Check that the callback was called and the room was upgraded.
        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_user_id, self.user_id)
        # Check that the initial state received by callback contains the topic event.
        self.assertTrue(
            any(
                event.get("type") == EventTypes.Topic
                and event.get("state_key") == ""
                and event.get("content").get(EventContentFields.TOPIC) == "foo"
                for event in self.last_room_config["initial_state"]
            )
        )

    def test_user_may_create_room_disallowed(self) -> None:
        """Test that the codes response from user_may_create_room callback is respected
        and returned via the API.
        """

        async def user_may_create_room(
            user_id: str, room_config: JsonDict
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_room_config = room_config
            self.last_user_id = user_id
            return Codes.UNAUTHORIZED

        self._module_api.register_spam_checker_callbacks(
            user_may_create_room=user_may_create_room
        )

        expected_room_config = {"foo": "baa"}
        channel = self.create_room(expected_room_config)

        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], Codes.UNAUTHORIZED)
        self.assertEqual(self.last_user_id, self.user_id)
        self.assertEqual(self.last_room_config, expected_room_config)

    def test_user_may_create_room_compatibility(self) -> None:
        """Test that the user_may_create_room callback is called when a user
        creates a room for a module that uses the old callback signature
        (without the `room_config` parameter)
        """

        async def user_may_create_room(
            user_id: str,
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_user_id = user_id
            return "NOT_SPAM"

        self._module_api.register_spam_checker_callbacks(
            user_may_create_room=user_may_create_room
        )

        channel = self.create_room({"foo": "baa"})

        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_user_id, self.user_id)

    def test_user_may_send_state_event(self) -> None:
        """Test that the user_may_send_state_event callback is called when a state event
        is sent, and that it receives the correct parameters.
        """

        async def user_may_send_state_event(
            user_id: str,
            room_id: str,
            event_type: str,
            state_key: str,
            content: JsonDict,
        ) -> Literal["NOT_SPAM"] | Codes:
            self.last_user_id = user_id
            self.last_room_id = room_id
            self.last_event_type = event_type
            self.last_state_key = state_key
            self.last_content = content
            return "NOT_SPAM"

        self._module_api.register_spam_checker_callbacks(
            user_may_send_state_event=user_may_send_state_event
        )

        channel = self.create_room({})

        self.assertEqual(channel.code, 200)

        room_id = channel.json_body["room_id"]

        event_type = "test.event.type"
        state_key = "test.state.key"
        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/rooms/%s/state/%s/%s"
            % (
                room_id,
                event_type,
                state_key,
            ),
            content={"foo": "bar"},
            access_token=self.token,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_user_id, self.user_id)
        self.assertEqual(self.last_room_id, room_id)
        self.assertEqual(self.last_event_type, event_type)
        self.assertEqual(self.last_state_key, state_key)
        self.assertEqual(self.last_content, {"foo": "bar"})

    def test_user_may_send_state_event_disallows(self) -> None:
        """Test that the user_may_send_state_event callback is called when a state event
        is sent, and that the response is honoured.
        """

        async def user_may_send_state_event(
            user_id: str,
            room_id: str,
            event_type: str,
            state_key: str,
            content: JsonDict,
        ) -> Literal["NOT_SPAM"] | Codes:
            return Codes.FORBIDDEN

        self._module_api.register_spam_checker_callbacks(
            user_may_send_state_event=user_may_send_state_event
        )

        channel = self.create_room({})

        self.assertEqual(channel.code, 200)

        room_id = channel.json_body["room_id"]

        event_type = "test.event.type"
        state_key = "test.state.key"
        channel = self.make_request(
            "PUT",
            "/_matrix/client/r0/rooms/%s/state/%s/%s"
            % (
                room_id,
                event_type,
                state_key,
            ),
            content={"foo": "bar"},
            access_token=self.token,
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)
