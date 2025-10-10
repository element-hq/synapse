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

from twisted.internet.testing import MemoryReactor

from synapse.replication.tcp.streams._base import (
    _STREAM_UPDATE_TARGET_ROW_COUNT,
    ThreadSubscriptionsStream,
)
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.util.clock import Clock

from tests.replication._base import BaseStreamTestCase


class ThreadSubscriptionsStreamTestCase(BaseStreamTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

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

        self.get_success(
            self.hs.get_datastores().main.db_pool.runInteraction(
                "disable_foreign_keys", f
            )
        )

    def test_thread_subscription_updates(self) -> None:
        """Test replication with thread subscription updates"""
        store = self.hs.get_datastores().main

        # Create thread subscription updates
        updates = []
        room_id = "!test_room:example.com"

        # Generate several thread subscription updates
        for i in range(_STREAM_UPDATE_TARGET_ROW_COUNT + 5):
            thread_root_id = f"$thread_{i}:example.com"
            self.get_success(
                store.subscribe_user_to_thread(
                    "@test_user:example.org",
                    room_id,
                    thread_root_id,
                    automatic_event_orderings=None,
                )
            )
            updates.append(thread_root_id)

        # Also add one in a different room
        other_room_id = "!other_room:example.com"
        other_thread_root_id = "$other_thread:example.com"
        self.get_success(
            store.subscribe_user_to_thread(
                "@test_user:example.org",
                other_room_id,
                other_thread_root_id,
                automatic_event_orderings=None,
            )
        )

        # Not yet connected: no rows should yet have been received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # Now reconnect to pull the updates
        self.reconnect()
        self.replicate()

        # We should have received all the expected rows in the right order
        # Filter the updates to only include thread subscription changes
        received_thread_subscription_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ThreadSubscriptionsStream.NAME
        ]

        # Verify all the thread subscription updates
        for thread_id in updates:
            (stream_name, token, row) = received_thread_subscription_rows.pop(0)
            self.assertEqual(stream_name, ThreadSubscriptionsStream.NAME)
            self.assertIsInstance(row, ThreadSubscriptionsStream.ROW_TYPE)
            self.assertEqual(row.user_id, "@test_user:example.org")
            self.assertEqual(row.room_id, room_id)
            self.assertEqual(row.event_id, thread_id)

        # Verify the last update in the different room
        (stream_name, token, row) = received_thread_subscription_rows.pop(0)
        self.assertEqual(stream_name, ThreadSubscriptionsStream.NAME)
        self.assertIsInstance(row, ThreadSubscriptionsStream.ROW_TYPE)
        self.assertEqual(row.user_id, "@test_user:example.org")
        self.assertEqual(row.room_id, other_room_id)
        self.assertEqual(row.event_id, other_thread_root_id)

        self.assertEqual([], received_thread_subscription_rows)

    def test_multiple_users_thread_subscription_updates(self) -> None:
        """Test replication with thread subscription updates for multiple users"""
        store = self.hs.get_datastores().main
        room_id = "!test_room:example.com"
        thread_root_id = "$thread_root:example.com"

        # Create updates for multiple users
        users = ["@user1:example.com", "@user2:example.com", "@user3:example.com"]
        for user_id in users:
            self.get_success(
                store.subscribe_user_to_thread(
                    user_id, room_id, thread_root_id, automatic_event_orderings=None
                )
            )

        # Check no rows have been received yet
        self.replicate()
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # Not yet connected: no rows should yet have been received
        self.reconnect()
        self.replicate()

        # We should have received all the expected rows
        # Filter the updates to only include thread subscription changes
        received_thread_subscription_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ThreadSubscriptionsStream.NAME
        ]

        # Should have one update per user
        self.assertEqual(len(received_thread_subscription_rows), len(users))

        # Verify all updates
        for i, user_id in enumerate(users):
            (stream_name, token, row) = received_thread_subscription_rows[i]
            self.assertEqual(stream_name, ThreadSubscriptionsStream.NAME)
            self.assertIsInstance(row, ThreadSubscriptionsStream.ROW_TYPE)
            self.assertEqual(row.user_id, user_id)
            self.assertEqual(row.room_id, room_id)
            self.assertEqual(row.event_id, thread_root_id)
