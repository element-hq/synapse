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

from unittest.mock import Mock

from synapse.replication.tcp.streams._base import ReceiptsStream

from tests.replication._base import BaseStreamTestCase

USER_ID = "@feeling:blue"


class ReceiptsStreamTestCase(BaseStreamTestCase):
    def _build_replication_data_handler(self):
        return Mock(wraps=super()._build_replication_data_handler())

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
        self.test_handler.on_rdata.assert_called_once()
        stream_name, _, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(1, len(rdata_rows))
        row: ReceiptsStream.ReceiptsStreamRow = rdata_rows[0]
        self.assertEqual("!room:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event:blue", row.event_id)
        self.assertIsNone(row.thread_id)
        self.assertEqual({"a": 1}, row.data)

        # Now let's disconnect and insert some data.
        self.disconnect()

        self.test_handler.on_rdata.reset_mock()

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

        # Nothing should have happened as we are disconnected
        self.test_handler.on_rdata.assert_not_called()

        self.reconnect()
        self.pump(0.1)

        # We should now have caught up and get the missing data
        self.test_handler.on_rdata.assert_called_once()
        stream_name, _, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(token, 3)
        self.assertEqual(1, len(rdata_rows))

        row: ReceiptsStream.ReceiptsStreamRow = rdata_rows[0]
        self.assertEqual("!room2:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event2:foo", row.event_id)
        self.assertEqual({"a": 2}, row.data)
