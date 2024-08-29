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
from typing import List, Optional

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, Membership
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
