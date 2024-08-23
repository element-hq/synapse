#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import List, Tuple
from unittest.mock import AsyncMock, patch

from immutabledict import immutabledict

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import Direction, EventTypes, Membership, RelationTypes
from synapse.api.filtering import Filter
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import FrozenEventV3
from synapse.federation.federation_client import SendJoinResult
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.databases.main.stream import CurrentStateDeltaMembership
from synapse.types import (
    JsonDict,
    PersistedEventPosition,
    RoomStreamToken,
    UserID,
    create_requester,
)
from synapse.util import Clock

from tests.test_utils.event_injection import create_event
from tests.unittest import FederatingHomeserverTestCase, HomeserverTestCase

logger = logging.getLogger(__name__)


class PaginationTestCase(HomeserverTestCase):
    """
    Test the pre-filtering done in the pagination code.

    This is similar to some of the tests in tests.rest.client.test_rooms but here
    we ensure that the filtering done in the database is applied successfully.
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc3874_enabled": True}
        return config

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.user_id = self.register_user("test", "test")
        self.tok = self.login("test", "test")
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.tok)

        self.second_user_id = self.register_user("second", "test")
        self.second_tok = self.login("second", "test")
        self.helper.join(
            room=self.room_id, user=self.second_user_id, tok=self.second_tok
        )

        self.third_user_id = self.register_user("third", "test")
        self.third_tok = self.login("third", "test")
        self.helper.join(room=self.room_id, user=self.third_user_id, tok=self.third_tok)

        # Store a token which is after all the room creation events.
        self.from_token = self.get_success(
            self.hs.get_event_sources().get_current_token_for_pagination(self.room_id)
        )

        # An initial event with a relation from second user.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "Message 1"},
            tok=self.tok,
        )
        self.event_id_1 = res["event_id"]
        res = self.helper.send_event(
            room_id=self.room_id,
            type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": RelationTypes.ANNOTATION,
                    "event_id": self.event_id_1,
                    "key": "👍",
                }
            },
            tok=self.second_tok,
        )
        self.event_id_annotation = res["event_id"]

        # Another event with a relation from third user.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "Message 2"},
            tok=self.tok,
        )
        self.event_id_2 = res["event_id"]
        res = self.helper.send_event(
            room_id=self.room_id,
            type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": RelationTypes.REFERENCE,
                    "event_id": self.event_id_2,
                }
            },
            tok=self.third_tok,
        )
        self.event_id_reference = res["event_id"]

        # An event with no relations.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={"msgtype": "m.text", "body": "No relations"},
            tok=self.tok,
        )
        self.event_id_none = res["event_id"]

    def _filter_messages(self, filter: JsonDict) -> List[str]:
        """Make a request to /messages with a filter, returns the chunk of events."""

        events, next_key = self.get_success(
            self.hs.get_datastores().main.paginate_room_events_by_topological_ordering(
                room_id=self.room_id,
                from_key=self.from_token.room_key,
                to_key=None,
                direction=Direction.FORWARDS,
                limit=10,
                event_filter=Filter(self.hs, filter),
            )
        )

        return [ev.event_id for ev in events]

    def test_filter_relation_senders(self) -> None:
        # Messages which second user reacted to.
        filter = {"related_by_senders": [self.second_user_id]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_1])

        # Messages which third user reacted to.
        filter = {"related_by_senders": [self.third_user_id]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_2])

        # Messages which either user reacted to.
        filter = {"related_by_senders": [self.second_user_id, self.third_user_id]}
        chunk = self._filter_messages(filter)
        self.assertCountEqual(chunk, [self.event_id_1, self.event_id_2])

    def test_filter_relation_type(self) -> None:
        # Messages which have annotations.
        filter = {"related_by_rel_types": [RelationTypes.ANNOTATION]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_1])

        # Messages which have references.
        filter = {"related_by_rel_types": [RelationTypes.REFERENCE]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_2])

        # Messages which have either annotations or references.
        filter = {
            "related_by_rel_types": [
                RelationTypes.ANNOTATION,
                RelationTypes.REFERENCE,
            ]
        }
        chunk = self._filter_messages(filter)
        self.assertCountEqual(chunk, [self.event_id_1, self.event_id_2])

    def test_filter_relation_senders_and_type(self) -> None:
        # Messages which second user reacted to.
        filter = {
            "related_by_senders": [self.second_user_id],
            "related_by_rel_types": [RelationTypes.ANNOTATION],
        }
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_1])

    def test_duplicate_relation(self) -> None:
        """An event should only be returned once if there are multiple relations to it."""
        self.helper.send_event(
            room_id=self.room_id,
            type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": RelationTypes.ANNOTATION,
                    "event_id": self.event_id_1,
                    "key": "A",
                }
            },
            tok=self.second_tok,
        )

        filter = {"related_by_senders": [self.second_user_id]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_1])

    def test_filter_rel_types(self) -> None:
        # Messages which are annotations.
        filter = {"org.matrix.msc3874.rel_types": [RelationTypes.ANNOTATION]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_annotation])

        # Messages which are references.
        filter = {"org.matrix.msc3874.rel_types": [RelationTypes.REFERENCE]}
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_reference])

        # Messages which are either annotations or references.
        filter = {
            "org.matrix.msc3874.rel_types": [
                RelationTypes.ANNOTATION,
                RelationTypes.REFERENCE,
            ]
        }
        chunk = self._filter_messages(filter)
        self.assertCountEqual(
            chunk,
            [self.event_id_annotation, self.event_id_reference],
        )

    def test_filter_not_rel_types(self) -> None:
        # Messages which are not annotations.
        filter = {"org.matrix.msc3874.not_rel_types": [RelationTypes.ANNOTATION]}
        chunk = self._filter_messages(filter)
        self.assertEqual(
            chunk,
            [
                self.event_id_1,
                self.event_id_2,
                self.event_id_reference,
                self.event_id_none,
            ],
        )

        # Messages which are not references.
        filter = {"org.matrix.msc3874.not_rel_types": [RelationTypes.REFERENCE]}
        chunk = self._filter_messages(filter)
        self.assertEqual(
            chunk,
            [
                self.event_id_1,
                self.event_id_annotation,
                self.event_id_2,
                self.event_id_none,
            ],
        )

        # Messages which are neither annotations or references.
        filter = {
            "org.matrix.msc3874.not_rel_types": [
                RelationTypes.ANNOTATION,
                RelationTypes.REFERENCE,
            ]
        }
        chunk = self._filter_messages(filter)
        self.assertEqual(chunk, [self.event_id_1, self.event_id_2, self.event_id_none])


class GetLastEventInRoomBeforeStreamOrderingTestCase(HomeserverTestCase):
    """
    Test `get_last_event_pos_in_room_before_stream_ordering(...)`
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def _update_persisted_instance_name_for_event(
        self, event_id: str, instance_name: str
    ) -> None:
        """
        Update the `instance_name` that persisted the the event in the database.
        """
        return self.get_success(
            self.store.db_pool.simple_update_one(
                "events",
                keyvalues={"event_id": event_id},
                updatevalues={"instance_name": instance_name},
            )
        )

    def _send_event_on_instance(
        self, instance_name: str, room_id: str, access_token: str
    ) -> Tuple[JsonDict, PersistedEventPosition]:
        """
        Send an event in a room and mimic that it was persisted by a specific
        instance/worker.
        """
        event_response = self.helper.send(
            room_id, f"{instance_name} message", tok=access_token
        )

        self._update_persisted_instance_name_for_event(
            event_response["event_id"], instance_name
        )

        event_pos = self.get_success(
            self.store.get_position_for_event(event_response["event_id"])
        )

        return event_response, event_pos

    def test_before_room_created(self) -> None:
        """
        Test that no event is returned if we are using a token before the room was even created
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id,
                end_token=before_room_token.room_key,
            )
        )

        self.assertIsNone(last_event_result)

    def test_after_room_created(self) -> None:
        """
        Test that an event is returned if we are using a token after the room was created
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id,
                end_token=after_room_token.room_key,
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        self.assertIsNotNone(last_event_id)

    def test_activity_in_other_rooms(self) -> None:
        """
        Test to make sure that the last event in the room is returned even if the
        `stream_ordering` has advanced from activity in other rooms.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response = self.helper.send(room_id1, "target!", tok=user1_tok)
        # Create another room to advance the stream_ordering
        self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id1,
                end_token=after_room_token.room_key,
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        # Make sure it's the event we expect (which also means we know it's from the
        # correct room)
        self.assertEqual(last_event_id, event_response["event_id"])

    def test_activity_after_token_has_no_effect(self) -> None:
        """
        Test to make sure we return the last event before the token even if there is
        activity after it.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response = self.helper.send(room_id1, "target!", tok=user1_tok)

        after_room_token = self.event_sources.get_current_token()

        # Send some events after the token
        self.helper.send(room_id1, "after1", tok=user1_tok)
        self.helper.send(room_id1, "after2", tok=user1_tok)

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id1,
                end_token=after_room_token.room_key,
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        # Make sure it's the last event before the token
        self.assertEqual(last_event_id, event_response["event_id"])

    def test_last_event_within_sharded_token(self) -> None:
        """
        Test to make sure we can find the last event that that is *within* the sharded
        token (a token that has an `instance_map` and looks like
        `m{min_pos}~{writer1}.{pos1}~{writer2}.{pos2}`). We are specifically testing
        that we can find an event within the tokens minimum and instance
        `stream_ordering`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response1, event_pos1 = self._send_event_on_instance(
            "worker1", room_id1, user1_tok
        )
        event_response2, event_pos2 = self._send_event_on_instance(
            "worker1", room_id1, user1_tok
        )
        event_response3, event_pos3 = self._send_event_on_instance(
            "worker1", room_id1, user1_tok
        )

        # Create another room to advance the `stream_ordering` on the same worker
        # so we can sandwich event3 in the middle of the token
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response4, event_pos4 = self._send_event_on_instance(
            "worker1", room_id2, user1_tok
        )

        # Assemble a token that encompasses event1 -> event4 on worker1
        end_token = RoomStreamToken(
            stream=event_pos2.stream,
            instance_map=immutabledict({"worker1": event_pos4.stream}),
        )

        # Send some events after the token
        self.helper.send(room_id1, "after1", tok=user1_tok)
        self.helper.send(room_id1, "after2", tok=user1_tok)

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id1,
                end_token=end_token,
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        # Should find closest event before the token in room1
        self.assertEqual(
            last_event_id,
            event_response3["event_id"],
            f"We expected {event_response3['event_id']} but saw {last_event_id} which corresponds to "
            + str(
                {
                    "event1": event_response1["event_id"],
                    "event2": event_response2["event_id"],
                    "event3": event_response3["event_id"],
                }
            ),
        )

    def test_last_event_before_sharded_token(self) -> None:
        """
        Test to make sure we can find the last event that is *before* the sharded token
        (a token that has an `instance_map` and looks like
        `m{min_pos}~{writer1}.{pos1}~{writer2}.{pos2}`).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response1, event_pos1 = self._send_event_on_instance(
            "worker1", room_id1, user1_tok
        )
        event_response2, event_pos2 = self._send_event_on_instance(
            "worker1", room_id1, user1_tok
        )

        # Create another room to advance the `stream_ordering` on the same worker
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response3, event_pos3 = self._send_event_on_instance(
            "worker1", room_id2, user1_tok
        )
        event_response4, event_pos4 = self._send_event_on_instance(
            "worker1", room_id2, user1_tok
        )

        # Assemble a token that encompasses event3 -> event4 on worker1
        end_token = RoomStreamToken(
            stream=event_pos3.stream,
            instance_map=immutabledict({"worker1": event_pos4.stream}),
        )

        # Send some events after the token
        self.helper.send(room_id1, "after1", tok=user1_tok)
        self.helper.send(room_id1, "after2", tok=user1_tok)

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id1,
                end_token=end_token,
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        # Should find closest event before the token in room1
        self.assertEqual(
            last_event_id,
            event_response2["event_id"],
            f"We expected {event_response2['event_id']} but saw {last_event_id} which corresponds to "
            + str(
                {
                    "event1": event_response1["event_id"],
                    "event2": event_response2["event_id"],
                }
            ),
        )

    def test_restrict_event_types(self) -> None:
        """
        Test that we only consider given `event_types` when finding the last event
        before a token.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)
        event_response = self.helper.send_event(
            room_id1,
            type="org.matrix.special_message",
            content={"body": "before1, target!"},
            tok=user1_tok,
        )
        self.helper.send(room_id1, "before2", tok=user1_tok)

        after_room_token = self.event_sources.get_current_token()

        # Send some events after the token
        self.helper.send_event(
            room_id1,
            type="org.matrix.special_message",
            content={"body": "after1"},
            tok=user1_tok,
        )
        self.helper.send(room_id1, "after2", tok=user1_tok)

        last_event_result = self.get_success(
            self.store.get_last_event_pos_in_room_before_stream_ordering(
                room_id=room_id1,
                end_token=after_room_token.room_key,
                event_types=["org.matrix.special_message"],
            )
        )
        assert last_event_result is not None
        last_event_id, _ = last_event_result

        # Make sure it's the last event before the token
        self.assertEqual(last_event_id, event_response["event_id"])


class GetCurrentStateDeltaMembershipChangesForUserTestCase(HomeserverTestCase):
    """
    Test `get_current_state_delta_membership_changes_for_user(...)`
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.state_handler = self.hs.get_state_handler()
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence

    def test_returns_membership_events(self) -> None:
        """
        A basic test that a membership event in the token range is returned for the user.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_pos = self.get_success(
            self.store.get_position_for_event(join_response["event_id"])
        )

        after_room1_token = self.event_sources.get_current_token()

        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_response["event_id"],
                    event_pos=join_pos,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                )
            ],
        )

    def test_server_left_room_after_us(self) -> None:
        """
        Test that when probing over part of the DAG where the server left the room *after
        us*, we still see the join and leave changes.

        This is to make sure we play nicely with this behavior: When the server leaves a
        room, it will insert new rows with `event_id = null` into the
        `current_state_delta_stream` table for all current state.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "power_level_content_override": {
                    "users": {
                        user2_id: 100,
                        # Allow user1 to send state in the room
                        user1_id: 100,
                    }
                }
            },
        )
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )
        # Make sure that random other non-member state that happens to have a `state_key`
        # matching the user ID doesn't mess with things.
        self.helper.send_state(
            room_id1,
            event_type="foobarbazdummy",
            state_key=user1_id,
            body={"foo": "bar"},
            tok=user1_tok,
        )
        # User1 should leave the room first
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        leave_pos1 = self.get_success(
            self.store.get_position_for_event(leave_response1["event_id"])
        )

        # User2 should also leave the room (everyone has left the room which means the
        # server is no longer in the room).
        self.helper.leave(room_id1, user2_id, tok=user2_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Get the membership changes for the user.
        #
        # At this point, the `current_state_delta_stream` table should look like the
        # following. When the server leaves a room, it will insert new rows with
        # `event_id = null` for all current state.
        #
        # | stream_id | room_id  | type                        | state_key      | event_id | prev_event_id |
        # |-----------|----------|-----------------------------|----------------|----------|---------------|
        # | 2         | !x:test  | 'm.room.create'             | ''             | $xxx     | None          |
        # | 3         | !x:test  | 'm.room.member'             | '@user2:test'  | $aaa     | None          |
        # | 4         | !x:test  | 'm.room.history_visibility' | ''             | $xxx     | None          |
        # | 4         | !x:test  | 'm.room.join_rules'         | ''             | $xxx     | None          |
        # | 4         | !x:test  | 'm.room.power_levels'       | ''             | $xxx     | None          |
        # | 7         | !x:test  | 'm.room.member'             | '@user1:test'  | $ooo     | None          |
        # | 8         | !x:test  | 'foobarbazdummy'            | '@user1:test'  | $xxx     | None          |
        # | 9         | !x:test  | 'm.room.member'             | '@user1:test'  | $ppp     | $ooo          |
        # | 10        | !x:test  | 'foobarbazdummy'            | '@user1:test'  | None     | $xxx          |
        # | 10        | !x:test  | 'm.room.create'             | ''             | None     | $xxx          |
        # | 10        | !x:test  | 'm.room.history_visibility' | ''             | None     | $xxx          |
        # | 10        | !x:test  | 'm.room.join_rules'         | ''             | None     | $xxx          |
        # | 10        | !x:test  | 'm.room.member'             | '@user1:test'  | None     | $ppp          |
        # | 10        | !x:test  | 'm.room.member'             | '@user2:test'  | None     | $aaa          |
        # | 10        | !x:test  | 'm.room.power_levels'       |                | None     | $xxx          |
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_response1["event_id"],
                    event_pos=join_pos1,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=leave_response1["event_id"],
                    event_pos=leave_pos1,
                    membership="leave",
                    sender=user1_id,
                    prev_event_id=join_response1["event_id"],
                    prev_event_pos=join_pos1,
                    prev_membership="join",
                    prev_sender=user1_id,
                ),
            ],
        )

    def test_server_left_room_after_us_later(self) -> None:
        """
        Test when the user leaves the room, then sometime later, everyone else leaves
        the room, causing the server to leave the room, we shouldn't see any membership
        changes.

        This is to make sure we play nicely with this behavior: When the server leaves a
        room, it will insert new rows with `event_id = null` into the
        `current_state_delta_stream` table for all current state.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # User1 should leave the room first
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_user1_leave_token = self.event_sources.get_current_token()

        # User2 should also leave the room (everyone has left the room which means the
        # server is no longer in the room).
        self.helper.leave(room_id1, user2_id, tok=user2_tok)

        after_server_leave_token = self.event_sources.get_current_token()

        # Join another room as user1 just to advance the stream_ordering and bust
        # `_membership_stream_cache`
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room_id2, user1_id, tok=user1_tok)

        # Get the membership changes for the user.
        #
        # At this point, the `current_state_delta_stream` table should look like the
        # following. When the server leaves a room, it will insert new rows with
        # `event_id = null` for all current state.
        #
        # TODO: Add DB rows to better see what's going on.
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=after_user1_leave_token.room_key,
                to_key=after_server_leave_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [],
        )

    def test_we_cause_server_left_room(self) -> None:
        """
        Test that when probing over part of the DAG where the user leaves the room
        causing the server to leave the room (because we were the last local user in the
        room), we still see the join and leave changes.

        This is to make sure we play nicely with this behavior: When the server leaves a
        room, it will insert new rows with `event_id = null` into the
        `current_state_delta_stream` table for all current state.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(
            user2_id,
            tok=user2_tok,
            extra_content={
                "power_level_content_override": {
                    "users": {
                        user2_id: 100,
                        # Allow user1 to send state in the room
                        user1_id: 100,
                    }
                }
            },
        )
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )
        # Make sure that random other non-member state that happens to have a `state_key`
        # matching the user ID doesn't mess with things.
        self.helper.send_state(
            room_id1,
            event_type="foobarbazdummy",
            state_key=user1_id,
            body={"foo": "bar"},
            tok=user1_tok,
        )

        # User2 should leave the room first.
        self.helper.leave(room_id1, user2_id, tok=user2_tok)

        # User1 (the person we're testing with) should also leave the room (everyone has
        # left the room which means the server is no longer in the room).
        leave_response1 = self.helper.leave(room_id1, user1_id, tok=user1_tok)
        leave_pos1 = self.get_success(
            self.store.get_position_for_event(leave_response1["event_id"])
        )

        after_room1_token = self.event_sources.get_current_token()

        # Get the membership changes for the user.
        #
        # At this point, the `current_state_delta_stream` table should look like the
        # following. When the server leaves a room, it will insert new rows with
        # `event_id = null` for all current state.
        #
        # | stream_id | room_id   | type                        | state_key     | event_id | prev_event_id |
        # |-----------|-----------|-----------------------------|---------------|----------|---------------|
        # | 2         | '!x:test' | 'm.room.create'             | ''            | '$xxx'   | None          |
        # | 3         | '!x:test' | 'm.room.member'             | '@user2:test' | '$aaa'   | None          |
        # | 4         | '!x:test' | 'm.room.history_visibility' | ''            | '$xxx'   | None          |
        # | 4         | '!x:test' | 'm.room.join_rules'         | ''            | '$xxx'   | None          |
        # | 4         | '!x:test' | 'm.room.power_levels'       | ''            | '$xxx'   | None          |
        # | 7         | '!x:test' | 'm.room.member'             | '@user1:test' | '$ooo'   | None          |
        # | 8         | '!x:test' | 'foobarbazdummy'            | '@user1:test' | '$xxx'   | None          |
        # | 9         | '!x:test' | 'm.room.member'             | '@user2:test' | '$bbb'   | '$aaa'        |
        # | 10        | '!x:test' | 'foobarbazdummy'            | '@user1:test' | None     | '$xxx'        |
        # | 10        | '!x:test' | 'm.room.create'             | ''            | None     | '$xxx'        |
        # | 10        | '!x:test' | 'm.room.history_visibility' | ''            | None     | '$xxx'        |
        # | 10        | '!x:test' | 'm.room.join_rules'         | ''            | None     | '$xxx'        |
        # | 10        | '!x:test' | 'm.room.member'             | '@user1:test' | None     | '$ooo'        |
        # | 10        | '!x:test' | 'm.room.member'             | '@user2:test' | None     | '$bbb'        |
        # | 10        | '!x:test' | 'm.room.power_levels'       | ''            | None     | '$xxx'        |
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_response1["event_id"],
                    event_pos=join_pos1,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=None,  # leave_response1["event_id"],
                    event_pos=leave_pos1,
                    membership="leave",
                    sender=None,  # user1_id,
                    prev_event_id=join_response1["event_id"],
                    prev_event_pos=join_pos1,
                    prev_membership="join",
                    prev_sender=user1_id,
                ),
            ],
        )

    def test_different_user_membership_persisted_in_same_batch(self) -> None:
        """
        Test batch of membership events from different users being processed at once.
        This will result in all of the memberships being stored in the
        `current_state_delta_stream` table with the same `stream_ordering` even though
        the individual events have different `stream_ordering`s.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")
        user3_id = self.register_user("user3", "pass")
        _user3_tok = self.login(user3_id, "pass")
        user4_id = self.register_user("user4", "pass")
        _user4_tok = self.login(user4_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # User2 is just the designated person to create the room (we do this across the
        # tests to be consistent)
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)

        # Persist the user1, user3, and user4 join events in the same batch so they all
        # end up in the `current_state_delta_stream` table with the same
        # stream_ordering.
        join_event3, join_event_context3 = self.get_success(
            create_event(
                self.hs,
                sender=user3_id,
                type=EventTypes.Member,
                state_key=user3_id,
                content={"membership": "join"},
                room_id=room_id1,
            )
        )
        # We want to put user1 in the middle of the batch. This way, regardless of the
        # implementation that inserts rows into current_state_delta_stream` (whether it
        # be minimum/maximum of stream position of the batch), we will still catch bugs.
        join_event1, join_event_context1 = self.get_success(
            create_event(
                self.hs,
                sender=user1_id,
                type=EventTypes.Member,
                state_key=user1_id,
                content={"membership": "join"},
                room_id=room_id1,
            )
        )
        join_event4, join_event_context4 = self.get_success(
            create_event(
                self.hs,
                sender=user4_id,
                type=EventTypes.Member,
                state_key=user4_id,
                content={"membership": "join"},
                room_id=room_id1,
            )
        )
        self.get_success(
            self.persistence.persist_events(
                [
                    (join_event3, join_event_context3),
                    (join_event1, join_event_context1),
                    (join_event4, join_event_context4),
                ]
            )
        )

        after_room1_token = self.event_sources.get_current_token()

        # Get the membership changes for the user.
        #
        # At this point, the `current_state_delta_stream` table should look like (notice
        # those three memberships at the end with `stream_id=7` because we persisted
        # them in the same batch):
        #
        # | stream_id | room_id   | type                       | state_key        | event_id | prev_event_id |
        # |-----------|-----------|----------------------------|------------------|----------|---------------|
        # | 2         | '!x:test' | 'm.room.create'            | ''               | '$xxx'   | None          |
        # | 3         | '!x:test' | 'm.room.member'            | '@user2:test'    | '$xxx'   | None          |
        # | 4         | '!x:test' | 'm.room.history_visibility'| ''               | '$xxx'   | None          |
        # | 4         | '!x:test' | 'm.room.join_rules'        | ''               | '$xxx'   | None          |
        # | 4         | '!x:test' | 'm.room.power_levels'      | ''               | '$xxx'   | None          |
        # | 7         | '!x:test' | 'm.room.member'            | '@user3:test'    | '$xxx'   | None          |
        # | 7         | '!x:test' | 'm.room.member'            | '@user1:test'    | '$xxx'   | None          |
        # | 7         | '!x:test' | 'm.room.member'            | '@user4:test'    | '$xxx'   | None          |
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
            )
        )

        join_pos3 = self.get_success(
            self.store.get_position_for_event(join_event3.event_id)
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_event1.event_id,
                    # Ideally, this would be `join_pos1` (to match the `event_id`) but
                    # when events are persisted in a batch, they are all stored in the
                    # `current_state_delta_stream` table with the minimum
                    # `stream_ordering` from the batch.
                    event_pos=join_pos3,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
            ],
        )

    def test_state_reset(self) -> None:
        """
        Test a state reset scenario where the user gets removed from the room (when
        there is no corresponding leave event)
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )

        before_reset_token = self.event_sources.get_current_token()

        # Send another state event to make a position for the state reset to happen at
        dummy_state_response = self.helper.send_state(
            room_id1,
            event_type="foobarbaz",
            state_key="",
            body={"foo": "bar"},
            tok=user2_tok,
        )
        dummy_state_pos = self.get_success(
            self.store.get_position_for_event(dummy_state_response["event_id"])
        )

        # Mock a state reset removing the membership for user1 in the current state
        self.get_success(
            self.store.db_pool.simple_delete(
                table="current_state_events",
                keyvalues={
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                },
                desc="state reset user in current_state_delta_stream",
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                table="current_state_delta_stream",
                values={
                    "stream_id": dummy_state_pos.stream,
                    "room_id": room_id1,
                    "type": EventTypes.Member,
                    "state_key": user1_id,
                    "event_id": None,
                    "prev_event_id": join_response1["event_id"],
                    "instance_name": dummy_state_pos.instance_name,
                },
                desc="state reset user in current_state_delta_stream",
            )
        )

        # Manually bust the cache since we we're just manually messing with the database
        # and not causing an actual state reset.
        self.store._membership_stream_cache.entity_has_changed(
            user1_id, dummy_state_pos.stream
        )

        after_reset_token = self.event_sources.get_current_token()

        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_reset_token.room_key,
                to_key=after_reset_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=None,
                    event_pos=dummy_state_pos,
                    membership="leave",
                    sender=None,  # user1_id,
                    prev_event_id=join_response1["event_id"],
                    prev_event_pos=join_pos1,
                    prev_membership="join",
                    prev_sender=user1_id,
                ),
            ],
        )

    def test_excluded_room_ids(self) -> None:
        """
        Test that the `excluded_room_ids` option excludes changes from the specified rooms.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )

        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok)
        join_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)
        join_pos2 = self.get_success(
            self.store.get_position_for_event(join_response2["event_id"])
        )

        after_room1_token = self.event_sources.get_current_token()

        # First test the the room is returned without the `excluded_room_ids` option
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_response1["event_id"],
                    event_pos=join_pos1,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
                CurrentStateDeltaMembership(
                    room_id=room_id2,
                    event_id=join_response2["event_id"],
                    event_pos=join_pos2,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
            ],
        )

        # The test that `excluded_room_ids` excludes room2 as expected
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_room1_token.room_key,
                to_key=after_room1_token.room_key,
                excluded_room_ids=[room_id2],
            )
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=room_id1,
                    event_id=join_response1["event_id"],
                    event_pos=join_pos1,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                )
            ],
        )


class GetCurrentStateDeltaMembershipChangesForUserFederationTestCase(
    FederatingHomeserverTestCase
):
    """
    Test `get_current_state_delta_membership_changes_for_user(...)` when joining remote federated rooms.
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()
        self.room_member_handler = hs.get_room_member_handler()

    def test_remote_join(self) -> None:
        """
        Test remote join where the first rows in `current_state_delta_stream` will just
        be the state when you joined the remote room.
        """
        user1_id = self.register_user("user1", "pass")
        _user1_tok = self.login(user1_id, "pass")

        before_join_token = self.event_sources.get_current_token()

        intially_unjoined_room_id = f"!example:{self.OTHER_SERVER_NAME}"

        # Remotely join a room on another homeserver.
        #
        # To do this we have to mock the responses from the remote homeserver. We also
        # patch out a bunch of event checks on our end.
        create_event_source = {
            "auth_events": [],
            "content": {
                "creator": f"@creator:{self.OTHER_SERVER_NAME}",
                "room_version": self.hs.config.server.default_room_version.identifier,
            },
            "depth": 0,
            "origin_server_ts": 0,
            "prev_events": [],
            "room_id": intially_unjoined_room_id,
            "sender": f"@creator:{self.OTHER_SERVER_NAME}",
            "state_key": "",
            "type": EventTypes.Create,
        }
        self.add_hashes_and_signatures_from_other_server(
            create_event_source,
            self.hs.config.server.default_room_version,
        )
        create_event = FrozenEventV3(
            create_event_source,
            self.hs.config.server.default_room_version,
            {},
            None,
        )
        creator_join_event_source = {
            "auth_events": [create_event.event_id],
            "content": {
                "membership": "join",
            },
            "depth": 1,
            "origin_server_ts": 1,
            "prev_events": [],
            "room_id": intially_unjoined_room_id,
            "sender": f"@creator:{self.OTHER_SERVER_NAME}",
            "state_key": f"@creator:{self.OTHER_SERVER_NAME}",
            "type": EventTypes.Member,
        }
        self.add_hashes_and_signatures_from_other_server(
            creator_join_event_source,
            self.hs.config.server.default_room_version,
        )
        creator_join_event = FrozenEventV3(
            creator_join_event_source,
            self.hs.config.server.default_room_version,
            {},
            None,
        )

        # Our local user is going to remote join the room
        join_event_source = {
            "auth_events": [create_event.event_id],
            "content": {"membership": "join"},
            "depth": 1,
            "origin_server_ts": 100,
            "prev_events": [creator_join_event.event_id],
            "sender": user1_id,
            "state_key": user1_id,
            "room_id": intially_unjoined_room_id,
            "type": EventTypes.Member,
        }
        add_hashes_and_signatures(
            self.hs.config.server.default_room_version,
            join_event_source,
            self.hs.hostname,
            self.hs.signing_key,
        )
        join_event = FrozenEventV3(
            join_event_source,
            self.hs.config.server.default_room_version,
            {},
            None,
        )

        mock_make_membership_event = AsyncMock(
            return_value=(
                self.OTHER_SERVER_NAME,
                join_event,
                self.hs.config.server.default_room_version,
            )
        )
        mock_send_join = AsyncMock(
            return_value=SendJoinResult(
                join_event,
                self.OTHER_SERVER_NAME,
                state=[create_event, creator_join_event],
                auth_chain=[create_event, creator_join_event],
                partial_state=False,
                servers_in_room=frozenset(),
            )
        )

        with patch.object(
            self.room_member_handler.federation_handler.federation_client,
            "make_membership_event",
            mock_make_membership_event,
        ), patch.object(
            self.room_member_handler.federation_handler.federation_client,
            "send_join",
            mock_send_join,
        ), patch(
            "synapse.event_auth._is_membership_change_allowed",
            return_value=None,
        ), patch(
            "synapse.handlers.federation_event.check_state_dependent_auth_rules",
            return_value=None,
        ):
            self.get_success(
                self.room_member_handler.update_membership(
                    requester=create_requester(user1_id),
                    target=UserID.from_string(user1_id),
                    room_id=intially_unjoined_room_id,
                    action=Membership.JOIN,
                    remote_room_hosts=[self.OTHER_SERVER_NAME],
                )
            )

        after_join_token = self.event_sources.get_current_token()

        # Get the membership changes for the user.
        #
        # At this point, the `current_state_delta_stream` table should look like the
        # following. Notice that all of the events are at the same `stream_id` because
        # the current state starts out where we remotely joined:
        #
        # | stream_id | room_id                      | type            | state_key                    | event_id | prev_event_id |
        # |-----------|------------------------------|-----------------|------------------------------|----------|---------------|
        # | 2         | '!example:other.example.com' | 'm.room.member' | '@user1:test'                | '$xxx'   | None          |
        # | 2         | '!example:other.example.com' | 'm.room.create' | ''                           | '$xxx'   | None          |
        # | 2         | '!example:other.example.com' | 'm.room.member' | '@creator:other.example.com' | '$xxx'   | None          |
        membership_changes = self.get_success(
            self.store.get_current_state_delta_membership_changes_for_user(
                user1_id,
                from_key=before_join_token.room_key,
                to_key=after_join_token.room_key,
            )
        )

        join_pos = self.get_success(
            self.store.get_position_for_event(join_event.event_id)
        )

        # Let the whole diff show on failure
        self.maxDiff = None
        self.assertEqual(
            membership_changes,
            [
                CurrentStateDeltaMembership(
                    room_id=intially_unjoined_room_id,
                    event_id=join_event.event_id,
                    event_pos=join_pos,
                    membership="join",
                    sender=user1_id,
                    prev_event_id=None,
                    prev_event_pos=None,
                    prev_membership=None,
                    prev_sender=None,
                ),
            ],
        )
