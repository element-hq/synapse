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

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import Codes
from synapse.app.generic_worker import GenericWorkerServer, GenericWorkerStore
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.types import JsonDict

from tests.replication._base import BaseMultiWorkerStreamTestCase


class ServerAnnouncementsWorkerTestCase(BaseMultiWorkerStreamTestCase):
    """Exercise real worker stores and event replication using the fake Redis server."""

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["stream_writers"] = {"events": ["event_writer"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "event_writer": {"host": "testserv", "port": 1001},
        }
        return config

    def setUp(self) -> None:
        super().setUp()
        self.store = self.hs.get_datastores().main
        self.writer_hs = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "event_writer"}
        )
        self.client_hs = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "client"}
        )
        self.worker_helper = attr.evolve(
            self.helper, hs=self.client_hs, site=self._hs_to_site[self.client_hs]
        )
        self.alice = self.register_user("alice", "pass")
        self.alice_token = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_token = self.login("bob", "pass")
        self.room_config = ServerAnnouncementRoom(
            id="updates",
            room_name="Server Updates",
            room_topic="News",
            room_avatar_url=None,
            room_avatar_path=None,
            allowed_user_ids=[self.alice],
        )
        # Configure after startup and registration: these tests explicitly drive
        # reconciliation, rather than depending on the startup background task.
        self._configure_rooms(self.room_config)

    def _configure_rooms(self, config: ServerAnnouncementRoom) -> None:
        for hs in (self.hs, self.writer_hs, self.client_hs):
            hs.config.serverannouncements.rooms = [
                attr.evolve(config, allowed_user_ids=list(config.allowed_user_ids))
            ]

    def _assert_written_on_worker(self, event_id: str) -> None:
        position = self.get_success(self.store.get_position_for_event(event_id))
        self.assertEqual(position.instance_name, "event_writer")

    def _reconcile(self) -> str:
        self.get_success(self.hs.get_server_announcements_handler().reconcile_rooms())
        self.replicate()
        room_id = self.get_success(self.store.get_server_announcement_room("updates"))
        assert room_id is not None
        return room_id

    def test_worker_reads_persisted_mapping_without_configuration(self) -> None:
        room_id = "!announcements:test"
        self.get_success(self.store.store_server_announcement_room("updates", room_id))
        reader = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "reader"}
        )
        self.assertIsInstance(reader, GenericWorkerServer)
        reader_store = reader.get_datastores().main
        self.assertIsInstance(reader_store, GenericWorkerStore)
        self.assertEqual(reader.config.serverannouncements.rooms, [])
        self.assertEqual(
            self.get_success(reader_store.get_server_announcement_room("updates")),
            room_id,
        )
        self.assertEqual(
            self.get_success(reader_store.get_server_announcement_id(room_id)),
            "updates",
        )

    def test_worker_mapping_misses_are_not_cached(self) -> None:
        room_id = "!announcements:test"
        for hs in (self.writer_hs, self.client_hs):
            store = hs.get_datastores().main
            self.assertIsNone(
                self.get_success(store.get_server_announcement_room("updates"))
            )
            self.assertIsNone(
                self.get_success(store.get_server_announcement_id(room_id))
            )

        self.get_success(self.store.store_server_announcement_room("updates", room_id))

        # There is no mapping replication stream or cache invalidation to wait for.
        for hs in (self.writer_hs, self.client_hs):
            store = hs.get_datastores().main
            self.assertEqual(
                self.get_success(store.get_server_announcement_room("updates")), room_id
            )
            self.assertEqual(
                self.get_success(store.get_server_announcement_id(room_id)), "updates"
            )

    def test_normal_room_sends_with_announcement_configuration(self) -> None:
        room_id = self.worker_helper.create_room_as(self.bob, tok=self.bob_token)
        for helper in (self.helper, self.worker_helper):
            response = helper.send(room_id, "Not an announcement", tok=self.bob_token)
            self._assert_written_on_worker(response["event_id"])
            response = helper.send_state(
                room_id,
                EventTypes.Topic,
                {"topic": response["event_id"]},
                tok=self.bob_token,
            )
            self._assert_written_on_worker(response["event_id"])
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_id(room_id))
        )

    def test_only_main_reconciles_with_separate_event_writer(self) -> None:
        for hs in (self.writer_hs, self.client_hs):
            self.get_success(hs.get_server_announcements_handler().reconcile_rooms())
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_room("updates"))
        )

        room_id = self._reconcile()
        creator = self.hs.config.serverannouncements.server_announcements_mxid
        for hs in (self.hs, self.writer_hs, self.client_hs):
            store = hs.get_datastores().main
            self.assertEqual(
                self.get_success(store.get_server_announcement_room("updates")), room_id
            )
            self.assertEqual(
                self.get_success(store.get_server_announcement_id(room_id)), "updates"
            )
            state = self.get_success(
                hs.get_storage_controllers().state.get_current_state(room_id)
            )
            self.assertFalse(state[(EventTypes.Create, "")].content["m.federate"])
            self.assertEqual(
                state[(EventTypes.Name, "")].content, {"name": "Server Updates"}
            )
            self.assertEqual(state[(EventTypes.Topic, "")].content, {"topic": "News"})
            self.assertEqual(
                state[(EventTypes.PowerLevels, "")].content["users"][self.alice], 50
            )
            for user_id in (creator, self.alice):
                member = state[(EventTypes.Member, user_id)]
                self.assertEqual(member.membership, Membership.JOIN)
                self._assert_written_on_worker(member.event_id)
            self._assert_written_on_worker(state[(EventTypes.Topic, "")].event_id)

        event_id = self.worker_helper.send(
            room_id, "An announcement", tok=self.alice_token
        )["event_id"]
        self._assert_written_on_worker(event_id)

        self._configure_rooms(
            attr.evolve(
                self.room_config, room_name="Renamed", allowed_user_ids=[self.bob]
            )
        )
        self.assertEqual(self._reconcile(), room_id)
        state_controller = self.client_hs.get_storage_controllers().state
        state = self.get_success(state_controller.get_current_state(room_id))
        self.assertEqual(state[(EventTypes.Name, "")].content, {"name": "Renamed"})
        self.assertEqual(
            state[(EventTypes.Member, self.alice)].membership, Membership.LEAVE
        )
        self.assertEqual(
            state[(EventTypes.Member, self.bob)].membership, Membership.JOIN
        )
        for key in (
            (EventTypes.Name, ""),
            (EventTypes.Member, self.alice),
            (EventTypes.Member, self.bob),
        ):
            self._assert_written_on_worker(state[key].event_id)
        event_id = self.worker_helper.send(
            room_id, "New announcement sender", tok=self.bob_token
        )["event_id"]
        self._assert_written_on_worker(event_id)

        before = self.get_success(state_controller.get_current_state_ids(room_id))
        self.assertEqual(self._reconcile(), room_id)
        self.assertEqual(
            self.get_success(state_controller.get_current_state_ids(room_id)), before
        )

    def _map_public_room(self, writer_only: bool) -> str:
        # Matrix auth permits Alice to change state and Bob to join this room.
        # Mapping it without reconciliation leaves permissive, stale room state,
        # so a rejection must come from the announcement guard, not Matrix auth.
        room_id = self.worker_helper.create_room_as(
            self.alice, room_version="11", tok=self.alice_token
        )
        self.get_success(self.store.store_server_announcement_room("updates", room_id))
        if writer_only:
            # Model a stale frontend config. The event must cross HTTP replication
            # and be rejected by the writer, without mocking either event handler.
            self.hs.config.serverannouncements.rooms = []
            self.client_hs.config.serverannouncements.rooms = []
        return room_id

    @parameterized.expand([(False,), (True,)])
    def test_worker_rejects_state_changes(self, writer_only: bool) -> None:
        room_id = self._map_public_room(writer_only)
        state = self.writer_hs.get_storage_controllers().state
        before = self.get_success(state.get_current_state_ids(room_id))
        response = self.worker_helper.send_state(
            room_id,
            EventTypes.Name,
            {"name": "Client-controlled"},
            tok=self.alice_token,
            expect_code=403,
        )
        self.assertEqual(response["errcode"], Codes.FORBIDDEN)
        self.assertIn("state is managed by the server", response["error"])
        self.assertEqual(self.get_success(state.get_current_state_ids(room_id)), before)

    @parameterized.expand(
        [
            (False, Membership.JOIN),
            (False, Membership.INVITE),
            (True, Membership.JOIN),
            (True, Membership.INVITE),
        ]
    )
    def test_worker_rejects_unauthorized_membership(
        self, writer_only: bool, membership: str
    ) -> None:
        room_id = self._map_public_room(writer_only)
        response = self.worker_helper.change_membership(
            room_id,
            self.alice if membership == Membership.INVITE else self.bob,
            self.bob,
            membership,
            tok=self.alice_token if membership == Membership.INVITE else self.bob_token,
            expect_code=403,
        )
        self.assertEqual(response["errcode"], Codes.FORBIDDEN)
        self.assertIn("User is not allowed", response["error"])
        self.assertIsNone(
            self.get_success(
                self.writer_hs.get_storage_controllers().state.get_current_state_event(
                    room_id, EventTypes.Member, self.bob
                )
            )
        )
