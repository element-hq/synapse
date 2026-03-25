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

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.types import Cursor
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.storage.util.sequence import (
    LocalSequenceGenerator,
    PostgresSequenceGenerator,
    SequenceGenerator,
)
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase
from tests.utils import USE_POSTGRES_FOR_TESTS


class MultiWriterIdGeneratorBase(HomeserverTestCase):
    positive: bool = True
    tables: list[str] = ["foobar"]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.db_pool: DatabasePool = self.store.db_pool
        self.instances: dict[str, MultiWriterIdGenerator] = {}

        self.get_success(self.db_pool.runInteraction("_setup_db", self._setup_db))

        if USE_POSTGRES_FOR_TESTS:
            self.seq_gen: SequenceGenerator = PostgresSequenceGenerator("foobar_seq")
        else:
            self.seq_gen = LocalSequenceGenerator(lambda _: 0)

    def _setup_db(self, txn: LoggingTransaction) -> None:
        if USE_POSTGRES_FOR_TESTS:
            txn.execute("CREATE SEQUENCE foobar_seq")

        for table in self.tables:
            txn.execute(
                """
                CREATE TABLE %s (
                    stream_id BIGINT NOT NULL,
                    instance_name TEXT NOT NULL,
                    data TEXT
                );
                """
                % (table,)
            )

    def _create_id_generator(
        self,
        instance_name: str = "master",
        writers: list[str] | None = None,
    ) -> MultiWriterIdGenerator:
        def _create(conn: LoggingDatabaseConnection) -> MultiWriterIdGenerator:
            return MultiWriterIdGenerator(
                db_conn=conn,
                db=self.db_pool,
                notifier=self.hs.get_replication_notifier(),
                stream_name="test_stream",
                server_name=self.hs.hostname,
                instance_name=instance_name,
                tables=[(table, "instance_name", "stream_id") for table in self.tables],
                sequence_name="foobar_seq",
                writers=writers or ["master"],
                positive=self.positive,
            )

        self.instances[instance_name] = self.get_success_or_raise(
            self.db_pool.runWithConnection(_create)
        )
        return self.instances[instance_name]

    def _replicate(self, instance_name: str) -> None:
        """Similate a replication event for the given instance."""

        writer = self.instances[instance_name]
        token = writer.get_current_token_for_writer(instance_name)
        for generator in self.instances.values():
            if writer != generator:
                generator.advance(instance_name, token)

    def _replicate_all(self) -> None:
        """Similate a replication event for all instances."""

        for instance_name in self.instances:
            self._replicate(instance_name)

    def _insert_row(
        self, instance_name: str, stream_id: int, table: str | None = None
    ) -> None:
        """Insert one row as the given instance with given stream_id."""

        if table is None:
            table = self.tables[0]

        factor = 1 if self.positive else -1

        def _insert(txn: LoggingTransaction) -> None:
            txn.execute(
                "INSERT INTO %s VALUES (?, ?)" % (table,),
                (
                    stream_id,
                    instance_name,
                ),
            )
            txn.execute(
                """
                INSERT INTO stream_positions VALUES ('test_stream', ?, ?)
                ON CONFLICT (stream_name, instance_name) DO UPDATE SET stream_id = ?
                """,
                (instance_name, stream_id * factor, stream_id * factor),
            )

        self.get_success(self.db_pool.runInteraction("_insert_row", _insert))

    def _insert_rows(
        self,
        instance_name: str,
        number: int,
        table: str | None = None,
        update_stream_table: bool = True,
    ) -> None:
        """Insert N rows as the given instance, inserting with stream IDs pulled
        from the postgres sequence.
        """

        if table is None:
            table = self.tables[0]

        factor = 1 if self.positive else -1

        def _insert(txn: LoggingTransaction) -> None:
            for _ in range(number):
                next_val = self.seq_gen.get_next_id_txn(txn)
                txn.execute(
                    "INSERT INTO %s (stream_id, instance_name) VALUES (?, ?)"
                    % (table,),
                    (next_val, instance_name),
                )

                if update_stream_table:
                    txn.execute(
                        """
                        INSERT INTO stream_positions VALUES ('test_stream', ?,  ?)
                        ON CONFLICT (stream_name, instance_name) DO UPDATE SET stream_id = ?
                        """,
                        (instance_name, next_val * factor, next_val * factor),
                    )

        self.get_success(self.db_pool.runInteraction("_insert_rows", _insert))


