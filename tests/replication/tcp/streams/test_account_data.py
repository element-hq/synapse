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

from synapse.replication.tcp.streams._base import (
    _STREAM_UPDATE_TARGET_ROW_COUNT,
    AccountDataStream,
)

from tests.replication._base import BaseStreamTestCase


class AccountDataStreamTestCase(BaseStreamTestCase):
    def test_update_function_room_account_data_limit(self) -> None:
        """Test replication with many room account data updates"""
        store = self.hs.get_datastores().main

        # generate lots of account data updates
        updates = []
        for i in range(_STREAM_UPDATE_TARGET_ROW_COUNT + 5):
            update = "m.test_type.%i" % (i,)
            self.get_success(
                store.add_account_data_to_room("test_user", "test_room", update, {})
            )
            updates.append(update)

        # also one global update
        self.get_success(store.add_account_data_for_user("test_user", "m.global", {}))

        # check we're testing what we think we are: no rows should yet have been
        # received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # now reconnect to pull the updates
        self.reconnect()
        self.replicate()

        # we should have received all the expected rows in the right order
        received_rows = self.test_handler.received_rdata_rows

        for t in updates:
            (stream_name, token, row) = received_rows.pop(0)
            self.assertEqual(stream_name, AccountDataStream.NAME)
            self.assertIsInstance(row, AccountDataStream.AccountDataStreamRow)
            self.assertEqual(row.data_type, t)
            self.assertEqual(row.room_id, "test_room")

        (stream_name, token, row) = received_rows.pop(0)
        self.assertIsInstance(row, AccountDataStream.AccountDataStreamRow)
        self.assertEqual(row.data_type, "m.global")
        self.assertIsNone(row.room_id)

        self.assertEqual([], received_rows)

    def test_update_function_global_account_data_limit(self) -> None:
        """Test replication with many global account data updates"""
        store = self.hs.get_datastores().main

        # generate lots of account data updates
        updates = []
        for i in range(_STREAM_UPDATE_TARGET_ROW_COUNT + 5):
            update = "m.test_type.%i" % (i,)
            self.get_success(store.add_account_data_for_user("test_user", update, {}))
            updates.append(update)

        # also one per-room update
        self.get_success(
            store.add_account_data_to_room("test_user", "test_room", "m.per_room", {})
        )

        # tell the notifier to catch up to avoid duplicate rows.
        # workaround for https://github.com/matrix-org/synapse/issues/7360
        # FIXME remove this when the above is fixed
        self.replicate()

        # check we're testing what we think we are: no rows should yet have been
        # received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # now reconnect to pull the updates
        self.reconnect()
        self.replicate()

        # we should have received all the expected rows in the right order
        received_rows = self.test_handler.received_rdata_rows

        for t in updates:
            (stream_name, token, row) = received_rows.pop(0)
            self.assertEqual(stream_name, AccountDataStream.NAME)
            self.assertIsInstance(row, AccountDataStream.AccountDataStreamRow)
            self.assertEqual(row.data_type, t)
            self.assertIsNone(row.room_id)

        (stream_name, token, row) = received_rows.pop(0)
        self.assertIsInstance(row, AccountDataStream.AccountDataStreamRow)
        self.assertEqual(row.data_type, "m.per_room")
        self.assertEqual(row.room_id, "test_room")

        self.assertEqual([], received_rows)
