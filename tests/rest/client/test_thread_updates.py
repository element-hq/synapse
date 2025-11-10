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
import logging

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import RelationTypes
from synapse.rest.client import login, relations, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest

logger = logging.getLogger(__name__)


class ThreadUpdatesTestCase(unittest.HomeserverTestCase):
    """
    Test the /thread_updates companion endpoint (MSC4360).
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        relations.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4360_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_no_updates_for_new_user(self) -> None:
        """
        Test that a user with no thread updates gets an empty response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Request thread updates
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            content={"include_roots": True},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Assert empty chunk and no next_batch
        self.assertEqual(channel.json_body["chunk"], {})
        self.assertNotIn("next_batch", channel.json_body)

    def test_single_thread_update(self) -> None:
        """
        Test that a single thread with one reply appears in the response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Add reply to thread
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply 1",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )

        # Request thread updates
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Assert thread is present
        chunk = channel.json_body["chunk"]
        self.assertIn(room_id, chunk)
        self.assertIn(thread_root_id, chunk[room_id])

        # Assert thread root is included
        thread_update = chunk[room_id][thread_root_id]
        self.assertIn("thread_root", thread_update)
        self.assertEqual(thread_update["thread_root"]["event_id"], thread_root_id)

        # Assert prev_batch is NOT present (only 1 update - the reply)
        self.assertNotIn("prev_batch", thread_update)

    def test_multiple_threads_single_room(self) -> None:
        """
        Test that multiple threads in the same room are grouped correctly.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create two threads
        thread1_root_id = self.helper.send(room_id, body="Thread 1", tok=user1_tok)[
            "event_id"
        ]
        thread2_root_id = self.helper.send(room_id, body="Thread 2", tok=user1_tok)[
            "event_id"
        ]

        # Add replies to both threads
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 1",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread1_root_id,
                },
            },
            tok=user1_tok,
        )
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 2",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread2_root_id,
                },
            },
            tok=user1_tok,
        )

        # Request thread updates
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Assert both threads are in the same room
        chunk = channel.json_body["chunk"]
        self.assertIn(room_id, chunk)
        self.assertEqual(len(chunk), 1, "Should only have one room")
        self.assertEqual(len(chunk[room_id]), 2, "Should have two threads")
        self.assertIn(thread1_root_id, chunk[room_id])
        self.assertIn(thread2_root_id, chunk[room_id])

    def test_threads_across_multiple_rooms(self) -> None:
        """
        Test that threads from different rooms are grouped by room_id.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_a_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_b_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create threads in both rooms
        thread_a_root_id = self.helper.send(room_a_id, body="Thread A", tok=user1_tok)[
            "event_id"
        ]
        thread_b_root_id = self.helper.send(room_b_id, body="Thread B", tok=user1_tok)[
            "event_id"
        ]

        # Add replies
        self.helper.send_event(
            room_a_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to A",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_a_root_id,
                },
            },
            tok=user1_tok,
        )
        self.helper.send_event(
            room_b_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to B",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_b_root_id,
                },
            },
            tok=user1_tok,
        )

        # Request thread updates
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Assert both rooms are present with their threads
        chunk = channel.json_body["chunk"]
        self.assertEqual(len(chunk), 2, "Should have two rooms")
        self.assertIn(room_a_id, chunk)
        self.assertIn(room_b_id, chunk)
        self.assertIn(thread_a_root_id, chunk[room_a_id])
        self.assertIn(thread_b_root_id, chunk[room_b_id])

    def test_pagination_with_from_token(self) -> None:
        """
        Test that pagination works using the next_batch token.
        This verifies that multiple calls to /thread_updates return all thread
        updates with no duplicates and no gaps.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create many threads (more than default limit)
        thread_ids = []
        for i in range(5):
            thread_root_id = self.helper.send(
                room_id, body=f"Thread {i}", tok=user1_tok
            )["event_id"]
            thread_ids.append(thread_root_id)

            # Add reply
            self.helper.send_event(
                room_id,
                type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"Reply to thread {i}",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                tok=user1_tok,
            )

        # Request first page with small limit
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=2",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Should have 2 threads and a next_batch token
        first_page_threads = set(channel.json_body["chunk"][room_id].keys())
        self.assertEqual(len(first_page_threads), 2)
        self.assertIn("next_batch", channel.json_body)

        next_batch = channel.json_body["next_batch"]

        # Request second page
        channel = self.make_request(
            "POST",
            f"/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=2&from={next_batch}",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        second_page_threads = set(channel.json_body["chunk"][room_id].keys())
        self.assertEqual(len(second_page_threads), 2)

        # Verify no overlap
        self.assertEqual(
            len(first_page_threads & second_page_threads),
            0,
            "Pages should not have overlapping threads",
        )

        # Request third page to get the remaining thread
        self.assertIn("next_batch", channel.json_body)
        next_batch_2 = channel.json_body["next_batch"]

        channel = self.make_request(
            "POST",
            f"/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=2&from={next_batch_2}",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        third_page_threads = set(channel.json_body["chunk"][room_id].keys())
        self.assertEqual(len(third_page_threads), 1)

        # Verify no overlap between any pages
        self.assertEqual(len(first_page_threads & third_page_threads), 0)
        self.assertEqual(len(second_page_threads & third_page_threads), 0)

        # Verify no gaps - all threads should be accounted for across all pages
        all_threads = set(thread_ids)
        combined_threads = first_page_threads | second_page_threads | third_page_threads
        self.assertEqual(
            combined_threads,
            all_threads,
            "Combined pages should include all thread updates with no gaps",
        )

    def test_invalid_dir_parameter(self) -> None:
        """
        Test that forward pagination (dir=f) is rejected with an error.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Request with forward direction should fail
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=f",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 400)

    def test_invalid_limit_parameter(self) -> None:
        """
        Test that invalid limit values are rejected.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Zero limit should fail
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=0",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 400)

        # Negative limit should fail
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=-5",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 400)

    def test_invalid_pagination_tokens(self) -> None:
        """
        Test that invalid from/to tokens are rejected with appropriate errors.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Invalid from token
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&from=invalid_token",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 400)

        # Invalid to token
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&to=invalid_token",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 400)

    def test_to_token_filtering(self) -> None:
        """
        Test that the to_token parameter correctly limits pagination to updates
        newer than the to_token (since we paginate backwards from newest to oldest).
        This also verifies the to_token boundary is exclusive - updates at exactly
        the to_token position should not be included (as they were already returned
        in a previous response that synced up to that position).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create two thread roots
        thread1_root_id = self.helper.send(room_id, body="Thread 1", tok=user1_tok)[
            "event_id"
        ]
        thread2_root_id = self.helper.send(room_id, body="Thread 2", tok=user1_tok)[
            "event_id"
        ]

        # Send replies to both threads
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 1",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread1_root_id,
                },
            },
            tok=user1_tok,
        )
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 2",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread2_root_id,
                },
            },
            tok=user1_tok,
        )

        # Request with limit=1 to get only the latest thread update
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=1",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200)
        self.assertIn("next_batch", channel.json_body)

        # next_batch points to before the update we just received
        next_batch = channel.json_body["next_batch"]
        first_response_threads = set(channel.json_body["chunk"][room_id].keys())

        # Request again with to=next_batch (lower bound for backward pagination) and no
        # limit.
        # This should get only the same thread updates as before, not the additional
        # update.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&to={next_batch}",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200)

        chunk = channel.json_body["chunk"]
        self.assertIn(room_id, chunk)
        # Should have exactly one thread update
        self.assertEqual(len(chunk[room_id]), 1)

        second_response_threads = set(chunk[room_id].keys())

        # Verify no overlap - the from parameter boundary should be exclusive
        self.assertEqual(
            first_response_threads,
            second_response_threads,
            "to parameter boundary should be exclusive - both responses should be identical",
        )

    def test_bundled_aggregations_on_thread_roots(self) -> None:
        """
        Test that thread root events include bundled aggregations with latest thread event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_id = self.helper.send(room_id, body="Thread root", tok=user1_tok)[
            "event_id"
        ]

        # Send replies to create bundled aggregation data
        for i in range(2):
            self.helper.send_event(
                room_id,
                type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": f"Reply {i + 1}",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                tok=user1_tok,
            )

        # Request thread updates
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200)

        # Check that thread root has bundled aggregations with latest event
        chunk = channel.json_body["chunk"]
        thread_update = chunk[room_id][thread_root_id]
        thread_root_event = thread_update["thread_root"]

        # Should have unsigned data with latest thread event content
        self.assertIn("unsigned", thread_root_event)
        self.assertIn("m.relations", thread_root_event["unsigned"])
        relations = thread_root_event["unsigned"]["m.relations"]
        self.assertIn(RelationTypes.THREAD, relations)

        # Check latest event is present in bundled aggregations
        thread_summary = relations[RelationTypes.THREAD]
        self.assertIn("latest_event", thread_summary)
        latest_event = thread_summary["latest_event"]
        self.assertEqual(latest_event["content"]["body"], "Reply 2")

    def test_only_joined_rooms(self) -> None:
        """
        Test that thread updates only include rooms where the user is currently joined.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create two rooms, user1 joins both
        room1_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        room2_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.join(room2_id, user1_id, tok=user1_tok)

        # Create threads in both rooms
        thread1_root_id = self.helper.send(room1_id, body="Thread 1", tok=user1_tok)[
            "event_id"
        ]
        thread2_root_id = self.helper.send(room2_id, body="Thread 2", tok=user2_tok)[
            "event_id"
        ]

        # Add replies to both threads
        self.helper.send_event(
            room1_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 1",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread1_root_id,
                },
            },
            tok=user1_tok,
        )
        self.helper.send_event(
            room2_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to thread 2",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread2_root_id,
                },
            },
            tok=user2_tok,
        )

        # User1 leaves room2
        self.helper.leave(room2_id, user1_id, tok=user1_tok)

        # Request thread updates for user1 - should only get room1
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={"include_roots": True},
        )
        self.assertEqual(channel.code, 200)

        chunk = channel.json_body["chunk"]
        # Should only have room1, not room2
        self.assertIn(room1_id, chunk)
        self.assertNotIn(room2_id, chunk)
        self.assertIn(thread1_root_id, chunk[room1_id])

    def test_room_filtering_with_lists(self) -> None:
        """
        Test that room filtering works correctly using the lists parameter.
        This verifies that thread updates are only returned for rooms matching
        the provided filters.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create an encrypted room and an unencrypted room
        encrypted_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "initial_state": [
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    }
                ]
            },
        )
        unencrypted_room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create threads in both rooms
        enc_thread_root_id = self.helper.send(
            encrypted_room_id, body="Encrypted thread", tok=user1_tok
        )["event_id"]
        unenc_thread_root_id = self.helper.send(
            unencrypted_room_id, body="Unencrypted thread", tok=user1_tok
        )["event_id"]

        # Add replies to both threads
        self.helper.send_event(
            encrypted_room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply in encrypted room",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": enc_thread_root_id,
                },
            },
            tok=user1_tok,
        )
        self.helper.send_event(
            unencrypted_room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply in unencrypted room",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": unenc_thread_root_id,
                },
            },
            tok=user1_tok,
        )

        # Request thread updates with filter for encrypted rooms only
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={
                "lists": {
                    "encrypted_list": {
                        "ranges": [[0, 99]],
                        "required_state": [["m.room.encryption", ""]],
                        "timeline_limit": 10,
                        "filters": {"is_encrypted": True},
                    }
                }
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        chunk = channel.json_body["chunk"]
        # Should only include the encrypted room
        self.assertIn(encrypted_room_id, chunk)
        self.assertNotIn(unencrypted_room_id, chunk)
        self.assertIn(enc_thread_root_id, chunk[encrypted_room_id])

    def test_room_filtering_with_room_subscriptions(self) -> None:
        """
        Test that room filtering works correctly using the room_subscriptions parameter.
        This verifies that thread updates are only returned for explicitly subscribed rooms.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create three rooms
        room1_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        room2_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        room3_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create threads in all three rooms
        thread1_root_id = self.helper.send(room1_id, body="Thread 1", tok=user1_tok)[
            "event_id"
        ]
        thread2_root_id = self.helper.send(room2_id, body="Thread 2", tok=user1_tok)[
            "event_id"
        ]
        thread3_root_id = self.helper.send(room3_id, body="Thread 3", tok=user1_tok)[
            "event_id"
        ]

        # Add replies to all threads
        for room_id, thread_root_id in [
            (room1_id, thread1_root_id),
            (room2_id, thread2_root_id),
            (room3_id, thread3_root_id),
        ]:
            self.helper.send_event(
                room_id,
                type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": "Reply",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                tok=user1_tok,
            )

        # Request thread updates with subscription to only room1 and room2
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={
                "room_subscriptions": {
                    room1_id: {
                        "required_state": [["m.room.name", ""]],
                        "timeline_limit": 10,
                    },
                    room2_id: {
                        "required_state": [["m.room.name", ""]],
                        "timeline_limit": 10,
                    },
                }
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        chunk = channel.json_body["chunk"]
        # Should only include room1 and room2, not room3
        self.assertIn(room1_id, chunk)
        self.assertIn(room2_id, chunk)
        self.assertNotIn(room3_id, chunk)
        self.assertIn(thread1_root_id, chunk[room1_id])
        self.assertIn(thread2_root_id, chunk[room2_id])

    def test_room_filtering_with_lists_and_room_subscriptions(self) -> None:
        """
        Test that room filtering works correctly when both lists and room_subscriptions
        are provided. The union of rooms from both should be included.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Create an encrypted room and two unencrypted rooms
        encrypted_room_id = self.helper.create_room_as(
            user1_id,
            tok=user1_tok,
            extra_content={
                "initial_state": [
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    }
                ]
            },
        )
        unencrypted_room1_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        unencrypted_room2_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create threads in all three rooms
        enc_thread_root_id = self.helper.send(
            encrypted_room_id, body="Encrypted thread", tok=user1_tok
        )["event_id"]
        unenc1_thread_root_id = self.helper.send(
            unencrypted_room1_id, body="Unencrypted thread 1", tok=user1_tok
        )["event_id"]
        unenc2_thread_root_id = self.helper.send(
            unencrypted_room2_id, body="Unencrypted thread 2", tok=user1_tok
        )["event_id"]

        # Add replies to all threads
        for room_id, thread_root_id in [
            (encrypted_room_id, enc_thread_root_id),
            (unencrypted_room1_id, unenc1_thread_root_id),
            (unencrypted_room2_id, unenc2_thread_root_id),
        ]:
            self.helper.send_event(
                room_id,
                type="m.room.message",
                content={
                    "msgtype": "m.text",
                    "body": "Reply",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                tok=user1_tok,
            )

        # Request thread updates with:
        # - lists: filter for encrypted rooms
        # - room_subscriptions: explicitly subscribe to unencrypted_room1_id
        # Expected: should get both encrypted_room_id (from list) and unencrypted_room1_id
        # (from subscription), but NOT unencrypted_room2_id
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b",
            access_token=user1_tok,
            content={
                "lists": {
                    "encrypted_list": {
                        "ranges": [[0, 99]],
                        "required_state": [["m.room.encryption", ""]],
                        "timeline_limit": 10,
                        "filters": {"is_encrypted": True},
                    }
                },
                "room_subscriptions": {
                    unencrypted_room1_id: {
                        "required_state": [["m.room.name", ""]],
                        "timeline_limit": 10,
                    }
                },
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        chunk = channel.json_body["chunk"]
        # Should include encrypted_room_id (from list filter) and unencrypted_room1_id
        # (from subscription), but not unencrypted_room2_id
        self.assertIn(encrypted_room_id, chunk)
        self.assertIn(unencrypted_room1_id, chunk)
        self.assertNotIn(unencrypted_room2_id, chunk)
        self.assertIn(enc_thread_root_id, chunk[encrypted_room_id])
        self.assertIn(unenc1_thread_root_id, chunk[unencrypted_room1_id])

    def test_threads_not_returned_after_leaving_room(self) -> None:
        """
        Test that thread updates are properly bounded when a user leaves a room.

        Users should see thread updates that occurred up to the point they left,
        but NOT updates that occurred after they left.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create room and both users join
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # Create thread
        res = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root = res["event_id"]

        # Reply in thread while user2 is joined
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply 1 while user2 joined",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root,
                },
            },
            tok=user1_tok,
        )

        # User2 leaves the room
        self.helper.leave(room_id, user2_id, tok=user2_tok)

        # Another reply after user2 left
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply 2 after user2 left",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root,
                },
            },
            tok=user1_tok,
        )

        # User2 gets thread updates with an explicit room subscription
        # (We need to explicitly subscribe to the room to include it after leaving;
        # otherwise only joined rooms are returned)
        channel = self.make_request(
            "POST",
            "/_matrix/client/unstable/io.element.msc4360/thread_updates?dir=b&limit=100",
            {
                "room_subscriptions": {
                    room_id: {
                        "required_state": [],
                        "timeline_limit": 0,
                    }
                }
            },
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        # Assert: User2 SHOULD see Reply 1 (happened while joined) but NOT Reply 2 (after leaving)
        chunk = channel.json_body["chunk"]
        self.assertIn(
            room_id,
            chunk,
            "Thread updates should include the room user2 left",
        )
        self.assertIn(
            thread_root,
            chunk[room_id],
            "Thread root should be in the updates",
        )

        # Verify that only a single update was seen (Reply 1) by checking that there's
        # no prev_batch token. If Reply 2 was also included, there would be multiple
        # updates and a prev_batch token would be present.
        thread_update = chunk[room_id][thread_root]
        self.assertNotIn(
            "prev_batch",
            thread_update,
            "No prev_batch should be present since only one update (Reply 1) is visible",
        )
