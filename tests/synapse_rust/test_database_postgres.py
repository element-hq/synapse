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

from typing import Any, Optional

# The Rust `database` submodule does not yet ship type stubs.
from synapse.synapse_rust.database import postgres  # type: ignore[import-not-found]

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
    # run_interaction()
    # ------------------------------------------------------------------

    def test_run_interaction_returns_func_result(self) -> None:
        """The return value of the callback is propagated back to the caller."""

        def interaction(cursor: Any) -> str:
            return "the-result"

        self.assertEqual(self.conn.run_interaction(interaction), "the-result")

    def test_run_interaction_forwards_args_and_kwargs(self) -> None:
        """Extra positional and keyword arguments are forwarded after the cursor."""

        def interaction(cursor: Any, a: int, b: int, c: int = 0) -> int:
            cursor.execute("SELECT $1::int + $2::int + $3::int", [a, b, c])
            row = cursor.fetch_one()
            assert row is not None
            return row

        row = self.conn.run_interaction(interaction, 1, 2, c=3)
        self.assertEqual(row, (6,))

    def test_run_interaction_propagates_exception(self) -> None:
        """An exception raised in the callback propagates out of run_interaction."""

        class MarkerError(Exception):
            pass

        def interaction(cursor: Any) -> None:
            raise MarkerError("boom")

        with self.assertRaises(MarkerError):
            self.conn.run_interaction(interaction)

    # ------------------------------------------------------------------
    # execute() / fetch_one() / fetch_all()
    # ------------------------------------------------------------------

    def test_fetch_one(self) -> None:
        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 42::int, 'hello'::text")
            return cursor.fetch_one()

        self.assertEqual(self.conn.run_interaction(interaction), (42, "hello"))

    def test_fetch_one_returns_none_when_exhausted(self) -> None:
        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 1 WHERE true")
            first = cursor.fetch_one()
            second = cursor.fetch_one()
            return [first, second]

        self.assertEqual(self.conn.run_interaction(interaction), [(1,), None])

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
            self.conn.run_interaction(interaction),
            [(1, "a"), (2, "b"), (3, "c")],
        )

    def test_fetch_all_empty(self) -> None:
        def interaction(cursor: Any) -> list[list[Any]]:
            cursor.execute("SELECT 1 WHERE false")
            return cursor.fetch_all()

        self.assertEqual(self.conn.run_interaction(interaction), [])

    def test_fetch_without_query_raises(self) -> None:
        """Calling fetch before execute is an error, not a silent empty result."""

        def interaction(cursor: Any) -> None:
            cursor.fetch_one()

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

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

        self.assertEqual(self.conn.run_interaction(interaction), [1, 2, 3])

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
        batch = self.conn.run_interaction(interaction)
        self.assertEqual(batch[0], (42, "hello"))

    def test_fetch_next_batch_empty_when_no_rows(self) -> None:
        """An empty result set yields an empty batch."""

        def interaction(cursor: Any) -> list[Any]:
            cursor.execute("SELECT 1 WHERE false")
            return cursor.fetch_next_batch()

        self.assertEqual(self.conn.run_interaction(interaction), [])

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
            self.conn.run_interaction(interaction),
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

        first, second = self.conn.run_interaction(interaction)
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
            self.conn.run_interaction(interaction),
            [(n,) for n in range(1, 101)],
        )

    def test_fetch_next_batch_without_query_raises(self) -> None:
        """Calling fetch_next_batch before execute is an error."""

        def interaction(cursor: Any) -> None:
            cursor.fetch_next_batch()

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

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
            self.conn.run_interaction(interaction),
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
            self.conn.run_interaction(interaction)

    def test_fetch_next_batch_after_exhaustion_raises(self) -> None:
        """A fetch_next_batch after the one that returned [] is an error."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetch_next_batch(), [(1,)])
            self.assertEqual(cursor.fetch_next_batch(), [])  # reports exhaustion
            cursor.fetch_next_batch()  # over-fetch

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

    def test_fetch_all_twice_raises(self) -> None:
        """fetch_all exhausts the result set, so a second fetch_all is an
        error."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetch_all(), [(1,)])
            cursor.fetch_all()  # over-fetch

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

    def test_fetch_one_after_fetch_all_raises(self) -> None:
        """fetch_all reports exhaustion, so a following fetch_one is an
        error rather than None."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            cursor.fetch_all()
            cursor.fetch_one()  # over-fetch

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

    def test_exhausted_error_is_distinct_from_no_query(self) -> None:
        """The exhausted-result error names exhaustion, not a missing query, so
        the two programming errors are distinguishable."""

        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            cursor.fetch_all()
            cursor.fetch_one()

        with self.assertRaisesRegex(RuntimeError, "exhausted"):
            self.conn.run_interaction(interaction)

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
            self.conn.run_interaction(interaction),
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

        self.assertEqual(self.conn.run_interaction(interaction), (None, None, None))

    def test_null_param(self) -> None:
        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT $1::text", [None])
            return cursor.fetch_one()

        self.assertEqual(self.conn.run_interaction(interaction), (None,))

    def test_float4_precision_is_lossy(self) -> None:
        """float4 params are narrowed to f32; document the resulting precision."""

        def interaction(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT $1::float4", [0.1])
            return cursor.fetch_one()

        row = self.conn.run_interaction(interaction)
        assert row is not None
        # 0.1 is not exactly representable in f32, so it comes back widened.
        self.assertAlmostEqual(row[0], 0.1, places=6)
        self.assertNotEqual(row[0], 0.1)

    def test_unsupported_param_type_raises_type_error(self) -> None:
        def interaction(cursor: Any) -> None:
            cursor.execute("SELECT $1", [object()])

        with self.assertRaises(TypeError):
            self.conn.run_interaction(interaction)

    def test_int_out_of_range_for_column_raises(self) -> None:
        def interaction(cursor: Any) -> None:
            # Bind the parameter to a genuine int4 column (rather than casting
            # the *result*), so the value is encoded against INT4 and hits the
            # range guard in `PgValue::to_sql` rather than some unrelated cast
            # error. 10**12 is far too large for int4.
            cursor.execute("CREATE TEMP TABLE oor (x int4)")
            cursor.execute("INSERT INTO oor VALUES ($1)", [10**12])

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(interaction)

    # ------------------------------------------------------------------
    # rowcount()
    # ------------------------------------------------------------------

    def test_rowcount_minus_one_before_query(self) -> None:
        def interaction(cursor: Any) -> int:
            return cursor.rowcount()

        self.assertEqual(self.conn.run_interaction(interaction), -1)

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

        self.assertEqual(self.conn.run_interaction(interaction), [3, 2, 3])

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

            self.conn.run_interaction(create)

            def read(cursor: Any) -> list[list[Any]]:
                cursor.execute(f"SELECT id FROM {table} ORDER BY id")
                return cursor.fetch_all()

            self.assertEqual(self.conn.run_interaction(read), [(1,), (2,)])
        finally:
            self.conn.run_interaction(
                lambda cursor: cursor.execute(f"DROP TABLE IF EXISTS {table}")
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
                self.conn.run_interaction(create_then_fail)

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

            self.assertEqual(self.conn.run_interaction(exists), (0,))
        finally:
            self.conn.run_interaction(
                lambda cursor: cursor.execute(f"DROP TABLE IF EXISTS {table}")
            )

    def test_connection_reusable_after_rolled_back_interaction(self) -> None:
        """A failed interaction returns the connection to a clean, usable state."""

        class MarkerError(Exception):
            pass

        def fail(cursor: Any) -> None:
            cursor.execute("SELECT 1")
            raise MarkerError()

        with self.assertRaises(MarkerError):
            self.conn.run_interaction(fail)

        # The connection should still work for a subsequent interaction.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 99::int")
            return cursor.fetch_one()

        self.assertEqual(self.conn.run_interaction(ok), (99,))

    def test_connection_reusable_after_server_side_error(self) -> None:
        """A *server-rejected* statement rolls back, but leaves the connection
        usable — distinct from the Python-exception rollback path above, since
        the error originates in Postgres (during prepare/execute)."""

        def bad_sql(cursor: Any) -> None:
            # A syntax error: the server rejects this during prepare, which
            # surfaces as a RuntimeError and rolls the transaction back.
            cursor.execute("SELECT FROM WHERE not valid sql")

        with self.assertRaises(RuntimeError):
            self.conn.run_interaction(bad_sql)

        # The transaction was rolled back and the connection handed back clean,
        # so a following interaction succeeds.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 7::int")
            return cursor.fetch_one()

        self.assertEqual(self.conn.run_interaction(ok), (7,))

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
            self.conn.run_interaction(violate)

        # TEMP table lived only in the rolled-back transaction; the connection
        # itself is fine.
        def ok(cursor: Any) -> Optional[list[Any]]:
            cursor.execute("SELECT 1::int")
            return cursor.fetch_one()

        self.assertEqual(self.conn.run_interaction(ok), (1,))


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
                conn.run_interaction(lambda cursor: _select_one(cursor)), (1,)
            )
        finally:
            del conn


def _select_one(cursor: Any) -> Optional[list[Any]]:
    cursor.execute("SELECT 1::int")
    return cursor.fetch_one()
