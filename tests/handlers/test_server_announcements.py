#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.


import attr
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.config import ConfigError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.handlers.server_announcements import ServerAnnouncementsHandler
from synapse.rest import admin
from synapse.rest.client import login, register, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config


class ServerAnnouncementsTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        register.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.alice = self.register_user("alice", "pass")
        self.alice_token = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_token = self.login("bob", "pass")
        self.store = hs.get_datastores().main
        self.state = hs.get_storage_controllers().state
        self.config = hs.config.serverannouncements
        self.creator = self.config.room_management_mxid
        self.room_config = ServerAnnouncementRoom(
            config_id="updates",
            room_name="Server Updates",
            room_topic="News",
            room_avatar_url="mxc://test/avatar",
            room_avatar_path=None,
            allowed_user_ids=[self.alice],
        )
        self.config.rooms = [self.room_config]
        self.handler = hs.get_server_announcements_handler()

    def _reconcile(self) -> str:
        self.get_success(self.handler.reconcile_rooms())
        room_id = self.get_success(self.store.get_server_announcement_room("updates"))
        assert room_id is not None
        return room_id

    def _content(self, room_id: str, event_type: str) -> JsonDict:
        """Pull a state event from the database and return its `content` dict."""
        event = self.get_success(
            self.state.get_current_state_event(room_id, event_type, "")
        )
        assert event is not None
        return dict(event.content)

    @parameterized.expand([("11",), ("12",)])
    def test_creation_and_permissions(self, version: str) -> None:
        """Test that a server announcements room is created with the expected room
        state.
        """
        from synapse.api.room_versions import KNOWN_ROOM_VERSIONS

        self.hs.config.server.default_room_version = KNOWN_ROOM_VERSIONS[version]
        room_id = self._reconcile()
        self.assertEqual(self._content(room_id, EventTypes.Create)["m.federate"], False)
        self.assertEqual(
            self._content(room_id, EventTypes.JoinRules), {"join_rule": "invite"}
        )
        self.assertEqual(
            self._content(room_id, EventTypes.RoomHistoryVisibility),
            {"history_visibility": "world_readable"},
        )
        self.assertEqual(
            self._content(room_id, EventTypes.Name), {"name": "Server Updates"}
        )
        self.assertEqual(self._content(room_id, EventTypes.Topic), {"topic": "News"})
        self.assertEqual(
            self._content(room_id, EventTypes.RoomAvatar), {"url": "mxc://test/avatar"}
        )
        powers = self._content(room_id, EventTypes.PowerLevels)
        self.assertEqual(powers["users"][self.alice], 50)
        self.assertEqual(powers["state_default"], 100)
        self.assertEqual(powers["events_default"], 50)
        self.assertCountEqual(
            self.get_success(self.store.get_users_in_room(room_id)),
            [self.creator, self.alice],
        )
        self.helper.send(room_id, "An announcement", tok=self.alice_token)
        self.helper.join(room_id, self.bob, tok=self.bob_token, expect_code=403)

    def test_multiple_rooms_including_empty_allowlist(self) -> None:
        """Test that configuring multiple rooms works."""
        self.config.rooms.append(
            attr.evolve(self.room_config, config_id="maintenance", allowed_user_ids=[])
        )
        first = self._reconcile()
        second = self.get_success(
            self.store.get_server_announcement_room("maintenance")
        )
        assert second is not None
        self.assertNotEqual(first, second)
        self.assertEqual(
            self.get_success(self.store.get_users_in_room(second)), [self.creator]
        )

    def test_metadata_and_allowlist_changes(self) -> None:
        """Test that updating config reflects on next reconciliation."""
        room_id = self._reconcile()

        # Update metadata about the room.
        self.config.rooms = [
            attr.evolve(
                self.room_config,
                room_name="Renamed",
                room_topic=None,
                room_avatar_url=None,
                # Alice has been removed, and bob added.
                allowed_user_ids=[self.bob],
            )
        ]
        self.assertEqual(self._reconcile(), room_id)

        self.assertEqual(self._content(room_id, EventTypes.Name), {"name": "Renamed"})
        self.assertEqual(self._content(room_id, EventTypes.Topic), {"topic": ""})
        self.assertEqual(self._content(room_id, EventTypes.RoomAvatar), {})

        # Check that bob was added to the room, and alice was removed.
        self.assertCountEqual(
            self.get_success(self.store.get_users_in_room(room_id)),
            [self.creator, self.bob],
        )
        self.assertNotIn(
            self.alice, self._content(room_id, EventTypes.PowerLevels)["users"]
        )
        self.helper.send(room_id, "New sender", tok=self.bob_token)
        self.helper.send(
            room_id, "Removed sender", tok=self.alice_token, expect_code=403
        )

    def test_removed_config_retains_mapping_and_room(self) -> None:
        """Test that an announcements room can be recovered even if it's removed from
        the config.
        """
        # Create an announcement room.
        room_id = self._reconcile()
        before = self.get_success(self.state.get_current_state_ids(room_id))

        # Remove that room from the homeserver config.
        self.config.rooms = []
        self.get_success(self.handler.reconcile_rooms())

        # But check that the mapping still exists.
        self.assertEqual(
            self.get_success(self.store.get_server_announcement_room("updates")),
            room_id,
        )
        self.assertEqual(
            self.get_success(self.state.get_current_state_ids(room_id)), before
        )

        # Check that it can be re-added again, and a new room isn't created.
        self.config.rooms = [self.room_config]
        self.assertEqual(self._reconcile(), room_id)

    @parameterized.expand([(False,), (True,)])
    def test_admin_deletion_and_recreation(self, replacement: bool) -> None:
        """Check that when an announcement room is removed from configuration and
        purged, that a new room is created if the same announcement room `id` is
        configured once again.

        Basically this tests the "re-use `id` but in a new room" flow.
        """
        # Create an announcement room.
        old_room_id = self._reconcile()

        # Create a server admin user.
        admin_id = self.register_user("admin", "pass", admin=True)
        admin_token = self.login("admin", "pass")

        # Purge the old room.
        content: JsonDict = {"purge": True}
        if replacement:
            content["new_room_user_id"] = admin_id
        response = self.make_request(
            "DELETE",
            f"/_synapse/admin/v1/rooms/{old_room_id}",
            content,
            access_token=admin_token,
        )
        self.assertEqual(response.code, 200, response.json_body)
        self.assertEqual(response.json_body["failed_to_kick_users"], [])
        self.assertCountEqual(
            response.json_body["kicked_users"], [self.creator, self.alice]
        )

        # Check that the mapping was purged.
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_room("updates"))
        )
        if replacement:
            self.assertCountEqual(
                self.get_success(
                    self.store.get_users_in_room(response.json_body["new_room_id"])
                ),
                [admin_id, self.alice],
            )
        self.reactor.advance(1)

        # Check that configuring the same announcement room `id` creates a fresh room.
        self.assertNotEqual(self._reconcile(), old_room_id)

    def test_invite_and_auto_join_appear_only_as_joined_in_sync(self) -> None:
        """Check that users that are auto-joined to rooms don't see the interim invite
        down /sync.
        """
        before = self.make_request("GET", "/sync", access_token=self.alice_token)
        self.assertEqual(before.code, 200, before.json_body)
        room_id = self._reconcile()
        after = self.make_request(
            "GET",
            "/sync?since=" + before.json_body["next_batch"],
            access_token=self.alice_token,
        )
        self.assertEqual(after.code, 200, after.json_body)
        self.assertIn(room_id, after.json_body["rooms"]["join"])
        self.assertNotIn(room_id, after.json_body["rooms"].get("invite", {}))
        self.assertNotIn(room_id, after.json_body["rooms"].get("leave", {}))

    def test_history_readable_by_nonmember(self) -> None:
        """Test that non-joined users may read historical announcements."""
        room_id = self._reconcile()
        event_id = self.helper.send(
            room_id, "Public announcement", tok=self.alice_token
        )["event_id"]

        response = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{room_id}/messages?dir=b",
            access_token=self.bob_token,
        )
        self.assertEqual(response.code, 200, response.json_body)
        self.assertIn(
            event_id, [event["event_id"] for event in response.json_body["chunk"]]
        )

    @override_config({"allow_guest_access": True})
    def test_guest_can_read_history_but_cannot_join(self) -> None:
        guest = self.make_request("POST", "/register?kind=guest", {})
        self.assertEqual(guest.code, 200, guest.json_body)
        token = guest.json_body["access_token"]

        # Create and configure the announcement room.
        room_id = self._reconcile()

        # Send an announcement.
        event_id = self.helper.send(
            room_id, "Public announcement", tok=self.alice_token
        )["event_id"]

        # Check the guest account may read announcements.
        response = self.make_request(
            "GET", f"/rooms/{room_id}/messages?dir=b", access_token=token
        )
        self.assertEqual(response.code, 200, response.json_body)
        self.assertIn(
            event_id, [event["event_id"] for event in response.json_body["chunk"]]
        )

        # Check that the guest may not join the announcement room.
        self.helper.join(
            room_id, guest.json_body["user_id"], tok=token, expect_code=403
        )

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_does_not_enable_encryption(self) -> None:
        """Test that announcement rooms are not encrypted, even when enabled by default
        for all rooms in the config.
        """
        room_id = self._reconcile()
        self.assertIsNone(
            self.get_success(
                self.state.get_current_state_event(
                    room_id, EventTypes.RoomEncryption, ""
                )
            )
        )

    @override_config({"block_non_admin_invites": True})
    def test_internal_invites_bypass_admin_only_invites(self) -> None:
        """Test that rooms can be reconciled even when invites are restricted only to
        server admin users.
        """
        # TODO: Do we need to configure some allowed users?
        self._reconcile()

    @override_config({"allow_underscore_prefixed_localpart": True})
    def test_creator_cannot_be_registered(self) -> None:
        """Test that the announcement management user cannot be registered."""
        error = self.get_failure(
            self.hs.get_registration_handler().register_user(
                localpart="_server_announcements"
            ),
            SynapseError,
        )
        self.assertIn("reserved", str(error.value))

    def test_existing_creator_account_rejected(self) -> None:
        """Test that Synapse will fail to start up if the management user is already
        registered.
        """
        self.get_success(
            self.store.register_user(user_id=self.creator, password_hash=None)
        )
        self.get_failure(self.handler.reconcile_rooms(), ConfigError)
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_room("updates"))
        )

    def test_cannot_change_creator_of_existing_room(self) -> None:
        room_id = self._reconcile()
        self.config.room_management_mxid = "@different_manager:test"
        error = self.get_failure(
            ServerAnnouncementsHandler(self.hs).reconcile_rooms(), ConfigError
        )
        self.assertIn("was created by", str(error.value))
        self.assertIn("purge", str(error.value))
        self.assertEqual(
            self.get_success(self.store.get_server_announcement_room("updates")),
            room_id,
        )
