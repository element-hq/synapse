#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import AccountDataTypes, EventTypes, RelationTypes
from synapse.rest import admin
from synapse.rest.client import login, register, relations, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.server import FakeChannel
from tests.test_utils.event_injection import inject_event


class BaseRelationsTestCase(unittest.HomeserverTestCase):
    servlets = [
        relations.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets_for_client_rest_resource,
    ]
    hijack_auth = False

    def default_config(self) -> Dict[str, Any]:
        # We need to enable msc1849 support for aggregations
        config = super().default_config()

        # We enable frozen dicts as relations/edits change event contents, so we
        # want to test that we don't modify the events in the caches.
        config["use_frozen_dicts"] = True

        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.user_id, self.user_token = self._create_user("alice")
        self.user2_id, self.user2_token = self._create_user("bob")

        self.room = self.helper.create_room_as(self.user_id, tok=self.user_token)
        self.helper.join(self.room, user=self.user2_id, tok=self.user2_token)
        res = self.helper.send(self.room, body="Hi!", tok=self.user_token)
        self.parent_id = res["event_id"]

    def _create_user(self, localpart: str) -> Tuple[str, str]:
        user_id = self.register_user(localpart, "abc123")
        access_token = self.login(localpart, "abc123")

        return user_id, access_token

    def _send_relation(
        self,
        relation_type: str,
        event_type: str,
        key: Optional[str] = None,
        content: Optional[dict] = None,
        access_token: Optional[str] = None,
        parent_id: Optional[str] = None,
        expected_response_code: int = 200,
    ) -> FakeChannel:
        """Helper function to send a relation pointing at `self.parent_id`

        Args:
            relation_type: One of `RelationTypes`
            event_type: The type of the event to create
            key: The aggregation key used for m.annotation relation type.
            content: The content of the created event. Will be modified to configure
                the m.relates_to key based on the other provided parameters.
            access_token: The access token used to send the relation, defaults
                to `self.user_token`
            parent_id: The event_id this relation relates to. If None, then self.parent_id

        Returns:
            FakeChannel
        """
        if not access_token:
            access_token = self.user_token

        original_id = parent_id if parent_id else self.parent_id

        if content is None:
            content = {}
        content["m.relates_to"] = {
            "event_id": original_id,
            "rel_type": relation_type,
        }
        if key is not None:
            content["m.relates_to"]["key"] = key

        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{self.room}/send/{event_type}",
            content,
            access_token=access_token,
        )
        self.assertEqual(expected_response_code, channel.code, channel.json_body)
        return channel

    def _get_related_events(self) -> List[str]:
        """
        Requests /relations on the parent ID and returns a list of event IDs.
        """
        # Request the relations of the event.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        return [ev["event_id"] for ev in channel.json_body["chunk"]]

    def _get_bundled_aggregations(self) -> JsonDict:
        """
        Requests /event on the parent ID and returns the m.relations field (from unsigned), if it exists.
        """
        # Fetch the bundled aggregations of the event.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/rooms/{self.room}/event/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        return channel.json_body["unsigned"].get("m.relations", {})

    def _find_event_in_chunk(self, events: List[JsonDict]) -> JsonDict:
        """
        Find the parent event in a chunk of events and assert that it has the proper bundled aggregations.
        """
        for event in events:
            if event["event_id"] == self.parent_id:
                return event

        raise AssertionError(f"Event {self.parent_id} not found in chunk")


