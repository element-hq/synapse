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
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest


class StickyEventsTestCase(unittest.HomeserverTestCase):
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
        self.store = self.hs.get_datastores().main

        # Register an account and create a room
        self.user_id = self.register_user("user", "pass")
        self.token = self.login(self.user_id, "pass")
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.token)

    def test_get_updated_sticky_events(self) -> None:
        """Test getting updated sticky events between stream IDs."""
        # Get the starting stream_id
        start_id = self.store.get_max_sticky_events_stream_id()

        event_id_1 = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "message 1", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        mid_id = self.store.get_max_sticky_events_stream_id()

        event_id_2 = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "message 2", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        end_id = self.store.get_max_sticky_events_stream_id()

        # Get all updates
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=end_id, limit=10
            )
        )
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].event_id, event_id_1)
        self.assertEqual(updates[0].soft_failed, False)
        self.assertEqual(updates[1].event_id, event_id_2)
        self.assertEqual(updates[1].soft_failed, False)

        # Get only the second update
        updates = self.get_success(
            self.store.get_updated_sticky_events(from_id=mid_id, to_id=end_id, limit=10)
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, event_id_2)
        self.assertEqual(updates[0].soft_failed, False)

    def test_delete_expired_sticky_events(self) -> None:
        """Test deletion of expired sticky events."""
        # Get the starting stream_id
        start_id = self.store.get_max_sticky_events_stream_id()

        # Insert an expired event by advancing time past its duration
        self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(milliseconds=1),
            content={"body": "expired message", "msgtype": "m.text"},
            tok=self.token,
        )
        self.reactor.advance(0.002)

        # Insert a non-expired event
        event_id_2 = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "non-expired message", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        # Delete expired events
        self.get_success(self.store._delete_expired_sticky_events())

        # Check that only the non-expired event remains
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=start_id + 2, limit=10
            )
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, event_id_2)

    def test_get_updated_sticky_events_with_limit(self) -> None:
        """Test that the limit parameter works correctly."""
        # Get the starting stream_id
        start_id = self.store.get_max_sticky_events_stream_id()

        event_id_1 = self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "message 1", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        self.helper.send_sticky_event(
            self.room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": "message 2", "msgtype": "m.text"},
            tok=self.token,
        )

        # Get only the first update
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=start_id + 2, limit=1
            )
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, event_id_1)
