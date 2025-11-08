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
from synapse.rest.client import login, relations, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


# The name of the extension. Currently unstable-prefixed.
EXT_NAME = "io.element.msc4360.threads"


class SlidingSyncThreadsExtensionTestCase(SlidingSyncBase):
    """
    Test the threads extension in the Sliding Sync API.
    """

    maxDiff = None

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        relations.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4360_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        super().prepare(reactor, clock, hs)

    def test_no_data_initial_sync(self) -> None:
        """
        Test enabling threads extension during initial sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        sync_body = {
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertNotIn(EXT_NAME, response_body["extensions"])

    def test_no_data_incremental_sync(self) -> None:
        """
        Test enabling threads extension during incremental sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        initial_sync_body: JsonDict = {}

        # Initial sync
        response_body, sync_pos = self.do_sync(initial_sync_body, tok=user1_tok)

        # Incremental sync with extension enabled
        sync_body = {
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert
        self.assertNotIn(
            EXT_NAME,
            response_body["extensions"],
            response_body,
        )

    def test_threads_initial_sync(self) -> None:
        """
        Test threads appear in initial sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        _latest_event_id = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": user1_id,
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )["event_id"]

        # # get the baseline stream_id of the thread_subscriptions stream
        # # before we write any data.
        # # Required because the initial value differs between SQLite and Postgres.
        # base = self.store.get_max_thread_subscriptions_stream_id()

        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0, # Set to 0, otherwise events will be in timeline, not extension
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {"updates": {room_id: {thread_root_id: {}}}},
        )


    def test_threads_incremental_sync(self) -> None:
        """
        Test new thread updates appear in incremental sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # get the baseline stream_id of the room events stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        # base = self.store.get_room_max_stream_ordering()

        # Initial sync
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)
        logger.info("Synced to: %r, now subscribing to thread", sync_pos)

        # Do thing
        _latest_event_id = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": user1_id,
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )["event_id"]

        # Incremental sync
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)
        logger.info("Synced to: %r", sync_pos)

        # Assert
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {"updates": {room_id: {thread_root_id: {}}}},
        )

    def test_threads_only_from_joined_rooms(self) -> None:
        """
        Test that thread updates are only returned for rooms the user is joined to
        at the time of the thread update.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # User1 creates two rooms
        room_a_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        room_b_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # User2 joins only Room A
        self.helper.join(room_a_id, user2_id, tok=user2_tok)

        # Create threads in both rooms
        thread_a_root = self.helper.send(room_a_id, body="Thread A", tok=user1_tok)[
            "event_id"
        ]
        thread_b_root = self.helper.send(room_b_id, body="Thread B", tok=user1_tok)[
            "event_id"
        ]

        # Add replies to both threads
        self.helper.send_event(
            room_a_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply to A",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_a_root,
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
                    "event_id": thread_b_root,
                },
            },
            tok=user1_tok,
        )

        # User2 syncs with threads extension enabled
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user2_tok)

        # Assert: User2 should only see thread from Room A (where they are joined)
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {"updates": {room_a_id: {thread_a_root: {}}}},
            "User2 should only see threads from Room A where they are joined, not Room B",
        )

    def test_threads_not_returned_after_leaving_room(self) -> None:
        """
        Test that thread updates are not returned after a user leaves the room,
        even if the thread was updated while they were joined.

        This tests the known limitation: if a thread has multiple updates and the
        user leaves between them, they won't see any updates (even earlier ones
        while joined).
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Create room and both users join
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.join(room_id, user2_id, tok=user2_tok)

        # Create thread
        thread_root = self.helper.send(room_id, body="Thread root", tok=user1_tok)[
            "event_id"
        ]

        # Initial sync for user2
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        _, sync_pos = self.do_sync(sync_body, tok=user2_tok)

        # Reply in thread while user2 is joined, but after initial sync
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

        # User2 incremental sync
        response_body, _ = self.do_sync(sync_body, tok=user2_tok, since=sync_pos)

        # Assert: User2 should NOT see the thread update (they left before latest update)
        # Note: This also demonstrates that only currently joined rooms are returned - user2
        # won't see the thread even though there was an update while they were joined (Reply 1)
        self.assertNotIn(
            EXT_NAME,
            response_body["extensions"],
            "User2 should not see thread updates after leaving the room",
        )

    def test_threads_with_include_roots_true(self) -> None:
        """
        Test that include_roots=True returns thread root events with latest_event
        in the unsigned field.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Add reply to thread
        latest_event_resp = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Latest reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )
        latest_event_id = latest_event_resp["event_id"]

        # Sync with include_roots=True
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                    "include_roots": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert thread root is present
        thread_root = response_body["extensions"][EXT_NAME]["updates"][room_id][
            thread_root_id
        ]["thread_root"]

        # Verify it's the correct event
        self.assertEqual(thread_root["event_id"], thread_root_id)
        self.assertEqual(thread_root["content"]["body"], "Thread root")

        # Verify latest_event is in unsigned.m.relations.m.thread
        latest_event = thread_root["unsigned"]["m.relations"]["m.thread"][
            "latest_event"
        ]
        self.assertEqual(latest_event["event_id"], latest_event_id)
        self.assertEqual(latest_event["content"]["body"], "Latest reply")

    def test_threads_with_include_roots_false(self) -> None:
        """
        Test that include_roots=False (or omitted) does not return thread root events.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Add reply
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

        # Sync with include_roots=False (explicitly)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                    "include_roots": False,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert thread update exists but has no thread_root
        thread_update = response_body["extensions"][EXT_NAME]["updates"][room_id][
            thread_root_id
        ]
        self.assertNotIn("thread_root", thread_update)

        # Also test with include_roots omitted (should behave the same)
        sync_body_no_param = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body_no_param, _ = self.do_sync(sync_body_no_param, tok=user1_tok)

        thread_update_no_param = response_body_no_param["extensions"][EXT_NAME][
            "updates"
        ][room_id][thread_root_id]
        self.assertNotIn("thread_root", thread_update_no_param)

    def test_per_thread_prev_batch_single_update(self) -> None:
        """
        Test that threads with only a single update do NOT get a prev_batch token.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Initial sync to establish baseline
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Add ONE reply to thread
        self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Single reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )

        # Incremental sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: Thread update should NOT have prev_batch (only 1 update)
        thread_update = response_body["extensions"][EXT_NAME]["updates"][room_id][
            thread_root_id
        ]
        self.assertNotIn(
            "prev_batch",
            thread_update,
            "Threads with single update should not have prev_batch",
        )

    def test_per_thread_prev_batch_multiple_updates(self) -> None:
        """
        Test that threads with multiple updates get a prev_batch token that can be
        used with /relations endpoint to paginate backwards.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Initial sync to establish baseline
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Add MULTIPLE replies to thread
        reply1_resp = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "First reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )
        reply1_id = reply1_resp["event_id"]

        reply2_resp = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Second reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )
        reply2_id = reply2_resp["event_id"]

        reply3_resp = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Third reply",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )
        reply3_id = reply3_resp["event_id"]

        # Incremental sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: Thread update SHOULD have prev_batch (3 updates)
        prev_batch = response_body["extensions"][EXT_NAME]["updates"][room_id][
            thread_root_id
        ]["prev_batch"]
        self.assertIsNotNone(prev_batch, "prev_batch should not be None")

        # Now use the prev_batch token with /relations endpoint to paginate backwards
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{room_id}/relations/{thread_root_id}?from={prev_batch}&to={sync_pos}&dir=b",
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        relations_response = channel.json_body
        returned_event_ids = [
            event["event_id"] for event in relations_response["chunk"]
        ]

        # Assert: Only the older replies should be returned (not the latest one we already saw)
        # The prev_batch token should be exclusive, pointing just before the latest event
        self.assertIn(
            reply1_id,
            returned_event_ids,
            "First reply should be in relations response",
        )
        self.assertIn(
            reply2_id,
            returned_event_ids,
            "Second reply should be in relations response",
        )
        self.assertNotIn(
            reply3_id,
            returned_event_ids,
            "Third reply (latest) should NOT be in relations response - already returned in sliding sync",
        )

    def test_per_thread_prev_batch_on_initial_sync(self) -> None:
        """
        Test that threads with multiple updates get prev_batch tokens on initial sync
        so clients can paginate through the full thread history.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread with multiple replies BEFORE any sync
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        reply1_resp = self.helper.send_event(
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
        reply1_id = reply1_resp["event_id"]

        reply2_resp = self.helper.send_event(
            room_id,
            type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": "Reply 2",
                "m.relates_to": {
                    "rel_type": RelationTypes.THREAD,
                    "event_id": thread_root_id,
                },
            },
            tok=user1_tok,
        )
        reply2_id = reply2_resp["event_id"]

        # Initial sync (no from_token)
        sync_body = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 0,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert: Thread update SHOULD have prev_batch on initial sync (2+ updates exist)
        prev_batch = response_body["extensions"][EXT_NAME]["updates"][room_id][
            thread_root_id
        ]["prev_batch"]
        self.assertIsNotNone(prev_batch)

        # Use prev_batch with /relations to fetch the thread history
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/rooms/{room_id}/relations/{thread_root_id}?from={prev_batch}&dir=b",
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        relations_response = channel.json_body
        returned_event_ids = [
            event["event_id"] for event in relations_response["chunk"]
        ]

        # Assert: Only the older reply should be returned (not the latest one we already saw)
        # The prev_batch token should be exclusive, pointing just before the latest event
        self.assertIn(
            reply1_id,
            returned_event_ids,
            "First reply should be in relations response",
        )
        self.assertNotIn(
            reply2_id,
            returned_event_ids,
            "Second reply (latest) should NOT be in relations response - already returned in sliding sync",
        )

    def test_thread_in_timeline_omitted_without_include_roots(self) -> None:
        """
        Test that threads with events in the room timeline are omitted from the
        extension response when include_roots=False. When all threads are filtered out,
        the entire extension should be omitted from the response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Initial sync to establish baseline
        sync_body: JsonDict = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 5,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                    "include_roots": False,
                }
            },
        }
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Send a reply to the thread
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

        # Incremental sync - the reply should be in the timeline
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: Extension should be omitted entirely since the only thread with updates
        # is already visible in the timeline (include_roots=False)
        self.assertNotIn(
            EXT_NAME,
            response_body.get("extensions", {}),
            "Extension should be omitted when all threads are filtered out (in timeline with include_roots=False)",
        )

    def test_thread_in_timeline_included_with_include_roots(self) -> None:
        """
        Test that threads with events in the room timeline are still included in the
        extension response when include_roots=True, because the client wants the root event.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread root
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Initial sync to establish baseline
        sync_body: JsonDict = {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 1]],
                    "required_state": [],
                    "timeline_limit": 5,
                }
            },
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                    "include_roots": True,
                }
            },
        }
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Send a reply to the thread
        reply_resp = self.helper.send_event(
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
        reply_id = reply_resp["event_id"]

        # Incremental sync - the reply should be in the timeline
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: The thread reply should be in the room timeline
        room_response = response_body["rooms"][room_id]
        timeline_event_ids = [event["event_id"] for event in room_response["timeline"]]
        self.assertIn(
            reply_id,
            timeline_event_ids,
            "Thread reply should be in the room timeline",
        )

        # Assert: Thread SHOULD be in extension (include_roots=True)
        thread_updates = response_body["extensions"][EXT_NAME]["updates"][room_id]
        self.assertIn(
            thread_root_id,
            thread_updates,
            "Thread should be included in extension when include_roots=True, even if in timeline",
        )
        # Verify the thread root event is present
        self.assertIn("thread_root", thread_updates[thread_root_id])
