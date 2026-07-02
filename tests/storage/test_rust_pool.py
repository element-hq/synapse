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

from typing import TYPE_CHECKING, Any, cast

# The reactor the trial runner spins up; real, so threads and callFromThread work.
from twisted.internet import reactor as _reactor
from twisted.internet.defer import gatherResults, inlineCallbacks
from twisted.trial import unittest as trial_unittest

from synapse.storage.database import LoggingDatabaseConnection
from synapse.storage.engines.postgres_rust import RustPostgresEngine
from synapse.storage.rust_pool import RustConnectionPool

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

# `twisted.internet.reactor` is a module-level singleton that is the reactor
# object; narrow it for the type checker.
reactor = cast("ISynapseReactor", _reactor)


def _build_dsn() -> str:
    """Build a libpq keyword/value connection string from the test config."""

    parts = [f"dbname={POSTGRES_BASE_DB}"]
    if POSTGRES_USER is not None:
        parts.append(f"user={POSTGRES_USER}")
    if POSTGRES_HOST is not None:
        parts.append(f"host={POSTGRES_HOST}")
    if POSTGRES_PORT is not None:
        parts.append(f"port={POSTGRES_PORT}")
    if POSTGRES_PASSWORD is not None:
        parts.append(f"password={POSTGRES_PASSWORD}")
    return " ".join(parts)


@skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class RustConnectionPoolTestCase(trial_unittest.TestCase):
    """The execution bridge: run blocking DB functions off the reactor thread."""

    def setUp(self) -> None:
        self.pool = RustConnectionPool(
            reactor, _build_dsn(), name="test-rust-db", threads=4
        )
        self.pool.start()
        self.addCleanup(self.pool.close)

    @inlineCallbacks
    def test_runs_function_against_a_live_connection(self) -> Any:
        # The function gets a usable connection: it can open a cursor, run a
        # query, commit, and its return value comes back through the Deferred.
        def txn(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT 42::int")
            row = cursor.fetchone()
            conn.commit()
            return row

        result = yield self.pool.runWithConnection(txn)
        self.assertEqual(result, (42,))

    @inlineCallbacks
    def test_forwards_args_and_kwargs(self) -> Any:
        def txn(conn: Any, a: int, b: int, c: int = 0) -> int:
            return a + b + c

        result = yield self.pool.runWithConnection(txn, 1, 2, c=3)
        self.assertEqual(result, 6)

    @inlineCallbacks
    def test_exception_propagates_as_errback(self) -> Any:
        class MarkerError(Exception):
            pass

        def txn(conn: Any) -> None:
            raise MarkerError("boom")

        # The failure crosses the thread boundary and surfaces as an errback.
        failure = yield self.assertFailure(
            self.pool.runWithConnection(txn), MarkerError
        )
        self.assertEqual(str(failure), "boom")

    @inlineCallbacks
    def test_connection_is_reusable_across_calls(self) -> Any:
        # Connections are returned to the pool after each call, so a second call
        # (which may reuse the same underlying connection) works and sees a
        # clean session rather than a leftover transaction.
        def one(conn: Any) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT 1::int")
            row = cursor.fetchone()
            conn.commit()
            return row

        self.assertEqual((yield self.pool.runWithConnection(one)), (1,))
        self.assertEqual((yield self.pool.runWithConnection(one)), (1,))

    @inlineCallbacks
    def test_concurrent_calls_are_serviced(self) -> Any:
        # Fire more calls than we have threads/connections at once; the pool and
        # thread pool should service them all and return the right answers.
        def txn(conn: Any, n: int) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT $1::int", [n])
            row = cursor.fetchone()
            conn.commit()
            return row

        results = yield gatherResults(
            [self.pool.runWithConnection(txn, n) for n in range(10)]
        )
        self.assertEqual(results, [(n,) for n in range(10)])

    @inlineCallbacks
    def test_drives_a_transaction_as_db_pool(self) -> Any:
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

        result = yield self.pool.runWithConnection(interaction)
        self.assertEqual(result, (5,))

    def test_run_when_not_running_raises(self) -> None:
        self.pool.close()
        with self.assertRaises(RuntimeError):
            self.pool.runWithConnection(lambda conn: None)
