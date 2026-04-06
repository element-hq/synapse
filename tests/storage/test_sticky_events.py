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
import sqlite3

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    Membership,
    StickyEvent,
    StickyEventField,
)
from synapse.api.room_versions import RoomVersions
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.utils import USE_POSTGRES_FOR_TESTS


class StickyEventsTestCase(unittest.HomeserverTestCase):
    """
    Tests for the storage functions related to MSC4354: Sticky Events
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

        end_id = self.store.get_max_sticky_events_stream_id()

        # Delete expired events
        self.get_success(self.store._delete_expired_sticky_events())

        # Check that only the non-expired event remains
        sticky_events = self.get_success(
            self.store.db_pool.simple_select_list(
                table="sticky_events", keyvalues=None, retcols=("stream_id", "event_id")
            )
        )
        self.assertEqual(
            sticky_events,
            [
                (end_id, event_id_2),
            ],
        )

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

    def test_outlier_events_not_in_table(self) -> None:
        """
        Tests the behaviour of outliered and then de-outliered events in the
        sticky_events table: they should only be added once they are de-outliered.
        """
        persist_controller = self.hs.get_storage_controllers().persistence
        assert persist_controller is not None

        user1_id = self.register_user("user1", "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        start_id = self.store.get_max_sticky_events_stream_id()

        room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, room_version=RoomVersions.V10.identifier
        )

        # Create a membership event
        event_dict = {
            "type": EventTypes.Member,
            "state_key": user1_id,
            "sender": user1_id,
            "room_id": room_id,
            "content": {EventContentFields.MEMBERSHIP: Membership.JOIN},
            StickyEvent.EVENT_FIELD_NAME: StickyEventField(
                duration_ms=Duration(hours=1).as_millis()
            ),
        }

        # Create the event twice: once as an outlier, once as a non-outlier.
        # It's not at all obvious, but event creation before is deterministic
        # (provided we don't change the forward extremities of the room!),
        # so these two events are actually the same event with the same event ID.
        (
            event_outlier,
            unpersisted_context_outlier,
        ) = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                requester=create_requester(user1_id),
                event_dict=event_dict,
                outlier=True,
            )
        )
        (
            event_non_outlier,
            unpersisted_context_non_outlier,
        ) = self.get_success(
            self.hs.get_event_creation_handler().create_event(
                requester=create_requester(user1_id),
                event_dict=event_dict,
                outlier=False,
            )
        )

        # Safety check that we're testing what we think we are
        self.assertEqual(event_outlier.event_id, event_non_outlier.event_id)

        # Now persist the event as an outlier first of all
        # FIXME: Should we use an `EventContext.for_outlier(...)` here?
        # Doesn't seem to matter for this test.
        context_outlier = self.get_success(
            unpersisted_context_outlier.persist(event_outlier)
        )
        self.get_success(
            persist_controller.persist_event(
                event_outlier,
                context_outlier,
            )
        )

        # Since the event is outliered, it won't show up in the sticky_events table...
        sticky_events = self.get_success(
            self.store.db_pool.simple_select_list(
                table="sticky_events", keyvalues=None, retcols=("stream_id", "event_id")
            )
        )
        self.assertEqual(len(sticky_events), 0)

        # Now persist the event properly so that it gets de-outliered.
        context_non_outlier = self.get_success(
            unpersisted_context_non_outlier.persist(event_non_outlier)
        )
        self.get_success(
            persist_controller.persist_event(
                event_non_outlier,
                context_non_outlier,
            )
        )

        end_id = self.store.get_max_sticky_events_stream_id()

        # Check the event made it into the sticky_events table
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=end_id, limit=10
            )
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, event_non_outlier.event_id)
