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
import logging
import time
from unittest.mock import patch

from twisted.test.proto_helpers import MemoryReactor
from twisted.internet import defer

from synapse.logging.context import defer_to_thread
from synapse.rest import admin, login, register, room
from synapse.server import HomeServer
from synapse.storage.database import (
    LoggingTransaction,
)
from synapse.types.storage import _BackgroundUpdates
from synapse.util import Clock
from synapse.logging.context import (
    PreserveLoggingContext,
    make_deferred_yieldable,
    run_coroutine_in_background,
    run_in_background,
)

from tests import unittest
from tests.replication._base import BaseMultiWorkerStreamTestCase

logger = logging.getLogger(__name__)


class EventStatsTestCase(unittest.HomeserverTestCase):
    """
    Tests for the `event_stats` table
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        register.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.hs = hs
        self.store = hs.get_datastores().main

        # Wait for the background updates to add the database triggers that keep the
        # `event_stats` table up-to-date.
        #
        # This also prevents background updates running during the tests and messing
        # with the results.
        self.wait_for_background_updates()

        super().prepare(reactor, clock, hs)

    def _perform_user_actions(self) -> None:
        """
        Perform some actions on the homeserver that would bump the event counts.

        This creates a few users, a room, and sends some messages. Expected number of
        events:
         - 10 unencrypted messages
         - 5 encrypted messages
         - 24 total events (including room state, etc)
        """
        # Create some users
        user_1_mxid = self.register_user(
            username="test_user_1",
            password="test",
        )
        user_2_mxid = self.register_user(
            username="test_user_2",
            password="test",
        )

        # Log in to each user
        user_1_token = self.login(username=user_1_mxid, password="test")
        user_2_token = self.login(username=user_2_mxid, password="test")

        # Create a room between the two users
        room_1_id = self.helper.create_room_as(
            is_public=False,
            tok=user_1_token,
        )

        # Mark this room as end-to-end encrypted
        self.helper.send_state(
            room_id=room_1_id,
            event_type="m.room.encryption",
            body={
                "algorithm": "m.megolm.v1.aes-sha2",
                "rotation_period_ms": 604800000,
                "rotation_period_msgs": 100,
            },
            state_key="",
            tok=user_1_token,
        )

        # User 1 invites user 2
        self.helper.invite(
            room=room_1_id,
            src=user_1_mxid,
            targ=user_2_mxid,
            tok=user_1_token,
        )

        # User 2 joins
        self.helper.join(
            room=room_1_id,
            user=user_2_mxid,
            tok=user_2_token,
        )

        # User 1 sends 10 unencrypted messages
        for _ in range(10):
            self.helper.send(
                room_id=room_1_id,
                body="Zoinks Scoob! A message!",
                tok=user_1_token,
            )

        # User 2 sends 5 encrypted "messages"
        for _ in range(5):
            self.helper.send_event(
                room_id=room_1_id,
                type="m.room.encrypted",
                content={
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": "some_key",
                    "ciphertext": {
                        "some_key": {
                            "type": 0,
                            "body": "encrypted_payload",
                        },
                    },
                },
                tok=user_2_token,
            )

    def test_concurrent_event_insert(self) -> None:
        """
        TODO

        Normally, the `events` stream is covered by a single "event_perister" worker but
        it does experimentally support multiple workers, where load is sharded
        between them by room ID.

        If we don't pay special attention to this, we will see errors like the following:
        ```
        psycopg2.errors.SerializationFailure: could not serialize access due to concurrent update
        CONTEXT:  SQL statement "UPDATE event_stats SET total_event_count = total_event_count + 1"
        ```

        This is a regression test for https://github.com/element-hq/synapse/issues/18349
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            is_public=False,
            tok=user1_tok,
        )

        block = True

        # Create a transaction that interacts with the `event_stats` table
        def _todo_txn(
            txn: LoggingTransaction,
        ) -> None:
            nonlocal block
            while block:
                logger.info("qwer")
                # txn.execute(
                #     "UPDATE event_stats SET total_event_count = total_event_count + 1",
                # )
                time.sleep(0.1)
            logger.info("qwer done")

            # We need to return something, so we return None.
            return None

        # def asdf() -> None:
        #     self.get_success(self.store.db_pool.runInteraction("test", _todo_txn))

        # Start a transaction that is interacting with the `event_stats` table
        # start_txn = defer_to_thread(self.reactor, asdf)

        start_txn = run_in_background(
            self.store.db_pool.runInteraction, "test", _todo_txn
        )

        logger.info("asdf1")
        _event_response = self.get_success(
            defer_to_thread(
                self.reactor, self.helper.send, room_id1, "activity", tok=user1_tok
            )
        )
        logger.info("asdf2")

        block = False

        self.get_success(start_txn)
        # self.pump(0.1)

    def test_background_update_with_events(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly when there are events in the database.
        """
        # Do things to bump the stats
        self._perform_user_actions()

        # Since the background update has already run once when Synapse started, let's
        # manually reset the database `event_stats` back to 0 to ensure this test is
        # starting from a clean slate. We want to be able to detect 0 -> 24 instead of
        # 24 -> 24 as it's not possible to prove that any work was actually done if the
        # number doesn't change.
        self.get_success(
            self.store.db_pool.simple_update_one(
                table="event_stats",
                keyvalues={},
                updatevalues={
                    "total_event_count": 0,
                    "unencrypted_message_count": 0,
                    "e2ee_event_count": 0,
                },
                desc="reset event_stats in test preparation",
            )
        )
        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Expect our `event_stats` table to be populated with the correct values
        self.assertEqual(self.get_success(self.store.count_total_events()), 24)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 10)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 5)

    def test_background_update_without_events(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly without events in the database.
        """
        # Keep in mind: These are already populated as the background update has already
        # ran once when Synapse started and added the database triggers which are
        # incrementing things as new events come in.
        #
        # In this case, no events have been sent, so we expect the counts to be 0.
        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        self.assertEqual(self.get_success(self.store.count_total_events()), 0)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 0)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 0)

    def test_background_update_resume_progress(self) -> None:
        """
        Test that the background update to populate the `event_stats` table works
        correctly to resume from `progress_json`.
        """
        # Do things to bump the stats
        self._perform_user_actions()

        # Keep in mind: These are already populated as the background update has already
        # ran once when Synapse started and added the database triggers which are
        # incrementing things as new events come in.
        self.assertEqual(self.get_success(self.store.count_total_events()), 24)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 10)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 5)

        # Run the background update again
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.EVENT_STATS_POPULATE_COUNTS_BG_UPDATE,
                    "progress_json": '{ "last_event_stream_ordering": 14, "stop_event_stream_ordering": 21 }',
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # We expect these values to increase as the background update is being run
        # *again* and will double-count some of the `events` over the range specified
        # by the `progress_json`.
        self.assertEqual(self.get_success(self.store.count_total_events()), 24 + 7)
        self.assertEqual(self.get_success(self.store.count_total_messages()), 16)
        self.assertEqual(self.get_success(self.store.count_total_e2ee_events()), 6)