class RelationsTestCase(BaseRelationsTestCase):
    def test_send_relation(self) -> None:
        """Tests that sending a relation works."""
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", key="👍")
        event_id = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{event_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        self.assert_dict(
            {
                "type": "m.reaction",
                "sender": self.user_id,
                "content": {
                    "m.relates_to": {
                        "event_id": self.parent_id,
                        "key": "👍",
                        "rel_type": RelationTypes.ANNOTATION,
                    }
                },
            },
            channel.json_body,
        )

    def test_deny_invalid_event(self) -> None:
        """Test that we deny relations on non-existant events"""
        self._send_relation(
            RelationTypes.ANNOTATION,
            EventTypes.Message,
            parent_id="foo",
            content={"body": "foo", "msgtype": "m.text"},
            expected_response_code=400,
        )

        # Unless that event is referenced from another event!
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_insert(
                table="event_relations",
                values={
                    "event_id": "bar",
                    "relates_to_id": "foo",
                    "relation_type": RelationTypes.THREAD,
                },
                desc="test_deny_invalid_event",
            )
        )
        self._send_relation(
            RelationTypes.THREAD,
            EventTypes.Message,
            parent_id="foo",
            content={"body": "foo", "msgtype": "m.text"},
        )

    def test_deny_invalid_room(self) -> None:
        """Test that we deny relations on non-existant events"""
        # Create another room and send a message in it.
        room2 = self.helper.create_room_as(self.user_id, tok=self.user_token)
        res = self.helper.send(room2, body="Hi!", tok=self.user_token)
        parent_id = res["event_id"]

        # Attempt to send an annotation to that event.
        self._send_relation(
            RelationTypes.ANNOTATION,
            "m.reaction",
            parent_id=parent_id,
            key="A",
            expected_response_code=400,
        )

    def test_deny_double_react(self) -> None:
        """Test that we deny relations on membership events"""
        self._send_relation(RelationTypes.ANNOTATION, "m.reaction", key="a")
        self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "a", expected_response_code=400
        )

    def test_deny_forked_thread(self) -> None:
        """It is invalid to start a thread off a thread."""
        channel = self._send_relation(
            RelationTypes.THREAD,
            "m.room.message",
            content={"msgtype": "m.text", "body": "foo"},
            parent_id=self.parent_id,
        )
        parent_id = channel.json_body["event_id"]

        self._send_relation(
            RelationTypes.THREAD,
            "m.room.message",
            content={"msgtype": "m.text", "body": "foo"},
            parent_id=parent_id,
            expected_response_code=400,
        )

    def test_ignore_invalid_room(self) -> None:
        """Test that we ignore invalid relations over federation."""
        # Create another room and send a message in it.
        room2 = self.helper.create_room_as(self.user_id, tok=self.user_token)
        res = self.helper.send(room2, body="Hi!", tok=self.user_token)
        parent_id = res["event_id"]

        # Disable the validation to pretend this came over federation.
        with patch(
            "synapse.handlers.message.EventCreationHandler._validate_event_relation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Generate a various relations from a different room.
            self.get_success(
                inject_event(
                    self.hs,
                    room_id=self.room,
                    type="m.reaction",
                    sender=self.user_id,
                    content={
                        "m.relates_to": {
                            "rel_type": RelationTypes.ANNOTATION,
                            "event_id": parent_id,
                            "key": "A",
                        }
                    },
                )
            )

            self.get_success(
                inject_event(
                    self.hs,
                    room_id=self.room,
                    type="m.room.message",
                    sender=self.user_id,
                    content={
                        "body": "foo",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": RelationTypes.REFERENCE,
                            "event_id": parent_id,
                        },
                    },
                )
            )

            self.get_success(
                inject_event(
                    self.hs,
                    room_id=self.room,
                    type="m.room.message",
                    sender=self.user_id,
                    content={
                        "body": "foo",
                        "msgtype": "m.text",
                        "m.relates_to": {
                            "rel_type": RelationTypes.THREAD,
                            "event_id": parent_id,
                        },
                    },
                )
            )

            self.get_success(
                inject_event(
                    self.hs,
                    room_id=self.room,
                    type="m.room.message",
                    sender=self.user_id,
                    content={
                        "body": "foo",
                        "msgtype": "m.text",
                        "new_content": {
                            "body": "new content",
                            "msgtype": "m.text",
                        },
                        "m.relates_to": {
                            "rel_type": RelationTypes.REPLACE,
                            "event_id": parent_id,
                        },
                    },
                )
            )

        # They should be ignored when fetching relations.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{room2}/relations/{parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(channel.json_body["chunk"], [])

        # And for bundled aggregations.
        channel = self.make_request(
            "GET",
            f"/rooms/{room2}/event/{parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertNotIn("m.relations", channel.json_body["unsigned"])

    def _assert_edit_bundle(
        self, event_json: JsonDict, edit_event_id: str, edit_event_content: JsonDict
    ) -> None:
        """
        Assert that the given event has a correctly-serialised edit event in its
        bundled aggregations

        Args:
            event_json: the serialised event to be checked
            edit_event_id: the ID of the edit event that we expect to be bundled
            edit_event_content: the content of that event, excluding the 'm.relates_to`
               property
        """
        relations_dict = event_json["unsigned"].get("m.relations")
        self.assertIn(RelationTypes.REPLACE, relations_dict)

        m_replace_dict = relations_dict[RelationTypes.REPLACE]
        for key in [
            "event_id",
            "sender",
            "origin_server_ts",
            "content",
            "type",
            "unsigned",
        ]:
            self.assertIn(key, m_replace_dict)

        expected_edit_content = {
            "m.relates_to": {
                "event_id": event_json["event_id"],
                "rel_type": "m.replace",
            }
        }
        expected_edit_content.update(edit_event_content)

        self.assert_dict(
            {
                "event_id": edit_event_id,
                "sender": self.user_id,
                "content": expected_edit_content,
                "type": "m.room.message",
            },
            m_replace_dict,
        )

    def test_edit(self) -> None:
        """Test that a simple edit works."""
        orig_body = {"body": "Hi!", "msgtype": "m.text"}
        new_body = {"msgtype": "m.text", "body": "I've been edited!"}
        edit_event_content = {
            "msgtype": "m.text",
            "body": "foo",
            "m.new_content": new_body,
        }
        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content=edit_event_content,
        )
        edit_event_id = channel.json_body["event_id"]

        # /event should return the *original* event
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(channel.json_body["content"], orig_body)
        self._assert_edit_bundle(channel.json_body, edit_event_id, edit_event_content)

        # Request the room messages.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/messages?dir=b",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self._assert_edit_bundle(
            self._find_event_in_chunk(channel.json_body["chunk"]),
            edit_event_id,
            edit_event_content,
        )

        # Request the room context.
        # /context should return the event.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/context/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self._assert_edit_bundle(
            channel.json_body["event"], edit_event_id, edit_event_content
        )
        self.assertEqual(channel.json_body["event"]["content"], orig_body)

        # Request sync, but limit the timeline so it becomes limited (and includes
        # bundled aggregations).
        filter = urllib.parse.quote_plus(b'{"room": {"timeline": {"limit": 2}}}')
        channel = self.make_request(
            "GET", f"/sync?filter={filter}", access_token=self.user_token
        )
        self.assertEqual(200, channel.code, channel.json_body)
        room_timeline = channel.json_body["rooms"]["join"][self.room]["timeline"]
        self.assertTrue(room_timeline["limited"])
        self._assert_edit_bundle(
            self._find_event_in_chunk(room_timeline["events"]),
            edit_event_id,
            edit_event_content,
        )

        # Request search.
        channel = self.make_request(
            "POST",
            "/search",
            # Search term matches the parent message.
            content={"search_categories": {"room_events": {"search_term": "Hi"}}},
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        chunk = [
            result["result"]
            for result in channel.json_body["search_categories"]["room_events"][
                "results"
            ]
        ]
        self._assert_edit_bundle(
            self._find_event_in_chunk(chunk),
            edit_event_id,
            edit_event_content,
        )

    def test_multi_edit(self) -> None:
        """Test that multiple edits, including attempts by people who
        shouldn't be allowed, are correctly handled.
        """
        orig_body = orig_body = {"body": "Hi!", "msgtype": "m.text"}
        self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Wibble",
                "m.new_content": {"msgtype": "m.text", "body": "First edit"},
            },
        )

        new_body = {"msgtype": "m.text", "body": "I've been edited!"}
        edit_event_content = {
            "msgtype": "m.text",
            "body": "foo",
            "m.new_content": new_body,
        }
        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content=edit_event_content,
        )
        edit_event_id = channel.json_body["event_id"]

        self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message.WRONG_TYPE",
            content={
                "msgtype": "m.text",
                "body": "Wibble",
                "m.new_content": {"msgtype": "m.text", "body": "Edit, but wrong type"},
            },
        )

        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/context/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        self.assertEqual(channel.json_body["event"]["content"], orig_body)
        self._assert_edit_bundle(
            channel.json_body["event"], edit_event_id, edit_event_content
        )

    def test_edit_reply(self) -> None:
        """Test that editing a reply works."""

        # Create a reply to edit.
        original_body = {"msgtype": "m.text", "body": "A reply!"}
        channel = self._send_relation(
            RelationTypes.REFERENCE, "m.room.message", content=original_body
        )
        reply = channel.json_body["event_id"]

        edit_event_content = {
            "msgtype": "m.text",
            "body": "foo",
            "m.new_content": {"msgtype": "m.text", "body": "I've been edited!"},
        }
        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content=edit_event_content,
            parent_id=reply,
        )
        edit_event_id = channel.json_body["event_id"]

        # /event returns the original event
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{reply}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        event_result = channel.json_body
        self.assertLessEqual(original_body.items(), event_result["content"].items())

        # also check /context, which returns the *edited* event
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/context/{reply}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        context_result = channel.json_body["event"]

        # check that the relations are correct for both APIs
        for result_event_dict, desc in (
            (event_result, "/event"),
            (context_result, "/context"),
        ):
            # The reference metadata should still be intact.
            self.assertLessEqual(
                {
                    "m.relates_to": {
                        "event_id": self.parent_id,
                        "rel_type": "m.reference",
                    }
                }.items(),
                result_event_dict["content"].items(),
                desc,
            )

            # We expect that the edit relation appears in the unsigned relations
            # section.
            self._assert_edit_bundle(
                result_event_dict, edit_event_id, edit_event_content
            )

    def test_edit_edit(self) -> None:
        """Test that an edit cannot be edited."""
        orig_body = {"body": "Hi!", "msgtype": "m.text"}
        new_body = {"msgtype": "m.text", "body": "Initial edit"}
        edit_event_content = {
            "msgtype": "m.text",
            "body": "Wibble",
            "m.new_content": new_body,
        }
        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content=edit_event_content,
        )
        edit_event_id = channel.json_body["event_id"]

        # Edit the edit event.
        self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content={
                "msgtype": "m.text",
                "body": "foo",
                "m.new_content": {"msgtype": "m.text", "body": "Ignored edit"},
            },
            parent_id=edit_event_id,
        )

        # Request the original event.
        # /event should return the original event.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(channel.json_body["content"], orig_body)

        # The relations information should not include the edit to the edit.
        self._assert_edit_bundle(channel.json_body, edit_event_id, edit_event_content)

        # /context should return the bundled edit for the *first* edit
        # (The edit to the edit should be ignored.)
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/context/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(channel.json_body["event"]["content"], orig_body)
        self._assert_edit_bundle(
            channel.json_body["event"], edit_event_id, edit_event_content
        )

        # Directly requesting the edit should not have the edit to the edit applied.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{edit_event_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual("Wibble", channel.json_body["content"]["body"])
        self.assertIn("m.new_content", channel.json_body["content"])

        # The relations information should not include the edit to the edit.
        self.assertNotIn("m.relations", channel.json_body["unsigned"])

    def test_unknown_relations(self) -> None:
        """Unknown relations should be accepted."""
        channel = self._send_relation("m.relation.test", "m.room.test")
        event_id = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # We expect to get back a single pagination result, which is the full
        # relation event we sent above.
        self.assertEqual(len(channel.json_body["chunk"]), 1, channel.json_body)
        self.assert_dict(
            {"event_id": event_id, "sender": self.user_id, "type": "m.room.test"},
            channel.json_body["chunk"][0],
        )

        # We also expect to get the original event (the id of which is self.parent_id)
        # when requesting the unstable endpoint.
        self.assertNotIn("original_event", channel.json_body)
        channel = self.make_request(
            "GET",
            f"/_matrix/client/unstable/rooms/{self.room}/relations/{self.parent_id}?limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(
            channel.json_body["original_event"]["event_id"], self.parent_id
        )

        # When bundling the unknown relation is not included.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{self.parent_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertNotIn("m.relations", channel.json_body["unsigned"])

    def test_background_update(self) -> None:
        """Test the event_arbitrary_relations background update."""
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", key="👍")
        annotation_event_id_good = channel.json_body["event_id"]

        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", key="A")
        annotation_event_id_bad = channel.json_body["event_id"]

        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_event_id = channel.json_body["event_id"]

        # Clean-up the table as if the inserts did not happen during event creation.
        self.get_success(
            self.store.db_pool.simple_delete_many(
                table="event_relations",
                column="event_id",
                iterable=(annotation_event_id_bad, thread_event_id),
                keyvalues={},
                desc="RelationsTestCase.test_background_update",
            )
        )

        # Only the "good" annotation should be found.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=10",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(
            [ev["event_id"] for ev in channel.json_body["chunk"]],
            [annotation_event_id_good],
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {"update_name": "event_arbitrary_relations", "progress_json": "{}"},
            )
        )

        # Ugh, have to reset this flag
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # The "good" annotation and the thread should be found, but not the "bad"
        # annotation.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=10",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertCountEqual(
            [ev["event_id"] for ev in channel.json_body["chunk"]],
            [annotation_event_id_good, thread_event_id],
        )


