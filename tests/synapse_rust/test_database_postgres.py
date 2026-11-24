# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Any

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.synapse_rust.database import postgres
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase
from tests.utils import POSTGRES_HOST, USE_POSTGRES_FOR_TESTS

# A statement that starts successfully and then fails part way through its
# rows, with a division by zero at `x = 3`. `execute` returns without an
# error, which is only reported once the rows are read.
FAILING_SELECT = "SELECT 1 / (x - 3) FROM generate_series(1, 5) x"

# Every error the driver raises is a `RuntimeError`. These match the messages
# for the cases the tests need to tell apart.
SERVER_ERROR = "db error"
ABORTED = "the transaction was aborted"
NO_RESULT_SET = "no result set"
EXHAUSTED = "already exhausted"
CLOSED = "connection already closed"


def make_dsn(args: dict[str, Any]) -> str:
    """Build a libpq-style DSN from the connection arguments that are set."""

    def quote(value: Any) -> str:
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

    return " ".join(
        f"{key}={quote(args[key])}"
        for key in ("host", "port", "user", "password", "dbname")
        if args.get(key) is not None
    )


class PostgresConnectionTestCase(HomeserverTestCase):
    """Tests the Rust Postgres `Connection` and `Cursor` against a real server.

    Each test gets the homeserver's freshly created database, with an empty
    table `t`.
    """

    # `connect` needs a host, where psycopg2 would fall back to libpq's default.
    if not USE_POSTGRES_FOR_TESTS or POSTGRES_HOST is None:
        skip = "Requires Postgres, with SYNAPSE_POSTGRES_HOST set"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.runtime = hs.get_rust_runtime()
        self.db_args = hs.config.database.databases[0].config["args"]
        dsn = make_dsn(self.db_args)

        # Each connection must be closed before the homeserver's cleanup stops
        # the Rust runtime and drops the database. Cleanups run last in, first
        # out, and the homeserver registered its own before `prepare` ran.

        # The connection under test.
        self.conn = postgres.connect(self.runtime, dsn)
        self.addCleanup(self.conn.close)

        # A second connection, used to check what has been committed.
        self.observer = postgres.connect(self.runtime, dsn)
        self.addCleanup(self.observer.close)
        self.observer.set_autocommit(True)
        # If a test leaves a lock held, fail the observer's statement rather
        # than wait for the lock forever.
        self.observer.cursor().execute("SET lock_timeout = '10s'")

        cur = self.conn.cursor()
        cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        self.conn.commit()

    def committed_ids(self) -> list[int]:
        """The ids in table `t`, as seen by another connection."""
        cur = self.observer.cursor()
        cur.execute("SELECT id FROM t ORDER BY id")
        return [row[0] for row in cur.fetch_all()]

    def test_connect_errors(self) -> None:
        """`connect` raises for a DSN it can't use, and when the server can't
        be reached or refuses the connection."""
        with self.assertRaisesRegex(RuntimeError, "Failed to parse DSN"):
            postgres.connect(self.runtime, "this is not a dsn")

        with self.assertRaisesRegex(RuntimeError, "No database host is configured"):
            postgres.connect(self.runtime, "dbname=synapse user=synapse")

        # No server listens in this socket directory.
        with self.assertRaises(RuntimeError):
            postgres.connect(self.runtime, "host=/nonexistent user=synapse")

        # The server refuses a database that doesn't exist.
        no_such_db = make_dsn({**self.db_args, "dbname": "no_such_database"})
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            postgres.connect(self.runtime, no_such_db)

    def test_fetch_one(self) -> None:
        """`fetch_one` returns each row in turn, then `None` once."""
        cur = self.conn.cursor()
        cur.execute("SELECT x FROM generate_series(1, 3) x")

        self.assertEqual(cur.fetch_one(), (1,))
        self.assertEqual(cur.fetch_one(), (2,))
        self.assertEqual(cur.fetch_one(), (3,))
        self.assertIsNone(cur.fetch_one())

        # Reading past the end is an error.
        with self.assertRaisesRegex(RuntimeError, EXHAUSTED):
            cur.fetch_one()

    def test_fetch_all(self) -> None:
        """`fetch_all` returns the remaining rows, with parameters bound."""
        cur = self.conn.cursor()
        cur.execute("SELECT x + $1 FROM generate_series(1, 3) x", [10])
        self.assertEqual(cur.fetch_one(), (11,))
        self.assertEqual(cur.fetch_all(), [(12,), (13,)])

        # The result set can only be read once.
        with self.assertRaisesRegex(RuntimeError, EXHAUSTED):
            cur.fetch_all()

        cur.execute("SELECT 1 WHERE false")
        self.assertEqual(cur.fetch_all(), [])

    def test_fetch_next_batch(self) -> None:
        """The batches hold every row in order, and an empty batch ends the
        result set."""
        cur = self.conn.cursor()
        cur.execute("SELECT x FROM generate_series(1, 5) x")

        # How the rows split into batches depends on when they arrive.
        rows: list[tuple[Any, ...]] = []
        while batch := cur.fetch_next_batch():
            rows.extend(batch)

        self.assertEqual(rows, [(x,) for x in range(1, 6)])

        with self.assertRaisesRegex(RuntimeError, EXHAUSTED):
            cur.fetch_next_batch()

    def test_rowcount(self) -> None:
        """`rowcount` reports the rows a statement affected."""
        cur = self.conn.cursor()

        cur.execute("INSERT INTO t VALUES (1), (2), (3)")
        self.assertEqual(cur.rowcount, 3)

        cur.execute("UPDATE t SET id = id + 10 WHERE id > $1", [1])
        self.assertEqual(cur.rowcount, 2)

        cur.execute("DELETE FROM t WHERE id = 100")
        self.assertEqual(cur.rowcount, 0)

        # Postgres sends the count after the last row, so reading it ends the
        # result set.
        cur.execute("SELECT id FROM t")
        self.assertEqual(cur.rowcount, 3)
        with self.assertRaisesRegex(RuntimeError, EXHAUSTED):
            cur.fetch_one()

    def test_values_round_trip(self) -> None:
        """Each supported Python type survives a trip through Postgres."""
        values = [True, -7, 70_000, 2**40, 0.5, 1.5, "héllo", b"\x00\xff", None]

        cur = self.conn.cursor()
        cur.execute(
            "SELECT $1::bool, $2::int2, $3::int4, $4::int8, $5::float4, $6::float8,"
            " $7::text, $8::bytea, $9::text",
            values,
        )
        rows = cur.fetch_all()
        self.assertEqual(rows, [tuple(values)])

        # `True == 1` and `1.0 == 1`, so compare the types as well.
        self.assertEqual([type(v) for v in rows[0]], [type(v) for v in values])

    def test_commit(self) -> None:
        """Writes are in a transaction, and visible to others once committed."""
        # With nothing to commit, this does nothing.
        self.conn.commit()

        # Cursors on one connection share its transaction.
        self.conn.cursor().execute("INSERT INTO t VALUES (1)")
        self.conn.cursor().execute("INSERT INTO t VALUES (2)")
        self.assertEqual(self.committed_ids(), [])

        self.conn.commit()
        self.assertEqual(self.committed_ids(), [1, 2])

    def test_rollback(self) -> None:
        """`rollback` discards the transaction and leaves the connection
        usable."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t VALUES (1)")
        self.conn.rollback()

        cur.execute("INSERT INTO t VALUES (2)")
        self.conn.commit()
        self.assertEqual(self.committed_ids(), [2])

    def test_autocommit(self) -> None:
        """In autocommit mode each statement commits by itself, and a failed
        statement doesn't block the next one."""
        self.conn.set_autocommit(True)
        cur = self.conn.cursor()

        cur.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(self.committed_ids(), [1])

        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.execute("INSERT INTO t VALUES (1)")
        cur.execute("INSERT INTO t VALUES (2)")
        self.assertEqual(self.committed_ids(), [1, 2])

        # The mode can't change while a transaction is open.
        self.conn.set_autocommit(False)
        cur.execute("INSERT INTO t VALUES (3)")
        with self.assertRaisesRegex(RuntimeError, "transaction is in progress"):
            self.conn.set_autocommit(True)
        self.conn.rollback()
        self.conn.set_autocommit(True)

    def test_context_manager(self) -> None:
        """`with conn` commits, or rolls back if the block raises. It leaves
        the connection open."""
        with self.conn as conn:
            self.assertIs(conn, self.conn)
            self.conn.cursor().execute("INSERT INTO t VALUES (1)")
        self.assertEqual(self.committed_ids(), [1])

        with self.assertRaises(ZeroDivisionError):
            with self.conn:
                self.conn.cursor().execute("INSERT INTO t VALUES (2)")
                raise ZeroDivisionError()
        self.assertEqual(self.committed_ids(), [1])

        with self.conn:
            self.conn.cursor().execute("INSERT INTO t VALUES (3)")
        self.assertEqual(self.committed_ids(), [1, 3])

    def test_close(self) -> None:
        """A closed connection raises from its cursors' methods, and the
        server rolls back its open transaction."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t VALUES (1)")
        cur.execute("SELECT 1")

        self.conn.close()
        # Closing twice is fine.
        self.conn.close()

        with self.assertRaisesRegex(RuntimeError, CLOSED):
            cur.fetch_one()
        with self.assertRaisesRegex(RuntimeError, CLOSED):
            cur.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, CLOSED):
            self.conn.cursor().execute("SELECT 1")
        cur.close()

        # The observer's insert of the same id waits for the closed
        # connection's transaction to end. It only succeeds if that
        # transaction rolled back.
        self.observer.cursor().execute("INSERT INTO t VALUES (1)")
        self.assertEqual(self.committed_ids(), [1])

    def test_statement_error_aborts_transaction(self) -> None:
        """After a statement fails, `execute` raises until the transaction
        ends, and `commit` rolls back."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t VALUES (1)")

        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.execute("SELECT * FROM no_such_table")

        # The failed statement replaced the insert's result.
        with self.assertRaisesRegex(RuntimeError, NO_RESULT_SET):
            _ = cur.rowcount

        with self.assertRaisesRegex(RuntimeError, ABORTED):
            cur.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, ABORTED):
            self.conn.commit()
        self.assertEqual(self.committed_ids(), [])

        # The failed commit ended the transaction, so the next one works.
        cur.execute("INSERT INTO t VALUES (2)")
        self.conn.commit()
        self.assertEqual(self.committed_ids(), [2])

        # A duplicate key is only detected when the statement runs. An insert
        # returns no rows, so `execute` reads its result and raises the error.
        cur.execute("INSERT INTO t VALUES (3)")
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.execute("INSERT INTO t VALUES (3)")
        with self.assertRaisesRegex(RuntimeError, ABORTED):
            self.conn.commit()
        self.assertEqual(self.committed_ids(), [2])

    def test_error_while_fetching(self) -> None:
        """An error while reading the rows is raised by the fetch, and aborts
        the transaction."""
        cur = self.conn.cursor()
        cur.execute(FAILING_SELECT)

        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.fetch_all()

        with self.assertRaisesRegex(RuntimeError, ABORTED):
            cur.execute("SELECT 1")

        self.conn.rollback()
        cur.execute("SELECT 1")
        self.assertEqual(cur.fetch_all(), [(1,)])
        self.conn.rollback()

        # A column type the driver can't decode fails on the client. The
        # server's transaction is unharmed, but the rows went unread, so the
        # connection treats it as aborted all the same.
        cur.execute("INSERT INTO t VALUES (1)")
        cur.execute("SELECT now()")
        with self.assertRaises(ValueError):
            cur.fetch_one()

        with self.assertRaisesRegex(RuntimeError, ABORTED):
            self.conn.commit()
        self.assertEqual(self.committed_ids(), [])

    def test_unread_error_is_raised_by_commit(self) -> None:
        """If a failed statement's rows are never read, `commit` raises its
        error and rolls back."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t VALUES (1)")
        cur.execute(FAILING_SELECT)

        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            self.conn.commit()
        self.assertEqual(self.committed_ids(), [])

        # `rollback` ignores the same error.
        cur.execute("INSERT INTO t VALUES (2)")
        cur.execute(FAILING_SELECT)
        self.conn.rollback()
        self.assertEqual(self.committed_ids(), [])

    def test_unread_error_in_autocommit(self) -> None:
        """With no transaction open, `commit` still raises an unread error and
        `rollback` still ignores it."""
        self.conn.set_autocommit(True)
        cur = self.conn.cursor()

        cur.execute(FAILING_SELECT)
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            self.conn.commit()

        cur.execute(FAILING_SELECT)
        self.conn.rollback()

        # The next `execute` raises it too, and only that one.
        cur.execute(FAILING_SELECT)
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.execute("SELECT 1")

        cur.execute("SELECT 1")
        self.assertEqual(cur.fetch_all(), [(1,)])

    def test_failed_commit_keeps_connection(self) -> None:
        """A `COMMIT` that the server rejects ends the transaction, and the
        connection can be used again."""
        cur = self.conn.cursor()
        cur.execute("CREATE TABLE d (id INT UNIQUE DEFERRABLE INITIALLY DEFERRED)")
        self.conn.commit()

        # The uniqueness check is deferred to the commit.
        cur.execute("INSERT INTO d VALUES (1), (1)")
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            self.conn.commit()

        # The next statement starts a new transaction. If the connection
        # still thought the old one was open, it would skip the `BEGIN` and
        # this insert would commit by itself.
        cur.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(self.committed_ids(), [])
        self.conn.commit()
        self.assertEqual(self.committed_ids(), [1])

    def test_execute_on_another_cursor_ends_result_set(self) -> None:
        """A connection has one result set, which belongs to the cursor that
        last executed."""
        first = self.conn.cursor()
        second = self.conn.cursor()

        first.execute("SELECT x FROM generate_series(1, 3) x")
        self.assertEqual(first.fetch_one(), (1,))

        second.execute("SELECT 'second'")

        with self.assertRaisesRegex(RuntimeError, NO_RESULT_SET):
            first.fetch_one()
        with self.assertRaisesRegex(RuntimeError, NO_RESULT_SET):
            _ = first.rowcount

        # Closing the first cursor leaves the second's result set alone.
        first.close()
        self.assertEqual(second.fetch_all(), [("second",)])

    def test_cursor_close(self) -> None:
        """Closing a cursor ends its result set, and raises an error the
        statement hit that nothing has read yet."""
        cur = self.conn.cursor()
        cur.execute("SELECT x FROM generate_series(1, 3) x")
        cur.close()
        # Closing twice is fine.
        cur.close()
        with self.assertRaisesRegex(RuntimeError, EXHAUSTED):
            cur.fetch_one()

        cur.execute(FAILING_SELECT)
        with self.assertRaisesRegex(RuntimeError, SERVER_ERROR):
            cur.close()
