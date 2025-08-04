#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import os.path
from unittest.mock import Mock, patch

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage import prepare_database
from synapse.storage.types import Cursor
from synapse.types import UserID, create_requester
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class CleanupExtremBackgroundUpdateStoreTestCase(HomeserverTestCase):
    """
    Test the background update to clean forward extremities table.
    """

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.room_creator = homeserver.get_room_creation_handler()

        # Create a test user and room
        self.user = UserID("alice", "test")
        self.requester = create_requester(self.user)
        self.room_id, _, _ = self.get_success(
            self.room_creator.create_room(self.requester, {})
        )

    def run_background_update(self) -> None:
        """Re run the background update to clean up the extremities."""
        # Make sure we don't clash with in progress updates.
        self.assertTrue(
            self.store.db_pool.updates._all_done, "Background updates are still ongoing"
        )

        schema_path = os.path.join(
            prepare_database.schema_path,
            "main",
            "delta",
            "54",
            "delete_forward_extremities.sql",
        )

        def run_delta_file(txn: Cursor) -> None:
            prepare_database.executescript(txn, schema_path)

        self.get_success(
            self.store.db_pool.runInteraction(
                "test_delete_forward_extremities", run_delta_file
            )
        )

        # Ugh, have to reset this flag
        self.store.db_pool.updates._all_done = False

        self.wait_for_background_updates()

    def add_extremity(self, room_id: str, event_id: str) -> None:
        """
        Add the given event as an extremity to the room.
        """
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_insert(
                table="event_forward_extremities",
                values={"room_id": room_id, "event_id": event_id},
                desc="test_add_extremity",
            )
        )

        self.hs.get_datastores().main.get_latest_event_ids_in_room.invalidate(
            (room_id,)
        )

    def test_soft_failed_extremities_handled_correctly(self) -> None:
        """Test that extremities are correctly calculated in the presence of
        soft failed events.

        Tests a graph like:

            A <- SF1 <- SF2 <- B

        Where SF* are soft failed.
        """

        # Create the room graph
        event_id_1 = self.create_and_send_event(self.room_id, self.user)
        event_id_2 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_1]
        )
        event_id_3 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_2]
        )
        event_id_4 = self.create_and_send_event(
            self.room_id, self.user, False, [event_id_3]
        )

        # Check the latest events are as expected
        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )

        self.assertEqual(latest_event_ids, {event_id_4})

    def test_basic_cleanup(self) -> None:
        """Test that extremities are correctly calculated in the presence of
        soft failed events.

        Tests a graph like:

            A <- SF1 <- B

        Where SF* are soft failed, and with extremities of A and B
        """
        # Create the room graph
        event_id_a = self.create_and_send_event(self.room_id, self.user)
        event_id_sf1 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_a]
        )
        event_id_b = self.create_and_send_event(
            self.room_id, self.user, False, [event_id_sf1]
        )

        # Add the new extremity and check the latest events are as expected
        self.add_extremity(self.room_id, event_id_a)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_a, event_id_b})

        # Run the background update and check it did the right thing
        self.run_background_update()

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_b})

    def test_chain_of_fail_cleanup(self) -> None:
        """Test that extremities are correctly calculated in the presence of
        soft failed events.

        Tests a graph like:

            A <- SF1 <- SF2 <- B

        Where SF* are soft failed, and with extremities of A and B
        """
        # Create the room graph
        event_id_a = self.create_and_send_event(self.room_id, self.user)
        event_id_sf1 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_a]
        )
        event_id_sf2 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_sf1]
        )
        event_id_b = self.create_and_send_event(
            self.room_id, self.user, False, [event_id_sf2]
        )

        # Add the new extremity and check the latest events are as expected
        self.add_extremity(self.room_id, event_id_a)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_a, event_id_b})

        # Run the background update and check it did the right thing
        self.run_background_update()

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_b})

    def test_forked_graph_cleanup(self) -> None:
        r"""Test that extremities are correctly calculated in the presence of
        soft failed events.

        Tests a graph like, where time flows down the page:

                A     B
               / \   /
              /   \ /
            SF1   SF2
             |     |
            SF3    |
           /   \   |
           |    \  |
           C     SF4

        Where SF* are soft failed, and with them A, B and C marked as
        extremities. This should resolve to B and C being marked as extremity.
        """

        # Create the room graph
        event_id_a = self.create_and_send_event(self.room_id, self.user)
        event_id_b = self.create_and_send_event(self.room_id, self.user)
        event_id_sf1 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_a]
        )
        event_id_sf2 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_a, event_id_b]
        )
        event_id_sf3 = self.create_and_send_event(
            self.room_id, self.user, True, [event_id_sf1]
        )
        self.create_and_send_event(
            self.room_id, self.user, True, [event_id_sf2, event_id_sf3]
        )  # SF4
        event_id_c = self.create_and_send_event(
            self.room_id, self.user, False, [event_id_sf3]
        )

        # Add the new extremity and check the latest events are as expected
        self.add_extremity(self.room_id, event_id_a)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_a, event_id_b, event_id_c})

        # Run the background update and check it did the right thing
        self.run_background_update()

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(latest_event_ids, {event_id_b, event_id_c})


