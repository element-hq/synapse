#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
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


from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import PostgresEngine
from synapse.util import Clock

from tests import unittest


class UserFiltersStoreTestCase(unittest.HomeserverTestCase):
    """
    Test background migration that copies entries from column user_id to full_user_id, adding
    the hostname in the process.
    """

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_bg_migration(self) -> None:
        updater = self.hs.get_datastores().main.db_pool.updates

        # drop the constraint so we can insert nulls in full_user_id to populate the test
        if isinstance(self.store.database_engine, PostgresEngine):

            def f(txn: LoggingTransaction) -> None:
                txn.execute(
                    "ALTER TABLE user_filters DROP CONSTRAINT full_user_id_not_null"
                )

            self.get_success(self.store.db_pool.runInteraction("", f))

        for i in range(70):
            self.get_success(
                self.store.db_pool.simple_insert(
                    "user_filters",
                    {
                        "user_id": f"hello{i:02}",
                        "filter_id": i,
                        "filter_json": bytearray(i),
                    },
                )
            )

        # re-add the constraint so that when it's validated it actually exists
        if isinstance(self.store.database_engine, PostgresEngine):

            def f(txn: LoggingTransaction) -> None:
                txn.execute(
                    "ALTER TABLE user_filters ADD CONSTRAINT full_user_id_not_null CHECK (full_user_id IS NOT NULL) NOT VALID"
                )

            self.get_success(self.store.db_pool.runInteraction("", f))

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={
                    "update_name": "populate_full_user_id_user_filters",
                    "progress_json": "{}",
                },
            )
        )

        self.get_success(
            updater.run_background_updates(False),
        )

        expected_values = []
        for i in range(70):
            expected_values.append((f"@hello{i:02}:{self.hs.hostname}",))

        res = self.get_success(
            self.store.db_pool.execute(
                "", "SELECT full_user_id from user_filters ORDER BY full_user_id"
            )
        )
        self.assertEqual(len(res), len(expected_values))
        self.assertEqual(res, expected_values)