class RelationPaginationTestCase(BaseRelationsTestCase):
    def test_basic_paginate_relations(self) -> None:
        """Tests that calling pagination API correctly the latest relations."""
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", "a")
        first_annotation_id = channel.json_body["event_id"]

        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", "b")
        second_annotation_id = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # We expect to get back a single pagination result, which is the latest
        # full relation event we sent above.
        self.assertEqual(len(channel.json_body["chunk"]), 1, channel.json_body)
        self.assert_dict(
            {
                "event_id": second_annotation_id,
                "sender": self.user_id,
                "type": "m.reaction",
            },
            channel.json_body["chunk"][0],
        )

        # Make sure next_batch has something in it that looks like it could be a
        # valid token.
        self.assertIsInstance(
            channel.json_body.get("next_batch"), str, channel.json_body
        )

        # Request the relations again, but with a different direction.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations"
            f"/{self.parent_id}?limit=1&dir=f",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # We expect to get back a single pagination result, which is the earliest
        # full relation event we sent above.
        self.assertEqual(len(channel.json_body["chunk"]), 1, channel.json_body)
        self.assert_dict(
            {
                "event_id": first_annotation_id,
                "sender": self.user_id,
                "type": "m.reaction",
            },
            channel.json_body["chunk"][0],
        )

    def test_repeated_paginate_relations(self) -> None:
        """Test that if we paginate using a limit and tokens then we get the
        expected events.
        """

        expected_event_ids = []
        for idx in range(10):
            channel = self._send_relation(
                RelationTypes.ANNOTATION, "m.reaction", chr(ord("a") + idx)
            )
            expected_event_ids.append(channel.json_body["event_id"])

        prev_token: Optional[str] = ""
        found_event_ids: List[str] = []
        for _ in range(20):
            from_token = ""
            if prev_token:
                from_token = "&from=" + prev_token

            channel = self.make_request(
                "GET",
                f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=3{from_token}",
                access_token=self.user_token,
            )
            self.assertEqual(200, channel.code, channel.json_body)

            found_event_ids.extend(e["event_id"] for e in channel.json_body["chunk"])
            next_batch = channel.json_body.get("next_batch")

            self.assertNotEqual(prev_token, next_batch)
            prev_token = next_batch

            if not prev_token:
                break

        # We paginated backwards, so reverse
        found_event_ids.reverse()
        self.assertEqual(found_event_ids, expected_event_ids)

        # Test forward pagination.
        prev_token = ""
        found_event_ids = []
        for _ in range(20):
            from_token = ""
            if prev_token:
                from_token = "&from=" + prev_token

            channel = self.make_request(
                "GET",
                f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?dir=f&limit=3{from_token}",
                access_token=self.user_token,
            )
            self.assertEqual(200, channel.code, channel.json_body)

            found_event_ids.extend(e["event_id"] for e in channel.json_body["chunk"])
            next_batch = channel.json_body.get("next_batch")

            self.assertNotEqual(prev_token, next_batch)
            prev_token = next_batch

            if not prev_token:
                break

        self.assertEqual(found_event_ids, expected_event_ids)

    def test_pagination_from_sync_and_messages(self) -> None:
        """Pagination tokens from /sync and /messages can be used to paginate /relations."""
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", "A")
        annotation_id = channel.json_body["event_id"]
        # Send an event after the relation events.
        self.helper.send(self.room, body="Latest event", tok=self.user_token)

        # Request /sync, limiting it such that only the latest event is returned
        # (and not the relation).
        filter = urllib.parse.quote_plus(b'{"room": {"timeline": {"limit": 1}}}')
        channel = self.make_request(
            "GET", f"/sync?filter={filter}", access_token=self.user_token
        )
        self.assertEqual(200, channel.code, channel.json_body)
        room_timeline = channel.json_body["rooms"]["join"][self.room]["timeline"]
        sync_prev_batch = room_timeline["prev_batch"]
        self.assertIsNotNone(sync_prev_batch)
        # Ensure the relation event is not in the batch returned from /sync.
        self.assertNotIn(
            annotation_id, [ev["event_id"] for ev in room_timeline["events"]]
        )

        # Request /messages, limiting it such that only the latest event is
        # returned (and not the relation).
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/messages?dir=b&limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        messages_end = channel.json_body["end"]
        self.assertIsNotNone(messages_end)
        # Ensure the relation event is not in the chunk returned from /messages.
        self.assertNotIn(
            annotation_id, [ev["event_id"] for ev in channel.json_body["chunk"]]
        )

        # Request /relations with the pagination tokens received from both the
        # /sync and /messages responses above, in turn.
        #
        # This is a tiny bit silly since the client wouldn't know the parent ID
        # from the requests above; consider the parent ID to be known from a
        # previous /sync.
        for from_token in (sync_prev_batch, messages_end):
            channel = self.make_request(
                "GET",
                f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?from={from_token}",
                access_token=self.user_token,
            )
            self.assertEqual(200, channel.code, channel.json_body)

            # The relation should be in the returned chunk.
            self.assertIn(
                annotation_id, [ev["event_id"] for ev in channel.json_body["chunk"]]
            )


