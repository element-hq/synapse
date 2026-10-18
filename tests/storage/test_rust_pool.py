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

"""Tests for the Rust-backed Twisted connection pool adapter
(:mod:`synapse.storage.rust_pool`).

These drive real worker threads talking to a real Postgres over the real
reactor, so they use a plain Twisted trial ``TestCase`` (Synapse's in-memory
test reactor deliberately mocks the database thread pool out) and are skipped
unless the suite is configured to run against Postgres.
"""

import time
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

from prometheus_client import REGISTRY

# The reactor the trial runner spins up; real, so threads and callFromThread work.
from twisted.internet import reactor as _reactor
from twisted.internet.defer import gatherResults
from twisted.trial import unittest as trial_unittest

from synapse.config.database import DatabaseConnectionConfig
from synapse.storage.database import LoggingDatabaseConnection, make_pool
from synapse.storage.engines.postgres_rust import RustPostgresEngine
from synapse.storage.rust_pool import RustConnectionPool

from tests.synapse_rust.test_database_postgres import _build_dsn, _test_runtime
from tests.unittest import skip_unless
from tests.utils import (
    POSTGRES_BASE_DB,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    USE_POSTGRES_FOR_TESTS,
)

if TYPE_CHECKING:
    from synapse.types import ISynapseReactor
    from synapse.util.clock import Clock

# `twisted.internet.reactor` is a module-level singleton that is the reactor
# object; narrow it for the type checker.
reactor = cast("ISynapseReactor", _reactor)


@skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class RustConnectionPoolTestCase(trial_unittest.TestCase):
    """The execution bridge: run blocking DB functions off the reactor thread."""

    def setUp(self) -> None:
        self.pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db",
            server_name="test",
            threads=4,
        )
        self.pool.start()
        self.addCleanup(self.pool.close)

    async def test_runs_function_against_a_live_connection(self) -> None:
        # The function gets a usable connection: it can open a cursor, run a
        # query, commit, and its return value comes back through the Deferred.
        def txn(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT 42::int")
            row = cursor.fetchone()
            conn.commit()
            return row

        result = await self.pool.runWithConnection(txn)
        self.assertEqual(result, (42,))

    async def test_forwards_args_and_kwargs(self) -> None:
        def txn(conn: Any, a: int, b: int, c: int = 0) -> int:
            return a + b + c

        result = await self.pool.runWithConnection(txn, 1, 2, c=3)
        self.assertEqual(result, 6)

    async def test_exception_propagates_as_errback(self) -> None:
        class MarkerError(Exception):
            pass

        def txn(conn: Any) -> None:
            raise MarkerError("boom")

        # The failure crosses the thread boundary and surfaces as an errback.
        with self.assertRaises(MarkerError) as ctx:
            await self.pool.runWithConnection(txn)
        self.assertEqual(str(ctx.exception), "boom")

    async def test_connection_is_reusable_across_calls(self) -> None:
        # Connections are returned to the pool after each call, so a second call
        # (which may reuse the same underlying connection) works and sees a
        # clean session rather than a leftover transaction.
        def one(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT 1::int")
            row = cursor.fetchone()
            conn.commit()
            return row

        self.assertEqual((await self.pool.runWithConnection(one)), (1,))
        self.assertEqual((await self.pool.runWithConnection(one)), (1,))

    async def test_commits_work_left_open_on_success(self) -> None:
        # Like adbapi, the pool commits any transaction the function leaves
        # open. One-shot background-update runners rely on this: they execute
        # DDL/DML and return without committing.
        def create_and_insert(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE implicit_commit_test (x INT)")
            cursor.execute("INSERT INTO implicit_commit_test VALUES (1)")
            # No commit: the pool must supply it.

        def count(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM implicit_commit_test")
            return cursor.fetchone()

        def drop(conn: Any) -> None:
            conn.cursor().execute("DROP TABLE IF EXISTS implicit_commit_test")

        self.addCleanup(lambda: self.pool.runWithConnection(drop))

        await self.pool.runWithConnection(create_and_insert)
        self.assertEqual((await self.pool.runWithConnection(count)), (1,))

    async def test_rolls_back_and_reuses_connection_on_exception(self) -> None:
        # Like adbapi, an exception rolls the open transaction back before the
        # connection is returned, so the pool reuses the (now clean) connection
        # rather than discarding it as mid-transaction — otherwise every
        # NotFoundError/StoreError raised inside a transaction would destroy a
        # TCP/TLS session.
        pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db-single",
            server_name="test",
            threads=1,
        )
        pool.start()
        self.addCleanup(pool.close)

        class MarkerError(Exception):
            pass

        pids: list[int] = []

        def create(conn: Any) -> None:
            conn.cursor().execute("CREATE TABLE rollback_reuse_test (x INT)")

        def drop(conn: Any) -> None:
            conn.cursor().execute("DROP TABLE IF EXISTS rollback_reuse_test")

        def insert_and_raise(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_backend_pid()")
            pids.append(cursor.fetchone()[0])
            cursor.execute("INSERT INTO rollback_reuse_test VALUES (1)")
            raise MarkerError("boom")

        def check(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_backend_pid()")
            pids.append(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM rollback_reuse_test")
            return cursor.fetchone()

        self.addCleanup(lambda: pool.runWithConnection(drop))

        await pool.runWithConnection(create)
        with self.assertRaises(MarkerError):
            await pool.runWithConnection(insert_and_raise)
        # The insert was rolled back...
        self.assertEqual((await pool.runWithConnection(check)), (0,))
        # ...and the single pooled connection survived the failed call.
        self.assertEqual(pids[0], pids[1])

    async def test_concurrent_calls_are_serviced(self) -> None:
        # Fire more calls than we have threads/connections at once; the pool and
        # thread pool should service them all and return the right answers.
        def txn(conn: Any, n: int) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT $1::int", [n])
            row = cursor.fetchone()
            conn.commit()
            return row

        results = await gatherResults(
            [self.pool.runWithConnection(txn, n) for n in range(10)]
        )
        self.assertEqual(results, [(n,) for n in range(10)])

    async def test_drives_a_transaction_as_db_pool(self) -> None:
        # Mirror what DatabasePool.runWithConnection's inner_func does: the pool
        # hands a DBAPI2 connection the engine can inspect, wrapped in a
        # LoggingDatabaseConnection whose cursor is a real LoggingTransaction.
        engine = RustPostgresEngine({})

        def interaction(conn: Any) -> Any:
            # A freshly checked-out connection is not mid-transaction.
            self.assertFalse(engine.in_transaction(conn))

            db_conn = LoggingDatabaseConnection(
                conn=conn,
                engine=engine,
                default_txn_name="test",
                server_name="test",
            )
            txn = db_conn.cursor(txn_name="test")
            txn.execute("SELECT ?::int + ?::int", (2, 3))
            row = txn.fetchone()
            db_conn.commit()
            return row

        result = await self.pool.runWithConnection(interaction)
        self.assertEqual(result, (5,))

    async def test_make_pool_builds_a_running_rust_pool(self) -> None:
        # `make_pool` returns a started RustConnectionPool for a use_rust_driver
        # config, ready to serve as `_db_pool`.
        args: dict = {"dbname": POSTGRES_BASE_DB}
        if POSTGRES_USER is not None:
            args["user"] = POSTGRES_USER
        if POSTGRES_HOST is not None:
            args["host"] = POSTGRES_HOST
        if POSTGRES_PORT is not None:
            args["port"] = POSTGRES_PORT
        if POSTGRES_PASSWORD is not None:
            args["password"] = POSTGRES_PASSWORD

        db_config = DatabaseConnectionConfig(
            "master", {"name": "psycopg2", "use_rust_driver": True, "args": args}
        )
        engine = RustPostgresEngine(db_config.config)

        pool = make_pool(
            reactor=reactor,
            clock=cast("Clock", Mock()),
            db_config=db_config,
            engine=engine,
            server_name="test",
            runtime=_test_runtime(),
        )
        self.addCleanup(pool.close)

        self.assertIsInstance(pool, RustConnectionPool)
        self.assertTrue(pool.running)

        def txn(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            conn.commit()
            return row

        self.assertEqual((await pool.runWithConnection(txn)), (1,))

    async def test_txn_limit_replaces_the_connection(self) -> None:
        # With a one-connection pool and `txn_limit=2`, the first two calls share
        # a server session and the third gets a fresh one.
        pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db-txn-limit",
            server_name="test",
            threads=1,
            txn_limit=2,
        )
        pool.start()
        self.addCleanup(pool.close)

        def backend_pid(conn: Any) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_backend_pid()")
            (pid,) = cursor.fetchone()
            return pid

        first = await pool.runWithConnection(backend_pid)
        second = await pool.runWithConnection(backend_pid)
        third = await pool.runWithConnection(backend_pid)
        self.assertEqual(first, second)
        self.assertNotEqual(second, third)

    async def test_stats_count_prepares_and_retirements(self) -> None:
        """A statement is prepared once per connection, then found in the cache.
        Connections retired by `txn_limit` are counted, the counts reach
        Prometheus, and closing the pool keeps them."""
        pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db-stats",
            server_name="test",
            threads=1,
            txn_limit=2,
        )
        pool.start()
        self.addCleanup(pool.close)

        def query(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 + $1::int", (1,))
            cursor.fetchall()

        # Two calls on the first connection, then one on its replacement.
        for _ in range(3):
            await pool.runWithConnection(query)

        hits, prepares, evictions, opened, retired = pool.stats()
        self.assertEqual((hits, prepares, evictions), (1, 2, 0))
        self.assertEqual((opened, retired), (2, 1))

        prepares_metric = REGISTRY.get_sample_value(
            "synapse_rust_db_pool_statement_prepares_total",
            {"name": "test-rust-db-stats", "server_name": "test"},
        )
        self.assertEqual(prepares_metric, 2)

        # Closing the native pool keeps what it counted.
        pool.close()
        self.assertEqual(pool.stats(), (1, 2, 0, 2, 1))

    async def test_statement_cache_evicts_least_recently_used(self) -> None:
        """A full statement cache drops the least recently used statement, so
        statements in use stay prepared."""
        pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db-lru",
            server_name="test",
            threads=1,
        )
        pool.start()
        self.addCleanup(pool.close)

        def run(conn: Any, queries: list[str]) -> None:
            cursor = conn.cursor()
            for query in queries:
                cursor.execute(query)
                cursor.fetchall()

        hot = "SELECT 'hot'"
        # Fill the 512-statement cache, using the hot statement throughout.
        fill = [f"SELECT {i}" for i in range(511)]
        await pool.runWithConnection(run, [hot] + fill + [hot])
        _, prepares, evictions, _, _ = pool.stats()
        self.assertEqual((prepares, evictions), (512, 0))

        # One more statement evicts the least recently used: `SELECT 0`, not
        # the hot statement.
        await pool.runWithConnection(run, ["SELECT 'new'", hot])
        _, prepares, evictions, _, _ = pool.stats()
        self.assertEqual((prepares, evictions), (513, 1))
        await pool.runWithConnection(run, ["SELECT 0"])
        _, prepares, evictions, _, _ = pool.stats()
        self.assertEqual((prepares, evictions), (514, 2))

    async def test_reuses_the_last_returned_connection(self) -> None:
        """Calls one after another get the same connection, so its statement
        cache stays warm, rather than rotating through the pool."""
        pool = RustConnectionPool(
            reactor,
            _test_runtime(),
            _build_dsn(),
            name="test-rust-db-lifo",
            server_name="test",
            threads=4,
        )
        pool.start()
        self.addCleanup(pool.close)

        def backend_pid(conn: Any) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_backend_pid()")
            (pid,) = cursor.fetchone()
            return pid

        # Open several connections at once, then use them one at a time.
        await gatherResults([pool.runWithConnection(backend_pid) for _ in range(4)])
        pids = {await pool.runWithConnection(backend_pid) for _ in range(5)}
        self.assertEqual(len(pids), 1)

    def test_status_reports_pool_occupancy(self) -> None:
        # Nothing checked out yet: at most the configured number of
        # connections, none of them in use.
        max_size, size, available, waiting = self.pool.status()
        self.assertEqual(max_size, 4)
        self.assertEqual(size, available)
        self.assertEqual(waiting, 0)

        # Holding a connection shows up as one fewer available.
        held = self.pool.connect()
        self.addCleanup(held.close)
        max_size, size, available, waiting = self.pool.status()
        self.assertEqual(size - available, 1)

        # A closed pool reports nothing rather than failing the scrape.
        self.pool.close()
        self.assertEqual(self.pool.status(), (0, 0, 0, 0))

    def test_run_when_not_running_raises(self) -> None:
        self.pool.close()
        with self.assertRaises(RuntimeError):
            self.pool.runWithConnection(lambda conn: None)

    def test_checkout_timeout_raises_when_pool_exhausted(self) -> None:
        # With a one-connection pool and a short checkout timeout, holding the
        # only connection makes a second checkout fail with an operational error
        # instead of blocking indefinitely (the `checkout_timeout_ms` plumbing).
        from synapse.synapse_rust.database import (
            postgres,
        )

        pool = postgres.ConnectionPool(
            _test_runtime(), _build_dsn(), 1, checkout_timeout_ms=200
        )
        self.addCleanup(pool.close)

        held = pool.connect()  # take the only connection and keep it checked out
        self.addCleanup(held.close)

        started = time.monotonic()
        with self.assertRaises(postgres.OperationalError) as ctx:
            pool.connect()
        elapsed = time.monotonic() - started

        self.assertIn("timed out", str(ctx.exception).lower())
        # It should fail promptly (around the 200ms budget), not hang.
        self.assertLess(elapsed, 5.0)
