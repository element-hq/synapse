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

"""Tests for the Rust-implemented, DBAPI2-shaped Postgres ``Connection`` /
``Cursor`` pair exposed as ``synapse.synapse_rust.database.postgres``.

These exercise the real ``tokio-postgres`` backend, so they require a live
Postgres server and are skipped unless the test suite is configured to run
against Postgres (i.e. ``SYNAPSE_POSTGRES`` is set, the same switch used by the
rest of the suite).
"""

import logging
from typing import Any, Optional

from synapse.synapse_rust.database import postgres

from tests import unittest
from tests.utils import (
    POSTGRES_BASE_DB,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    USE_POSTGRES_FOR_TESTS,
)


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


def run_interaction(conn: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``func(cursor, *args, **kwargs)`` in a transaction on ``conn``:
    commit on success, roll back if it raises.

    The shape Synapse's ``new_transaction`` drives the shim with, as a test
    harness. (The shim used to expose this itself as
    ``Connection.run_interaction``, but nothing in production called it.)
    The commit sits inside the protected region — as in
    ``RustConnectionPool._in_transaction`` — so a failed commit also rolls
    back; a rollback failure is logged rather than allowed to mask the
    original exception.
    """
    cursor = conn.cursor()
    try:
        result = func(cursor, *args, **kwargs)
        conn.commit()
        return result
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            logging.getLogger(__name__).warning(
                "Rollback failed in test run_interaction", exc_info=True
            )
        raise


@unittest.skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class PostgresConnectionTestCase(unittest.TestCase):
    """Tests for the Rust Postgres ``Connection`` / ``Cursor``."""

    def setUp(self) -> None:
        self.conn = postgres.connect(_build_dsn())

    def tearDown(self) -> None:
        # Explicitly drop the connection to ensure that the underlying Rust
        # object is dropped before the Python interpreter shuts down. Otherwise,
        # the open connection will block us tearing down the test database.
        del self.conn

    # ------------------------------------------------------------------
    # connect()
    # ------------------------------------------------------------------

    def test_connect_bad_dsn_raises(self) -> None:
        # A syntactically valid but unconnectable DSN should raise rather than
        # return a half-open connection.
        with self.assertRaises(RuntimeError):
            postgres.connect("host=127.0.0.1 port=1 dbname=does_not_exist")

    # ------------------------------------------------------------------
    # execute() / fetch_one() / fetch_all()
    # ------------------------------------------------------------------

    def test_fetch_one(self) -> None:
        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 42::int, 'hello'::text")
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, interaction), (42, "hello"))

    def test_fetch_one_returns_none_when_exhausted(self) -> None:
        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 1 WHERE true")
            first = cursor.fetch_one()
            second = cursor.fetch_one()
            return [first, second]

        self.assertEqual(run_interaction(self.conn, interaction), [(1,), None])

    def test_fetch_all(self) -> None:
        def interaction(cursor: Any) -> list[list[Any]]:
            cursor.execute(
                """
                SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c'))
                AS v(id, name) ORDER BY id
                """
            )
            return cursor.fetch_all()

        self.assertEqual(
            run_interaction(self.conn, interaction),
            [(1, "a"), (2, "b"), (3, "c")],
        )

    def test_fetch_all_empty(self) -> None:
        def interaction(cursor: Any) -> list[list[Any]]:
            cursor.execute("SELECT 1 WHERE false")
            return cursor.fetch_all()

        self.assertEqual(run_interaction(self.conn, interaction), [])

    def test_fetch_without_query_raises(self) -> None:
        """Calling fetch before execute is an error, not a silent empty result."""

        def interaction(cursor: Any) -> None:
            cursor.fetch_one()

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_reuse_cursor_for_multiple_queries(self) -> None:
        """A single cursor can run several queries in sequence."""

        def interaction(cursor: Any) -> list[Any]:
            results = []
            for n in (1, 2, 3):
                cursor.execute("SELECT $1::int", [n])
                row = cursor.fetch_one()
                assert row is not None
                results.append(row[0])
            return results

        self.assertEqual(run_interaction(self.conn, interaction), [1, 2, 3])

    # ------------------------------------------------------------------
    # fetch_next_batch()
    # ------------------------------------------------------------------

    def test_fetch_next_batch_returns_first_row(self) -> None:
        """A non-empty result set yields a batch containing at least the first
        row, which blocks until available."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 42::int, 'hello'::text")
            return cursor.fetch_next_batch()

        # The batch must be non-empty and start with the first row. We don't
        # assert the exact length: how many further rows are already buffered
        # (and so returned without blocking) is timing-dependent.
        batch = run_interaction(self.conn, interaction)
        self.assertEqual(batch[0], (42, "hello"))

    def test_fetch_next_batch_empty_when_no_rows(self) -> None:
        """An empty result set yields an empty batch."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 1 WHERE false")
            return cursor.fetch_next_batch()

        self.assertEqual(run_interaction(self.conn, interaction), [])

    def test_fetch_next_batch_collects_all_rows_across_batches(self) -> None:
        """Looping until an empty batch is returned yields every row exactly
        once, in order, regardless of how the rows are split across batches."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute(
                """
                SELECT id FROM generate_series(1, 1000) AS s(id) ORDER BY id
                """
            )
            rows = []
            while True:
                batch = cursor.fetch_next_batch()
                if not batch:
                    break
                rows.extend(batch)
            return rows

        self.assertEqual(
            run_interaction(self.conn, interaction),
            [(n,) for n in range(1, 1001)],
        )

    def test_fetch_next_batch_reports_exhaustion_with_empty_batch(self) -> None:
        """After the last rows, one further call returns an empty batch to
        report exhaustion."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 1")
            first = cursor.fetch_next_batch()
            second = cursor.fetch_next_batch()
            return [first, second]

        first, second = run_interaction(self.conn, interaction)
        self.assertEqual(first, [(1,)])
        self.assertEqual(second, [])

    def test_fetch_next_batch_capacity_is_not_a_limit(self) -> None:
        """`capacity` is only a buffer hint; a batch may exceed it."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute(
                "SELECT id FROM generate_series(1, 100) AS s(id) ORDER BY id"
            )
            rows = []
            while True:
                batch = cursor.fetch_next_batch(1)
                if not batch:
                    break
                rows.extend(batch)
            return rows

        self.assertEqual(
            run_interaction(self.conn, interaction),
            [(n,) for n in range(1, 101)],
        )

    def test_fetch_next_batch_without_query_raises(self) -> None:
        """Calling fetch_next_batch before execute is an error."""

        def interaction(cursor: Any) -> None:
            cursor.fetch_next_batch()

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_fetch_next_batch_interleaves_with_fetch_one(self) -> None:
        """fetch_one and fetch_next_batch share the same underlying stream, so
        rows already consumed by one are not seen by the other."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT id FROM generate_series(1, 10) AS s(id) ORDER BY id")
            first = cursor.fetch_one()
            rows = [first]
            while True:
                batch = cursor.fetch_next_batch()
                if not batch:
                    break
                rows.extend(batch)
            return rows

        self.assertEqual(
            run_interaction(self.conn, interaction),
            [(n,) for n in range(1, 11)],
        )

    # ------------------------------------------------------------------
    # Fetching after exhaustion is a programming error
    # ------------------------------------------------------------------
    #
    # Once a fetch has *reported* exhaustion (fetch_one -> None,
    # fetch_next_batch -> [], or fetch_all returning), the result set is spent.
    # Fetching again indicates a bug in the caller, so it raises rather than
    # silently returning nothing.

    def test_fetch_one_after_exhaustion_raises(self) -> None:
        """A fetch_one after the one that returned None is an error."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetch_one(), (1,))
            self.assertIsNone(cursor.fetch_one())  # reports exhaustion
            cursor.fetch_one()  # over-fetch

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_fetch_next_batch_after_exhaustion_raises(self) -> None:
        """A fetch_next_batch after the one that returned [] is an error."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetch_next_batch(), [(1,)])
            self.assertEqual(cursor.fetch_next_batch(), [])  # reports exhaustion
            cursor.fetch_next_batch()  # over-fetch

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_fetch_all_twice_raises(self) -> None:
        """fetch_all exhausts the result set, so a second fetch_all is an
        error."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetch_all(), [(1,)])
            cursor.fetch_all()  # over-fetch

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_fetch_one_after_fetch_all_raises(self) -> None:
        """fetch_all reports exhaustion, so a following fetch_one is an
        error rather than None."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            cursor.fetch_all()
            cursor.fetch_one()  # over-fetch

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    def test_exhausted_error_is_distinct_from_no_query(self) -> None:
        """The exhausted-result error names exhaustion, not a missing query, so
        the two programming errors are distinguishable."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            cursor.fetch_all()
            cursor.fetch_one()

        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            run_interaction(self.conn, interaction)

    # ------------------------------------------------------------------
    # Value round-tripping (ToSql + FromSql for each supported type)
    # ------------------------------------------------------------------

    def test_value_roundtrip_all_types(self) -> None:
        """Each supported type survives a param -> column -> row round trip."""

        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute(
                """
                CREATE TEMP TABLE types_roundtrip (
                  c_bool bool,
                  c_int2 smallint,
                  c_int4 int,
                  c_int8 bigint,
                  c_float4 float4,
                  c_float8 float8,
                  c_text text,
                  c_varchar varchar,
                  c_bytea bytea
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO types_roundtrip VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                [
                    True,
                    7,
                    1234,
                    10**15,
                    1.5,  # exactly representable as float4
                    3.5,
                    "hello",
                    "world",
                    b"\x00\x01\x02bytes",
                ],
            )
            cursor.execute(
                """
                SELECT c_bool, c_int2, c_int4, c_int8, c_float4, c_float8,
                c_text, c_varchar, c_bytea FROM types_roundtrip
                """
            )
            return cursor.fetch_one()

        self.assertEqual(
            run_interaction(self.conn, interaction),
            (
                True,
                7,
                1234,
                10**15,
                1.5,
                3.5,
                "hello",
                "world",
                b"\x00\x01\x02bytes",
            ),
        )

    def test_null_values_become_none(self) -> None:
        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT NULL::int, NULL::text, NULL::bool")
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, interaction), (None, None, None))

    def test_null_param(self) -> None:
        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT $1::text", [None])
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, interaction), (None,))

    def test_float4_precision_is_lossy(self) -> None:
        """float4 params are narrowed to f32; document the resulting precision."""

        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT $1::float4", [0.1])
            return cursor.fetch_one()

        row = run_interaction(self.conn, interaction)
        assert row is not None
        # 0.1 is not exactly representable in f32, so it comes back widened.
        self.assertAlmostEqual(row[0], 0.1, places=6)
        self.assertNotEqual(row[0], 0.1)

    def test_unsupported_param_type_raises_type_error(self) -> None:
        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT $1", [object()])

        with self.assertRaises(TypeError):
            run_interaction(self.conn, interaction)

    def test_int_out_of_range_for_column_raises(self) -> None:
        def interaction(cursor: Any) -> None:
            # Bind the parameter to a genuine int4 column (rather than casting
            # the *result*), so the value is encoded against INT4 and hits the
            # range guard in `PgValue::to_sql` rather than some unrelated cast
            # error. 10**12 is far too large for int4.
            cursor.execute("CREATE TEMP TABLE oor (x int4)")
            cursor.execute("INSERT INTO oor VALUES ($1)", [10**12])

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, interaction)

    # ------------------------------------------------------------------
    # rowcount()
    # ------------------------------------------------------------------

    def test_rowcount_minus_one_before_query(self) -> None:
        def interaction(cursor: Any) -> int:
            return cursor.rowcount()

        self.assertEqual(run_interaction(self.conn, interaction), -1)

    def test_rowcount_for_dml(self) -> None:
        def interaction(cursor: Any) -> list[int]:
            cursor.execute("CREATE TEMP TABLE rc (id int)")
            cursor.execute("INSERT INTO rc VALUES (1), (2), (3)")
            inserted = cursor.rowcount()
            cursor.execute("UPDATE rc SET id = id WHERE id > 1")
            updated = cursor.rowcount()
            cursor.execute("DELETE FROM rc")
            deleted = cursor.rowcount()
            return [inserted, updated, deleted]

        self.assertEqual(run_interaction(self.conn, interaction), [3, 2, 3])

    # ------------------------------------------------------------------
    # Transaction handling (COMMIT on success, ROLLBACK on error)
    # ------------------------------------------------------------------

    def test_commit_persists_changes(self) -> None:
        """A successful interaction commits; changes are visible afterwards."""

        table = "rust_pg_test_commit"
        try:

            def create(cursor: Any) -> None:
                cursor.execute(f"CREATE TABLE {table} (id int)")
                cursor.execute(f"INSERT INTO {table} VALUES (1), (2)")

            run_interaction(self.conn, create)

            def read(cursor: Any) -> list[list[Any]]:
                cursor.execute(f"SELECT id FROM {table} ORDER BY id")
                return cursor.fetch_all()

            self.assertEqual(run_interaction(self.conn, read), [(1,), (2,)])
        finally:
            run_interaction(
                self.conn,
                lambda cursor: cursor.execute(f"DROP TABLE IF EXISTS {table}"),
            )

    def test_rollback_on_exception(self) -> None:
        """If the interaction raises, its changes are rolled back."""

        table = "rust_pg_test_rollback"
        try:

            class MarkerError(Exception):
                pass

            def create_then_fail(cursor: Any) -> None:
                cursor.execute(f"CREATE TABLE {table} (id int)")
                raise MarkerError()

            with self.assertRaises(MarkerError):
                run_interaction(self.conn, create_then_fail)

            # The table creation should have been rolled back, so the table
            # must not exist.
            def exists(cursor: Any) -> Optional[list[Any]]:
                cursor.execute(
                    """
                    SELECT count(*)::int FROM information_schema.tables
                    WHERE table_name = $1
                    """,
                    [table],
                )
                return cursor.fetch_one()

            self.assertEqual(run_interaction(self.conn, exists), (0,))
        finally:
            run_interaction(
                self.conn,
                lambda cursor: cursor.execute(f"DROP TABLE IF EXISTS {table}"),
            )

    def test_connection_reusable_after_rolled_back_interaction(self) -> None:
        """A failed interaction returns the connection to a clean, usable state."""

        class MarkerError(Exception):
            pass

        def fail(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            raise MarkerError()

        with self.assertRaises(MarkerError):
            run_interaction(self.conn, fail)

        # The connection should still work for a subsequent interaction.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 99::int")
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, ok), (99,))

    def test_connection_reusable_after_server_side_error(self) -> None:
        """A *server-rejected* statement rolls back, but leaves the connection
        usable — distinct from the Python-exception rollback path above, since
        the error originates in Postgres (during prepare/execute)."""

        def bad_sql(cursor: Any) -> None:
            # A syntax error: the server rejects this during prepare, which
            # surfaces as a RuntimeError and rolls the transaction back.
            cursor.execute("SELECT FROM WHERE not valid sql")

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, bad_sql)

        # The transaction was rolled back and the connection handed back clean,
        # so a following interaction succeeds.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 7::int")
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, ok), (7,))

    def test_constraint_violation_rolls_back_and_connection_recovers(self) -> None:
        """A constraint violation mid-transaction aborts and rolls back, and the
        connection remains usable afterwards."""

        def violate(cursor: Any) -> None:
            cursor.execute("CREATE TEMP TABLE uniq (x int primary key)")
            cursor.execute("INSERT INTO uniq VALUES (1)")
            # Duplicate key. The error is reported by the server while the
            # statement's result stream is driven, so we drain it (via
            # rowcount) to surface it as a RuntimeError.
            cursor.execute("INSERT INTO uniq VALUES (1)")
            cursor.rowcount()

        with self.assertRaises(RuntimeError):
            run_interaction(self.conn, violate)

        # TEMP table lived only in the rolled-back transaction; the connection
        # itself is fine.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 1::int")
            return cursor.fetch_one()

        self.assertEqual(run_interaction(self.conn, ok), (1,))


@unittest.skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class PostgresConnectionDrivenTestCase(unittest.TestCase):
    """Tests for the *connection-driven* transaction API.

    Where ``PostgresConnectionTestCase`` runs each interaction in its own
    transaction via the ``run_interaction`` test harness, these drive the
    connection the way Synapse's own
    ``synapse.storage.database.new_transaction`` does: open a cursor with
    ``conn.cursor()``, run statements against it, then ``conn.commit()`` /
    ``conn.rollback()`` at the *connection* level (while the cursor is still
    open). They also cover the implicit ``BEGIN``, the context-manager
    protocol, ``close()`` and autocommit.
    """

    def setUp(self) -> None:
        self.conn = postgres.connect(_build_dsn())

    def tearDown(self) -> None:
        # Drop the connection before the interpreter shuts down (see the note
        # in PostgresConnectionTestCase.tearDown).
        del self.conn

    # -- small helpers ------------------------------------------------------

    def _exec_commit(self, sql: str) -> None:
        """Run a single statement and commit it (its own transaction)."""
        cursor = self.conn.cursor()
        cursor.execute(sql)
        self.conn.commit()

    def _scalar(self, sql: str) -> Any:
        """Run a query and return the first column of its single row."""
        cursor = self.conn.cursor()
        cursor.execute(sql)
        row = cursor.fetch_one()
        assert row is not None
        return row[0]

    def _table_exists(self, table: str) -> bool:
        return (
            self._scalar(
                "SELECT count(*)::int FROM information_schema.tables "
                f"WHERE table_name = '{table}'"
            )
            == 1
        )

    # -- commit / rollback at the connection level --------------------------

    def test_commit_persists_changes(self) -> None:
        """Statements run against a cursor are persisted by ``conn.commit()``."""
        table = "rust_pg_conn_commit"
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"CREATE TABLE {table} (id int)")
            cursor.execute(f"INSERT INTO {table} VALUES (1), (2)")
            self.conn.commit()

            # A fresh cursor on the same connection (new transaction) sees them.
            read = self.conn.cursor()
            read.execute(f"SELECT id FROM {table} ORDER BY id")
            self.assertEqual(read.fetch_all(), [(1,), (2,)])
            self.conn.commit()
        finally:
            self._exec_commit(f"DROP TABLE IF EXISTS {table}")

    def test_rollback_discards_changes(self) -> None:
        """``conn.rollback()`` undoes everything since the implicit ``BEGIN``."""
        table = "rust_pg_conn_rollback"
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"CREATE TABLE {table} (id int)")
            self.conn.rollback()

            # The table creation was rolled back.
            self.assertFalse(self._table_exists(table))
            self.conn.commit()
        finally:
            self._exec_commit(f"DROP TABLE IF EXISTS {table}")

    def test_commit_without_transaction_is_noop(self) -> None:
        """commit/rollback with no statement run yet are harmless no-ops."""
        # No execute() has happened, so no implicit BEGIN was issued.
        self.conn.commit()
        self.conn.rollback()
        # The connection is still usable.
        self.assertEqual(self._scalar("SELECT 1::int"), 1)
        self.conn.commit()

    def test_two_cursors_share_one_transaction(self) -> None:
        """Cursors are cheap views over the connection's single transaction:
        work done through one is visible to the other and committed together."""
        table = "rust_pg_conn_shared"
        try:
            first = self.conn.cursor()
            first.execute(f"CREATE TABLE {table} (id int)")

            # A second cursor opened mid-transaction sees the first's work and
            # adds to the same transaction.
            second = self.conn.cursor()
            second.execute(f"INSERT INTO {table} VALUES (7)")
            self.conn.commit()

            self.assertEqual(self._scalar(f"SELECT id FROM {table}"), 7)
            self.conn.commit()
        finally:
            self._exec_commit(f"DROP TABLE IF EXISTS {table}")

    # -- context manager ----------------------------------------------------

    def test_context_manager_commits_on_success(self) -> None:
        table = "rust_pg_conn_ctx_commit"
        try:
            self._exec_commit(f"CREATE TABLE {table} (id int)")

            with self.conn as conn:
                cursor = conn.cursor()
                cursor.execute(f"INSERT INTO {table} VALUES (5)")
            # Leaving the block committed.

            self.assertEqual(self._scalar(f"SELECT id FROM {table}"), 5)
            self.conn.commit()
        finally:
            self._exec_commit(f"DROP TABLE IF EXISTS {table}")

    def test_context_manager_rolls_back_on_exception(self) -> None:
        table = "rust_pg_conn_ctx_rollback"

        class MarkerError(Exception):
            pass

        # The body lives in a helper (rather than inline) so the unconditional
        # ``raise`` doesn't make mypy treat the rest of the test as unreachable.
        def insert_then_fail() -> None:
            with self.conn as conn:
                cursor = conn.cursor()
                cursor.execute(f"INSERT INTO {table} VALUES (6)")
                raise MarkerError()

        try:
            self._exec_commit(f"CREATE TABLE {table} (id int)")

            with self.assertRaises(MarkerError):
                insert_then_fail()

            # The exception caused a rollback, so the row isn't there.
            self.assertEqual(self._scalar(f"SELECT count(*)::int FROM {table}"), 0)
            self.conn.commit()
        finally:
            self._exec_commit(f"DROP TABLE IF EXISTS {table}")

    # -- close() ------------------------------------------------------------

    def test_use_after_close_raises(self) -> None:
        self.conn.close()
        cursor = self.conn.cursor()
        with self.assertRaises(RuntimeError):
            cursor.execute("SELECT 1")

    def test_close_is_idempotent(self) -> None:
        self.conn.close()
        # A second close is fine.
        self.conn.close()

    def test_cursor_close_discards_result_set(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        # The result set is gone; a fetch now errors ("no active query").
        with self.assertRaises(RuntimeError):
            cursor.fetch_one()
        self.conn.rollback()

    # -- autocommit ---------------------------------------------------------

    def test_autocommit_persists_without_explicit_commit(self) -> None:
        """In autocommit mode each statement commits on its own, so a later
        ``rollback()`` doesn't undo it."""
        table = "rust_pg_conn_autocommit"
        try:
            self.conn.set_autocommit(True)
            cursor = self.conn.cursor()
            cursor.execute(f"CREATE TABLE {table} (id int)")
            # No implicit transaction was opened, so this rolls back nothing.
            self.conn.rollback()

            self.assertTrue(self._table_exists(table))
        finally:
            self.conn.set_autocommit(True)
            self.conn.cursor().execute(f"DROP TABLE IF EXISTS {table}")

    def test_set_autocommit_rejected_mid_transaction(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1")  # opens an implicit transaction
        with self.assertRaises(RuntimeError):
            self.conn.set_autocommit(True)
        self.conn.rollback()


@unittest.skip_unless(
    bool(USE_POSTGRES_FOR_TESTS) and POSTGRES_HOST in (None, "", "localhost"),
    "requires Postgres reachable on libpq's default host",
)
class PostgresDefaultHostTestCase(unittest.TestCase):
    """Covers the libpq default-host fixup in ``connect``.

    When the DSN omits ``host=``, ``tokio-postgres`` would default to localhost,
    but Synapse wants libpq's default (honouring ``PGHOST`` / the compiled-in
    socket dir). This only runs when the test Postgres is actually reachable on
    that default host, so it's guarded separately from the main suite.
    """

    def test_connect_without_host_uses_libpq_default(self) -> None:
        # A DSN with no host=: connecting at all proves the fixup resolved a
        # usable default host rather than tokio-postgres' own localhost guess.
        parts = [f"dbname={POSTGRES_BASE_DB}"]
        if POSTGRES_USER is not None:
            parts.append(f"user={POSTGRES_USER}")
        if POSTGRES_PORT is not None:
            parts.append(f"port={POSTGRES_PORT}")
        if POSTGRES_PASSWORD is not None:
            parts.append(f"password={POSTGRES_PASSWORD}")
        conn = postgres.connect(" ".join(parts))
        try:
            self.assertEqual(
                run_interaction(conn, lambda cursor: _select_one(cursor)), (1,)
            )
        finally:
            del conn


def _select_one(cursor: Any) -> Optional[list[Any]]:
    cursor.execute("SELECT 1::int")
    return cursor.fetch_one()