class RecursiveRelationTestCase(BaseRelationsTestCase):
    def test_recursive_relations(self) -> None:
        """Generate a complex, multi-level relationship tree and query it."""
        # Create a thread with a few messages in it.
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_1 = channel.json_body["event_id"]

        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_2 = channel.json_body["event_id"]

        # Add annotations.
        channel = self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "a", parent_id=thread_2
        )
        annotation_1 = channel.json_body["event_id"]

        channel = self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "b", parent_id=thread_1
        )
        annotation_2 = channel.json_body["event_id"]

        # Add a reference to part of the thread, then edit the reference and annotate it.
        channel = self._send_relation(
            RelationTypes.REFERENCE, "m.room.test", parent_id=thread_2
        )
        reference_1 = channel.json_body["event_id"]

        channel = self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "c", parent_id=reference_1
        )
        annotation_3 = channel.json_body["event_id"]

        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.test",
            parent_id=reference_1,
        )
        edit = channel.json_body["event_id"]

        # Also more events off the root.
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", "d")
        annotation_4 = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}"
            "?dir=f&limit=20&recurse=true",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # The above events should be returned in creation order.
        event_ids = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(
            event_ids,
            [
                thread_1,
                thread_2,
                annotation_1,
                annotation_2,
                reference_1,
                annotation_3,
                edit,
                annotation_4,
            ],
        )

    def test_recursive_relations_with_filter(self) -> None:
        """The event_type and rel_type still apply."""
        # Create a thread with a few messages in it.
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_1 = channel.json_body["event_id"]

        # Add annotations.
        channel = self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "b", parent_id=thread_1
        )
        annotation_1 = channel.json_body["event_id"]

        # Add a reference to part of the thread, then edit the reference and annotate it.
        channel = self._send_relation(
            RelationTypes.REFERENCE, "m.room.test", parent_id=thread_1
        )
        reference_1 = channel.json_body["event_id"]

        channel = self._send_relation(
            RelationTypes.ANNOTATION, "org.matrix.reaction", "c", parent_id=reference_1
        )
        annotation_2 = channel.json_body["event_id"]

        # Fetch only annotations, but recursively.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}/{RelationTypes.ANNOTATION}"
            "?dir=f&limit=20&recurse=true",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # The above events should be returned in creation order.
        event_ids = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(event_ids, [annotation_1, annotation_2])

        # Fetch only m.reactions, but recursively.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}/{RelationTypes.ANNOTATION}/m.reaction"
            "?dir=f&limit=20&recurse=true",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # The above events should be returned in creation order.
        event_ids = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(event_ids, [annotation_1])


