#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import List, Tuple, cast
from unittest.mock import AsyncMock, Mock

import yaml

from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.background_updates import (
    BackgroundUpdater,
    ForeignKeyConstraint,
    NotNullConstraint,
    run_validate_constraint_and_delete_rows_schema_delta,
)
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import PostgresEngine, Sqlite3Engine
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.unittest import override_config


class BackgroundUpdateTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.updates: BackgroundUpdater = self.hs.get_datastores().main.db_pool.updates
        # the base test class should have run the real bg updates for us
        self.assertTrue(
            self.get_success(self.updates.has_completed_background_updates())
        )

        self.update_handler = Mock()
        self.updates.register_background_update_handler(
            "test_update", self.update_handler
        )
        self.store = self.hs.get_datastores().main

    async def update(self, progress: JsonDict, count: int) -> int:
        duration_ms = 10
        await self.clock.sleep((count * duration_ms) / 1000)
        progress = {"my_key": progress["my_key"] + 1}
        await self.store.db_pool.runInteraction(
            "update_progress",
            self.updates._background_update_progress_txn,
            "test_update",
            progress,
        )
        return count

    def test_do_background_update(self) -> None:
        # the time we claim it takes to update one item when running the update
        duration_ms = 10

        # the target runtime for each bg update
        target_background_update_duration_ms = 100

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        res = self.get_success(
            self.updates.do_next_background_update(False),
            by=0.02,
        )
        self.assertFalse(res)

        # on the first call, we should get run with the default background update size
        self.update_handler.assert_called_once_with(
            {"my_key": 1}, self.updates.default_background_batch_size
        )

        # second step: complete the update
        # we should now get run with a much bigger number of items to update
        async def update(progress: JsonDict, count: int) -> int:
            self.assertEqual(progress, {"my_key": 2})
            self.assertAlmostEqual(
                count,
                target_background_update_duration_ms / duration_ms,
                places=0,
            )
            await self.updates._end_background_update("test_update")
            return count

        self.update_handler.side_effect = update
        self.update_handler.reset_mock()
        result = self.get_success(self.updates.do_next_background_update(False))
        self.assertFalse(result)
        self.update_handler.assert_called_once()

        # third step: we don't expect to be called any more
        self.update_handler.reset_mock()
        result = self.get_success(self.updates.do_next_background_update(False))
        self.assertTrue(result)
        self.assertFalse(self.update_handler.called)

    @override_config(
        yaml.safe_load(
            """
            background_updates:
                default_batch_size: 20
            """
        )
    )
    def test_background_update_default_batch_set_by_config(self) -> None:
        """
        Test that the background update is run with the default_batch_size set by the config
        """

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        res = self.get_success(
            self.updates.do_next_background_update(False),
            by=0.01,
        )
        self.assertFalse(res)

        # on the first call, we should get run with the default background update size specified in the config
        self.update_handler.assert_called_once_with({"my_key": 1}, 20)

    def test_background_update_default_sleep_behavior(self) -> None:
        """
        Test default background update behavior, which is to sleep
        """

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        self.updates.start_doing_background_updates()

        # 2: advance the reactor less than the default sleep duration (1000ms)
        self.reactor.pump([0.5])
        # check that an update has not been run
        self.update_handler.assert_not_called()

        # advance reactor past default sleep duration
        self.reactor.pump([1])
        # check that update has been run
        self.update_handler.assert_called()

    @override_config(
        yaml.safe_load(
            """
            background_updates:
                sleep_duration_ms: 500
            """
        )
    )
    def test_background_update_sleep_set_in_config(self) -> None:
        """
        Test that changing the sleep time in the config changes how long it sleeps
        """

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        self.updates.start_doing_background_updates()

        # 2: advance the reactor less than the configured sleep duration (500ms)
        self.reactor.pump([0.45])
        # check that an update has not been run
        self.update_handler.assert_not_called()

        # advance reactor past config sleep duration but less than default duration
        self.reactor.pump([0.75])
        # check that update has been run
        self.update_handler.assert_called()

    @override_config(
        yaml.safe_load(
            """
            background_updates:
                sleep_enabled: false
            """
        )
    )
    def test_disabling_background_update_sleep(self) -> None:
        """
        Test that disabling sleep in the config results in bg update not sleeping
        """

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        self.updates.start_doing_background_updates()

        # 2: advance the reactor very little
        self.reactor.pump([0.025])
        # check that an update has run
        self.update_handler.assert_called()

    @override_config(
        yaml.safe_load(
            """
            background_updates:
                background_update_duration_ms: 500
            """
        )
    )
    def test_background_update_duration_set_in_config(self) -> None:
        """
        Test that the desired duration set in the config is used in determining batch size
        """
        # Duration of one background update item
        duration_ms = 10

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        self.update_handler.side_effect = self.update
        self.update_handler.reset_mock()
        res = self.get_success(
            self.updates.do_next_background_update(False),
            by=0.02,
        )
        self.assertFalse(res)

        # the first update was run with the default batch size, this should be run with 500ms as the
        # desired duration
        async def update(progress: JsonDict, count: int) -> int:
            self.assertEqual(progress, {"my_key": 2})
            self.assertAlmostEqual(
                count,
                500 / duration_ms,
                places=0,
            )
            await self.updates._end_background_update("test_update")
            return count

        self.update_handler.side_effect = update
        self.get_success(self.updates.do_next_background_update(False))

    @override_config(
        yaml.safe_load(
            """
            background_updates:
                min_batch_size: 5
            """
        )
    )
    def test_background_update_min_batch_set_in_config(self) -> None:
        """
        Test that the minimum batch size set in the config is used
        """
        # a very long-running individual update
        duration_ms = 50

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": '{"my_key": 1}'},
            )
        )

        # Run the update with the long-running update item
        async def update_long(progress: JsonDict, count: int) -> int:
            await self.clock.sleep((count * duration_ms) / 1000)
            progress = {"my_key": progress["my_key"] + 1}
            await self.store.db_pool.runInteraction(
                "update_progress",
                self.updates._background_update_progress_txn,
                "test_update",
                progress,
            )
            return count

        self.update_handler.side_effect = update_long
        self.update_handler.reset_mock()
        res = self.get_success(
            self.updates.do_next_background_update(False),
            by=1,
        )
        self.assertFalse(res)

        # the first update was run with the default batch size, this should be run with minimum batch size
        # as the first items took a very long time
        async def update_short(progress: JsonDict, count: int) -> int:
            self.assertEqual(progress, {"my_key": 2})
            self.assertEqual(count, 5)
            await self.updates._end_background_update("test_update")
            return count

        self.update_handler.side_effect = update_short
        self.get_success(self.updates.do_next_background_update(False))

    def test_failed_update_logs_exception_details(self) -> None:
        needle = "RUH ROH RAGGY"

        def failing_update(progress: JsonDict, count: int) -> int:
            raise Exception(needle)

        self.update_handler.side_effect = failing_update
        self.update_handler.reset_mock()

        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": "{}"},
            )
        )

        with self.assertLogs(level=logging.ERROR) as logs:
            # Expect a back-to-back RuntimeError to be raised
            self.get_failure(self.updates.run_background_updates(False), RuntimeError)

        self.assertTrue(any(needle in log for log in logs.output), logs.output)


class BackgroundUpdateControllerTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.updates: BackgroundUpdater = self.hs.get_datastores().main.db_pool.updates
        # the base test class should have run the real bg updates for us
        self.assertTrue(
            self.get_success(self.updates.has_completed_background_updates())
        )

        self.update_deferred: Deferred[int] = Deferred()
        self.update_handler = Mock(return_value=self.update_deferred)
        self.updates.register_background_update_handler(
            "test_update", self.update_handler
        )

        # Mock out the AsyncContextManager
        class MockCM:
            __aenter__ = AsyncMock(return_value=None)
            __aexit__ = AsyncMock(return_value=None)

        self._update_ctx_manager = MockCM

        # Mock out the `update_handler` callback
        self._on_update = Mock(return_value=self._update_ctx_manager())

        # Define a default batch size value that's not the same as the internal default
        # value (100).
        self._default_batch_size = 500

        # Register the callbacks with more mocks
        self.hs.get_module_api().register_background_update_controller_callbacks(
            on_update=self._on_update,
            min_batch_size=AsyncMock(return_value=self._default_batch_size),
            default_batch_size=AsyncMock(
                return_value=self._default_batch_size,
            ),
        )

    def test_controller(self) -> None:
        store = self.hs.get_datastores().main
        self.get_success(
            store.db_pool.simple_insert(
                "background_updates",
                values={"update_name": "test_update", "progress_json": "{}"},
            )
        )

        # Set the return value for the context manager.
        enter_defer: Deferred[int] = Deferred()
        self._update_ctx_manager.__aenter__ = Mock(return_value=enter_defer)

        # Start the background update.
        do_update_d = ensureDeferred(self.updates.do_next_background_update(True))

        self.pump()

        # `run_update` should have been called, but the update handler won't be
        # called until the `enter_defer` (returned by `__aenter__`) is resolved.
        self._on_update.assert_called_once_with(
            "test_update",
            "master",
            False,
        )
        self.assertFalse(do_update_d.called)
        self.assertFalse(self.update_deferred.called)

        # Resolving the `enter_defer` should call the update handler, which then
        # blocks.
        enter_defer.callback(100)
        self.pump()
        self.update_handler.assert_called_once_with({}, self._default_batch_size)
        self.assertFalse(self.update_deferred.called)
        self._update_ctx_manager.__aexit__.assert_not_called()

        # Resolving the update handler deferred should cause the
        # `do_next_background_update` to finish and return
        self.update_deferred.callback(100)
        self.pump()
        self._update_ctx_manager.__aexit__.assert_called()
        self.get_success(do_update_d)


