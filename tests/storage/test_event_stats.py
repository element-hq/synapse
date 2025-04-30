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


from twisted.test.proto_helpers import MemoryReactor

from synapse.rest import admin, login, register, room
from synapse.server import HomeServer
from synapse.types.storage import _BackgroundUpdates
from synapse.util import Clock

from tests import unittest


class EventStatsTestCase(unittest.HomeserverTestCase):
    """
    Tests for the `event_stats` table
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        register.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        # Wait for the background updates to add the database triggers that keep the
        # `event_stats` table up-to-date.
        #
        # This also prevents background updates running during the tests and messing
        # with the results.
        self.wait_for_background_updates()

        super().prepare(reactor, clock, hs)

    def _perform_user_actions(self) -> None:
        """
        Perform some actions on the homeserver that would bump the event counts.

        This creates a few users, a room, and sends some messages. Expected number of
        events:
         - 10 unencrypted messages
         - 5 encrypted messages
         - 24 total events (including room state, etc)
        """
        # Create some users
        user_1_mxid = self.register_user(
            username="test_user_1",
            password="test",
        )
        user_2_mxid = self.register_user(
            username="test_user_2",
            password="test",
        )

        # Log in to each user
        user_1_token = self.login(username=user_1_mxid, password="test")
        user_2_token = self.login(username=user_2_mxid, password="test")

        # Create a room between the two users
        room_1_id = self.helper.create_room_as(
            is_public=False,
            tok=user_1_token,
        )

        # Mark this room as end-to-end encrypted
        self.helper.send_state(
            room_id=room_1_id,
            event_type="m.room.encryption",
            body={
                "algorithm": "m.megolm.v1.aes-sha2",
                "rotation_period_ms": 604800000,
                "rotation_period_msgs": 100,
            },
            state_key="",
            tok=user_1_token,
        )

        # User 1 invites user 2
        self.helper.invite(
            room=room_1_id,
            src=user_1_mxid,
            targ=user_2_mxid,
            tok=user_1_token,
        )

        # User 2 joins
        self.helper.join(
            room=room_1_id,
            user=user_2_mxid,
            tok=user_2_token,
        )

        # User 1 sends 10 unencrypted messages
        for _ in range(10):
            self.helper.send(
                room_id=room_1_id,
                body="Zoinks Scoob! A message!",
                tok=user_1_token,
            )

        # User 2 sends 5 encrypted "messages"
        for _ in range(5):
            self.helper.send_event(
                room_id=room_1_id,
                type="m.room.encrypted",
                content={
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": "some_key",
                    "ciphertext": {
                        "some_key": {
                            "type": 0,
                            "body": "encrypted_payload",
                        },
                    },
                },
                tok=user_2_token,
            )

    def test_background_update_with_events(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly when there are events in the database.
        """
        # Do things to bump the stats
        self._perform_user_actions()

        # Since the background update has already run once when Synapse started, let's
        # manually reset the database `event_stats` back to 0 to ensure this test is
        # starting from a clean slate. We want to be able to detect 0 -> 24 instead of
        # 24 -> 24 as it's not possible to prove that any work was actually done if the
        # number doesn't change.
        self.get_success(
            self.store.db_pool.simple_update_one(
                table="event_stats",
                keyvalues={},
                updatevalues={
                    "total_event_count": 0,
                    "unencrypted_message_count": 0,
                    "e2ee_event_count": 0,
                },
                desc="reset event_stats in test preparation",
            )
        )
        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Expect our `event_stats` table to be populated with the correct values
        self.assertEqual(self.get_success(self.store.count_total_events()), 24)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 10)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 5)

    def test_background_update_without_events(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly without events in the database.
        """
        # Keep in mind: These are already populated as the background update has already
        # ran once when Synapse started and added the database triggers which are
        # incrementing things as new events come in.
        #
        # In this case, no events have been sent, so we expect the counts to be 0.
        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

    def test_background_update_resume_progress(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly to resume from `progress_json`.
        """
        # Do things to bump the stats
        self._perform_user_actions()

        # Keep in mind: These are already populated as the background update has already
        # ran once when Synapse started and added the database triggers which are
        # incrementing things as new events come in.
        self.assertEqual(self.get_success(self.store.count_total_events()), 24)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 10)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 5)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": '{ "last_event_stream_ordering": 14, "stop_event_stream_ordering": 21 }',
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # We expect these values to increase as the background update is being run
        # *again* and will double-count some of the `events` over the range specified
        # by the `progress_json`.
        self.assertEqual(self.get_success(self.store.count_total_events()), 24 + 7)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 16)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 6)
