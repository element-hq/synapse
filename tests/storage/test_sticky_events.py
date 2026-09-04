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
from synapse.replication.tcp.streams._base import StickyEventStreamPosition
from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomID, create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.test_utils.event_injection import inject_event, inject_member_event
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

    def test_soft_failed_events_are_tracked(self) -> None:
        """
        Tests that sticky events marked as soft_failed ARE inserted
        into the sticky_events table, as their soft-failed status can be re-evaluated later,
        as per MSC4354.
        """
        user_id = self.register_user("testuser", "pass")
        token = self.login(user_id, "pass")
        room_id = self.helper.create_room_as(user_id, tok=token)

        start_id = self.store.get_max_sticky_events_stream_id()

        # Create and persist a sticky event that is soft-failed
        soft_failed_sticky_event = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender=user_id,
                type=EventTypes.Message,
                content={"body": "spam checker spammy message", "msgtype": "m.text"},
                internal_metadata={"soft_failed": True},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        )

        end_id = self.store.get_max_sticky_events_stream_id()

        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=end_id, limit=10
            )
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, soft_failed_sticky_event.event_id)

    def test_policy_server_spammy_events_are_not_tracked(self) -> None:
        """
        Tests that sticky events marked as policy_server_spammy are NOT inserted
        into the sticky_events table, as they are exempt from the soft-failed
        re-evaluation logic.
        """
        user_id = self.register_user("testuser", "pass")
        token = self.login(user_id, "pass")
        room_id = self.helper.create_room_as(user_id, tok=token)

        start_id = self.store.get_max_sticky_events_stream_id()

        # Create and persist a sticky event that is marked policy_server_spammy
        # N.B. policy_server_spammy events are always soft-failed too
        _spammy_sticky_event = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender=user_id,
                type=EventTypes.Message,
                content={"body": "spam checker spammy message", "msgtype": "m.text"},
                internal_metadata={"soft_failed": True, "policy_server_spammy": True},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        )

        # Also insert a valid sticky event as a canary for the test setup
        valid_sticky_event = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender=user_id,
                type=EventTypes.Message,
                content={"body": "normal sticky", "msgtype": "m.text"},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        )

        end_id = self.store.get_max_sticky_events_stream_id()

        # Verify only the regular event was inserted
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=end_id, limit=10
            )
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, valid_sticky_event.event_id)

    def test_spam_checker_spammy_events_are_not_tracked(self) -> None:
        """
        Tests that sticky events marked as spam_checker_spammy are NOT inserted
        into the sticky_events table, as they are exempt from the soft-failed
        re-evaluation logic.
        """
        user_id = self.register_user("testuser", "pass")
        token = self.login(user_id, "pass")
        room_id = self.helper.create_room_as(user_id, tok=token)

        start_id = self.store.get_max_sticky_events_stream_id()

        # Create and persist a sticky event that is marked spam_checker_spammy
        # N.B. spam_checker_spammy events are always soft-failed too
        _spammy_sticky_event = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender=user_id,
                type=EventTypes.Message,
                content={"body": "spam checker spammy message", "msgtype": "m.text"},
                internal_metadata={"soft_failed": True, "spam_checker_spammy": True},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        )

        # Also insert a valid sticky event as a canary for the test setup
        valid_sticky_event = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender=user_id,
                type=EventTypes.Message,
                content={"body": "normal sticky", "msgtype": "m.text"},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        )

        end_id = self.store.get_max_sticky_events_stream_id()

        # Verify only the valid sticky event was inserted
        updates = self.get_success(
            self.store.get_updated_sticky_events(
                from_id=start_id, to_id=end_id, limit=10
            )
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, valid_sticky_event.event_id)