class BackgroundUpdateValidateConstraintTestCase(unittest.HomeserverTestCase):
    """Tests the validate contraint and delete background handlers."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.updates: BackgroundUpdater = self.hs.get_datastores().main.db_pool.updates
        # the base test class should have run the real bg updates for us
        self.assertTrue(
            self.get_success(self.updates.has_completed_background_updates())
        )

        self.store = self.hs.get_datastores().main

    def test_not_null_constraint(self) -> None:
        # Create the initial tables, where we have some invalid data.
        """Tests adding a not null constraint."""
        table_sql = """
            CREATE TABLE test_constraint(
                a INT PRIMARY KEY,
                b INT
            );
        """
        self.get_success(
            self.store.db_pool.runInteraction(
                "test_not_null_constraint", lambda txn: txn.execute(table_sql)
            )
        )

        # We add an index so that we can check that its correctly recreated when
        # using SQLite.
        index_sql = "CREATE INDEX test_index ON test_constraint(a)"
        self.get_success(
            self.store.db_pool.runInteraction(
                "test_not_null_constraint", lambda txn: txn.execute(index_sql)
            )
        )

        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 1, "b": 1})
        )
        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 2, "b": None})
        )
        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 3, "b": 3})
        )

        # Now lets do the migration

        table2_sqlite = """
            CREATE TABLE test_constraint2(
                a INT PRIMARY KEY,
                b INT,
                CONSTRAINT test_constraint_name CHECK (b is NOT NULL)
            );
        """

        def delta(txn: LoggingTransaction) -> None:
            run_validate_constraint_and_delete_rows_schema_delta(
                txn,
                ordering=1000,
                update_name="test_bg_update",
                table="test_constraint",
                constraint_name="test_constraint_name",
                constraint=NotNullConstraint("b"),
                sqlite_table_name="test_constraint2",
                sqlite_table_schema=table2_sqlite,
            )

        self.get_success(
            self.store.db_pool.runInteraction(
                "test_not_null_constraint",
                delta,
            )
        )

        if isinstance(self.store.database_engine, PostgresEngine):
            # Postgres uses a background update
            self.updates.register_background_validate_constraint_and_delete_rows(
                "test_bg_update",
                table="test_constraint",
                constraint_name="test_constraint_name",
                constraint=NotNullConstraint("b"),
                unique_columns=["a"],
            )

            # Tell the DataStore that it hasn't finished all updates yet
            self.store.db_pool.updates._all_done = False

            # Now let's actually drive the updates to completion
            self.wait_for_background_updates()

        # Check the correct values are in the new table.
        rows = cast(
            List[Tuple[int, int]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    table="test_constraint",
                    keyvalues={},
                    retcols=("a", "b"),
                )
            ),
        )

        self.assertCountEqual(rows, [(1, 1), (3, 3)])

        # And check that invalid rows get correctly rejected.
        self.get_failure(
            self.store.db_pool.simple_insert("test_constraint", {"a": 2, "b": None}),
            exc=self.store.database_engine.module.IntegrityError,
        )

        # Check the index is still there for SQLite.
        if isinstance(self.store.database_engine, Sqlite3Engine):
            # Ensure the index exists in the schema.
            self.get_success(
                self.store.db_pool.simple_select_one_onecol(
                    table="sqlite_master",
                    keyvalues={"tbl_name": "test_constraint"},
                    retcol="name",
                )
            )

    def test_foreign_constraint(self) -> None:
        """Tests adding a not foreign key constraint."""

        # Create the initial tables, where we have some invalid data.
        base_sql = """
            CREATE TABLE base_table(
                b INT PRIMARY KEY
            );
        """

        table_sql = """
            CREATE TABLE test_constraint(
                a INT PRIMARY KEY,
                b INT NOT NULL
            );
        """
        self.get_success(
            self.store.db_pool.runInteraction(
                "test_foreign_key_constraint", lambda txn: txn.execute(base_sql)
            )
        )
        self.get_success(
            self.store.db_pool.runInteraction(
                "test_foreign_key_constraint", lambda txn: txn.execute(table_sql)
            )
        )

        self.get_success(self.store.db_pool.simple_insert("base_table", {"b": 1}))
        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 1, "b": 1})
        )
        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 2, "b": 2})
        )
        self.get_success(self.store.db_pool.simple_insert("base_table", {"b": 3}))
        self.get_success(
            self.store.db_pool.simple_insert("test_constraint", {"a": 3, "b": 3})
        )

        table2_sqlite = """
            CREATE TABLE test_constraint2(
                a INT PRIMARY KEY,
                b INT NOT NULL,
                CONSTRAINT test_constraint_name FOREIGN KEY (b) REFERENCES base_table (b)
            );
        """

        def delta(txn: LoggingTransaction) -> None:
            run_validate_constraint_and_delete_rows_schema_delta(
                txn,
                ordering=1000,
                update_name="test_bg_update",
                table="test_constraint",
                constraint_name="test_constraint_name",
                constraint=ForeignKeyConstraint(
                    "base_table", [("b", "b")], deferred=False
                ),
                sqlite_table_name="test_constraint2",
                sqlite_table_schema=table2_sqlite,
            )

        self.get_success(
            self.store.db_pool.runInteraction(
                "test_foreign_key_constraint",
                delta,
            )
        )

        if isinstance(self.store.database_engine, PostgresEngine):
            # Postgres uses a background update
            self.updates.register_background_validate_constraint_and_delete_rows(
                "test_bg_update",
                table="test_constraint",
                constraint_name="test_constraint_name",
                constraint=ForeignKeyConstraint(
                    "base_table", [("b", "b")], deferred=False
                ),
                unique_columns=["a"],
            )

            # Tell the DataStore that it hasn't finished all updates yet
            self.store.db_pool.updates._all_done = False

            # Now let's actually drive the updates to completion
            self.wait_for_background_updates()

        # Check the correct values are in the new table.
        rows = cast(
            List[Tuple[int, int]],
            self.get_success(
                self.store.db_pool.simple_select_list(
                    table="test_constraint",
                    keyvalues={},
                    retcols=("a", "b"),
                )
            ),
        )
        self.assertCountEqual(rows, [(1, 1), (3, 3)])

        # And check that invalid rows get correctly rejected.
        self.get_failure(
            self.store.db_pool.simple_insert("test_constraint", {"a": 2, "b": 2}),
            exc=self.store.database_engine.module.IntegrityError,
        )
