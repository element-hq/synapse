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

import sqlite3

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, EventUnsignedContentFields
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.utils import USE_POSTGRES_FOR_TESTS


class StickyEventsClientTestCase(unittest.HomeserverTestCase):
    """
    Tests for the client-server API parts of MSC4354: Sticky Events
    """

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        # We need the JSON functionality in SQLite
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    servlets = [
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4354_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Register an account
        self.user_id = self.register_user("user1", "pass")
        self.token = self.login(self.user_id, "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.token)

    def _assert_event_sticky_for(self, event_id: str, sticky_ttl: int) -> None:
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/event/{event_id}",
            access_token=self.token,
        )

        self.assertEqual(
            channel.code, 200, f"could not retrieve event {event_id}: {channel.result}"
        )
        event = channel.json_body

        self.assertIn(
            EventUnsignedContentFields.STICKY_TTL,
            event["unsigned"],
            f"No {EventUnsignedContentFields.STICKY_TTL} field in {event_id}; event not sticky: {event}",
        )
        self.assertEqual(
            event["unsigned"][EventUnsignedContentFields.STICKY_TTL],
            sticky_ttl,
            f"{event_id} had an unexpected sticky TTL: {event}",
        )

    def _assert_event_not_sticky(self, event_id: str) -> None:
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/event/{event_id}",
            access_token=self.token,
        )

        self.assertEqual(
            channel.code, 200, f"could not retrieve event {event_id}: {channel.result}"
        )
        event = channel.json_body

        self.assertNotIn(
            EventUnsignedContentFields.STICKY_TTL,
            event["unsigned"],
            f"{EventUnsignedContentFields.STICKY_TTL} field unexpectedly found in {event_id}: {event}",
        )

    def test_sticky_event_via_event_endpoint(self) -> None:
        # Arrange: Send a sticky event with a specific duration
        sticky_event_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=self.token,
        )
        event_id = sticky_event_response["event_id"]

        # If we request the event immediately, it will still have
        # 1 minute of stickiness
        # The other 100 ms is advanced in FakeChannel.await_result.
        self._assert_event_sticky_for(event_id, 59_900)

        # But if we advance time by 59.799 seconds...
        # we will get the event on its last millisecond of stickiness
        # The other 100 ms is advanced in FakeChannel.await_result.
        self.reactor.advance(59.799)
        self._assert_event_sticky_for(event_id, 1)

        # Advancing time any more, the event is no longer sticky
        self.reactor.advance(0.001)
        self._assert_event_not_sticky(event_id)


class StickyEventsDisabledClientTestCase(unittest.HomeserverTestCase):
    """
    Tests client-facing behaviour of MSC4354: Sticky Events when the feature is
    disabled.
    """

    servlets = [
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Register an account
        self.user_id = self.register_user("user1", "pass")
        self.token = self.login(self.user_id, "pass")

        # Create a room
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.token)

    def _assert_event_not_sticky(self, event_id: str) -> None:
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room_id}/event/{event_id}",
            access_token=self.token,
        )

        self.assertEqual(
            channel.code, 200, f"could not retrieve event {event_id}: {channel.result}"
        )
        event = channel.json_body

        self.assertNotIn(
            EventUnsignedContentFields.STICKY_TTL,
            event["unsigned"],
            f"{EventUnsignedContentFields.STICKY_TTL} field unexpectedly found in {event_id}: {event}",
        )

    def test_sticky_event_via_event_endpoint(self) -> None:
        sticky_event_response = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "sticky message", "msgtype": "m.text"},
            tok=self.token,
        )
        event_id = sticky_event_response["event_id"]

        # Since the feature is disabled, the event isn't sticky
        self._assert_event_not_sticky(event_id)