class MultiWriterIdGeneratorTestCase(MultiWriterIdGeneratorBase):
    def test_empty(self) -> None:
        """Test an ID generator against an empty database gives sensible
        current positions.
        """

        id_gen = self._create_id_generator()

        # The table is empty so we expect the map for positions to have a dummy
        # minimum value.
        self.assertEqual(id_gen.get_positions(), {"master": 1})

    def test_single_instance(self) -> None:
        """Test that reads and writes from a single process are handled
        correctly.
        """

        # Prefill table with 7 rows written by 'master'
        self._insert_rows("master", 7)

        id_gen = self._create_id_generator()

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        # Try allocating a new ID gen and check that we only see position
        # advanced after we leave the context manager.

        async def _get_next_async() -> None:
            async with id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 8)

                self.assertEqual(id_gen.get_positions(), {"master": 7})
                self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        self.get_success(_get_next_async())

        self.assertEqual(id_gen.get_positions(), {"master": 8})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 8)

    def test_out_of_order_finish(self) -> None:
        """Test that IDs persisted out of order are correctly handled"""

        # Prefill table with 7 rows written by 'master'
        self._insert_rows("master", 7)

        id_gen = self._create_id_generator()

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        ctx1 = id_gen.get_next()
        ctx2 = id_gen.get_next()
        ctx3 = id_gen.get_next()
        ctx4 = id_gen.get_next()

        s1 = self.get_success(ctx1.__aenter__())
        s2 = self.get_success(ctx2.__aenter__())
        s3 = self.get_success(ctx3.__aenter__())
        s4 = self.get_success(ctx4.__aenter__())

        self.assertEqual(s1, 8)
        self.assertEqual(s2, 9)
        self.assertEqual(s3, 10)
        self.assertEqual(s4, 11)

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        self.get_success(ctx2.__aexit__(None, None, None))

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        self.get_success(ctx1.__aexit__(None, None, None))

        self.assertEqual(id_gen.get_positions(), {"master": 9})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 9)

        self.get_success(ctx4.__aexit__(None, None, None))

        self.assertEqual(id_gen.get_positions(), {"master": 9})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 9)

        self.get_success(ctx3.__aexit__(None, None, None))

        self.assertEqual(id_gen.get_positions(), {"master": 11})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 11)

    def test_get_next_txn(self) -> None:
        """Test that the `get_next_txn` function works correctly."""

        # Prefill table with 7 rows written by 'master'
        self._insert_rows("master", 7)

        id_gen = self._create_id_generator()

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        # Try allocating a new ID gen and check that we only see position
        # advanced after we leave the context manager.

        def _get_next_txn(txn: LoggingTransaction) -> None:
            stream_id = id_gen.get_next_txn(txn)
            self.assertEqual(stream_id, 8)

            self.assertEqual(id_gen.get_positions(), {"master": 7})
            self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        self.get_success(self.db_pool.runInteraction("test", _get_next_txn))

        self.assertEqual(id_gen.get_positions(), {"master": 8})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 8)

    def test_restart_during_out_of_order_persistence(self) -> None:
        """Test that restarting a process while another process is writing out
        of order updates are handled correctly.
        """

        # Prefill table with 7 rows written by 'master'
        self._insert_rows("master", 7)

        id_gen = self._create_id_generator()

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        # Persist two rows at once
        ctx1 = id_gen.get_next()
        ctx2 = id_gen.get_next()

        s1 = self.get_success(ctx1.__aenter__())
        s2 = self.get_success(ctx2.__aenter__())

        self.assertEqual(s1, 8)
        self.assertEqual(s2, 9)

        self.assertEqual(id_gen.get_positions(), {"master": 7})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), 7)

        # We finish persisting the second row before restart
        self.get_success(ctx2.__aexit__(None, None, None))

        # We simulate a restart of another worker by just creating a new ID gen.
        id_gen_worker = self._create_id_generator("worker")

        # Restarted worker should not see the second persisted row
        self.assertEqual(id_gen_worker.get_positions(), {"master": 7})
        self.assertEqual(id_gen_worker.get_current_token_for_writer("master"), 7)

        # Now if we persist the first row then both instances should jump ahead
        # correctly.
        self.get_success(ctx1.__aexit__(None, None, None))

        self.assertEqual(id_gen.get_positions(), {"master": 9})
        id_gen_worker.advance("master", 9)
        self.assertEqual(id_gen_worker.get_positions(), {"master": 9})