class StickyEventsFederationBacklogTestCase(unittest.HomeserverTestCase):
    """
    Storage-level tests for the federation sticky event backlog mechanism.

    This mechanism is used to catch a destination up on sticky events that were skipped over by a
    federation catch-up transaction.
    """

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
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

    def _send_sticky(self, room_id: str, body: str) -> str:
        """
        Send a sticky event.
        """
        return self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": body, "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

    def _stream_ordering_for(self, event_id: str) -> int:
        """
        Get the `stream_ordering` for the given event.
        """
        event = self.get_success(self.hs.get_datastores().main.get_event(event_id))
        stream_ordering = event.internal_metadata.stream_ordering
        assert stream_ordering is not None
        return stream_ordering

    def _sticky_stream_id_for(self, event_id: str) -> int:
        """
        Get the `sticky_events` `stream_id` for the given event.
        """
        return self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_one_onecol(
                table="sticky_events",
                keyvalues={"event_id": event_id},
                retcol="stream_id",
                desc="test:get_sticky_stream_id",
            )
        )

    def _backlog_rows(self) -> list[tuple[str, str, int]]:
        """
        All rows of the `destination_room_sticky_events_backlog` table.
        """
        rows = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_list(
                table="destination_room_sticky_events_backlog",
                keyvalues=None,
                retcols=("destination", "room_id", "sticky_events_stream_position"),
            )
        )
        return sorted(rows)

    def test_mark_backlogged_after_catchup_records_earliest_unsent_per_room(
        self,
    ) -> None:
        """
        Tests that a catch-up transaction that advances over a gap records,
        per room and per destination, one row for every earliest sticky event
        left unsent in the gap.

        See the docstring on `mark_backlogged_sticky_events_after_catchup_transaction`
        for a diagrammatical description.
        """
        room1 = self.helper.create_room_as(self.user_id, tok=self.token)
        room2 = self.helper.create_room_as(self.user_id, tok=self.token)

        # The event immediately before the gap.
        # Suppose that this is where the destination had
        # successfully been caught up to.
        before_gap = self.helper.send(room1, "before the gap", tok=self.token)[
            "event_id"
        ]

        # Send 2 sticky events into room1
        room1_sticky1 = self._send_sticky(room1, "sticky 1")
        _room1_sticky2 = self._send_sticky(room1, "sticky 2")

        # Send a sticky event into room2
        room2_sticky3 = self._send_sticky(room2, "sticky 3")

        # Send a couple of events for the the catch-up transaction to advance us to.
        (room1_after_gap,) = self.helper.send_messages(room1, 1, tok=self.token)
        (room2_after_gap,) = self.helper.send_messages(room2, 1, tok=self.token)

        # We store destination rooms entries for those:
        # this is how the outstanding events to be sent are tracked.
        # It's also a necessary prerequisite for the backlog marking calculation.
        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room1, self._stream_ordering_for(room1_after_gap)
            )
        )
        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room2, self._stream_ordering_for(room2_after_gap)
            )
        )

        # Now suppose we sent the first catch-up transaction (for room1, since the forward extremity
        # of room1 is the oldest catch-up forward extremity in our database).
        # This creates a gap of unsent sticky events that need to be caught up.
        self.get_success(
            self.store.mark_backlogged_sticky_events_after_catchup_transaction(
                "host2",
                old_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    before_gap
                ),
                new_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    room1_after_gap
                ),
                event_stream_orderings_sent_in_transaction={
                    self._stream_ordering_for(room1_after_gap)
                },
            )
        )

        self.assertEqual(
            self._backlog_rows(),
            [
                # In room1: we need to catch up from the first sticky event
                ("host2", room1, self._sticky_stream_id_for(room1_sticky1)),
                # In room2: we need to catch up from the first sticky event in that room
                ("host2", room2, self._sticky_stream_id_for(room2_sticky3)),
            ],
        )

    def test_mark_backlogged_after_catchup_ignores_events_outside_the_gap(self) -> None:
        """
        Tests that when marking a backlog, we ignore:
            - sticky events outside the gap
            - non-sticky events inside the gap
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        # Send a sticky event. Suppose we had already delivered this one to the destination.
        event1_sticky = self._send_sticky(room_id, "already sent")

        # Send 2 regular events that we suppose we had _not_ delivered to the destination yet.
        (_event2_nonsticky, _event3_nonsticky) = self.helper.send_messages(
            room_id, 2, tok=self.token
        )
        # Send a sticky event. This is the one we'll treat as the forward extremity
        event4_sticky = self._send_sticky(room_id, "not yet due to be sent")

        # Note down that we have events up to `event4_sticky` that need to be
        # sent out.
        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room_id, self._stream_ordering_for(event4_sticky)
            )
        )

        # Suppose we sent a catch-up transaction with `event4_sticky`,
        self.get_success(
            self.store.mark_backlogged_sticky_events_after_catchup_transaction(
                "host2",
                old_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event1_sticky
                ),
                new_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event4_sticky
                ),
                # Really we 'should' put `event4_sticky` here to match reality
                # However we're interested in testing the range being correct without
                # the set difference operation covering up any mistakes.
                event_stream_orderings_sent_in_transaction=set(),
            )
        )

        # There should be no backlog of unsent sticky events tracked,
        # because there were none in the gap.
        self.assertEqual(self._backlog_rows(), [])

    def test_mark_backlogged_after_catchup_keeps_earliest_position(self) -> None:
        """
        Tests that repeated catch-up transactions do not advance the backlog position
        (as that would lose sticky events in the gap).
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        # First of all, suppose we had already sent out an event
        event1_start = self.helper.send(room_id, "gap start", tok=self.token)[
            "event_id"
        ]
        # then send a sticky event that we will lose in the gap
        event2_sticky = self._send_sticky(room_id, "early sticky")
        # Send a 'middle' event that we will send out in a catch-up transaction
        event3_middle = self.helper.send(room_id, "middle", tok=self.token)["event_id"]

        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room_id, self._stream_ordering_for(event3_middle)
            )
        )

        # We get a catch-up transaction sent out with `middle` in it.
        # This creates a gap of unsent sticky events.
        self.get_success(
            self.store.mark_backlogged_sticky_events_after_catchup_transaction(
                "host2",
                old_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event1_start
                ),
                new_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event3_middle
                ),
                event_stream_orderings_sent_in_transaction={
                    self._stream_ordering_for(event3_middle)
                },
            )
        )
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(event2_sticky))],
        )

        # Now send another sticky event and 'lose' it in a gap again.
        event4_sticky = self._send_sticky(room_id, "late sticky")
        # Send a final event that we will send out in a catch-up transaction
        # (in order to create a gap for `event4_sticky` to sit in)
        event5_end = self.helper.send(room_id, "gap end", tok=self.token)["event_id"]

        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room_id, self._stream_ordering_for(event5_end)
            )
        )

        self.get_success(
            self.store.mark_backlogged_sticky_events_after_catchup_transaction(
                "host2",
                old_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event3_middle
                ),
                new_last_successfully_sent_stream_ordering=self._stream_ordering_for(
                    event5_end
                ),
                event_stream_orderings_sent_in_transaction={
                    self._stream_ordering_for(event5_end)
                },
            )
        )

        # We should find that the backlog still starts at `event2_sticky`,
        # because it's the earliest sticky event that needs catching up.
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(event2_sticky))],
        )

    def test_get_backlogged_sticky_events_returns_none_when_no_backlog(self) -> None:
        """
        Tests that `get_backlogged_sticky_events` returns `None` when
        there is nothing to catch up on (empty backlog table).
        """
        # Make a room with a sticky event that we intend to send to
        # the destination
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)
        event_id = self._send_sticky(room_id, "sticky")

        # This notes our intention to send the event to the destination
        # But it's not a sticky event backlog yet, just part of the regular
        # event flow
        self.get_success(
            self.store.store_destination_rooms_entries(
                {"host2"}, room_id, self._stream_ordering_for(event_id)
            )
        )

        # So there should be no backlog
        self.assertIsNone(
            self.get_success(
                self.store.get_backlogged_sticky_events_for_destination("host2")
            )
        )

    def test_get_backlogged_sticky_events_returns_local_events_in_stream_order(
        self,
    ) -> None:
        """
        Tests that `get_backlogged_sticky_events` returns sticky events in stream order.
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        _sticky_1 = self._send_sticky(room_id, "sticky 1")
        sticky_2 = self._send_sticky(room_id, "sticky 2")
        sticky_3 = self._send_sticky(room_id, "sticky 3")
        sticky_4 = self._send_sticky(room_id, "sticky 4")

        # Pretend a catch-up transaction left a gap of unsent sticky events,
        # starting from sticky_2 onwards.
        self.get_success(
            self.store.db_pool.simple_insert(
                table="destination_room_sticky_events_backlog",
                values={
                    "destination": "host2",
                    "room_id": room_id,
                    "sticky_events_stream_position": self._sticky_stream_id_for(
                        sticky_2
                    ),
                },
                desc="test:insert_backlog",
            )
        )

        result = self.get_success(
            self.store.get_backlogged_sticky_events_for_destination("host2")
        )

        self.assertEqual(
            result,
            (
                RoomID.from_string(room_id),
                self._sticky_stream_id_for(sticky_4),
                # We see sticky 2 events up to and including 4, in that order
                [sticky_2, sticky_3, sticky_4],
            ),
        )

    def test_get_backlogged_sticky_events_respects_limit(self) -> None:
        """
        Tests that `get_backlogged_sticky_events` respects the limit.
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        sticky_1 = self._send_sticky(room_id, "sticky 1")
        sticky_2 = self._send_sticky(room_id, "sticky 2")
        _sticky_3 = self._send_sticky(room_id, "sticky 3")

        # Pretend a catch-up transaction left a gap of unsent sticky events,
        # starting from sticky_1 onwards.
        self.get_success(
            self.store.db_pool.simple_insert(
                table="destination_room_sticky_events_backlog",
                values={
                    "destination": "host2",
                    "room_id": room_id,
                    "sticky_events_stream_position": self._sticky_stream_id_for(
                        sticky_1
                    ),
                },
                desc="test:insert_backlog",
            )
        )

        self.assertEqual(
            self.get_success(
                self.store.get_backlogged_sticky_events_for_destination(
                    "host2", limit=2
                )
            ),
            (
                RoomID.from_string(room_id),
                # We get the sticky event stream ID of sticky_2 as that's the last one we received
                # in this window
                self._sticky_stream_id_for(sticky_2),
                # We limit to 2 so we don't see sticky_3 here
                [sticky_1, sticky_2],
            ),
        )

    def test_get_backlogged_sticky_events_excludes_remote_senders(self) -> None:
        """
        Tests that we only consider our own (locally-sent) sticky events as
        eligible for backlog catch-up.

        > As with regular events, servers are only responsible for sending sticky events originating from their own server.
        > — https://github.com/matrix-org/matrix-spec-proposals/blob/kegan/persist-edu/proposals/4354-sticky-events.md?plain=1#L195C1-L195C114
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)
        self.get_success(
            inject_member_event(self.hs, room_id, "@remote:host3", Membership.JOIN)
        )

        remote_sticky = self.get_success(
            inject_event(
                self.hs,
                room_id=room_id,
                sender="@remote:host3",
                type=EventTypes.Message,
                content={"body": "remote sticky", "msgtype": "m.text"},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        ).event_id
        local_sticky = self._send_sticky(room_id, "local sticky")

        # Sanity check our test: the remote event _is_ in the sticky events table.
        self.assertEqual(
            set(
                self.get_success(
                    self.store.db_pool.simple_select_onecol(
                        table="sticky_events",
                        keyvalues={"room_id": room_id},
                        retcol="event_id",
                        desc="test:all_sticky_event_ids",
                    )
                )
            ),
            {remote_sticky, local_sticky},
        )

        assert self._sticky_stream_id_for(remote_sticky) < self._sticky_stream_id_for(
            local_sticky
        )

        self.get_success(
            self.store.db_pool.simple_insert(
                table="destination_room_sticky_events_backlog",
                values={
                    "destination": "host2",
                    "room_id": room_id,
                    "sticky_events_stream_position": self._sticky_stream_id_for(
                        remote_sticky
                    ),
                },
                desc="test:insert_backlog",
            )
        )

        self.assertEqual(
            self.get_success(
                self.store.get_backlogged_sticky_events_for_destination("host2")
            ),
            (
                RoomID.from_string(room_id),
                self._sticky_stream_id_for(local_sticky),
                [local_sticky],
            ),
        )

    def test_get_backlogged_sticky_events_cleans_up_stale_backlog(self) -> None:
        """
        Tests that backlog rows are removed on-demand when it turns out there are
        no unexpired sticky events remaining.

        The backlog row is essentially just a 'hint' that there might be sticky events
        left to send, but not a guarantee (due to expiry).
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        # A sticky event that expires almost immediately.
        short_lived = self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(milliseconds=1),
            content={"body": "short lived", "msgtype": "m.text"},
            tok=self.token,
        )["event_id"]

        self.get_success(
            self.store.db_pool.simple_insert(
                table="destination_room_sticky_events_backlog",
                values={
                    "destination": "host2",
                    "room_id": room_id,
                    "sticky_events_stream_position": self._sticky_stream_id_for(
                        short_lived
                    ),
                },
                desc="test:insert_backlog",
            )
        )

        # Advance the reactor and trigger the deletion of expired sticky events
        self.reactor.advance(0.002)
        self.get_success(self.store._delete_expired_sticky_events())

        self.assertIsNone(
            self.get_success(
                self.store.get_backlogged_sticky_events_for_destination("host2")
            )
        )

        # Also note that the `destination_room_sticky_events_backlog` has been cleared
        # so that we don't keep reconsidering this room that no longer has any
        # unexpired sticky events to be sent.
        self.assertEqual(self._backlog_rows(), [])

    def test_mark_backlogged_sticky_events_sent_advances_position(self) -> None:
        """
        Marking a batch as sent moves the recorded position to just *after* the
        highest sent position, so the next batch starts with the first unsent event.
        """
        room_id = self.helper.create_room_as(self.user_id, tok=self.token)

        sticky_1 = self._send_sticky(room_id, "sticky 1")
        sticky_2 = self._send_sticky(room_id, "sticky 2")
        sticky_3 = self._send_sticky(room_id, "sticky 3")

        self.get_success(
            self.store.db_pool.simple_insert(
                table="destination_room_sticky_events_backlog",
                values={
                    "destination": "host2",
                    "room_id": room_id,
                    "sticky_events_stream_position": self._sticky_stream_id_for(
                        sticky_1
                    ),
                },
                desc="test:insert_backlog",
            )
        )

        self.get_success(
            self.store.mark_backlogged_sticky_events_sent(
                "host2",
                RoomID.from_string(room_id),
                StickyEventStreamPosition(self._sticky_stream_id_for(sticky_2)),
            )
        )

        # The stored position is an *inclusive lower bound on what is left*, hence
        # exactly one past the highest event we sent.
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(sticky_2) + 1)],
        )

        # And the next batch is just the remaining event.
        self.assertEqual(
            self.get_success(
                self.store.get_backlogged_sticky_events_for_destination("host2")
            ),
            (
                RoomID.from_string(room_id),
                self._sticky_stream_id_for(sticky_3),
                [sticky_3],
            ),
        )
