#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import AsyncMock, patch

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.utils import USE_POSTGRES_FOR_TESTS

logger = logging.getLogger(__name__)


class ShardedStickyEventsTestCase(BaseMultiWorkerStreamTestCase):
    """
    Tests for sharded Sticky Events.
    """

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        # We need the JSON functionality in SQLite
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        conf["experimental_features"] = {
            # Sliding Sync
            "msc3575_enabled": True,
            # Sticky Events
            "msc4354_enabled": True,
        }
        conf["stream_writers"] = {"events": ["worker1", "worker2"]}
        conf["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
        }
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION`, see
        # `SlidingSyncBase`.
        self.store.have_finished_sliding_sync_background_jobs = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

        self.user_id = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

    def _setup_workers_and_rooms(self) -> None:
        """
        Start the two event persisters and create a room on each of them.
        """
        self.make_worker_hs("synapse.app.generic_worker", {"worker_name": "worker1"})
        self.worker_hs2 = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "worker2"}
        )

        # Specially selected room IDs that get persisted on different workers.
        self.room_id1 = "!foo:test"
        self.room_id2 = "!baz:test"
        self.assertEqual(
            self.hs.config.worker.events_shard_config.get_instance(self.room_id1),
            "worker1",
        )
        self.assertEqual(
            self.hs.config.worker.events_shard_config.get_instance(self.room_id2),
            "worker2",
        )
        self._create_room(self.room_id1)
        self._create_room(self.room_id2)

    def _create_room(self, room_id: str) -> None:
        """
        Create a room with the given room ID, so that we control which event
        persister ends up owning it.
        """
        with patch(
            "synapse.handlers.room.RoomCreationHandler._generate_room_id"
        ) as mock:
            mock.side_effect = lambda: room_id
            self.helper.create_room_as(self.user_id, tok=self.tok)

    def _send_sticky_event(self, room_id: str, body: str) -> str:
        return self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=5),
            content={"body": body, "msgtype": "m.text"},
            tok=self.tok,
        )["event_id"]

    @contextmanager
    def _stalled_sticky_writer(self) -> Iterator[None]:
        """
        Manipulate worker2's sticky events `MultiWriterIdGenerator` to mimic
        it getting stuck part way through persisting a sticky event.

        Whilst the context manager is entered, worker2's sticky events stream position
        will not advance.
        This stops the 'all writers have persisted up to' position from advancing as well.
        """
        worker_store2 = self.worker_hs2.get_datastores().main
        stalled_write = worker_store2._sticky_events_id_gen.get_next()

        self.get_success(stalled_write.__aenter__())
        try:
            yield
        finally:
            self.get_success(stalled_write.__aexit__(None, None, None))

    def test_torn_oldschool_sync(self) -> None:
        """
        Tests that a sticky event must not be presented in two sync responses
        as a 'torn' sync response. This test is for oldschool sync.

        Specifically, a sticky event must not arrive in the timeline of
        the first sync response and then in the sticky events section in
        a subsequent sync response.
        (This is a specific failure mode that occurred when the reader of the
        sticky events stream was not shard-aware.)
        """
        self._setup_workers_and_rooms()

        # Get our initial sync out of the way
        channel = self.make_request("GET", "/sync", access_token=self.tok)
        self.assertEqual(channel.code, 200, channel.result)
        since = channel.json_body["next_batch"]

        with self._stalled_sticky_writer():
            sticky_event_id = self._send_sticky_event(self.room_id1, "sticky message")

            # First response of the torn syncs: the event comes down the timeline, because the
            # events stream token covers worker1's write despite worker2 lagging.
            channel = self.make_request(
                "GET", f"/sync?since={since}", access_token=self.tok
            )
            self.assertEqual(channel.code, 200, channel.result)
            room = channel.json_body["rooms"]["join"][self.room_id1]
            self.assertIn(
                sticky_event_id,
                [event["event_id"] for event in room["timeline"]["events"]],
                room,
            )
            # Having been sent it in the timeline, we shouldn't also be sent it in
            # the sticky section of the same response.
            self.assertNotIn("msc4354_sticky", room, room)
            since = channel.json_body["next_batch"]

        # worker2 catches up, so an unsharded sticky events position is now free to
        # advance past worker1's write.
        self.replicate()

        # Second response of the torn syncs: the sticky event shouldn't come down
        # in the sticky event section.
        channel = self.make_request(
            "GET", f"/sync?since={since}", access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.result)
        room = channel.json_body.get("rooms", {}).get("join", {}).get(self.room_id1, {})
        self.assertNotIn(
            "msc4354_sticky",
            room,
            f"sticky event {sticky_event_id} was sent down the timeline in the "
            f"previous sync, so we should not expect to see a sticky section in this sync: "
            f"{room}",
        )

    def test_torn_sliding_sync(self) -> None:
        """
        Tests that a sticky event must not be presented in two sync responses
        as a 'torn' sync response. This test is for sliding sync.

        Specifically, a sticky event must not arrive in the timeline of
        the first sync response and then in the sticky events section in
        a subsequent sync response.
        (This is a specific failure mode that occurred when the reader of the
        sticky events stream was not shard-aware.)
        """
        self._setup_workers_and_rooms()

        sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"
        sync_body: JsonDict = {
            "lists": {
                "main": {
                    "ranges": [[0, 99]],
                    "required_state": [],
                    "timeline_limit": 10,
                }
            },
            "extensions": {"org.matrix.msc4354.sticky_events": {"enabled": True}},
        }

        # Initial sync
        channel = self.make_request(
            "POST", sync_endpoint, sync_body, access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.result)
        pos = channel.json_body["pos"]
        sync_body["extensions"]["org.matrix.msc4354.sticky_events"]["since"] = (
            channel.json_body["extensions"]["org.matrix.msc4354.sticky_events"][
                "next_batch"
            ]
        )

        with self._stalled_sticky_writer():
            sticky_event_id = self._send_sticky_event(self.room_id1, "sticky message")

            # First response of the torn syncs: the event comes down the room timeline.
            channel = self.make_request(
                "POST", f"{sync_endpoint}?pos={pos}", sync_body, access_token=self.tok
            )
            self.assertEqual(channel.code, 200, channel.result)
            pos = channel.json_body["pos"]
            room = channel.json_body["rooms"][self.room_id1]
            self.assertIn(
                sticky_event_id,
                [event["event_id"] for event in room["timeline"]],
                room,
            )

            # ... and so should have been deduplicated out of the sticky section of
            # the same response, leaving an empty room entry behind.
            sticky_extension = channel.json_body["extensions"][
                "org.matrix.msc4354.sticky_events"
            ]
            next_batch = sticky_extension["next_batch"]
            self.assertEqual(
                sticky_extension,
                {
                    "rooms": {self.room_id1: {"events": []}},
                    "next_batch": next_batch,
                },
                sticky_extension,
            )
            # The token should carry the per-writer positions, as worker2 is stalled.
            self.assertTrue(next_batch.startswith("sticky_m"), next_batch)

            sync_body["extensions"]["org.matrix.msc4354.sticky_events"]["since"] = (
                next_batch
            )

        # worker2 catches up, so an unsharded sticky events position is now free to
        # advance past worker1's write.
        self.replicate()

        # Second response of the torn syncs: the sticky event shouldn't come down
        # in the sticky event section.
        channel = self.make_request(
            "POST", f"{sync_endpoint}?pos={pos}", sync_body, access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.result)
        # There is nothing left to send, so the extension should be omitted entirely.
        self.assertNotIn(
            "org.matrix.msc4354.sticky_events",
            channel.json_body["extensions"],
            f"sticky event {sticky_event_id} was sent down the timeline in the "
            f"previous sync, so no sticky events extension should be sent now: "
            f"{channel.json_body['extensions']}",
        )
