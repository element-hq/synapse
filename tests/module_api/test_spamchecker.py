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
from http import HTTPStatus
from typing import Literal

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    Membership,
)
from synapse.api.room_versions import RoomVersions
from synapse.config.server import DEFAULT_ROOM_VERSION
from synapse.events import make_event_from_dict
from synapse.module_api import EventBase
from synapse.rest import admin, login, room, room_upgrade_rest_servlet
from synapse.server import HomeServer
from synapse.types import Codes, JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel
from tests.unittest import HomeserverTestCase


class SpamCheckerTestCase(HomeserverTestCase):
    """Tests for the spam checker module API."""

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


class FederatedEventSpamCheckMetadataTestCase(unittest.FederatingHomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self._module_api = hs.get_module_api()
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._federation_event_handler = hs.get_federation_event_handler()
        self._federation_server = hs.get_federation_server()
        self._state_handler = hs.get_state_handler()
        self._persistence_controller = hs.get_storage_controllers().persistence

        # Create a room
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        self.room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            is_public=True,
            room_version=RoomVersions.V10.identifier,
        )

        # Prepare a join for the 'remote' user
        state_map = self.get_success(
            self._storage_controllers.state.get_current_state(self.room_id)
        )
        forward_extremity_event_ids = self.get_success(
            self.hs.get_datastores().main.get_latest_event_ids_in_room(self.room_id)
        )
        self.remote_user_id = f"@remoteuser:{self.OTHER_SERVER_NAME}"
        self.remote_user_join_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": self.remote_user_id,
                    "state_key": self.remote_user_id,
                    "depth": 1000,
                    "origin_server_ts": 1,
                    "type": EventTypes.Member,
                    "content": {"membership": Membership.JOIN},
                    "auth_events": [
                        state_map[(EventTypes.Create, "")].event_id,
                        state_map[(EventTypes.JoinRules, "")].event_id,
                    ],
                    "prev_events": list(forward_extremity_event_ids),
                }
            ),
            room_version=RoomVersions.V10,
        )

        # Send the join
        self.get_success(
            self._federation_event_handler.on_receive_pdu(
                self.OTHER_SERVER_NAME, self.remote_user_join_event
            )
        )

        # Check the join made it to the 'local' view of the room
        self.helper.get_event(
            room_id=self.room_id,
            event_id=self.remote_user_join_event.event_id,
            tok=user1_tok,
            expect_code=HTTPStatus.OK,
        )

    def test_federated_events_with_spam_checker_metadata(self) -> None:
        """
        Simulates receiving spammy and non-spammy events over federation,
        then checks their `spam_checker_spammy` flag is set properly.
        """

        async def check_event_for_spam(event: EventBase) -> Literal["NOT_SPAM"] | Codes:
            if event.type == EventTypes.Message:
                if "ham" not in event.content["body"]:
                    return Codes.FORBIDDEN
            return "NOT_SPAM"

        # Register a spam checker callback that only allows messages with 'ham'
        self._module_api.register_spam_checker_callbacks(
            check_event_for_spam=check_event_for_spam
        )

        # Prepare a spammy and a non-spammy event.
        forward_extremity_event_ids = self.get_success(
            self._store.get_latest_event_ids_in_room(self.room_id)
        )
        state_map = self.get_success(
            self._storage_controllers.state.get_current_state(self.room_id)
        )
        spammy_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": self.remote_user_id,
                    "depth": 2000,
                    "origin_server_ts": 2,
                    "type": EventTypes.Message,
                    "content": {"body": "this is spam", "msgtype": "m.text"},
                    "auth_events": [
                        state_map[(EventTypes.Create, "")].event_id,
                        state_map[(EventTypes.JoinRules, "")].event_id,
                        state_map[(EventTypes.Member, self.remote_user_id)].event_id,
                    ],
                    "prev_events": list(forward_extremity_event_ids),
                }
            ),
            room_version=RoomVersions.V10,
        )
        non_spammy_event = make_event_from_dict(
            self.add_hashes_and_signatures_from_other_server(
                {
                    "room_id": self.room_id,
                    "sender": self.remote_user_id,
                    "depth": 2000,
                    "origin_server_ts": 2,
                    "type": EventTypes.Message,
                    "content": {"body": "delicious ham", "msgtype": "m.text"},
                    "auth_events": [
                        state_map[(EventTypes.Create, "")].event_id,
                        state_map[(EventTypes.JoinRules, "")].event_id,
                        state_map[(EventTypes.Member, self.remote_user_id)].event_id,
                    ],
                    "prev_events": list(forward_extremity_event_ids),
                }
            ),
            room_version=RoomVersions.V10,
        )

        # Receive these events over federation
        # We need to let the federation server have them because it will
        # invoke `_check_sigs_and_hash` which invokes the spam checker.
        self.get_success(
            self._federation_server._handle_received_pdu(
                self.OTHER_SERVER_NAME, spammy_event
            )
        )
        self.get_success(
            self._federation_server._handle_received_pdu(
                self.OTHER_SERVER_NAME, non_spammy_event
            )
        )

        # Retrieve the events from the database
        retrieved_spammy_event = self.get_success(
            self._store.get_event(spammy_event.event_id, allow_rejected=True)
        )
        retrieved_non_spammy_event = self.get_success(
            self._store.get_event(non_spammy_event.event_id, allow_rejected=True)
        )

        # Assert the spammy flags (and soft-failed flags, for good measure) are set properly
        self.assertTrue(
            retrieved_spammy_event.internal_metadata.spam_checker_spammy,
            "Spammy inbound event should be marked as spam_checker_spammy!",
        )
        self.assertTrue(
            retrieved_spammy_event.internal_metadata.is_soft_failed(),
            "Spammy inbound event should be soft-failed.",
        )

        self.assertFalse(
            retrieved_non_spammy_event.internal_metadata.spam_checker_spammy,
            "Non-spammy inbound event should not be marked as spam_checker_spammy!",
        )
        self.assertFalse(
            retrieved_non_spammy_event.internal_metadata.is_soft_failed(),
            "Non-spammy inbound event should not be soft-failed.",
        )
