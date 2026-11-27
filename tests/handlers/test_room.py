from http import HTTPStatus
from unittest.mock import patch

import synapse
from synapse.api.constants import EventTypes, RoomEncryptionAlgorithms
from synapse.api.errors import Codes
from synapse.rest.client import login, room
from synapse.types import JsonDict, create_requester

from tests import unittest
from tests.unittest import override_config


class EncryptedByDefaultTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_encrypted_by_default_config_option_all(self) -> None:
        """Tests that invite-only and non-invite-only rooms have encryption enabled by
        default when the config option encryption_enabled_by_default_for_room_type is "all".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

    @override_config({"encryption_enabled_by_default_for_room_type": "invite"})
    def test_encrypted_by_default_config_option_invite(self) -> None:
        """Tests that only new, invite-only rooms have encryption enabled by default when
        the config option encryption_enabled_by_default_for_room_type is "invite".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

    @override_config({"encryption_enabled_by_default_for_room_type": "off"})
    def test_encrypted_by_default_config_option_off(self) -> None:
        """Tests that neither new invite-only nor non-invite-only rooms have encryption
        enabled by default when the config option
        encryption_enabled_by_default_for_room_type is "off".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_user_supplied_encryption_event_is_not_overwritten(self) -> None:
        """Tests that an m.room.encryption event supplied by the user in the
        initial state takes precedence over the one that would otherwise be forced
        by encryption_enabled_by_default_for_room_type, rather than a duplicate
        default event being sent.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying our own encryption event with a non-default
        # algorithm in the initial state.
        custom_content = {"algorithm": "some.custom.invalid.algorithm"}
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": custom_content,
                    }
                ]
            },
        )

        # Check that the room's encryption event is the one we supplied, not the
        # default that the config option would otherwise force.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, custom_content)

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_empty_encryption_event_does_not_bypass_forced_encryption(self) -> None:
        """Tests that a user cannot bypass encryption_enabled_by_default_for_room_type
        by supplying an empty m.room.encryption event in the initial state. Since
        such an event is not valid (it lacks the required `algorithm` key), the
        forced default must still be applied.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying an empty encryption event in the initial state.
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": {},
                    }
                ]
            },
        )

        # Check that the forced default encryption was still applied on top of the
        # empty event, rather than the bypass succeeding.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_non_string_algorithm_does_not_bypass_forced_encryption(self) -> None:
        """Tests that a user cannot bypass encryption_enabled_by_default_for_room_type
        by supplying an m.room.encryption event whose `algorithm` is not a string.
        The forced default must still be applied.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying an encryption event with a malformed
        # (non-string) algorithm in the initial state.
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": {"algorithm": 42},
                    }
                ]
            },
        )

        # Check that the forced default encryption was still applied.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})


class PreventEncryptionTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["prevent_room_encryption_setup"] = True
        return config

    def test_can_create_room_without_encryption(self) -> None:
        """
        Tests that a room can be created with no encryption,
        when the `prevent_room_encryption_setup` option is set.
        (Just to ensure that enabling the option doesn't break the creation of a room.)
        """

        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, with initial state to set up encryption
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {},
            access_token=user_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

    def test_cannot_create_room_with_encryption_when_prevented(self) -> None:
        """
        Tests that a room can't be created with end-to-end encryption enabled
        when the `prevent_room_encryption_setup` option is set.
        """

        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, with initial state to set up encryption
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/createRoom",
            {
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": {"algorithm": "io.element.example"},
                    }
                ]
            },
            access_token=user_token,
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)

        # Also avoid a half-created room being left behind
        channel = self.make_request(
            "GET", "/_matrix/client/v3/joined_rooms", access_token=user_token
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body["joined_rooms"], [])

    def test_cannot_enable_encryption_after_room_creation(self) -> None:
        """
        Tests that encryption can't be enabled in an existing room.
        """

        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room
        room_id = self.helper.create_room_as(tok=user_token)
        body = self.helper.send_state(
            room_id,
            EventTypes.RoomEncryption,
            {"algorithm": "io.element.example"},
            tok=user_token,
            expect_code=HTTPStatus.FORBIDDEN,
        )
        self.assertEqual(body["errcode"], Codes.FORBIDDEN)


class RoomIDCollisionTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def test_colliding_v12_room_ids_are_retried(self) -> None:
        """In v12+ rooms the room ID is the reference hash of the
        create event, so two rooms whose create events have identical content
        collide on the same room ID. This happens when the same user creates
        several rooms at once (e.g. concurrent /createRoom requests with the
        same config within the same millisecond).

        Regression test: the collision must be retried transparently and both
        rooms created with distinct IDs.
        """
        handler = self.hs.get_room_creation_handler()
        user_id = self.register_user("alice", "pass")
        requester = create_requester(user_id)

        # Freeze the clock so both create events carry the same
        # `origin_server_ts`; with identical config this forces the two room
        # IDs to hash to the same value, reproducing the collision.
        with patch.object(self.hs.get_clock(), "time_msec", return_value=1234567890000):
            room_id1, _, _ = self.get_success(
                handler.create_room(requester, {"room_version": "12"}, ratelimit=False)
            )
            room_id2, _, _ = self.get_success(
                handler.create_room(requester, {"room_version": "12"}, ratelimit=False)
            )

        self.assertNotEqual(room_id1, room_id2)
        # v12 room IDs are content hashes with no domain component.
        self.assertNotIn(":", room_id1)
        self.assertNotIn(":", room_id2)
