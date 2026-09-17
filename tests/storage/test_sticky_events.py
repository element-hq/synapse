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
from http import HTTPStatus

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
from synapse.rest.client import login, register, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.test_utils.event_injection import inject_event
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
        sync.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {
            "msc3575_enabled": True,
            "msc4354_enabled": True,
        }
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

    def _get_visible_sticky_event_ids(self) -> set[str]:
        """
        Returns the IDs of the sticky events visible to clients in sync.
        """
        sync_body: JsonDict = {
            "lists": {
                "main": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    # We don't want any timeline events, just sticky events
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                "org.matrix.msc4354.sticky_events": {
                    "enabled": True,
                }
            },
        }
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync",
            sync_body,
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        sticky_events = channel.json_body["extensions"].get(
            "org.matrix.msc4354.sticky_events"
        )
        if sticky_events is None:
            return set()
        events_in_room = (
            sticky_events.get("rooms", {}).get(self.room_id, {}).get("events", [])
        )
        return {event["event_id"] for event in events_in_room}

    def test_soft_failure_cleared_when_state_changes(self) -> None:
        """
        Tests that a soft-failed sticky event stops being soft-failed once a change to
        the room's current state means that it passes auth after all.
        """
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        self.helper.join(self.room_id, user2_id, tok=user2_tok)

        # Devoice user2, so that their sticky event will (realistically) fail auth
        # against the room's current state.
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            body={"users": {self.user_id: 100, user2_id: -1}, "events_default": 0},
            tok=self.token,
        )

        # Inject a soft-failed sticky event. This is cheating a bit for brevity.
        # In the real world, we'd need to craft a soft-failed sticky event to arrive over federation.
        # The Complement test will do this: https://github.com/matrix-org/complement/pull/806/files#diff-6c9d6d169485d0848c6b20dd9b43f6fe669a8a710e42f953d08fa25a99cc8f4cR509
        event_id = self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                sender=user2_id,
                type=EventTypes.Message,
                content={"body": "sticky", "msgtype": "m.text"},
                internal_metadata={"soft_failed": True},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        ).event_id

        # Whilst it is soft-failed, the event isn't shown to clients.
        self.assertEqual(self._get_visible_sticky_event_ids(), set())

        # Change the room's power levels to voice user2 back.
        # This triggers the soft-fail re-evaluation and also allows the soft-failed sticky
        # event to pass state-dependent auth checks against the current state, becoming
        # un-soft-failed
        self.helper.send_state(
            self.room_id,
            EventTypes.PowerLevels,
            body={"users": {self.user_id: 100, user2_id: 0}, "events_default": 0},
            tok=self.token,
        )

        # The event has been re-evaluated and is now shown to clients...
        self.assertEqual(self._get_visible_sticky_event_ids(), {event_id})
        # ...and the soft-failure flag has been cleared.
        event = self.get_success(self.store.get_event(event_id))
        self.assertFalse(event.internal_metadata.is_soft_failed())

    def test_soft_failure_cleared_when_sender_membership_changes(self) -> None:
        """
        Tests that soft-failure status of a sticky event is reconsidered when
        the sender's membership changes.
        """
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Inject a soft-failed sticky event from user2
        event_id = self.get_success(
            inject_event(
                self.hs,
                room_id=self.room_id,
                sender=user2_id,
                type=EventTypes.Message,
                content={"body": "sticky", "msgtype": "m.text"},
                internal_metadata={"soft_failed": True},
                # Corresponds to StickyEvent.EVENT_FIELD_NAME
                msc4354_sticky=StickyEventField(
                    duration_ms=Duration(minutes=1).as_millis()
                ),
            )
        ).event_id

        # Whilst it is soft-failed, the event isn't shown to clients.
        self.assertEqual(self._get_visible_sticky_event_ids(), set())

        # Check that an irrelevant user's membership changing doesn't affect the event
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")
        self.helper.join(self.room_id, user3_id, tok=user3_tok)
        self.assertEqual(self._get_visible_sticky_event_ids(), set())

        # The sender joins, so the event now passes auth and is un-soft-failed.
        self.helper.join(self.room_id, user2_id, tok=user2_tok)
        self.assertEqual(self._get_visible_sticky_event_ids(), {event_id})
        # ...and the soft-failure has been cleared from the event itself.
        event = self.get_success(self.store.get_event(event_id))
        self.assertFalse(event.internal_metadata.is_soft_failed())
