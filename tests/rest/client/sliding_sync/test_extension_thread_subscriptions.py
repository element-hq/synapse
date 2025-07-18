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
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


class SlidingSyncThreadSubscriptionsExtensionTestCase(SlidingSyncBase):
    """
    Test the thread subscriptions extension in the Sliding Sync API.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        super().prepare(reactor, clock, hs)

    def test_no_data_initial_sync(self) -> None:
        """
        Test enabling thread subscriptions extension during initial sync with no data.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }

        # Act
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertNotIn("thread_subscriptions", response_body["extensions"])

    def test_no_data_incremental_sync(self) -> None:
        """
        Test enabling thread subscriptions extension during incremental sync with no data.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        initial_sync_body: JsonDict = {
            "lists": {},
        }

        # Act: Initial sync
        response_body, sync_pos = self.do_sync(initial_sync_body, tok=user1_tok)

        # Act: Incremental sync with extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert
        self.assertNotIn(
            "thread_subscriptions", response_body["extensions"], response_body
        )

    def test_thread_subscription_initial_sync(self) -> None:
        """
        Test thread subscriptions appear in initial sync response.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]
        self._subscribe_to_thread(user1_id, room_id, thread_root_id, automatic=False)
        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }

        # Act
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertEqual(
            response_body["extensions"]["thread_subscriptions"],
            {
                "changes": [
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id,
                        "subscribed": True,
                        "automatic": False,
                    }
                ]
            },
        )

    def test_thread_subscription_incremental_sync(self) -> None:
        """
        Test new thread subscriptions appear in incremental sync response.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # Act
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)
        logger.info("Synced to: %r, now subscribing to thread", sync_pos)
        self._subscribe_to_thread(user1_id, room_id, thread_root_id, automatic=True)
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)
        logger.info("Synced to: %r", sync_pos)

        # Assert
        self.assertEqual(
            response_body["extensions"]["thread_subscriptions"],
            {
                "changes": [
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id,
                        "subscribed": True,
                        "automatic": True,
                    }
                ]
            },
        )

    def test_unsubscribe_from_thread(self) -> None:
        """
        Test unsubscribing from a thread.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]
        self._subscribe_to_thread(user1_id, room_id, thread_root_id, automatic=False)
        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }

        # Act: Initial sync
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Assert: Subscription present
        self.assertIn("thread_subscriptions", response_body["extensions"])
        changes = response_body["extensions"]["thread_subscriptions"]["changes"]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["subscribed"])

        # Arrange: Unsubscribe
        self._unsubscribe_from_thread(user1_id, room_id, thread_root_id)

        # Act: Incremental sync
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: Unsubscription present
        self.assertEqual(
            response_body["extensions"]["thread_subscriptions"],
            {
                "changes": [
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id,
                        "subscribed": False,
                    }
                ]
            },
        )

    def test_multiple_thread_subscriptions(self) -> None:
        """
        Test handling of multiple thread subscriptions.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread roots
        thread_root_resp1 = self.helper.send(
            room_id, body="Thread root 1", tok=user1_tok
        )
        thread_root_id1 = thread_root_resp1["event_id"]
        thread_root_resp2 = self.helper.send(
            room_id, body="Thread root 2", tok=user1_tok
        )
        thread_root_id2 = thread_root_resp2["event_id"]
        thread_root_resp3 = self.helper.send(
            room_id, body="Thread root 3", tok=user1_tok
        )
        thread_root_id3 = thread_root_resp3["event_id"]

        # Subscribe to threads
        self._subscribe_to_thread(user1_id, room_id, thread_root_id1, automatic=False)
        self._subscribe_to_thread(user1_id, room_id, thread_root_id2, automatic=True)
        self._subscribe_to_thread(user1_id, room_id, thread_root_id3, automatic=False)

        sync_body = {
            "lists": {},
            "extensions": {
                "thread_subscriptions": {
                    "enabled": True,
                }
            },
        }

        # Act
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertEqual(
            response_body["extensions"]["thread_subscriptions"],
            {
                "changes": [
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id1,
                        "subscribed": True,
                        "automatic": False,
                    },
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id2,
                        "subscribed": True,
                        "automatic": True,
                    },
                    {
                        "room_id": room_id,
                        "root_event_id": thread_root_id3,
                        "subscribed": True,
                        "automatic": False,
                    },
                ]
            },
        )

    def test_limit_parameter(self) -> None:
        """
        Test limit parameter in thread subscriptions extension.
        """
        # Arrange
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create 5 thread roots and subscribe to each
        thread_root_ids = []
        for i in range(5):
            thread_root_resp = self.helper.send(
                room_id, body=f"Thread root {i}", tok=user1_tok
            )
            thread_root_ids.append(thread_root_resp["event_id"])
            self._subscribe_to_thread(
                user1_id, room_id, thread_root_ids[-1], automatic=False
            )

        sync_body = {
            "lists": {},
            "extensions": {"thread_subscriptions": {"enabled": True, "limit": 3}},
        }

        # Act
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        thread_subscriptions = response_body["extensions"]["thread_subscriptions"]
        self.assertEqual(len(thread_subscriptions["changes"]), 3)

    def _subscribe_to_thread(
        self, user_id: str, room_id: str, thread_root_id: str, automatic: bool
    ) -> None:
        """
        Helper method to subscribe a user to a thread.
        """
        self.get_success(
            self.store.subscribe_user_to_thread(
                user_id=user_id,
                room_id=room_id,
                thread_root_event_id=thread_root_id,
                automatic=automatic,
            )
        )

    def _unsubscribe_from_thread(
        self, user_id: str, room_id: str, thread_root_id: str
    ) -> None:
        """
        Helper method to unsubscribe a user from a thread.
        """
        self.get_success(
            self.store.unsubscribe_user_from_thread(
                user_id=user_id,
                room_id=room_id,
                thread_root_event_id=thread_root_id,
            )
        )
