from unittest.mock import patch

import synapse
from synapse.api.constants import EventTypes, RoomEncryptionAlgorithms
from synapse.rest.client import login, room
from synapse.types import create_requester

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


class RoomIDCollisionTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def test_colliding_v12_room_ids_are_retried(self) -> None:
        """In v12 room the room ID is the reference hash of the
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
