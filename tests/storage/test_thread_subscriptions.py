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

from typing import Optional

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines.sqlite import Sqlite3Engine
from synapse.util import Clock

from tests import unittest


class ThreadSubscriptionsTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.user_id = "@user:test"
        self.room_id = "!room:test"
        self.thread_root_id = "$thread_root:test"
        self.other_thread_root_id = "$other_thread_root:test"

        # Disable foreign key checks for testing
        # This allows us to insert test data without having to create actual events
        db_pool = self.store.db_pool
        if isinstance(db_pool.engine, Sqlite3Engine):
            self.get_success(
                db_pool.execute("disable_foreign_keys", "PRAGMA foreign_keys = OFF;")
            )
        else:
            # Postgres
            def f(txn: LoggingTransaction) -> None:
                txn.execute(
                    """
                    ALTER TABLE thread_subscriptions
                        DROP CONSTRAINT thread_subscriptions_fk_users,
                        DROP CONSTRAINT thread_subscriptions_fk_rooms,
                        DROP CONSTRAINT thread_subscriptions_fk_events;
                    """,
                )

            self.get_success(db_pool.runInteraction("disable_foreign_keys", f))

        # Create rooms and events in the db to satisfy foreign key constraints
        self.get_success(db_pool.simple_insert("rooms", {"room_id": self.room_id}))

        self.get_success(
            db_pool.simple_insert(
                "events",
                {
                    "event_id": self.thread_root_id,
                    "room_id": self.room_id,
                    "topological_ordering": 1,
                    "stream_ordering": 1,
                    "type": "m.room.message",
                    "depth": 1,
                    "processed": True,
                    "outlier": False,
                },
            )
        )

        self.get_success(
            db_pool.simple_insert(
                "events",
                {
                    "event_id": self.other_thread_root_id,
                    "room_id": self.room_id,
                    "topological_ordering": 2,
                    "stream_ordering": 2,
                    "type": "m.room.message",
                    "depth": 2,
                    "processed": True,
                    "outlier": False,
                },
            )
        )

        # Create the user
        self.get_success(
            db_pool.simple_insert("users", {"name": self.user_id, "is_guest": 0})
        )

    def _subscribe(
        self,
        thread_root_id: str,
        *,
        automatic: bool,
        room_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[int]:
        if user_id is None:
            user_id = self.user_id

        if room_id is None:
            room_id = self.room_id

        return self.get_success(
            self.store.subscribe_user_to_thread(
                user_id,
                room_id,
                thread_root_id,
                automatic=automatic,
            )
        )

    def _unsubscribe(
        self,
        thread_root_id: str,
        room_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[int]:
        if user_id is None:
            user_id = self.user_id

        if room_id is None:
            room_id = self.room_id

        return self.get_success(
            self.store.unsubscribe_user_from_thread(
                user_id,
                room_id,
                thread_root_id,
            )
        )

    def test_set_and_get_thread_subscription(self) -> None:
        """Test basic setting and getting of thread subscriptions."""
        # Initial state: no subscription
        subscription = self.get_success(
            self.store.get_subscription_for_thread(
                self.user_id, self.room_id, self.thread_root_id
            )
        )
        self.assertIsNone(subscription)

        # Subscribe
        self._subscribe(
            self.thread_root_id,
            automatic=True,
        )

        # Assert subscription went through
        subscription = self.get_success(
            self.store.get_subscription_for_thread(
                self.user_id, self.room_id, self.thread_root_id
            )
        )
        self.assertIsNotNone(subscription)
        self.assertTrue(subscription.automatic)  # type: ignore

        # Now make it a manual subscription
        self._subscribe(
            self.thread_root_id,
            automatic=False,
        )

        # Assert the manual subscription overrode the automatic one
        subscription = self.get_success(
            self.store.get_subscription_for_thread(
                self.user_id, self.room_id, self.thread_root_id
            )
        )
        self.assertFalse(subscription.automatic)  # type: ignore

    def test_purge_thread_subscriptions_for_user(self) -> None:
        """Test purging all thread subscription settings for a user."""
        # Set subscription settings for multiple threads
        self._subscribe(self.thread_root_id, automatic=True)
        self._subscribe(self.other_thread_root_id, automatic=False)

        subscriptions = self.get_success(
            self.store.get_updated_thread_subscriptions_for_user(
                self.user_id,
                from_id=0,
                to_id=50,
                limit=50,
            )
        )
        min_id = min(id for (id, _, _) in subscriptions)
        self.assertEqual(
            subscriptions,
            [
                (min_id, self.room_id, self.thread_root_id),
                (min_id + 1, self.room_id, self.other_thread_root_id),
            ],
        )

        # Purge all settings for the user
        self.get_success(
            self.store.purge_thread_subscription_settings_for_user(self.user_id)
        )

        # Check user has no subscriptions
        subscriptions = self.get_success(
            self.store.get_updated_thread_subscriptions_for_user(
                self.user_id,
                from_id=0,
                to_id=50,
                limit=50,
            )
        )
        self.assertEqual(subscriptions, [])

    def test_get_updated_thread_subscriptions(self) -> None:
        """Test getting updated thread subscriptions since a stream ID."""

        stream_id1 = self._subscribe(self.thread_root_id, automatic=False)
        stream_id2 = self._subscribe(self.other_thread_root_id, automatic=True)
        assert stream_id1 is not None
        assert stream_id2 is not None

        # Get updates since initial ID (should include both changes)
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions(0, stream_id2, 10)
        )
        self.assertEqual(len(updates), 2)

        # Get updates since first change (should include only the second change)
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions(stream_id1, stream_id2, 10)
        )
        self.assertEqual(
            updates,
            [(stream_id2, self.user_id, self.room_id, self.other_thread_root_id)],
        )

    def test_get_updated_thread_subscriptions_for_user(self) -> None:
        """Test getting updated thread subscriptions for a specific user."""
        other_user_id = "@other_user:test"

        # Set thread subscription for main user
        stream_id1 = self._subscribe(self.thread_root_id, automatic=True)
        assert stream_id1 is not None

        # Set thread subscription for other user
        stream_id2 = self._subscribe(
            self.other_thread_root_id,
            automatic=True,
            user_id=other_user_id,
        )
        assert stream_id2 is not None

        # Get updates for main user
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions_for_user(
                self.user_id, 0, stream_id2, 10
            )
        )
        self.assertEqual(updates, [(stream_id1, self.room_id, self.thread_root_id)])

        # Get updates for other user
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions_for_user(
                other_user_id, 0, max(stream_id1, stream_id2), 10
            )
        )
        self.assertEqual(
            updates, [(stream_id2, self.room_id, self.other_thread_root_id)]
        )
