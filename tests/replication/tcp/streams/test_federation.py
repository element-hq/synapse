#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from synapse.federation.send_queue import EduRow
from synapse.replication.tcp.streams.federation import FederationStream

from tests.replication._base import BaseStreamTestCase


class FederationStreamTestCase(BaseStreamTestCase):
    def _get_worker_hs_config(self) -> dict:
        # enable federation sending on the worker
        config = super()._get_worker_hs_config()
        config["worker_name"] = "federation_sender1"
        config["federation_sender_instances"] = ["federation_sender1"]
        return config

    def test_catchup(self) -> None:
        """Basic test of catchup on reconnect

        Makes sure that updates sent while we are offline are received later.
        """
        fed_sender = self.hs.get_federation_sender()

        # Send an update before we connect
        fed_sender.build_and_send_edu("testdest", "m.test_edu", {"a": "b"})

        # Now reconnect and pull the updates
        self.reconnect()
        # FIXME: This seems odd, why aren't we calling `self.replicate()` here? but also
        # doing so, causes other assumptions to fail (multiple HTTP replication attempts
        # are made).
        self.reactor.advance(0)

        # Check we're testing what we think we are: no rows should yet have been
        # received
        #
        # Filter the updates to only include typing changes
        received_federation_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == FederationStream.NAME
        ]
        self.assertEqual(received_federation_rows, [])

        # We should now see an attempt to connect to the master
        request = self.handle_http_replication_attempt()
        self.assert_request_is_get_repl_stream_updates(request, FederationStream.NAME)

        # we should have received an update row
        received_federation_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == FederationStream.NAME
        ]
        self.assertEqual(
            len(received_federation_rows),
            1,
            "Expected exactly one row for the federation stream",
        )
        (stream_name, token, row) = received_federation_rows[0]
        self.assertEqual(stream_name, FederationStream.NAME)
        self.assertIsInstance(row, FederationStream.FederationStreamRow)
        self.assertEqual(row.type, EduRow.TypeId)
        edurow = EduRow.from_data(row.data)
        self.assertEqual(edurow.edu.edu_type, "m.test_edu")
        self.assertEqual(edurow.edu.origin, self.hs.hostname)
        self.assertEqual(edurow.edu.destination, "testdest")
        self.assertEqual(edurow.edu.content, {"a": "b"})
        # Clear out the received rows that we've checked so we can check for new ones later
        self.test_handler.received_rdata_rows.clear()

        # additional updates should be transferred without an HTTP hit
        fed_sender.build_and_send_edu("testdest", "m.test1", {"c": "d"})
        # Pull in the updates
        self.replicate()

        # there should be no http hit
        self.assertEqual(len(self.reactor.tcpClients), 0)

        # ... but we should have a row
        received_federation_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == FederationStream.NAME
        ]
        self.assertEqual(
            len(received_federation_rows),
            1,
            "Expected exactly one row for the federation stream",
        )
        (stream_name, token, row) = received_federation_rows[0]
        self.assertEqual(stream_name, FederationStream.NAME)
        self.assertIsInstance(row, FederationStream.FederationStreamRow)
        self.assertEqual(row.type, EduRow.TypeId)
        edurow = EduRow.from_data(row.data)
        self.assertEqual(edurow.edu.edu_type, "m.test1")
        self.assertEqual(edurow.edu.origin, self.hs.hostname)
        self.assertEqual(edurow.edu.destination, "testdest")
        self.assertEqual(edurow.edu.content, {"c": "d"})
