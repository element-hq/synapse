#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import logging
from typing import Dict, List, Optional, Tuple, cast

import attr

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, Membership, RoomTypes
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.federation.federation_base import event_from_pdu_json
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import StateMap
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)


class ExtremPruneTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.state = self.hs.get_state_handler()
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self._persistence = persistence
        self._state_storage_controller = self.hs.get_storage_controllers().state
        self.store = self.hs.get_datastores().main

        self.register_user("user", "pass")
        self.token = self.login("user", "pass")

        self.room_id = self.helper.create_room_as(
            "user", room_version=RoomVersions.V6.identifier, tok=self.token
        )

        body = self.helper.send(self.room_id, body="Test", tok=self.token)
        local_message_event_id = body["event_id"]

        # Fudge a remote event and persist it. This will be the extremity before
        # the gap.
        self.remote_event_1 = event_from_pdu_json(
            {
                "type": EventTypes.Message,
                "state_key": "@user:other",
                "content": {},
                "room_id": self.room_id,
                "sender": "@user:other",
                "depth": 5,
                "prev_events": [local_message_event_id],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        self.persist_event(self.remote_event_1)

        # Check that the current extremities is the remote event.
        self.assert_extremities([self.remote_event_1.event_id])

    def persist_event(
        self, event: EventBase, state: Optional[StateMap[str]] = None
    ) -> None:
        """Persist the event, with optional state"""
        context = self.get_success(
            self.state.compute_event_context(
                event,
                state_ids_before_event=state,
                partial_state=None if state is None else False,
            )
        )
        self.get_success(self._persistence.persist_event(event, context))

    def assert_extremities(self, expected_extremities: List[str]) -> None:
        """Assert the current extremities for the room"""
        extremities = self.get_success(
            self.store.get_prev_events_for_room(self.room_id)
        )
        self.assertCountEqual(extremities, expected_extremities)

    def test_prune_gap(self) -> None:
        """Test that we drop extremities after a gap when we see an event from
        the same domain.
        """

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other",
                "depth": 50,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([remote_event_2.event_id])

    def test_do_not_prune_gap_if_state_different(self) -> None:
        """Test that we don't prune extremities after a gap if the resolved
        state is different.
        """

        # Fudge a second event which points to an event we don't have.
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Message,
                "state_key": "@user:other",
                "content": {},
                "room_id": self.room_id,
                "sender": "@user:other",
                "depth": 10,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        # Now we persist it with state with a dropped history visibility
        # setting. The state resolution across the old and new event will then
        # include it, and so the resolved state won't match the new state.
        state_before_gap = dict(
            self.get_success(
                self._state_storage_controller.get_current_state_ids(self.room_id)
            )
        )
        state_before_gap.pop(("m.room.history_visibility", ""))

        context = self.get_success(
            self.state.compute_event_context(
                remote_event_2,
                state_ids_before_event=state_before_gap,
                partial_state=False,
            )
        )

        self.get_success(self._persistence.persist_event(remote_event_2, context))

        # Check that we haven't dropped the old extremity.
        self.assert_extremities([self.remote_event_1.event_id, remote_event_2.event_id])

    def test_prune_gap_if_old(self) -> None:
        """Test that we drop extremities after a gap when the previous extremity
        is "old"
        """

        # Advance the clock for many days to make the old extremity "old". We
        # also set the depth to "lots".
        self.reactor.advance(7 * 24 * 60 * 60)

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other2",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other2",
                "depth": 10000,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([remote_event_2.event_id])

    def test_do_not_prune_gap_if_other_server(self) -> None:
        """Test that we do not drop extremities after a gap when we see an event
        from a different domain.
        """

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other2",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other2",
                "depth": 10,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([self.remote_event_1.event_id, remote_event_2.event_id])

    def test_prune_gap_if_dummy_remote(self) -> None:
        """Test that we drop extremities after a gap when the previous extremity
        is a local dummy event and only points to remote events.
        """

        body = self.helper.send_event(
            self.room_id, type=EventTypes.Dummy, content={}, tok=self.token
        )
        local_message_event_id = body["event_id"]
        self.assert_extremities([local_message_event_id])

        # Advance the clock for many days to make the old extremity "old". We
        # also set the depth to "lots".
        self.reactor.advance(7 * 24 * 60 * 60)

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other2",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other2",
                "depth": 10000,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([remote_event_2.event_id])

    def test_prune_gap_if_dummy_local(self) -> None:
        """Test that we don't drop extremities after a gap when the previous
        extremity is a local dummy event and points to local events.
        """

        body = self.helper.send(self.room_id, body="Test", tok=self.token)

        body = self.helper.send_event(
            self.room_id, type=EventTypes.Dummy, content={}, tok=self.token
        )
        local_message_event_id = body["event_id"]
        self.assert_extremities([local_message_event_id])

        # Advance the clock for many days to make the old extremity "old". We
        # also set the depth to "lots".
        self.reactor.advance(7 * 24 * 60 * 60)

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other2",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other2",
                "depth": 10000,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([remote_event_2.event_id, local_message_event_id])

    def test_do_not_prune_gap_if_not_dummy(self) -> None:
        """Test that we do not drop extremities after a gap when the previous extremity
        is not a dummy event.
        """

        body = self.helper.send(self.room_id, body="test", tok=self.token)
        local_message_event_id = body["event_id"]
        self.assert_extremities([local_message_event_id])

        # Fudge a second event which points to an event we don't have. This is a
        # state event so that the state changes (otherwise we won't prune the
        # extremity as they'll have the same state group).
        remote_event_2 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": "@user:other2",
                "content": {"membership": Membership.JOIN},
                "room_id": self.room_id,
                "sender": "@user:other2",
                "depth": 10000,
                "prev_events": ["$some_unknown_message"],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        state_before_gap = self.get_success(
            self._state_storage_controller.get_current_state_ids(self.room_id)
        )

        self.persist_event(remote_event_2, state=state_before_gap)

        # Check the new extremity is just the new remote event.
        self.assert_extremities([local_message_event_id, remote_event_2.event_id])


class InvalideUsersInRoomCacheTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.state = self.hs.get_state_handler()
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self._persistence = persistence
        self.store = self.hs.get_datastores().main

    def test_remote_user_rooms_cache_invalidated(self) -> None:
        """Test that if the server leaves a room the `get_rooms_for_user` cache
        is invalidated for remote users.
        """

        # Set up a room with a local and remote user in it.
        user_id = self.register_user("user", "pass")
        token = self.login("user", "pass")

        room_id = self.helper.create_room_as(
            "user", room_version=RoomVersions.V6.identifier, tok=token
        )

        body = self.helper.send(room_id, body="Test", tok=token)
        local_message_event_id = body["event_id"]

        # Fudge a join event for a remote user.
        remote_user = "@user:other"
        remote_event_1 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": remote_user,
                "content": {"membership": Membership.JOIN},
                "room_id": room_id,
                "sender": remote_user,
                "depth": 5,
                "prev_events": [local_message_event_id],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        context = self.get_success(self.state.compute_event_context(remote_event_1))
        self.get_success(self._persistence.persist_event(remote_event_1, context))

        # Call `get_rooms_for_user` to add the remote user to the cache
        rooms = self.get_success(self.store.get_rooms_for_user(remote_user))
        self.assertEqual(set(rooms), {room_id})

        # Now we have the local server leave the room, and check that calling
        # `get_user_in_room` for the remote user no longer includes the room.
        self.helper.leave(room_id, user_id, tok=token)

        rooms = self.get_success(self.store.get_rooms_for_user(remote_user))
        self.assertEqual(set(rooms), set())

    def test_room_remote_user_cache_invalidated(self) -> None:
        """Test that if the server leaves a room the `get_users_in_room` cache
        is invalidated for remote users.
        """

        # Set up a room with a local and remote user in it.
        user_id = self.register_user("user", "pass")
        token = self.login("user", "pass")

        room_id = self.helper.create_room_as(
            "user", room_version=RoomVersions.V6.identifier, tok=token
        )

        body = self.helper.send(room_id, body="Test", tok=token)
        local_message_event_id = body["event_id"]

        # Fudge a join event for a remote user.
        remote_user = "@user:other"
        remote_event_1 = event_from_pdu_json(
            {
                "type": EventTypes.Member,
                "state_key": remote_user,
                "content": {"membership": Membership.JOIN},
                "room_id": room_id,
                "sender": remote_user,
                "depth": 5,
                "prev_events": [local_message_event_id],
                "auth_events": [],
                "origin_server_ts": self.clock.time_msec(),
            },
            RoomVersions.V6,
        )

        context = self.get_success(self.state.compute_event_context(remote_event_1))
        self.get_success(self._persistence.persist_event(remote_event_1, context))

        # Call `get_users_in_room` to add the remote user to the cache
        users = self.get_success(self.store.get_users_in_room(room_id))
        self.assertEqual(set(users), {user_id, remote_user})

        # Now we have the local server leave the room, and check that calling
        # `get_user_in_room` for the remote user no longer includes the room.
        self.helper.leave(room_id, user_id, tok=token)

        users = self.get_success(self.store.get_users_in_room(room_id))
        self.assertEqual(users, [])


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _SlidingSyncJoinedRoomResult:
    room_id: str
    event_stream_ordering: int
    bump_stamp: int
    room_type: str
    room_name: str
    is_encrypted: bool


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _SlidingSyncNonJoinMembershipResult:
    room_id: str
    user_id: str
    membership_event_id: str
    membership: str
    event_stream_ordering: int
    room_type: str
    room_name: str
    is_encrypted: bool


class SlidingSyncPrePopulatedTablesTestCase(HomeserverTestCase):
    """
    Tests to make sure the
    `sliding_sync_joined_rooms`/`sliding_sync_non_join_memberships` database tables are
    populated correctly.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

    def _get_sliding_sync_joined_rooms(self) -> Dict[str, _SlidingSyncJoinedRoomResult]:
        """
        Return the rows from the `sliding_sync_joined_rooms` table.

        Returns:
            Mapping from room_id to _SlidingSyncJoinedRoomResult.
        """
        rows = cast(
            List[Tuple[str, int, int, str, str, bool]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "sliding_sync_joined_rooms",
                    None,
                    retcols=(
                        "room_id",
                        "event_stream_ordering",
                        "bump_stamp",
                        "room_type",
                        "room_name",
                        "is_encrypted",
                    ),
                ),
            ),
        )

        return {
            row[0]: _SlidingSyncJoinedRoomResult(
                room_id=row[0],
                event_stream_ordering=row[1],
                bump_stamp=row[2],
                room_type=row[3],
                room_name=row[4],
                is_encrypted=row[5],
            )
            for row in rows
        }

    def _get_sliding_sync_non_join_memberships(
        self,
    ) -> Dict[Tuple[str, str], _SlidingSyncNonJoinMembershipResult]:
        """
        Return the rows from the `sliding_sync_non_join_memberships` table.

        Returns:
            Mapping from the (room_id, user_id) to _SlidingSyncNonJoinMembershipResult.
        """
        rows = cast(
            List[Tuple[str, str, str, str, int, str, str, bool]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "sliding_sync_non_join_memberships",
                    None,
                    retcols=(
                        "room_id",
                        "user_id",
                        "membership_event_id",
                        "membership",
                        "event_stream_ordering",
                        "room_type",
                        "room_name",
                        "is_encrypted",
                    ),
                ),
            ),
        )

        return {
            (row[0], row[1]): _SlidingSyncNonJoinMembershipResult(
                room_id=row[0],
                user_id=row[1],
                membership_event_id=row[2],
                membership=row[3],
                event_stream_ordering=row[4],
                room_type=row[5],
                room_name=row[6],
                is_encrypted=row[7],
            )
            for row in rows
        }

    def test_joined_room_with_no_info(self) -> None:
        """
        Test joined room that doesn't have a room type, encryption, or name shows up in
        `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            set(),
            exact=True,
        )

    def test_joined_room_with_info(self) -> None:
        """
        Test joined encrypted room with name shows up in `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id1,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=True,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            set(),
            exact=True,
        )

    def test_joined_room_with_info_updated(self) -> None:
        """
        Test info in `sliding_sync_joined_rooms` is updated when the current state is
        updated.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        # Add a room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=None,
                room_name="my super duper room",
                is_encrypted=False,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            set(),
            exact=True,
        )

        # Update the room name
        self.helper.send_state(
            room_id1,
            EventTypes.Name,
            {"name": "my super duper room was renamed"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            room_id1,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        # Make sure we see the new room name
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=None,
                room_name="my super duper room was renamed",
                is_encrypted=True,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            set(),
            exact=True,
        )

    def test_joined_space_room_with_info(self) -> None:
        """
        Test joined space room with name shows up in `sliding_sync_joined_rooms`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )

        # User1 joins the room
        self.helper.join(space_room_id, user1_id, tok=user1_tok)

        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {space_room_id},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[space_room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=space_room_id,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=False,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            set(),
            exact=True,
        )

    # TODO: test_joined_room_state_reset

    def test_non_join_space_room_with_info(self) -> None:
        """
        Test users who was invited shows up in `sliding_sync_non_join_memberships`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        space_room_id = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "creation_content": {EventContentFields.ROOM_TYPE: RoomTypes.SPACE}
            },
        )
        # Add a room name
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space"},
            tok=user2_tok,
        )
        # Encrypt the room
        self.helper.send_state(
            space_room_id,
            EventTypes.RoomEncryption,
            {EventContentFields.ENCRYPTION_ALGORITHM: "m.megolm.v1.aes-sha2"},
            tok=user2_tok,
        )

        # User1 is invited to the room
        user1_invited_response = self.helper.invite(
            space_room_id, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invited_event_pos = self.get_success(
            self.store.get_position_for_event(user1_invited_response["event_id"])
        )

        # Update the room name after we are invited just to make sure
        # we don't update non-join memberships when the room name changes.
        self.helper.send_state(
            space_room_id,
            EventTypes.Name,
            {"name": "my super duper space was renamed"},
            tok=user2_tok,
        )

        # User2 is still joined to the room so we should still have an entry in the
        # `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {space_room_id},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[space_room_id],
            _SlidingSyncJoinedRoomResult(
                room_id=space_room_id,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space was renamed",
                is_encrypted=True,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            {
                (space_room_id, user1_id),
            },
            exact=True,
        )
        # Holds the info according to the current state when the user was invited
        self.assertEqual(
            sliding_sync_non_join_memberships_results.get((space_room_id, user1_id)),
            _SlidingSyncNonJoinMembershipResult(
                room_id=space_room_id,
                user_id=user1_id,
                membership_event_id=user1_invited_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=user1_invited_event_pos.stream,
                room_type=RoomTypes.SPACE,
                room_name="my super duper space",
                is_encrypted=True,
            ),
        )

    def test_non_join_invite_ban(self) -> None:
        """
        Test users who have invite/ban membership in room shows up in
        `sliding_sync_non_join_memberships`.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        user3_tok = self.login(user3_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 is invited to the room
        user1_invited_response = self.helper.invite(
            room_id1, src=user2_id, targ=user1_id, tok=user2_tok
        )
        user1_invited_event_pos = self.get_success(
            self.store.get_position_for_event(user1_invited_response["event_id"])
        )

        # User3 joins the room
        self.helper.join(room_id1, user3_id, tok=user3_tok)
        # User3 is banned from the room
        user3_ban_response = self.helper.ban(
            room_id1, src=user2_id, targ=user3_id, tok=user2_tok
        )
        user3_ban_event_pos = self.get_success(
            self.store.get_position_for_event(user3_ban_response["event_id"])
        )

        # User2 is still joined to the room so we should still have an entry
        # in the `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            {room_id1},
            exact=True,
        )
        self.assertEqual(
            sliding_sync_joined_rooms_results[room_id1],
            _SlidingSyncJoinedRoomResult(
                room_id=room_id1,
                # TODO
                event_stream_ordering=None,
                bump_stamp=None,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )

        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user3_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_non_join_memberships_results.get((room_id1, user1_id)),
            _SlidingSyncNonJoinMembershipResult(
                room_id=room_id1,
                user_id=user1_id,
                membership_event_id=user1_invited_response["event_id"],
                membership=Membership.INVITE,
                event_stream_ordering=user1_invited_event_pos.stream,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )
        self.assertEqual(
            sliding_sync_non_join_memberships_results.get((room_id1, user3_id)),
            _SlidingSyncNonJoinMembershipResult(
                room_id=room_id1,
                user_id=user3_id,
                membership_event_id=user3_ban_response["event_id"],
                membership=Membership.BAN,
                event_stream_ordering=user3_ban_event_pos.stream,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )

    # TODO: Test remote invite

    def test_non_join_server_left_room(self) -> None:
        """
        Test everyone local leaves the room but their leave membership still shows up in
        `sliding_sync_non_join_memberships`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # User1 joins the room
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        # User2 leaves the room
        user2_leave_response = self.helper.leave(room_id1, user2_id, tok=user2_tok)
        user2_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user2_leave_response["event_id"])
        )

        # User1 leaves the room
        user1_leave_response = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        user1_leave_event_pos = self.get_success(
            self.store.get_position_for_event(user1_leave_response["event_id"])
        )

        # No one is joined to the room anymore so we shouldn't have an entry in the
        # `sliding_sync_joined_rooms` table.
        sliding_sync_joined_rooms_results = self._get_sliding_sync_joined_rooms()
        self.assertIncludes(
            set(sliding_sync_joined_rooms_results.keys()),
            set(),
            exact=True,
        )

        # We should still see rows for the leave events (non-joins)
        sliding_sync_non_join_memberships_results = (
            self._get_sliding_sync_non_join_memberships()
        )
        self.assertIncludes(
            set(sliding_sync_non_join_memberships_results.keys()),
            {
                (room_id1, user1_id),
                (room_id1, user2_id),
            },
            exact=True,
        )
        self.assertEqual(
            sliding_sync_non_join_memberships_results.get((room_id1, user1_id)),
            _SlidingSyncNonJoinMembershipResult(
                room_id=room_id1,
                user_id=user1_id,
                membership_event_id=user1_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user1_leave_event_pos.stream,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )
        self.assertEqual(
            sliding_sync_non_join_memberships_results.get((room_id1, user2_id)),
            _SlidingSyncNonJoinMembershipResult(
                room_id=room_id1,
                user_id=user2_id,
                membership_event_id=user2_leave_response["event_id"],
                membership=Membership.LEAVE,
                event_stream_ordering=user2_leave_event_pos.stream,
                room_type=None,
                room_name=None,
                is_encrypted=False,
            ),
        )

    # TODO: test_non_join_state_reset
