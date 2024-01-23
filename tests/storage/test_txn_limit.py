#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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
from synapse.storage.types import Cursor
from synapse.util import Clock

from tests import unittest


class SQLTransactionLimitTestCase(unittest.HomeserverTestCase):
    """Test SQL transaction limit doesn't break transactions."""

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver(db_txn_limit=1000)

    def test_config(self) -> None:
        db_config = self.hs.config.database.get_single_database()
        self.assertEqual(db_config.config["txn_limit"], 1000)

    def test_select(self) -> None:
        def do_select(txn: Cursor) -> None:
            txn.execute("SELECT 1")

        db_pool = self.hs.get_datastores().databases[0]

        # force txn limit to roll over at least once
        for _ in range(1001):
            self.get_success_or_raise(db_pool.runInteraction("test_select", do_select))
