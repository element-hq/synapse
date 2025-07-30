#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import logging
from unittest.mock import patch

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.util import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import make_request

logger = logging.getLogger(__name__)


class EventPersisterShardTestCase(BaseMultiWorkerStreamTestCase):
    """Checks event persisting sharding works"""

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Register a user who sends a message that we'll get notified about
        self.other_user_id = self.register_user("otheruser", "pass")
        self.other_access_token = self.login("otheruser", "pass")

        self.room_creator = self.hs.get_room_creation_handler()
        self.store = hs.get_datastores().main

    def default_config(self) -> dict:
        conf = super().default_config()
        conf["stream_writers"] = {"events": ["worker1", "worker2"]}
        conf["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
        }
        return conf

    def _create_room(self, room_id: str, user_id: str, tok: str) -> None:
        """Create a room with given room_id"""

        # We control the room ID generation by patching out the
        # `_generate_room_id` method
        with patch(
            "synapse.handlers.room.RoomCreationHandler._generate_room_id"
        ) as mock:
            mock.side_effect = lambda: room_id
            self.helper.create_room_as(user_id, tok=tok)

    def test_basic(self) -> None:
        """Simple test to ensure that multiple rooms can be created and joined,
        and that different rooms get handled by different instances.
        """

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        persisted_on_1 = False
        persisted_on_2 = False

        store = self.hs.get_datastores().main

        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        # Keep making new rooms until we see rooms being persisted on both
        # workers.
        for _ in range(10):
            # Create a room
            room = self.helper.create_room_as(user_id, tok=access_token)

            # The other user joins
            self.helper.join(
                room=room, user=self.other_user_id, tok=self.other_access_token
            )

            # The other user sends some messages
            rseponse = self.helper.send(room, body="Hi!", tok=self.other_access_token)
            event_id = rseponse["event_id"]

            # The event position includes which instance persisted the event.
            pos = self.get_success(store.get_position_for_event(event_id))

            persisted_on_1 |= pos.instance_name == "worker1"
            persisted_on_2 |= pos.instance_name == "worker2"

            if persisted_on_1 and persisted_on_2:
                break

        self.assertTrue(persisted_on_1)
        self.assertTrue(persisted_on_2)

    def test_vector_clock_token(self) -> None:
        """Tests that using a stream token with a vector clock component works
        correctly with basic /sync and /messages usage.
        """

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        worker_hs2 = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        sync_hs = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "sync"},
        )
        sync_hs_site = self._hs_to_site[sync_hs]

        # Specially selected room IDs that get persisted on different workers.
        room_id1 = "!foo:test"
        room_id2 = "!baz:test"

        self.assertEqual(
            self.hs.config.worker.events_shard_config.get_instance(room_id1), "worker1"
        )
        self.assertEqual(
            self.hs.config.worker.events_shard_config.get_instance(room_id2), "worker2"
        )

        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        store = self.hs.get_datastores().main

        # Create two room on the different workers.
        self._create_room(room_id1, user_id, access_token)
        self._create_room(room_id2, user_id, access_token)

        # The other user joins
        self.helper.join(
            room=room_id1, user=self.other_user_id, tok=self.other_access_token
        )
        self.helper.join(
            room=room_id2, user=self.other_user_id, tok=self.other_access_token
        )

        # Do an initial sync so that we're up to date.
        channel = make_request(
            self.reactor, sync_hs_site, "GET", "/sync", access_token=access_token
        )
        next_batch = channel.json_body["next_batch"]

        # We now gut wrench into the events stream MultiWriterIdGenerator on
        # worker2 to mimic it getting stuck persisting an event. This ensures
        # that when we send an event on worker1 we end up in a state where
        # worker2 events stream position lags that on worker1, resulting in a
        # RoomStreamToken with a non-empty instance map component.
        #
        # Worker2's event stream position will not advance until we call
        # __aexit__ again.
        worker_store2 = worker_hs2.get_datastores().main
        assert isinstance(worker_store2._stream_id_gen, MultiWriterIdGenerator)

        actx = worker_store2._stream_id_gen.get_next()
        self.get_success(actx.__aenter__())

        response = self.helper.send(room_id1, body="Hi!", tok=self.other_access_token)
        first_event_in_room1 = response["event_id"]

        # Assert that the current stream token has an instance map component, as
        # we are trying to test vector clock tokens.
        room_stream_token = store.get_room_max_token()
        self.assertNotEqual(len(room_stream_token.instance_map), 0)

        # Check that syncing still gets the new event, despite the gap in the
        # stream IDs.
        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            f"/sync?since={next_batch}",
            access_token=access_token,
        )

        # We should only see the new event and nothing else
        self.assertIn(room_id1, channel.json_body["rooms"]["join"])
        self.assertNotIn(room_id2, channel.json_body["rooms"]["join"])

        events = channel.json_body["rooms"]["join"][room_id1]["timeline"]["events"]
        self.assertListEqual(
            [first_event_in_room1], [event["event_id"] for event in events]
        )

        # Get the next batch and makes sure its a vector clock style token.
        vector_clock_token = channel.json_body["next_batch"]
        self.assertTrue(vector_clock_token.startswith("m"))

        # Now that we've got a vector clock token we finish the fake persisting
        # an event we started above.
        self.get_success(actx.__aexit__(None, None, None))

        # Now try and send an event to the other rooom so that we can test that
        # the vector clock style token works as a `since` token.
        response = self.helper.send(room_id2, body="Hi!", tok=self.other_access_token)
        first_event_in_room2 = response["event_id"]

        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            f"/sync?since={vector_clock_token}",
            access_token=access_token,
        )

        self.assertNotIn(room_id1, channel.json_body["rooms"]["join"])
        self.assertIn(room_id2, channel.json_body["rooms"]["join"])

        events = channel.json_body["rooms"]["join"][room_id2]["timeline"]["events"]
        self.assertListEqual(
            [first_event_in_room2], [event["event_id"] for event in events]
        )

        next_batch = channel.json_body["next_batch"]

        # We also want to test that the vector clock style token works with
        # pagination. We do this by sending a couple of new events into the room
        # and syncing again to get a prev_batch token for each room, then
        # paginating from there back to the vector clock token.
        self.helper.send(room_id1, body="Hi again!", tok=self.other_access_token)
        self.helper.send(room_id2, body="Hi again!", tok=self.other_access_token)

        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            f"/sync?since={next_batch}",
            access_token=access_token,
        )

        prev_batch1 = channel.json_body["rooms"]["join"][room_id1]["timeline"][
            "prev_batch"
        ]
        prev_batch2 = channel.json_body["rooms"]["join"][room_id2]["timeline"][
            "prev_batch"
        ]

        # Paginating back in the first room should not produce any results, as
        # no events have happened in it. This tests that we are correctly
        # filtering results based on the vector clock portion.
        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            "/rooms/{}/messages?from={}&to={}&dir=b".format(
                room_id1, prev_batch1, vector_clock_token
            ),
            access_token=access_token,
        )
        self.assertListEqual([], channel.json_body["chunk"])

        # Paginating back on the second room should produce the first event
        # again. This tests that pagination isn't completely broken.
        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            "/rooms/{}/messages?from={}&to={}&dir=b".format(
                room_id2, prev_batch2, vector_clock_token
            ),
            access_token=access_token,
        )
        self.assertEqual(len(channel.json_body["chunk"]), 1)
        self.assertEqual(
            channel.json_body["chunk"][0]["event_id"], first_event_in_room2
        )

        # Paginating forwards should give the same results
        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            "/rooms/{}/messages?from={}&to={}&dir=f".format(
                room_id1, vector_clock_token, prev_batch1
            ),
            access_token=access_token,
        )
        self.assertListEqual([], channel.json_body["chunk"])

        channel = make_request(
            self.reactor,
            sync_hs_site,
            "GET",
            "/rooms/{}/messages?from={}&to={}&dir=f".format(
                room_id2,
                vector_clock_token,
                prev_batch2,
            ),
            access_token=access_token,
        )
        self.assertEqual(len(channel.json_body["chunk"]), 1)
        self.assertEqual(
            channel.json_body["chunk"][0]["event_id"], first_event_in_room2
        )
