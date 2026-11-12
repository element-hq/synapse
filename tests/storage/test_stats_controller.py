#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

from unittest.mock import AsyncMock, Mock

from twisted.internet import defer

from synapse.storage.controllers.stats import StatsController
from synapse.storage.engines import PostgresEngine
from tests import unittest


class FakeTxn:
    """Minimal txn that returns canned rows per query, keyed on the table arg."""

    def __init__(self, column_rows):
        self._column_rows = column_rows
        self._last = None

    def execute(self, sql, args):
        (table,) = args
        if "pg_total_relation_size" in sql:
            self._last = ("size", table)
        else:
            self._last = ("column", table)

    def fetchone(self):
        kind, table = self._last
        if kind == "size":
            return (1000,)
        return self._column_rows[table]


class GetRoomDbSizeEstimateTestCase(unittest.TestCase):
    def _controller(self, column_rows):
        stores = Mock()
        stores.main.database_engine = Mock(spec=PostgresEngine)
        stores.state.database_engine = Mock(spec=PostgresEngine)

        async def run_interaction(desc, func, tables, **kwargs):
            return func(FakeTxn(column_rows), tables)

        stores.main.db_pool.runInteraction = AsyncMock(side_effect=run_interaction)
        stores.state.db_pool.runInteraction = AsyncMock(side_effect=run_interaction)

        return StatsController(Mock(), stores)

    def test_null_most_common_vals_is_skipped(self) -> None:
        """A table whose room_id has no MCV list (NULL arrays) must not crash."""
        tables = (
            "event_json",
            "events",
            "event_search",
            "event_edges",
            "event_push_actions",
            "stream_ordering_to_exterm",
            "state_groups_state",
        )
        column_rows = {t: (["!room:test"], [0.5]) for t in tables}
        # event_edges.room_id is entirely NULL -> pg_stats returns NULL arrays.
        column_rows["event_edges"] = (None, None)

        controller = self._controller(column_rows)
        result = self.successResultOf(
            defer.ensureDeferred(controller.get_room_db_size_estimate())
        )

        self.assertEqual(result, [("!room:test", 3000)])