class EventStatsConcurrentEventsTestCase(BaseMultiWorkerStreamTestCase):
    """
    Test `event_stats` when events are being inserted/deleted concurrently with sharded
    event stream_writers enabled ("event_persisters").
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict:
        config = super().default_config()

        # Enable shared event stream_writers
        config["stream_writers"] = {"events": ["worker1", "worker2", "worker3"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
            "worker3": {"host": "testserv", "port": 1003},
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def _create_room(self, room_id: str, user_id: str, tok: str) -> None:
        """
        Create a room with a specific room_id. We use this so that that we have a
        consistent room_id across test runs that hashes to the same value and will be
        sharded to a known worker in the tests.
        """

        # We control the room ID generation by patching out the
        # `_generate_room_id` method
        with patch(
            "synapse.handlers.room.RoomCreationHandler._generate_room_id"
        ) as mock:
            mock.side_effect = lambda: room_id
            self.helper.create_room_as(user_id, tok=tok)

    def test_concurrent_event_insert(self) -> None:
        """
        TODO

        Normally, the `events` stream is covered by a single "event_perister" worker but
        it does experimentally support multiple workers, where load is sharded
        between them by room ID.

        If we don't pay special attention to this, we will see errors like the following:
        ```
        psycopg2.errors.SerializationFailure: could not serialize access due to concurrent update
        CONTEXT:  SQL statement "UPDATE event_stats SET total_event_count = total_event_count + 1"
        ```

        This is a regression test for https://github.com/element-hq/synapse/issues/18349
        """
        worker_hs1 = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        worker_hs2 = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker3"},
        )

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(
            is_public=False,
            tok=user1_tok,
        )

        block = True

        # Create a transaction that interacts with the `event_stats` table
        def _todo_txn(
            txn: LoggingTransaction,
        ) -> None:
            nonlocal block
            while block:
                logger.info("qwer")
                # txn.execute(
                #     "UPDATE event_stats SET total_event_count = total_event_count + 1",
                # )
                time.sleep(0.1)
            logger.info("qwer done")

            # We need to return something, so we return None.
            return None

        # Start a transaction that is interacting with the `event_stats` table
        #
        # Try from worker2 which may have it's own thread pool.
        worker2_store = worker_hs2.get_datastores().main
        start_txn = run_in_background(
            worker2_store.db_pool.runInteraction, "test", _todo_txn
        )

        # Then in room1 (handled by worker1) we send an event.
        logger.info("asdf1")
        _event_response = self.helper.send(room_id1, "activity", tok=user1_tok)
        logger.info("asdf2")

        block = False

        self.get_success(start_txn)
        # self.pump(0.1)

    def test_sharded_event_persisters(self) -> None:
        """
        TODO

        The test creates three event persister workers and a room that is sharded to
        each worker.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker3"},
        )

        # Specially crafted room IDs that get persisted on different workers.
        #
        # Sharded to worker1
        room_id1 = "!fooo:test"
        # Sharded to worker2
        room_id2 = "!bar:test"
        # Sharded to worker3
        room_id3 = "!quux:test"

        # Create rooms on the different workers.
        self._create_room(room_id1, user1_id, user1_tok)
        self._create_room(room_id2, user1_id, user1_tok)
        self._create_room(room_id3, user1_id, user1_tok)

        # Ensure that the events were sharded to different workers.
        pos1 = self.get_success(
            self.store.get_position_for_event(
                self.get_success(
                    self.store.get_create_event_for_room(room_id1)
                ).event_id
            )
        )
        self.assertEqual(pos1.instance_name, "worker1")
        pos2 = self.get_success(
            self.store.get_position_for_event(
                self.get_success(
                    self.store.get_create_event_for_room(room_id2)
                ).event_id
            )
        )
        self.assertEqual(pos2.instance_name, "worker2")
        pos3 = self.get_success(
            self.store.get_position_for_event(
                self.get_success(
                    self.store.get_create_event_for_room(room_id3)
                ).event_id
            )
        )
        self.assertEqual(pos3.instance_name, "worker3")

        def send_events_in_room_id(room_id: str) -> None:
            for i in range(
                2
                # 10
            ):
                logger.info("Sending event %s in %s", i, room_id)
                # self.helper.send(room_id1, f"activity{i}", tok=user1_tok)
                defer_to_thread(
                    self.reactor,
                    self.helper.send,
                    room_id,
                    f"activity{i}",
                    tok=user1_tok,
                )

        # Start creating events in the room at the same time.
        wait_send_events_in_room1 = defer_to_thread(
            self.reactor, send_events_in_room_id, room_id1
        )
        wait_send_events_in_room2 = defer_to_thread(
            self.reactor, send_events_in_room_id, room_id2
        )
        wait_send_events_in_room3 = defer_to_thread(
            self.reactor, send_events_in_room_id, room_id3
        )

        # self.pump(0.1)

        # Wait for the events to be sent
        self.get_success(wait_send_events_in_room1)
        self.get_success(wait_send_events_in_room2)
        self.get_success(wait_send_events_in_room3)
