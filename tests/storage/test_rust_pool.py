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
from twisted.internet.defer import Deferred, gatherResults, inlineCallbacks
from twisted.trial import unittest as trial_unittest

from synapse.storage.rust_pool import RustConnectionPool

from tests.synapse_rust.test_database_postgres import _build_dsn, _test_runtime
from tests.unittest import skip_unless
from tests.utils import USE_POSTGRES_FOR_TESTS

if TYPE_CHECKING:
    from synapse.types import ISynapseReactor

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
            reactor, _test_runtime(), _build_dsn(), name="test-rust-db", threads=4
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
            row = cursor.fetch_one()
            conn.commit()
            return row

        result = yield self.pool.run_with_connection(txn)
        self.assertEqual(result, (42,))

    @inlineCallbacks
    def test_forwards_args_and_kwargs(self) -> Any:
        def txn(conn: Any, a: int, b: int, c: int = 0) -> int:
            return a + b + c

        result = yield self.pool.run_with_connection(txn, 1, 2, c=3)
        self.assertEqual(result, 6)

    @inlineCallbacks
    def test_exception_propagates_as_errback(self) -> Any:
        class MarkerError(Exception):
            pass

        def txn(conn: Any) -> None:
            raise MarkerError("boom")

        # The failure crosses the thread boundary and surfaces as an errback.
        failure = yield self.assertFailure(
            self.pool.run_with_connection(txn), MarkerError
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
            row = cursor.fetch_one()
            conn.commit()
            return row

        self.assertEqual((yield self.pool.run_with_connection(one)), (1,))
        self.assertEqual((yield self.pool.run_with_connection(one)), (1,))

    @inlineCallbacks
    def test_concurrent_calls_are_serviced(self) -> Any:
        # Fire more calls than we have threads/connections at once; the pool and
        # thread pool should service them all and return the right answers.
        def txn(conn: Any, n: int) -> Any:
            cursor = conn.cursor()
            cursor.execute("SELECT $1::int", [n])
            row = cursor.fetch_one()
            conn.commit()
            return row

        results = yield gatherResults(
            [self.pool.run_with_connection(txn, n) for n in range(10)]
        )
        self.assertEqual(results, [(n,) for n in range(10)])

    @inlineCallbacks
    def test_open_transaction_is_committed_on_return(self) -> Any:
        # adbapi's contract: a function that leaves its transaction open has it
        # committed for it when it returns, so its writes are visible to the
        # next caller (who may be on a different connection).
        yield self._create_scratch_table()

        def insert_without_commit(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO rust_pool_scratch (v) VALUES ($1)", [1])

        yield self.pool.run_with_connection(insert_without_commit)
        self.assertEqual((yield self._count_rows()), 1)

    @inlineCallbacks
    def test_open_transaction_is_rolled_back_on_error(self) -> Any:
        # ...and a function that raises has its open transaction rolled back, so
        # nothing it wrote is visible and the connection is clean for reuse.
        yield self._create_scratch_table()

        class MarkerError(Exception):
            pass

        def insert_then_fail(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO rust_pool_scratch (v) VALUES ($1)", [1])
            raise MarkerError("boom")

        yield self.assertFailure(
            self.pool.run_with_connection(insert_then_fail), MarkerError
        )
        self.assertEqual((yield self._count_rows()), 0)

    def _create_scratch_table(self) -> "Deferred[None]":
        def create(conn: Any) -> None:
            cursor = conn.cursor()
            cursor.execute("DROP TABLE IF EXISTS rust_pool_scratch")
            cursor.execute("CREATE TABLE rust_pool_scratch (v INT)")

        def drop() -> "Deferred[None]":
            def _drop(conn: Any) -> None:
                conn.cursor().execute("DROP TABLE IF EXISTS rust_pool_scratch")

            return self.pool.run_with_connection(_drop)

        self.addCleanup(drop)
        return self.pool.run_with_connection(create)

    def _count_rows(self) -> "Deferred[int]":
        def count(conn: Any) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM rust_pool_scratch")
            (n,) = cursor.fetch_one()
            return n

        return self.pool.run_with_connection(count)

    def test_run_when_not_running_raises(self) -> None:
        self.pool.close()
        with self.assertRaises(RuntimeError):
            self.pool.run_with_connection(lambda conn: None)
