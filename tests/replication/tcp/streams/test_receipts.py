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

# type: ignore


from synapse.replication.tcp.streams._base import ReceiptsStream

from tests.replication._base import BaseStreamTestCase

USER_ID = "@feeling:blue"


class ReceiptsStreamTestCase(BaseStreamTestCase):
    def test_receipt(self):
        self.reconnect()

        # tell the master to send a new receipt
        self.get_success(
            self.hs.get_datastores().main.insert_receipt(
                "!room:blue",
                "m.read",
                USER_ID,
                ["$event:blue"],
                thread_id=None,
                data={"a": 1},
            )
        )
        self.replicate()

        # there should be one RDATA command
        received_receipt_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ReceiptsStream.NAME
        ]
        self.assertEqual(
            len(received_receipt_rows),
            1,
            "Expected exactly one row for the receipts stream",
        )
        (stream_name, token, row) = received_receipt_rows[0]
        self.assertEqual(stream_name, ReceiptsStream.NAME)
        self.assertEqual("!room:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event:blue", row.event_id)
        self.assertIsNone(row.thread_id)
        self.assertEqual({"a": 1}, row.data)
        # Clear out the received rows that we've checked so we can check for new ones later
        self.test_handler.received_rdata_rows.clear()

        # Now let's disconnect and insert some data.
        self.disconnect()

        self.get_success(
            self.hs.get_datastores().main.insert_receipt(
                "!room2:blue",
                "m.read",
                USER_ID,
                ["$event2:foo"],
                thread_id=None,
                data={"a": 2},
            )
        )
        self.replicate()

        # Not yet connected: no rows should yet have been received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # Now reconnect and pull the updates
        self.reconnect()
        self.replicate()

        # We should now have caught up and get the missing data
        received_receipt_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ReceiptsStream.NAME
        ]
        self.assertEqual(
            len(received_receipt_rows),
            1,
            "Expected exactly one row for the receipts stream",
        )
        (stream_name, token, row) = received_receipt_rows[0]
        self.assertEqual(stream_name, ReceiptsStream.NAME)
        self.assertEqual(token, 3)
        self.assertEqual("!room2:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event2:foo", row.event_id)
        self.assertEqual({"a": 2}, row.data)
