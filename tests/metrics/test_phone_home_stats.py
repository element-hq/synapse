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

import logging
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

from synapse.app.phone_stats_home import (
    PHONE_HOME_INTERVAL_SECONDS,
    start_phone_stats_home,
)
from synapse.rest import admin, login, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.server import ThreadedMemoryReactorClock

TEST_REPORT_STATS_ENDPOINT = "https://fake.endpoint/stats"
TEST_SERVER_CONTEXT = "test-server-context"


class PhoneHomeStatsTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        register.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(
        self, reactor: ThreadedMemoryReactorClock, clock: Clock
    ) -> HomeServer:
        # Configure the homeserver to enable stats reporting.
        config = self.default_config()
        config["report_stats"] = True
        config["report_stats_endpoint"] = TEST_REPORT_STATS_ENDPOINT

        # Configure the server context so we can check it ends up being reported
        config["server_context"] = TEST_SERVER_CONTEXT

        # Allow guests to be registered
        config["allow_guest_access"] = True

        hs = self.setup_test_homeserver(config=config)

        # Replace the proxied http client with a mock, so we can inspect outbound requests to
        # the configured stats endpoint.
        self.put_json_mock = AsyncMock(return_value={})
        hs.get_proxied_http_client().put_json = self.put_json_mock  # type: ignore[method-assign]
        return hs

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main

        # Wait for the background updates to add the database triggers that keep the
        # `event_stats` table up-to-date.
        self.wait_for_background_updates()

        # Force stats reporting to occur
        start_phone_stats_home(hs=homeserver)

        super().prepare(reactor, clock, homeserver)

    def _get_latest_phone_home_stats(self) -> JsonDict:
        # Wait for `phone_stats_home` to be called again + a healthy margin (50s).
        self.reactor.advance(2 * PHONE_HOME_INTERVAL_SECONDS + 50)

        # Extract the reported stats from our http client mock
        mock_calls = self.put_json_mock.call_args_list
        report_stats_calls = []
        for call in mock_calls:
            if call.args[0] == TEST_REPORT_STATS_ENDPOINT:
                report_stats_calls.append(call)

        self.assertGreaterEqual(
            (len(report_stats_calls)),
            1,
            "Expected at-least one call to the report_stats endpoint",
        )

        # Extract the phone home stats from the call
        phone_home_stats = report_stats_calls[0].args[1]

        return phone_home_stats

    def _perform_user_actions(self) -> None:
        """
        Perform some actions on the homeserver that would bump the phone home
        stats.

        This creates a few users (including a guest), a room, and sends some messages.
        Expected number of events:
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
        # Note: `self.register_user` does not support guest registration, and updating the
        # Admin API it calls to add a new parameter would cause the `mac` parameter to fail
        # in a backwards-incompatible manner. Hence, we make a manual request here.
        _guest_user_mxid = self.make_request(
            method="POST",
            path="/_matrix/client/v3/register?kind=guest",
            content={
                "username": "guest_user",
                "password": "test",
            },
            shorthand=False,
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

    def test_phone_home_stats(self) -> None:
        """
        Test that the phone home stats contain the stats we expect based on
        the scenario carried out in `prepare`
        """
        # Do things to bump the stats
        self._perform_user_actions()

        # Wait for the stats to be reported
        phone_home_stats = self._get_latest_phone_home_stats()

        self.assertEqual(
            phone_home_stats["homeserver"], self.hs.config.server.server_name
        )

        self.assertTrue(isinstance(phone_home_stats["memory_rss"], int))
        self.assertTrue(isinstance(phone_home_stats["cpu_average"], int))

        self.assertEqual(phone_home_stats["server_context"], TEST_SERVER_CONTEXT)

        self.assertTrue(isinstance(phone_home_stats["timestamp"], int))
        self.assertTrue(isinstance(phone_home_stats["uptime_seconds"], int))
        self.assertTrue(isinstance(phone_home_stats["python_version"], str))

        # We expect only our test users to exist on the homeserver
        self.assertEqual(phone_home_stats["total_users"], 3)
        self.assertEqual(phone_home_stats["total_nonbridged_users"], 3)
        self.assertEqual(phone_home_stats["daily_user_type_native"], 2)
        self.assertEqual(phone_home_stats["daily_user_type_guest"], 1)
        self.assertEqual(phone_home_stats["daily_user_type_bridged"], 0)
        self.assertEqual(phone_home_stats["total_room_count"], 1)
        self.assertEqual(phone_home_stats["daily_active_users"], 2)
        self.assertEqual(phone_home_stats["monthly_active_users"], 2)
        self.assertEqual(phone_home_stats["daily_active_rooms"], 1)
        self.assertEqual(phone_home_stats["daily_active_e2ee_rooms"], 1)
        self.assertEqual(phone_home_stats["daily_messages"], 10)
        self.assertEqual(phone_home_stats["daily_e2ee_messages"], 5)
        self.assertEqual(phone_home_stats["daily_sent_messages"], 10)
        self.assertEqual(phone_home_stats["daily_sent_e2ee_messages"], 5)

        # Our users have not been around for >30 days, hence these are all 0.
        self.assertEqual(phone_home_stats["r30v2_users_all"], 0)
        self.assertEqual(phone_home_stats["r30v2_users_android"], 0)
        self.assertEqual(phone_home_stats["r30v2_users_ios"], 0)
        self.assertEqual(phone_home_stats["r30v2_users_electron"], 0)
        self.assertEqual(phone_home_stats["r30v2_users_web"], 0)
        self.assertEqual(
            phone_home_stats["cache_factor"], self.hs.config.caches.global_factor
        )
        self.assertEqual(
            phone_home_stats["event_cache_size"],
            self.hs.config.caches.event_cache_size,
        )
        self.assertEqual(
            phone_home_stats["database_engine"],
            self.hs.config.database.databases[0].config["name"],
        )
        self.assertEqual(
            phone_home_stats["database_server_version"],
            self.hs.get_datastores().main.database_engine.server_version,
        )

        synapse_logger = logging.getLogger("synapse")
        log_level = synapse_logger.getEffectiveLevel()
        self.assertEqual(phone_home_stats["log_level"], logging.getLevelName(log_level))