class BundledAggregationsTestCase(BaseRelationsTestCase):
    """
    See RelationsTestCase.test_edit for a similar test for edits.

    Note that this doesn't test against /relations since only thread relations
    get bundled via that API. See test_aggregation_get_event_for_thread.
    """

    def _test_bundled_aggregations(
        self,
        relation_type: str,
        assertion_callable: Callable[[JsonDict], None],
        expected_db_txn_for_event: int,
        access_token: Optional[str] = None,
    ) -> None:
        """
        Makes requests to various endpoints which should include bundled aggregations
        and then calls an assertion function on the bundled aggregations.

        Args:
            relation_type: The field to search for in the `m.relations` field in unsigned.
            assertion_callable: Called with the contents of unsigned["m.relations"][relation_type]
                for relation-specific assertions.
            expected_db_txn_for_event: The number of database transactions which
                are expected for a call to /event/.
            access_token: The access token to user, defaults to self.user_token.
        """
        access_token = access_token or self.user_token

        def assert_bundle(event_json: JsonDict) -> None:
            """Assert the expected values of the bundled aggregations."""
            relations_dict = event_json["unsigned"].get("m.relations")

            # Ensure the fields are as expected.
            self.assertCountEqual(relations_dict.keys(), (relation_type,))
            assertion_callable(relations_dict[relation_type])

        # Request the event directly.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{self.parent_id}",
            access_token=access_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        assert_bundle(channel.json_body)
        assert channel.resource_usage is not None
        self.assertEqual(channel.resource_usage.db_txn_count, expected_db_txn_for_event)

        # Request the room messages.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/messages?dir=b",
            access_token=access_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        assert_bundle(self._find_event_in_chunk(channel.json_body["chunk"]))

        # Request the room context.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/context/{self.parent_id}",
            access_token=access_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        assert_bundle(channel.json_body["event"])

        # Request sync.
        filter = urllib.parse.quote_plus(b'{"room": {"timeline": {"limit": 4}}}')
        channel = self.make_request(
            "GET", f"/sync?filter={filter}", access_token=access_token
        )
        self.assertEqual(200, channel.code, channel.json_body)
        room_timeline = channel.json_body["rooms"]["join"][self.room]["timeline"]
        self.assertTrue(room_timeline["limited"])
        assert_bundle(self._find_event_in_chunk(room_timeline["events"]))

        # Request search.
        channel = self.make_request(
            "POST",
            "/search",
            # Search term matches the parent message.
            content={"search_categories": {"room_events": {"search_term": "Hi"}}},
            access_token=access_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        chunk = [
            result["result"]
            for result in channel.json_body["search_categories"]["room_events"][
                "results"
            ]
        ]
        assert_bundle(self._find_event_in_chunk(chunk))

    def test_reference(self) -> None:
        """
        Test that references get correctly bundled.
        """
        channel = self._send_relation(RelationTypes.REFERENCE, "m.room.test")
        reply_1 = channel.json_body["event_id"]

        channel = self._send_relation(RelationTypes.REFERENCE, "m.room.test")
        reply_2 = channel.json_body["event_id"]

        def assert_annotations(bundled_aggregations: JsonDict) -> None:
            self.assertEqual(
                {"chunk": [{"event_id": reply_1}, {"event_id": reply_2}]},
                bundled_aggregations,
            )

        self._test_bundled_aggregations(RelationTypes.REFERENCE, assert_annotations, 6)

    def test_thread(self) -> None:
        """
        Test that threads get correctly bundled.
        """
        # The root message is from "user", send replies as "user2".
        self._send_relation(
            RelationTypes.THREAD, "m.room.test", access_token=self.user2_token
        )
        channel = self._send_relation(
            RelationTypes.THREAD, "m.room.test", access_token=self.user2_token
        )
        thread_2 = channel.json_body["event_id"]

        # This needs two assertion functions which are identical except for whether
        # the current_user_participated flag is True, create a factory for the
        # two versions.
        def _gen_assert(participated: bool) -> Callable[[JsonDict], None]:
            def assert_thread(bundled_aggregations: JsonDict) -> None:
                self.assertEqual(2, bundled_aggregations.get("count"))
                self.assertEqual(
                    participated, bundled_aggregations.get("current_user_participated")
                )
                # The latest thread event has some fields that don't matter.
                self.assertIn("latest_event", bundled_aggregations)
                self.assert_dict(
                    {
                        "content": {
                            "m.relates_to": {
                                "event_id": self.parent_id,
                                "rel_type": RelationTypes.THREAD,
                            }
                        },
                        "event_id": thread_2,
                        "sender": self.user2_id,
                        "type": "m.room.test",
                    },
                    bundled_aggregations["latest_event"],
                )

            return assert_thread

        # The "user" sent the root event and is making queries for the bundled
        # aggregations: they have participated.
        self._test_bundled_aggregations(RelationTypes.THREAD, _gen_assert(True), 6)
        # The "user2" sent replies in the thread and is making queries for the
        # bundled aggregations: they have participated.
        #
        # Note that this re-uses some cached values, so the total number of
        # queries is much smaller.
        self._test_bundled_aggregations(
            RelationTypes.THREAD, _gen_assert(True), 3, access_token=self.user2_token
        )

        # A user with no interactions with the thread: they have not participated.
        user3_id, user3_token = self._create_user("charlie")
        self.helper.join(self.room, user=user3_id, tok=user3_token)
        self._test_bundled_aggregations(
            RelationTypes.THREAD, _gen_assert(False), 3, access_token=user3_token
        )

    def test_thread_with_bundled_aggregations_for_latest(self) -> None:
        """
        Bundled aggregations should get applied to the latest thread event.
        """
        self._send_relation(RelationTypes.THREAD, "m.room.test")
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_2 = channel.json_body["event_id"]

        channel = self._send_relation(
            RelationTypes.REFERENCE, "org.matrix.test", parent_id=thread_2
        )
        reference_event_id = channel.json_body["event_id"]

        def assert_thread(bundled_aggregations: JsonDict) -> None:
            self.assertEqual(2, bundled_aggregations.get("count"))
            self.assertTrue(bundled_aggregations.get("current_user_participated"))
            # The latest thread event has some fields that don't matter.
            self.assertIn("latest_event", bundled_aggregations)
            self.assert_dict(
                {
                    "content": {
                        "m.relates_to": {
                            "event_id": self.parent_id,
                            "rel_type": RelationTypes.THREAD,
                        }
                    },
                    "event_id": thread_2,
                    "sender": self.user_id,
                    "type": "m.room.test",
                },
                bundled_aggregations["latest_event"],
            )
            # Check the unsigned field on the latest event.
            self.assert_dict(
                {
                    "m.relations": {
                        RelationTypes.REFERENCE: {
                            "chunk": [{"event_id": reference_event_id}]
                        },
                    }
                },
                bundled_aggregations["latest_event"].get("unsigned"),
            )

        self._test_bundled_aggregations(RelationTypes.THREAD, assert_thread, 6)

    def test_nested_thread(self) -> None:
        """
        Ensure that a nested thread gets ignored by bundled aggregations, as
        those are forbidden.
        """

        # Start a thread.
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        reply_event_id = channel.json_body["event_id"]

        # Disable the validation to pretend this came over federation, since it is
        # not an event the Client-Server API will allow..
        with patch(
            "synapse.handlers.message.EventCreationHandler._validate_event_relation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Create a sub-thread off the thread, which is not allowed.
            self._send_relation(
                RelationTypes.THREAD, "m.room.test", parent_id=reply_event_id
            )

        # Fetch the thread root, to get the bundled aggregation for the thread.
        relations_from_event = self._get_bundled_aggregations()

        # Ensure that requesting the room messages also does not return the sub-thread.
        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/messages?dir=b",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        event = self._find_event_in_chunk(channel.json_body["chunk"])
        relations_from_messages = event["unsigned"]["m.relations"]

        # Check the bundled aggregations from each point.
        for aggregations, desc in (
            (relations_from_event, "/event"),
            (relations_from_messages, "/messages"),
        ):
            # The latest event should have bundled aggregations.
            self.assertIn(RelationTypes.THREAD, aggregations, desc)
            thread_summary = aggregations[RelationTypes.THREAD]
            self.assertIn("latest_event", thread_summary, desc)
            self.assertEqual(
                thread_summary["latest_event"]["event_id"], reply_event_id, desc
            )

            # The latest event should not have any bundled aggregations (since the
            # only relation to it is another thread, which is invalid).
            self.assertNotIn(
                "m.relations", thread_summary["latest_event"]["unsigned"], desc
            )

    def test_thread_edit_latest_event(self) -> None:
        """Test that editing the latest event in a thread works."""

        # Create a thread and edit the last event.
        channel = self._send_relation(
            RelationTypes.THREAD,
            "m.room.message",
            content={"msgtype": "m.text", "body": "A threaded reply!"},
        )
        threaded_event_id = channel.json_body["event_id"]

        new_body = {"msgtype": "m.text", "body": "I've been edited!"}
        channel = self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            content={"msgtype": "m.text", "body": "foo", "m.new_content": new_body},
            parent_id=threaded_event_id,
        )
        edit_event_id = channel.json_body["event_id"]

        # Fetch the thread root, to get the bundled aggregation for the thread.
        relations_dict = self._get_bundled_aggregations()

        # We expect that the edit message appears in the thread summary in the
        # unsigned relations section.
        self.assertIn(RelationTypes.THREAD, relations_dict)

        thread_summary = relations_dict[RelationTypes.THREAD]
        self.assertIn("latest_event", thread_summary)
        latest_event_in_thread = thread_summary["latest_event"]
        # The latest event in the thread should have the edit appear under the
        # bundled aggregations.
        self.assertLessEqual(
            {"event_id": edit_event_id, "sender": "@alice:test"}.items(),
            latest_event_in_thread["unsigned"]["m.relations"][
                RelationTypes.REPLACE
            ].items(),
        )

    def test_aggregation_get_event_for_annotation(self) -> None:
        """Test that annotations do not get bundled aggregations included
        when directly requested.
        """
        channel = self._send_relation(RelationTypes.ANNOTATION, "m.reaction", "a")
        annotation_id = channel.json_body["event_id"]

        # Annotate the annotation.
        self._send_relation(
            RelationTypes.ANNOTATION, "m.reaction", "a", parent_id=annotation_id
        )

        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{annotation_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertIsNone(channel.json_body["unsigned"].get("m.relations"))

    def test_aggregation_get_event_for_thread(self) -> None:
        """Test that threads get bundled aggregations included when directly requested."""
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        thread_id = channel.json_body["event_id"]

        # Make a reference to the thread.
        channel = self._send_relation(
            RelationTypes.REFERENCE, "org.matrix.test", parent_id=thread_id
        )
        reference_event_id = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/rooms/{self.room}/event/{thread_id}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(
            channel.json_body["unsigned"].get("m.relations"),
            {
                RelationTypes.REFERENCE: {"chunk": [{"event_id": reference_event_id}]},
            },
        )

        # It should also be included when the entire thread is requested.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/relations/{self.parent_id}?limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        self.assertEqual(len(channel.json_body["chunk"]), 1)

        thread_message = channel.json_body["chunk"][0]
        self.assertEqual(
            thread_message["unsigned"].get("m.relations"),
            {
                RelationTypes.REFERENCE: {"chunk": [{"event_id": reference_event_id}]},
            },
        )

    def test_bundled_aggregations_with_filter(self) -> None:
        """
        If "unsigned" is an omitted field (due to filtering), adding the bundled
        aggregations should not break.

        Note that the spec allows for a server to return additional fields beyond
        what is specified.
        """
        channel = self._send_relation(RelationTypes.REFERENCE, "org.matrix.test")
        reference_event_id = channel.json_body["event_id"]

        # Note that the sync filter does not include "unsigned" as a field.
        filter = urllib.parse.quote_plus(
            b'{"event_fields": ["content", "event_id"], "room": {"timeline": {"limit": 3}}}'
        )
        channel = self.make_request(
            "GET", f"/sync?filter={filter}", access_token=self.user_token
        )
        self.assertEqual(200, channel.code, channel.json_body)

        # Ensure the timeline is limited, find the parent event.
        room_timeline = channel.json_body["rooms"]["join"][self.room]["timeline"]
        self.assertTrue(room_timeline["limited"])
        parent_event = self._find_event_in_chunk(room_timeline["events"])

        # Ensure there's bundled aggregations on it.
        self.assertIn("unsigned", parent_event)
        self.assertEqual(
            parent_event["unsigned"].get("m.relations"),
            {
                RelationTypes.REFERENCE: {"chunk": [{"event_id": reference_event_id}]},
            },
        )


class RelationIgnoredUserTestCase(BaseRelationsTestCase):
    """Relations sent from an ignored user should be ignored."""

    def _test_ignored_user(
        self,
        relation_type: str,
        allowed_event_ids: List[str],
        ignored_event_ids: List[str],
    ) -> Tuple[JsonDict, JsonDict]:
        """
        Fetch the relations and ensure they're all there, then ignore user2, and
        repeat.

        Returns:
            A tuple of two JSON dictionaries, each are bundled aggregations, the
            first is from before the user is ignored, and the second is after.
        """
        # Get the relations.
        event_ids = self._get_related_events()
        self.assertCountEqual(event_ids, allowed_event_ids + ignored_event_ids)

        # And the bundled aggregations.
        before_aggregations = self._get_bundled_aggregations()
        self.assertIn(relation_type, before_aggregations)

        # Ignore user2 and re-do the requests.
        self.get_success(
            self.store.add_account_data_for_user(
                self.user_id,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": {self.user2_id: {}}},
            )
        )

        # Get the relations.
        event_ids = self._get_related_events()
        self.assertCountEqual(event_ids, allowed_event_ids)

        # And the bundled aggregations.
        after_aggregations = self._get_bundled_aggregations()
        self.assertIn(relation_type, after_aggregations)

        return before_aggregations[relation_type], after_aggregations[relation_type]

    def test_reference(self) -> None:
        """Aggregations should exclude reference relations from ignored users"""
        channel = self._send_relation(RelationTypes.REFERENCE, "m.room.test")
        allowed_event_ids = [channel.json_body["event_id"]]

        channel = self._send_relation(
            RelationTypes.REFERENCE, "m.room.test", access_token=self.user2_token
        )
        ignored_event_ids = [channel.json_body["event_id"]]

        before_aggregations, after_aggregations = self._test_ignored_user(
            RelationTypes.REFERENCE, allowed_event_ids, ignored_event_ids
        )

        self.assertCountEqual(
            [e["event_id"] for e in before_aggregations["chunk"]],
            allowed_event_ids + ignored_event_ids,
        )

        self.assertCountEqual(
            [e["event_id"] for e in after_aggregations["chunk"]], allowed_event_ids
        )

    def test_thread(self) -> None:
        """Aggregations should exclude thread releations from ignored users"""
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        allowed_event_ids = [channel.json_body["event_id"]]

        channel = self._send_relation(
            RelationTypes.THREAD, "m.room.test", access_token=self.user2_token
        )
        ignored_event_ids = [channel.json_body["event_id"]]

        before_aggregations, after_aggregations = self._test_ignored_user(
            RelationTypes.THREAD, allowed_event_ids, ignored_event_ids
        )

        self.assertEqual(before_aggregations["count"], 2)
        self.assertTrue(before_aggregations["current_user_participated"])
        # The latest thread event has some fields that don't matter.
        self.assertEqual(
            before_aggregations["latest_event"]["event_id"], ignored_event_ids[0]
        )

        self.assertEqual(after_aggregations["count"], 1)
        self.assertTrue(after_aggregations["current_user_participated"])
        # The latest thread event has some fields that don't matter.
        self.assertEqual(
            after_aggregations["latest_event"]["event_id"], allowed_event_ids[0]
        )


class RelationRedactionTestCase(BaseRelationsTestCase):
    """
    Test the behaviour of relations when the parent or child event is redacted.

    The behaviour of each relation type is subtly different which causes the tests
    to be a bit repetitive, they follow a naming scheme of:

        test_redact_(relation|parent)_{relation_type}

    The first bit of "relation" means that the event with the relation defined
    on it (the child event) is to be redacted. A "parent" means that the target
    of the relation (the parent event) is to be redacted.

    The relation_type describes which type of relation is under test (i.e. it is
    related to the value of rel_type in the event content).
    """

    def _redact(self, event_id: str) -> None:
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{self.room}/redact/{event_id}",
            access_token=self.user_token,
            content={},
        )
        self.assertEqual(200, channel.code, channel.json_body)

    def _get_threads(self) -> List[Tuple[str, str]]:
        """Request the threads in the room and returns a list of thread ID and latest event ID."""
        # Request the threads in the room.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        threads = channel.json_body["chunk"]
        return [
            (
                t["event_id"],
                t["unsigned"]["m.relations"][RelationTypes.THREAD]["latest_event"][
                    "event_id"
                ],
            )
            for t in threads
        ]

    def test_redact_relation_thread(self) -> None:
        """
        Test that thread replies are properly handled after the thread reply redacted.

        The redacted event should not be included in bundled aggregations or
        the response to relations.
        """
        # Create a thread with a few events in it.
        thread_replies = []
        for i in range(3):
            channel = self._send_relation(
                RelationTypes.THREAD,
                EventTypes.Message,
                content={"body": f"reply {i}", "msgtype": "m.text"},
            )
            thread_replies.append(channel.json_body["event_id"])

        ##################################################
        # Check the test data is configured as expected. #
        ##################################################
        self.assertEqual(self._get_related_events(), list(reversed(thread_replies)))
        relations = self._get_bundled_aggregations()
        self.assertLessEqual(
            {"count": 3, "current_user_participated": True}.items(),
            relations[RelationTypes.THREAD].items(),
        )
        # The latest event is the last sent event.
        self.assertEqual(
            relations[RelationTypes.THREAD]["latest_event"]["event_id"],
            thread_replies[-1],
        )

        # There should be one thread, the latest event is the event that will be redacted.
        self.assertEqual(self._get_threads(), [(self.parent_id, thread_replies[-1])])

        ##########################
        # Redact the last event. #
        ##########################
        self._redact(thread_replies.pop())

        # The thread should still exist, but the latest event should be updated.
        self.assertEqual(self._get_related_events(), list(reversed(thread_replies)))
        relations = self._get_bundled_aggregations()
        self.assertLessEqual(
            {"count": 2, "current_user_participated": True}.items(),
            relations[RelationTypes.THREAD].items(),
        )
        # And the latest event is the last unredacted event.
        self.assertEqual(
            relations[RelationTypes.THREAD]["latest_event"]["event_id"],
            thread_replies[-1],
        )
        self.assertEqual(self._get_threads(), [(self.parent_id, thread_replies[-1])])

        ###########################################
        # Redact the *first* event in the thread. #
        ###########################################
        self._redact(thread_replies.pop(0))

        # Nothing should have changed (except the thread count).
        self.assertEqual(self._get_related_events(), thread_replies)
        relations = self._get_bundled_aggregations()
        self.assertLessEqual(
            {"count": 1, "current_user_participated": True}.items(),
            relations[RelationTypes.THREAD].items(),
        )
        # And the latest event is the last unredacted event.
        self.assertEqual(
            relations[RelationTypes.THREAD]["latest_event"]["event_id"],
            thread_replies[-1],
        )
        self.assertEqual(self._get_threads(), [(self.parent_id, thread_replies[-1])])

        ####################################
        # Redact the last remaining event. #
        ####################################
        self._redact(thread_replies.pop(0))
        self.assertEqual(thread_replies, [])

        # The event should no longer be considered a thread.
        self.assertEqual(self._get_related_events(), [])
        self.assertEqual(self._get_bundled_aggregations(), {})
        self.assertEqual(self._get_threads(), [])

    def test_redact_parent_edit(self) -> None:
        """Test that edits of an event are redacted when the original event
        is redacted.
        """
        # Add a relation
        self._send_relation(
            RelationTypes.REPLACE,
            "m.room.message",
            parent_id=self.parent_id,
            content={
                "msgtype": "m.text",
                "body": "Wibble",
                "m.new_content": {"msgtype": "m.text", "body": "First edit"},
            },
        )

        # Check the relation is returned
        event_ids = self._get_related_events()
        relations = self._get_bundled_aggregations()
        self.assertEqual(len(event_ids), 1)
        self.assertIn(RelationTypes.REPLACE, relations)

        # Redact the original event
        self._redact(self.parent_id)

        # The relations are not returned.
        event_ids = self._get_related_events()
        relations = self._get_bundled_aggregations()
        self.assertEqual(len(event_ids), 0)
        self.assertEqual(relations, {})

    def test_redact_parent_annotation(self) -> None:
        """Test that annotations of an event are viewable when the original event
        is redacted.
        """
        # Add a relation
        channel = self._send_relation(RelationTypes.REFERENCE, "org.matrix.test")
        related_event_id = channel.json_body["event_id"]

        # The relations should exist.
        event_ids = self._get_related_events()
        relations = self._get_bundled_aggregations()
        self.assertEqual(len(event_ids), 1)
        self.assertIn(RelationTypes.REFERENCE, relations)

        # Redact the original event.
        self._redact(self.parent_id)

        # The relations are returned.
        event_ids = self._get_related_events()
        relations = self._get_bundled_aggregations()
        self.assertEqual(event_ids, [related_event_id])
        self.assertEqual(
            relations[RelationTypes.REFERENCE],
            {"chunk": [{"event_id": related_event_id}]},
        )

    def test_redact_parent_thread(self) -> None:
        """
        Test that thread replies are still available when the root event is redacted.
        """
        channel = self._send_relation(
            RelationTypes.THREAD,
            EventTypes.Message,
            content={"body": "reply 1", "msgtype": "m.text"},
        )
        related_event_id = channel.json_body["event_id"]

        # Redact one of the reactions.
        self._redact(self.parent_id)

        # The unredacted relation should still exist.
        event_ids = self._get_related_events()
        relations = self._get_bundled_aggregations()
        self.assertEqual(len(event_ids), 1)
        self.assertLessEqual(
            {
                "count": 1,
                "current_user_participated": True,
            }.items(),
            relations[RelationTypes.THREAD].items(),
        )
        self.assertEqual(
            relations[RelationTypes.THREAD]["latest_event"]["event_id"],
            related_event_id,
        )


class ThreadsTestCase(BaseRelationsTestCase):
    def _get_threads(self, body: JsonDict) -> List[Tuple[str, str]]:
        return [
            (
                ev["event_id"],
                ev["unsigned"]["m.relations"]["m.thread"]["latest_event"]["event_id"],
            )
            for ev in body["chunk"]
        ]

    def test_threads(self) -> None:
        """Create threads and ensure the ordering is due to their latest event."""
        # Create 2 threads.
        thread_1 = self.parent_id
        res = self.helper.send(self.room, body="Thread Root!", tok=self.user_token)
        thread_2 = res["event_id"]

        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        reply_1 = channel.json_body["event_id"]
        channel = self._send_relation(
            RelationTypes.THREAD, "m.room.test", parent_id=thread_2
        )
        reply_2 = channel.json_body["event_id"]

        # Request the threads in the room.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        threads = self._get_threads(channel.json_body)
        self.assertEqual(threads, [(thread_2, reply_2), (thread_1, reply_1)])

        # Update the first thread, the ordering should swap.
        channel = self._send_relation(RelationTypes.THREAD, "m.room.test")
        reply_3 = channel.json_body["event_id"]

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        # Tuple of (thread ID, latest event ID) for each thread.
        threads = self._get_threads(channel.json_body)
        self.assertEqual(threads, [(thread_1, reply_3), (thread_2, reply_2)])

    def test_pagination(self) -> None:
        """Create threads and paginate through them."""
        # Create 2 threads.
        thread_1 = self.parent_id
        res = self.helper.send(self.room, body="Thread Root!", tok=self.user_token)
        thread_2 = res["event_id"]

        self._send_relation(RelationTypes.THREAD, "m.room.test")
        self._send_relation(RelationTypes.THREAD, "m.room.test", parent_id=thread_2)

        # Request the threads in the room.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads?limit=1",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        thread_roots = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(thread_roots, [thread_2])

        # Make sure next_batch has something in it that looks like it could be a
        # valid token.
        next_batch = channel.json_body.get("next_batch")
        self.assertIsInstance(next_batch, str, channel.json_body)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads?limit=1&from={next_batch}",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        thread_roots = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(thread_roots, [thread_1], channel.json_body)

        self.assertNotIn("next_batch", channel.json_body, channel.json_body)

    def test_include(self) -> None:
        """Filtering threads to all or participated in should work."""
        # Thread 1 has the user as the root event.
        thread_1 = self.parent_id
        self._send_relation(
            RelationTypes.THREAD, "m.room.test", access_token=self.user2_token
        )

        # Thread 2 has the user replying.
        res = self.helper.send(self.room, body="Thread Root!", tok=self.user2_token)
        thread_2 = res["event_id"]
        self._send_relation(RelationTypes.THREAD, "m.room.test", parent_id=thread_2)

        # Thread 3 has the user not participating in.
        res = self.helper.send(self.room, body="Another thread!", tok=self.user2_token)
        thread_3 = res["event_id"]
        self._send_relation(
            RelationTypes.THREAD,
            "m.room.test",
            access_token=self.user2_token,
            parent_id=thread_3,
        )

        # All threads in the room.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        thread_roots = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(
            thread_roots, [thread_3, thread_2, thread_1], channel.json_body
        )

        # Only participated threads.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads?include=participated",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        thread_roots = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(thread_roots, [thread_2, thread_1], channel.json_body)

    def test_ignored_user(self) -> None:
        """Events from ignored users should be ignored."""
        # Thread 1 has a reply from an ignored user.
        thread_1 = self.parent_id
        self._send_relation(
            RelationTypes.THREAD, "m.room.test", access_token=self.user2_token
        )

        # Thread 2 is created by an ignored user.
        res = self.helper.send(self.room, body="Thread Root!", tok=self.user2_token)
        thread_2 = res["event_id"]
        self._send_relation(RelationTypes.THREAD, "m.room.test", parent_id=thread_2)

        # Ignore user2.
        self.get_success(
            self.store.add_account_data_for_user(
                self.user_id,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": {self.user2_id: {}}},
            )
        )

        # Only thread 1 is returned.
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{self.room}/threads",
            access_token=self.user_token,
        )
        self.assertEqual(200, channel.code, channel.json_body)
        thread_roots = [ev["event_id"] for ev in channel.json_body["chunk"]]
        self.assertEqual(thread_roots, [thread_1], channel.json_body)