class WorkerMultiWriterIdGeneratorTestCase(MultiWriterIdGeneratorBase):
    if not USE_POSTGRES_FOR_TESTS:
        skip = "Requires Postgres"

    def _insert_row_with_id(self, instance_name: str, stream_id: int) -> None:
        """Insert one row as the given instance with given stream_id, updating
        the postgres sequence position to match.
        """

        def _insert(txn: LoggingTransaction) -> None:
            txn.execute(
                "INSERT INTO foobar (stream_id, instance_name) VALUES (?, ?)",
                (
                    stream_id,
                    instance_name,
                ),
            )

            txn.execute("SELECT setval('foobar_seq', ?)", (stream_id,))

            txn.execute(
                """
                INSERT INTO stream_positions VALUES ('test_stream', ?, ?)
                ON CONFLICT (stream_name, instance_name) DO UPDATE SET stream_id = ?
                """,
                (instance_name, stream_id, stream_id),
            )

        self.get_success(self.db_pool.runInteraction("_insert_row_with_id", _insert))

    def test_get_persisted_upto_position(self) -> None:
        """Test that `get_persisted_upto_position` correctly tracks updates to
        positions.
        """

        # The following tests are a bit cheeky in that we notify about new
        # positions via `advance` without *actually* advancing the postgres
        # sequence.

        self._insert_row_with_id("first", 3)
        self._insert_row_with_id("second", 5)

        id_gen = self._create_id_generator("worker", writers=["first", "second"])

        self.assertEqual(id_gen.get_positions(), {"first": 3, "second": 5})

        # Min is 3 and there is a gap between 5, so we expect it to be 3.
        self.assertEqual(id_gen.get_persisted_upto_position(), 3)

        # We advance "first" straight to 6. Min is now 5 but there is no gap so
        # we expect it to be 6
        id_gen.advance("first", 6)
        self.assertEqual(id_gen.get_persisted_upto_position(), 6)

        # No gap, so we expect 7.
        id_gen.advance("second", 7)
        self.assertEqual(id_gen.get_persisted_upto_position(), 7)

        # We haven't seen 8 yet, so we expect 7 still.
        id_gen.advance("second", 9)
        self.assertEqual(id_gen.get_persisted_upto_position(), 7)

        # Now that we've seen 7, 8 and 9 we can got straight to 9.
        id_gen.advance("first", 8)
        self.assertEqual(id_gen.get_persisted_upto_position(), 9)

        # Jump forward with gaps. The minimum is 11, even though we haven't seen
        # 10 we know that everything before 11 must be persisted.
        id_gen.advance("first", 11)
        id_gen.advance("second", 15)
        self.assertEqual(id_gen.get_persisted_upto_position(), 11)

    def test_get_persisted_upto_position_get_next(self) -> None:
        """Test that `get_persisted_upto_position` correctly tracks updates to
        positions when `get_next` is called.
        """

        self._insert_row_with_id("first", 3)
        self._insert_row_with_id("second", 5)

        id_gen = self._create_id_generator("first", writers=["first", "second"])

        # When the writer is created, it assumes its own position is the current head of
        # the sequence
        self.assertEqual(id_gen.get_positions(), {"first": 5, "second": 5})

        self.assertEqual(id_gen.get_persisted_upto_position(), 5)

        async def _get_next_async() -> None:
            async with id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 6)
                self.assertEqual(id_gen.get_persisted_upto_position(), 5)

        self.get_success(_get_next_async())

        self.assertEqual(id_gen.get_persisted_upto_position(), 6)

        # We assume that so long as `get_next` does correctly advance the
        # `persisted_upto_position` in this case, then it will be correct in the
        # other cases that are tested above (since they'll hit the same code).

    def test_multi_instance(self) -> None:
        """Test that reads and writes from multiple processes are handled
        correctly.
        """
        self._insert_rows("first", 3)
        first_id_gen = self._create_id_generator("first", writers=["first", "second"])

        self._insert_rows("second", 4)
        second_id_gen = self._create_id_generator("second", writers=["first", "second"])

        self._replicate_all()

        self.assertEqual(first_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(first_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(first_id_gen.get_current_token_for_writer("second"), 7)

        self.assertEqual(second_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 7)

        # Try allocating a new ID gen and check that we only see position
        # advanced after we leave the context manager.

        async def _get_next_async() -> None:
            async with first_id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 8)

                self.assertEqual(
                    first_id_gen.get_positions(), {"first": 3, "second": 7}
                )
                self.assertEqual(
                    second_id_gen.get_positions(), {"first": 3, "second": 7}
                )
                self.assertEqual(first_id_gen.get_persisted_upto_position(), 7)

        self.get_success(_get_next_async())

        self.assertEqual(first_id_gen.get_positions(), {"first": 8, "second": 7})

        # However the ID gen on the second instance won't have seen the update
        self.assertEqual(second_id_gen.get_positions(), {"first": 3, "second": 7})

        # ... but calling `get_next` on the second instance should give a unique
        # stream ID

        async def _get_next_async2() -> None:
            async with second_id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 9)

                self.assertEqual(
                    second_id_gen.get_positions(), {"first": 3, "second": 7}
                )

        self.get_success(_get_next_async2())

        self.assertEqual(second_id_gen.get_positions(), {"first": 3, "second": 9})

        # If the second ID gen gets told about the first, it correctly updates
        second_id_gen.advance("first", 8)
        self.assertEqual(second_id_gen.get_positions(), {"first": 8, "second": 9})

    def test_multi_instance_empty_row(self) -> None:
        """Test that reads and writes from multiple processes are handled
        correctly, when one of the writers starts without any rows.
        """
        # Insert some rows for two out of three of the ID gens.
        self._insert_rows("first", 3)
        first_id_gen = self._create_id_generator(
            "first", writers=["first", "second", "third"]
        )

        self._insert_rows("second", 4)
        second_id_gen = self._create_id_generator(
            "second", writers=["first", "second", "third"]
        )
        third_id_gen = self._create_id_generator(
            "third", writers=["first", "second", "third"]
        )

        self._replicate_all()

        self.assertEqual(
            first_id_gen.get_positions(), {"first": 3, "second": 7, "third": 7}
        )
        self.assertEqual(first_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(first_id_gen.get_current_token_for_writer("second"), 7)
        self.assertEqual(first_id_gen.get_current_token_for_writer("third"), 7)

        self.assertEqual(
            second_id_gen.get_positions(), {"first": 3, "second": 7, "third": 7}
        )
        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 7)
        self.assertEqual(second_id_gen.get_current_token_for_writer("third"), 7)

        # Try allocating a new ID gen and check that we only see position
        # advanced after we leave the context manager.

        async def _get_next_async() -> None:
            async with third_id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 8)

                self.assertEqual(
                    third_id_gen.get_positions(), {"first": 3, "second": 7, "third": 7}
                )
                self.assertEqual(third_id_gen.get_persisted_upto_position(), 7)

        self.get_success(_get_next_async())

        self.assertEqual(
            third_id_gen.get_positions(), {"first": 3, "second": 7, "third": 8}
        )

    def test_writer_config_change(self) -> None:
        """Test that changing the writer config correctly works."""

        self._insert_row_with_id("first", 3)
        self._insert_row_with_id("second", 5)

        # Initial config has two writers
        id_gen = self._create_id_generator("worker", writers=["first", "second"])
        self.assertEqual(id_gen.get_persisted_upto_position(), 3)
        self.assertEqual(id_gen.get_current_token_for_writer("first"), 3)
        self.assertEqual(id_gen.get_current_token_for_writer("second"), 5)

        # New config removes one of the configs. Note that if the writer is
        # removed from config we assume that it has been shut down and has
        # finished persisting, hence why the persisted upto position is 5.
        id_gen_2 = self._create_id_generator("second", writers=["second"])
        self.assertEqual(id_gen_2.get_persisted_upto_position(), 5)
        self.assertEqual(id_gen_2.get_current_token_for_writer("second"), 5)

        # This config points to a single, previously unused writer.
        id_gen_3 = self._create_id_generator("third", writers=["third"])
        self.assertEqual(id_gen_3.get_persisted_upto_position(), 5)

        # For new writers we assume their initial position to be the current
        # persisted up to position. This stops Synapse from doing a full table
        # scan when a new writer comes along.
        self.assertEqual(id_gen_3.get_current_token_for_writer("third"), 5)

        id_gen_4 = self._create_id_generator("fourth", writers=["third"])
        self.assertEqual(id_gen_4.get_current_token_for_writer("third"), 5)

        # Check that we get a sane next stream ID with this new config.

        async def _get_next_async() -> None:
            async with id_gen_3.get_next() as stream_id:
                self.assertEqual(stream_id, 6)

        self.get_success(_get_next_async())
        self.assertEqual(id_gen_3.get_persisted_upto_position(), 6)

        # If we add back the old "first" then we shouldn't see the persisted up
        # to position revert back to 3.
        id_gen_5 = self._create_id_generator("five", writers=["first", "third"])
        self.assertEqual(id_gen_5.get_persisted_upto_position(), 6)
        self.assertEqual(id_gen_5.get_current_token_for_writer("first"), 6)
        self.assertEqual(id_gen_5.get_current_token_for_writer("third"), 6)

    def test_sequence_consistency(self) -> None:
        """Test that we correct the sequence if the table and sequence diverges."""

        # Prefill with some rows
        self._insert_row_with_id("master", 3)

        # Now we add a row *without* updating the stream ID
        def _insert(txn: Cursor) -> None:
            txn.execute("INSERT INTO foobar VALUES (26, 'master')")

        self.get_success(self.db_pool.runInteraction("_insert", _insert))

        # Creating the ID gen should now fix the inconsistency
        id_gen = self._create_id_generator()

        async def _get_next_async() -> None:
            async with id_gen.get_next() as stream_id:
                self.assertEqual(stream_id, 27)

        self.get_success(_get_next_async())

    def test_minimal_local_token(self) -> None:
        self._insert_rows("first", 3)
        first_id_gen = self._create_id_generator("first", writers=["first", "second"])

        self._insert_rows("second", 4)
        second_id_gen = self._create_id_generator("second", writers=["first", "second"])

        self._replicate_all()

        self.assertEqual(first_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(first_id_gen.get_minimal_local_current_token(), 3)

        self.assertEqual(second_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(second_id_gen.get_minimal_local_current_token(), 7)

    def test_current_token_gap(self) -> None:
        """Test that getting the current token for a writer returns the maximal
        token when there are no writes.
        """
        self._insert_rows("first", 3)
        first_id_gen = self._create_id_generator(
            "first", writers=["first", "second", "third"]
        )

        self._insert_rows("second", 4)
        second_id_gen = self._create_id_generator(
            "second", writers=["first", "second", "third"]
        )

        self._replicate_all()

        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 7)
        self.assertEqual(second_id_gen.get_current_token(), 7)

        # Check that the first ID gen advancing causes the second ID gen to
        # advance (as the second ID gen has nothing in flight).

        async def _get_next_async() -> None:
            async with first_id_gen.get_next_mult(2):
                pass

        self.get_success(_get_next_async())
        second_id_gen.advance("first", 9)

        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 9)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 9)
        self.assertEqual(second_id_gen.get_current_token(), 7)

        # Check that the first ID gen advancing doesn't advance the second ID
        # gen when the second ID gen has stuff in flight.
        self.get_success(_get_next_async())

        ctxmgr = second_id_gen.get_next()
        self.get_success(ctxmgr.__aenter__())

        second_id_gen.advance("first", 11)

        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 11)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 9)
        self.assertEqual(second_id_gen.get_current_token(), 7)

        self.get_success(ctxmgr.__aexit__(None, None, None))

        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 11)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 12)
        self.assertEqual(second_id_gen.get_current_token(), 7)


