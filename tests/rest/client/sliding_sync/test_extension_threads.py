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
from synapse.rest.client import login, room, sync
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
        # TODO:
        # threads.register_servlets,
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
            "lists": {},
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
        initial_sync_body: JsonDict = {
            "lists": {},
        }

        # Initial sync
        response_body, sync_pos = self.do_sync(initial_sync_body, tok=user1_tok)

        # Incremental sync with extension enabled
        sync_body = {
            "lists": {},
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
            "lists": {},
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
            "lists": {},
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
            "lists": {},
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
            "lists": {},
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
        # Note: This demonstrates the known limitation - user2 won't see the thread
        # even though there was an update while they were joined (Reply 1)
        self.assertNotIn(
            EXT_NAME,
            response_body["extensions"],
            "User2 should not see thread updates after leaving the room",
        )
