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

from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.thread_subscriptions import (
    AutomaticSubscriptionConflicted,
    ThreadSubscriptionsWorkerStore,
)
from synapse.storage.engines.sqlite import Sqlite3Engine
from synapse.types import EventOrderings
from synapse.util.clock import Clock

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
        automatic_event_orderings: EventOrderings | None,
        room_id: str | None = None,
        user_id: str | None = None,
    ) -> int | AutomaticSubscriptionConflicted | None:
        if user_id is None:
            user_id = self.user_id

        if room_id is None:
            room_id = self.room_id

        return self.get_success(
            self.store.subscribe_user_to_thread(
                user_id,
                room_id,
                thread_root_id,
                automatic_event_orderings=automatic_event_orderings,
            )
        )

    def _unsubscribe(
        self,
        thread_root_id: str,
        room_id: str | None = None,
        user_id: str | None = None,
    ) -> int | None:
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
            automatic_event_orderings=EventOrderings(1, 1),
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
            automatic_event_orderings=None,
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
        self._subscribe(
            self.thread_root_id, automatic_event_orderings=EventOrderings(1, 1)
        )
        self._subscribe(self.other_thread_root_id, automatic_event_orderings=None)

        subscriptions = self.get_success(
            self.store.get_latest_updated_thread_subscriptions_for_user(
                self.user_id,
                from_id=0,
                to_id=50,
                limit=50,
            )
        )
        min_id = min(id for (id, _, _, _, _) in subscriptions)
        self.assertEqual(
            subscriptions,
            [
                (min_id, self.room_id, self.thread_root_id, True, True),
                (min_id + 1, self.room_id, self.other_thread_root_id, True, False),
            ],
        )

        # Purge all settings for the user
        self.get_success(
            self.store.purge_thread_subscription_settings_for_user(self.user_id)
        )

        # Check user has no subscriptions
        subscriptions = self.get_success(
            self.store.get_latest_updated_thread_subscriptions_for_user(
                self.user_id,
                from_id=0,
                to_id=50,
                limit=50,
            )
        )
        self.assertEqual(subscriptions, [])

    def test_get_updated_thread_subscriptions(self) -> None:
        """Test getting updated thread subscriptions since a stream ID."""

        stream_id1 = self._subscribe(
            self.thread_root_id, automatic_event_orderings=EventOrderings(1, 1)
        )
        stream_id2 = self._subscribe(
            self.other_thread_root_id, automatic_event_orderings=EventOrderings(2, 2)
        )
        assert stream_id1 is not None and not isinstance(
            stream_id1, AutomaticSubscriptionConflicted
        )
        assert stream_id2 is not None and not isinstance(
            stream_id2, AutomaticSubscriptionConflicted
        )

        # Get updates since initial ID (should include both changes)
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions(
                from_id=0, to_id=stream_id2, limit=10
            )
        )
        self.assertEqual(len(updates), 2)

        # Get updates since first change (should include only the second change)
        updates = self.get_success(
            self.store.get_updated_thread_subscriptions(
                from_id=stream_id1, to_id=stream_id2, limit=10
            )
        )
        self.assertEqual(
            updates,
            [(stream_id2, self.user_id, self.room_id, self.other_thread_root_id)],
        )

    def test_get_updated_thread_subscriptions_for_user(self) -> None:
        """Test getting updated thread subscriptions for a specific user."""
        other_user_id = "@other_user:test"

        # Set thread subscription for main user
        stream_id1 = self._subscribe(
            self.thread_root_id, automatic_event_orderings=EventOrderings(1, 1)
        )
        assert stream_id1 is not None and not isinstance(
            stream_id1, AutomaticSubscriptionConflicted
        )

        # Set thread subscription for other user
        stream_id2 = self._subscribe(
            self.other_thread_root_id,
            automatic_event_orderings=EventOrderings(1, 1),
            user_id=other_user_id,
        )
        assert stream_id2 is not None and not isinstance(
            stream_id2, AutomaticSubscriptionConflicted
        )

        # Get updates for main user
        updates = self.get_success(
            self.store.get_latest_updated_thread_subscriptions_for_user(
                self.user_id, from_id=0, to_id=stream_id2, limit=10
            )
        )
        self.assertEqual(
            updates, [(stream_id1, self.room_id, self.thread_root_id, True, True)]
        )

        # Get updates for other user
        updates = self.get_success(
            self.store.get_latest_updated_thread_subscriptions_for_user(
                other_user_id, from_id=0, to_id=max(stream_id1, stream_id2), limit=10
            )
        )
        self.assertEqual(
            updates, [(stream_id2, self.room_id, self.other_thread_root_id, True, True)]
        )

    def test_should_skip_autosubscription_after_unsubscription(self) -> None:
        """
        Tests the comparison logic for whether an autoscription should be skipped
        due to a chronologically earlier but logically later unsubscription.
        """

        func = ThreadSubscriptionsWorkerStore._should_skip_autosubscription_after_unsubscription

        # Order of arguments:
        # automatic cause event: stream order, then topological order
        # unsubscribe maximums: stream order, then tological order

        # both orderings agree that the unsub is after the cause event
        self.assertTrue(
            func(autosub=EventOrderings(1, 1), unsubscribed_at=EventOrderings(2, 2))
        )

        # topological ordering is inconsistent with stream ordering,
        # in that case favour stream ordering because it's what /sync uses
        self.assertTrue(
            func(autosub=EventOrderings(1, 2), unsubscribed_at=EventOrderings(2, 1))
        )

        # the automatic subscription is caused by a backfilled event here
        # unfortunately we must fall back to topological ordering here
        self.assertTrue(
            func(autosub=EventOrderings(-50, 2), unsubscribed_at=EventOrderings(2, 3))
        )
        self.assertFalse(
            func(autosub=EventOrderings(-50, 2), unsubscribed_at=EventOrderings(2, 1))
        )

    def test_get_subscribers_to_thread(self) -> None:
        """
        Test getting all subscribers to a thread at once.

        To check cache invalidations are correct, we do multiple
        step-by-step rounds of subscription changes and assertions.
        """
        other_user_id = "@other_user:test"

        subscribers = self.get_success(
            self.store.get_subscribers_to_thread(self.room_id, self.thread_root_id)
        )
        self.assertEqual(subscribers, frozenset())

        self._subscribe(
            self.thread_root_id, automatic_event_orderings=None, user_id=self.user_id
        )

        subscribers = self.get_success(
            self.store.get_subscribers_to_thread(self.room_id, self.thread_root_id)
        )
        self.assertEqual(subscribers, frozenset((self.user_id,)))

        self._subscribe(
            self.thread_root_id, automatic_event_orderings=None, user_id=other_user_id
        )

        subscribers = self.get_success(
            self.store.get_subscribers_to_thread(self.room_id, self.thread_root_id)
        )
        self.assertEqual(subscribers, frozenset((self.user_id, other_user_id)))

        self._unsubscribe(self.thread_root_id, user_id=self.user_id)

        subscribers = self.get_success(
            self.store.get_subscribers_to_thread(self.room_id, self.thread_root_id)
        )
        self.assertEqual(subscribers, frozenset((other_user_id,)))