class BackwardsMultiWriterIdGeneratorTestCase(MultiWriterIdGeneratorBase):
    """Tests MultiWriterIdGenerator that produce *negative* stream IDs."""

    if not USE_POSTGRES_FOR_TESTS:
        skip = "Requires Postgres"

    positive = False

    def test_single_instance(self) -> None:
        """Test that reads and writes from a single process are handled
        correctly.
        """
        id_gen = self._create_id_generator()

        async def _get_next_async() -> None:
            async with id_gen.get_next() as stream_id:
                self._insert_row("master", stream_id)

        self.get_success(_get_next_async())

        self.assertEqual(id_gen.get_positions(), {"master": -1})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), -1)
        self.assertEqual(id_gen.get_persisted_upto_position(), -1)

        async def _get_next_async2() -> None:
            async with id_gen.get_next_mult(3) as stream_ids:
                for stream_id in stream_ids:
                    self._insert_row("master", stream_id)

        self.get_success(_get_next_async2())

        self.assertEqual(id_gen.get_positions(), {"master": -4})
        self.assertEqual(id_gen.get_current_token_for_writer("master"), -4)
        self.assertEqual(id_gen.get_persisted_upto_position(), -4)

        # Test loading from DB by creating a second ID gen
        second_id_gen = self._create_id_generator()

        self.assertEqual(second_id_gen.get_positions(), {"master": -4})
        self.assertEqual(second_id_gen.get_current_token_for_writer("master"), -4)
        self.assertEqual(second_id_gen.get_persisted_upto_position(), -4)

    def test_multiple_instance(self) -> None:
        """Tests that having multiple instances that get advanced over
        federation works corretly.
        """
        id_gen_1 = self._create_id_generator("first", writers=["first", "second"])
        id_gen_2 = self._create_id_generator("second", writers=["first", "second"])

        async def _get_next_async() -> None:
            async with id_gen_1.get_next() as stream_id:
                self._insert_row("first", stream_id)
            self._replicate("first")

        self.get_success(_get_next_async())

        self.assertEqual(id_gen_1.get_positions(), {"first": -1, "second": -1})
        self.assertEqual(id_gen_2.get_positions(), {"first": -1, "second": -1})
        self.assertEqual(id_gen_1.get_persisted_upto_position(), -1)
        self.assertEqual(id_gen_2.get_persisted_upto_position(), -1)

        async def _get_next_async2() -> None:
            async with id_gen_2.get_next() as stream_id:
                self._insert_row("second", stream_id)
            self._replicate("second")

        self.get_success(_get_next_async2())

        self.assertEqual(id_gen_1.get_positions(), {"first": -1, "second": -2})
        self.assertEqual(id_gen_2.get_positions(), {"first": -1, "second": -2})
        self.assertEqual(id_gen_1.get_persisted_upto_position(), -2)
        self.assertEqual(id_gen_2.get_persisted_upto_position(), -2)


class MultiTableMultiWriterIdGeneratorTestCase(MultiWriterIdGeneratorBase):
    if not USE_POSTGRES_FOR_TESTS:
        skip = "Requires Postgres"

    tables = ["foobar1", "foobar2"]

    def test_load_existing_stream(self) -> None:
        """Test creating ID gens with multiple tables that have rows from after
        the position in `stream_positions` table.
        """
        self._insert_rows("first", 3, table="foobar1")
        first_id_gen = self._create_id_generator("first", writers=["first", "second"])

        self._insert_rows("second", 3, table="foobar2")
        self._insert_rows("second", 1, table="foobar2", update_stream_table=False)
        second_id_gen = self._create_id_generator("second", writers=["first", "second"])

        self._replicate_all()

        self.assertEqual(first_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(first_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(first_id_gen.get_current_token_for_writer("second"), 7)
        self.assertEqual(first_id_gen.get_persisted_upto_position(), 7)

        self.assertEqual(second_id_gen.get_positions(), {"first": 3, "second": 7})
        self.assertEqual(second_id_gen.get_current_token_for_writer("first"), 7)
        self.assertEqual(second_id_gen.get_current_token_for_writer("second"), 7)
        self.assertEqual(second_id_gen.get_persisted_upto_position(), 7)
