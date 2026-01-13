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

from typing import Callable
from unittest.mock import Mock, call, patch

import attr

from twisted.internet import defer
from twisted.internet.defer import CancelledError, Deferred
from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_comparison_clause,
)
from synapse.util.clock import Clock

from tests import unittest


class TupleComparisonClauseTestCase(unittest.TestCase):
    def test_native_tuple_comparison(self) -> None:
        clause, args = make_tuple_comparison_clause([("a", 1), ("b", 2)])
        self.assertEqual(clause, "(a,b) > (?,?)")
        self.assertEqual(args, [1, 2])


class ExecuteScriptTestCase(unittest.HomeserverTestCase):
    """Tests for `BaseDatabaseEngine.executescript` implementations."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.db_pool: DatabasePool = self.store.db_pool
        self.get_success(
            self.db_pool.runInteraction(
                "create",
                lambda txn: txn.execute("CREATE TABLE foo (name TEXT PRIMARY KEY)"),
            )
        )

    def test_transaction(self) -> None:
        """Test that all statements are run in a single transaction."""

        def run(conn: LoggingDatabaseConnection) -> None:
            cur = conn.cursor(txn_name="test_transaction")
            self.db_pool.engine.executescript(
                cur,
                ";".join(
                    [
                        "INSERT INTO foo (name) VALUES ('transaction test')",
                        # This next statement will fail. When `executescript` is not
                        # transactional, the previous row will be observed later.
                        "INSERT INTO foo (name) VALUES ('transaction test')",
                    ]
                ),
            )

        self.get_failure(
            self.db_pool.runWithConnection(run),
            self.db_pool.engine.module.IntegrityError,
        )

        self.assertIsNone(
            self.get_success(
                self.db_pool.simple_select_one_onecol(
                    "foo",
                    keyvalues={"name": "transaction test"},
                    retcol="name",
                    allow_none=True,
                )
            ),
            "executescript is not running statements inside a transaction",
        )

    def test_commit(self) -> None:
        """Test that the script transaction remains open and can be committed."""

        def run(conn: LoggingDatabaseConnection) -> None:
            cur = conn.cursor(txn_name="test_commit")
            self.db_pool.engine.executescript(
                cur, "INSERT INTO foo (name) VALUES ('commit test')"
            )
            cur.execute("COMMIT")

        self.get_success(self.db_pool.runWithConnection(run))

        self.assertIsNotNone(
            self.get_success(
                self.db_pool.simple_select_one_onecol(
                    "foo",
                    keyvalues={"name": "commit test"},
                    retcol="name",
                    allow_none=True,
                )
            ),
        )

    def test_rollback(self) -> None:
        """Test that the script transaction remains open and can be rolled back."""

        def run(conn: LoggingDatabaseConnection) -> None:
            cur = conn.cursor(txn_name="test_rollback")
            self.db_pool.engine.executescript(
                cur, "INSERT INTO foo (name) VALUES ('rollback test')"
            )
            cur.execute("ROLLBACK")

        self.get_success(self.db_pool.runWithConnection(run))

        self.assertIsNone(
            self.get_success(
                self.db_pool.simple_select_one_onecol(
                    "foo",
                    keyvalues={"name": "rollback test"},
                    retcol="name",
                    allow_none=True,
                )
            ),
            "executescript is not leaving the script transaction open",
        )


@attr.s(slots=True, auto_attribs=True)
class TransactionMocks:
    after_callback: Mock
    exception_callback: Mock
    commit: Mock
    rollback: Mock


class CallbacksTestCase(unittest.HomeserverTestCase):
    """Tests for transaction callbacks."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.db_pool: DatabasePool = self.store.db_pool

    def _run_interaction(
        self, func: Callable[[LoggingTransaction], object]
    ) -> TransactionMocks:
        """Run the given function in a database transaction, with callbacks registered.

        Args:
            func: The function to be run in a transaction. The transaction will be
                retried if `func` raises an `OperationalError`.

        Returns:
            Two mocks, which were registered as an `after_callback` and an
            `exception_callback` respectively, on every transaction attempt.
        """
        after_callback = Mock()
        exception_callback = Mock()

        # Track commit/rollback calls on the LoggingDatabaseConnection used
        # for the transaction so tests can assert whether attempts committed
        # or rolled back.
        commit_mock = Mock()
        rollback_mock = Mock()

        def _test_txn(txn: LoggingTransaction) -> None:
            txn.call_after(after_callback, 123, 456, extra=789)
            txn.call_on_exception(exception_callback, 987, 654, extra=321)
            func(txn)

        # Wrap the real commit/rollback so we record calls but still perform
        # the original behaviour.
        orig_commit = LoggingDatabaseConnection.commit
        orig_rollback = LoggingDatabaseConnection.rollback

        # type-ignore becauase we're just transparently passing through args/kwargs and
        # returning whatever result that the original function does
        def _commit(self, *a, **kw):  # type: ignore[no-untyped-def]
            commit_mock()
            return orig_commit(self, *a, **kw)

        # type-ignore becauase we're just transparently passing through args/kwargs and
        # returning whatever result that the original function does
        def _rollback(self, *a, **kw):  # type: ignore[no-untyped-def]
            rollback_mock()
            return orig_rollback(self, *a, **kw)

        try:
            with (
                patch.object(LoggingDatabaseConnection, "commit", _commit),
                patch.object(LoggingDatabaseConnection, "rollback", _rollback),
            ):
                self.get_success_or_raise(
                    self.db_pool.runInteraction("test_transaction", _test_txn)
                )
        except Exception:
            pass

        # FIXME: Sanity check that every transaction is either committed or rolled back,
        # see https://github.com/element-hq/synapse/issues/19202
        # transaction_count = after_callback.call_count + exception_callback.call_count
        # self.assertEqual(
        #     transaction_count,
        #     commit_mock.call_count + rollback_mock.call_count,
        #     "We expect every transaction attempt to either commit or rollback. "
        #     f"Saw {transaction_count} transactions, but only {commit_mock.call_count} commits and {rollback_mock.call_count} rollbacks",
        # )

        return TransactionMocks(
            after_callback=after_callback,
            exception_callback=exception_callback,
            commit=commit_mock,
            rollback=rollback_mock,
        )

    def test_after_callback(self) -> None:
        """Test that the after callback is called when a transaction succeeds."""
        txn_mocks = self._run_interaction(lambda txn: None)

        txn_mocks.after_callback.assert_called_once_with(123, 456, extra=789)
        txn_mocks.exception_callback.assert_not_called()

        # Should have commited right away
        self.assertEqual(txn_mocks.commit.call_count, 1)
        # (nothing was rolled back)
        self.assertEqual(txn_mocks.rollback.call_count, 0)

    def test_exception_callback(self) -> None:
        """Test that the exception callback is called when a transaction fails."""
        _test_txn = Mock(side_effect=ZeroDivisionError)
        txn_mocks = self._run_interaction(_test_txn)

        txn_mocks.after_callback.assert_not_called()
        txn_mocks.exception_callback.assert_called_once_with(987, 654, extra=321)

        # Nothing should have committed.
        self.assertEqual(txn_mocks.commit.call_count, 0)
        # FIXME: Every transaction should have been rolled back, see
        # https://github.com/element-hq/synapse/issues/19202
        # self.assertEqual(txn_mocks.rollback.call_count, 1)

    def test_failed_retry(self) -> None:
        """Test that the exception callback is called for every failed attempt."""
        # Always raise an `OperationalError`.
        _test_txn = Mock(side_effect=self.db_pool.engine.module.OperationalError)
        txn_mocks = self._run_interaction(_test_txn)

        txn_mocks.after_callback.assert_not_called()
        txn_mocks.exception_callback.assert_has_calls(
            [
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
            ]
        )
        # no additional calls
        self.assertEqual(txn_mocks.exception_callback.call_count, 5)

        # Nothing should have committed.
        self.assertEqual(txn_mocks.commit.call_count, 0)
        # Every transaction should have been rolled back.
        self.assertEqual(txn_mocks.rollback.call_count, 5)

    def test_successful_retry(self) -> None:
        """Test callbacks for a failed transaction followed by a successful attempt."""
        # Raise an `OperationalError` on the first attempt only.
        _test_txn = Mock(
            side_effect=[self.db_pool.engine.module.OperationalError, None]
        )
        txn_mocks = self._run_interaction(_test_txn)

        # Calling both `after_callback`s when the first attempt failed is rather
        # surprising (https://github.com/matrix-org/synapse/issues/12184).
        # Let's document the behaviour in a test.
        txn_mocks.after_callback.assert_has_calls(
            [
                call(123, 456, extra=789),
                call(123, 456, extra=789),
            ]
        )
        # no additional calls
        self.assertEqual(txn_mocks.after_callback.call_count, 2)
        txn_mocks.exception_callback.assert_not_called()

        # The last attempt should have committed.
        self.assertEqual(txn_mocks.commit.call_count, 1)
        # The first attempt should have been rolled back.
        self.assertEqual(txn_mocks.rollback.call_count, 1)


class CancellationTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.db_pool: DatabasePool = self.store.db_pool

    def test_after_callback(self) -> None:
        """Test that the after callback is called when a transaction succeeds."""
        d: "Deferred[None]"
        after_callback = Mock()
        exception_callback = Mock()

        def _test_txn(txn: LoggingTransaction) -> None:
            txn.call_after(after_callback, 123, 456, extra=789)
            txn.call_on_exception(exception_callback, 987, 654, extra=321)
            d.cancel()

        d = defer.ensureDeferred(
            self.db_pool.runInteraction("test_transaction", _test_txn)
        )
        self.get_failure(d, CancelledError)

        after_callback.assert_called_once_with(123, 456, extra=789)
        exception_callback.assert_not_called()

    def test_exception_callback(self) -> None:
        """Test that the exception callback is called when a transaction fails."""
        d: "Deferred[None]"
        after_callback = Mock()
        exception_callback = Mock()

        def _test_txn(txn: LoggingTransaction) -> None:
            txn.call_after(after_callback, 123, 456, extra=789)
            txn.call_on_exception(exception_callback, 987, 654, extra=321)
            d.cancel()
            # Simulate a retryable failure on every attempt.
            raise self.db_pool.engine.module.OperationalError()

        d = defer.ensureDeferred(
            self.db_pool.runInteraction("test_transaction", _test_txn)
        )
        self.get_failure(d, CancelledError)

        after_callback.assert_not_called()
        exception_callback.assert_has_calls(
            [
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
                call(987, 654, extra=321),
            ]
        )
        self.assertEqual(exception_callback.call_count, 5)  # no additional calls
