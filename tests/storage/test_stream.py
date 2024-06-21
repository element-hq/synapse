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

from immutabledict import immutabledict

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import Direction, EventTypes, RelationTypes
from synapse.api.filtering import Filter
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, PersistedEventPosition, RoomStreamToken
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

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
            self.hs.get_datastores().main.paginate_room_events(
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
