#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from synapse.replication.tcp.streams import QuarantinedMediaStream
from synapse.types import UserID

from tests.replication._base import BaseMultiWorkerStreamTestCase


class QuarantinedMediaWorkerWriterTestCase(BaseMultiWorkerStreamTestCase):
    """Checks that the quarantined_media stream is replicated when the
    configured stream writer is a worker rather than the main process.
    """

    def default_config(self) -> dict:
        conf = super().default_config()
        conf["stream_writers"] = {"quarantined_media_changes": ["worker1"]}
        conf["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
        }
        return conf

    def test_quarantine_on_worker_writer_replicates_to_main(self) -> None:
        main_store = self.hs.get_datastores().main

        worker_hs = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "worker1"}
        )
        worker_store = worker_hs.get_datastores().main

        # The worker must consider itself a source of the stream...
        self.assertIn(
            QuarantinedMediaStream.NAME,
            {
                stream.NAME
                for stream in worker_hs.get_replication_command_handler().get_streams_to_replicate()
            },
        )
        # ... and the main process must not, as it isn't a writer.
        self.assertNotIn(
            QuarantinedMediaStream.NAME,
            {
                stream.NAME
                for stream in self.hs.get_replication_command_handler().get_streams_to_replicate()
            },
        )

        # Quarantining only records a change for media that exists.
        self.get_success(
            main_store.store_local_media(
                media_id="media_id1",
                media_type="text/plain",
                time_now_ms=self.clock.time_msec(),
                upload_name=None,
                media_length=100,
                user_id=UserID.from_string("@user:test"),
            )
        )

        initial_token = main_store.get_current_quarantined_media_stream_id()

        # Quarantine the media on the worker, i.e. the configured writer.
        self.get_success(
            worker_store.quarantine_media_by_id("test", "media_id1", "@admin:test")
        )

        self.replicate()

        # The main process only learns of the new stream ID over replication,
        # even though the two instances share a database.
        self.assertEqual(
            main_store.get_current_quarantined_media_stream_id(),
            initial_token + 1,
        )