class CleanupExtremDummyEventsTestCase(HomeserverTestCase):
    CONSENT_VERSION = "1"
    EXTREMITIES_COUNT = 50
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["cleanup_extremities_with_dummy_events"] = True
        return self.setup_test_homeserver(config=config)

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.room_creator = homeserver.get_room_creation_handler()
        self.event_creator_handler = homeserver.get_event_creation_handler()

        # Create a test user and room
        self.user = UserID.from_string(self.register_user("user1", "password"))
        self.token1 = self.login("user1", "password")
        self.requester = create_requester(self.user)
        self.room_id, _, _ = self.get_success(
            self.room_creator.create_room(self.requester, {"visibility": "public"})
        )
        self.event_creator = homeserver.get_event_creation_handler()
        homeserver.config.consent.user_consent_version = self.CONSENT_VERSION

    def test_send_dummy_event(self) -> None:
        self._create_extremity_rich_graph()

        # Pump the reactor repeatedly so that the background updates have a
        # chance to run.
        self.pump(20)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertTrue(len(latest_event_ids) < 10, len(latest_event_ids))

    @patch("synapse.handlers.message._DUMMY_EVENT_ROOM_EXCLUSION_EXPIRY", new=0)
    def test_send_dummy_events_when_insufficient_power(self) -> None:
        self._create_extremity_rich_graph()
        # Criple power levels
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            body={"users": {str(self.user): -1}},
            tok=self.token1,
        )
        # Pump the reactor repeatedly so that the background updates have a
        # chance to run.
        self.pump(10 * 60)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        # Check that the room has not been pruned
        self.assertTrue(len(latest_event_ids) > 10)

        # New user with regular levels
        user2 = self.register_user("user2", "password")
        token2 = self.login("user2", "password")
        self.helper.join(self.room_id, user2, tok=token2)
        self.pump(10 * 60)

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertTrue(len(latest_event_ids) < 10, len(latest_event_ids))

    @patch("synapse.handlers.message._DUMMY_EVENT_ROOM_EXCLUSION_EXPIRY", new=250)
    def test_expiry_logic(self) -> None:
        """Simple test to ensure that _expire_rooms_to_exclude_from_dummy_event_insertion()
        expires old entries correctly.
        """
        self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion["1"] = (
            100000
        )
        self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion["2"] = (
            200000
        )
        self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion["3"] = (
            300000
        )

        self.event_creator_handler._expire_rooms_to_exclude_from_dummy_event_insertion()
        # All entries within time frame
        self.assertEqual(
            len(
                self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion
            ),
            3,
        )
        # Oldest room to expire
        self.pump(1.01)
        self.event_creator_handler._expire_rooms_to_exclude_from_dummy_event_insertion()
        self.assertEqual(
            len(
                self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion
            ),
            2,
        )
        # All rooms to expire
        self.pump(2)
        self.assertEqual(
            len(
                self.event_creator_handler._rooms_to_exclude_from_dummy_event_insertion
            ),
            0,
        )

    def _create_extremity_rich_graph(self) -> None:
        """Helper method to create bushy graph on demand"""

        event_id_start = self.create_and_send_event(self.room_id, self.user)

        for _ in range(self.EXTREMITIES_COUNT):
            self.create_and_send_event(
                self.room_id, self.user, prev_event_ids=[event_id_start]
            )

        latest_event_ids = self.get_success(
            self.store.get_latest_event_ids_in_room(self.room_id)
        )
        self.assertEqual(len(latest_event_ids), 50)

    def _enable_consent_checking(self) -> None:
        """Helper method to enable consent checking"""
        self.event_creator._block_events_without_consent_error = "No consent from user"
        consent_uri_builder = Mock()
        consent_uri_builder.build_user_consent_uri.return_value = "http://example.com"
        self.event_creator._consent_uri_builder = consent_uri_builder
