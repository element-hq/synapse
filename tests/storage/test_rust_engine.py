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

"""Tests for :class:`synapse.storage.engines.postgres_rust.RustPostgresEngine`."""

from typing import Any

from synapse.storage.engines import PostgresEngine, Psycopg2Engine, create_engine
from synapse.storage.engines.postgres_rust import RustPostgresEngine
from synapse.synapse_rust.database import postgres

from tests import unittest
from tests.unittest import skip_unless
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


class RustPostgresEngineTestCase(unittest.TestCase):
    """The engine's connection-independent behaviour (no database needed)."""

    def setUp(self) -> None:
        self.engine = RustPostgresEngine({})

    def test_module_is_the_rust_backend(self) -> None:
        # The transaction driver catches `engine.module.<Error>`, so the module
        # must be the Rust backend's DBAPI2 hierarchy, not psycopg2's.
        self.assertIs(self.engine.module, postgres)

    def test_convert_param_style_rewrites_to_dollar_placeholders(self) -> None:
        self.assertEqual(
            self.engine.convert_param_style("SELECT * FROM t WHERE a = ? AND b = ?"),
            "SELECT * FROM t WHERE a = $1 AND b = $2",
        )
        # No placeholders: unchanged. Past nine: decimal, not a single digit.
        self.assertEqual(self.engine.convert_param_style("SELECT 1"), "SELECT 1")
        self.assertTrue(
            self.engine.convert_param_style("VALUES " + "(?)" * 11).endswith("($11)")
        )

    def test_is_deadlock_matches_rust_database_error_pgcodes(self) -> None:
        for pgcode in ("40001", "40P01"):
            err = postgres.DatabaseError("boom")
            err.pgcode = pgcode
            self.assertTrue(self.engine.is_deadlock(err))

        # A DatabaseError with some other pgcode is not a deadlock.
        other = postgres.DatabaseError("nope")
        other.pgcode = "23505"
        self.assertFalse(self.engine.is_deadlock(other))

        # Neither is an unrelated exception.
        self.assertFalse(self.engine.is_deadlock(ValueError("unrelated")))

    def test_isolation_level_override_not_yet_supported(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.engine.attempt_to_set_isolation_level(object(), None)

    def test_create_engine_selects_rust_only_when_opted_in(self) -> None:
        # Default: the psycopg2 engine.
        self.assertIsInstance(create_engine({"name": "psycopg2"}), Psycopg2Engine)

        # With the opt-in flag: the Rust engine — still a PostgresEngine, so the
        # storage layer's `isinstance(engine, PostgresEngine)` dialect checks hold.
        engine = create_engine({"name": "psycopg2", "use_rust_driver": True})
        self.assertIsInstance(engine, RustPostgresEngine)
        self.assertIsInstance(engine, PostgresEngine)
        self.assertNotIsInstance(engine, Psycopg2Engine)


@skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class RustPostgresEngineConnectionTestCase(unittest.TestCase):
    """The engine's connection-touching checks, against a live shim connection."""

    def setUp(self) -> None:
        self.engine = RustPostgresEngine({})
        self.pool = postgres.ConnectionPool(_build_dsn())
        self.conn = self.pool.connect()

    def tearDown(self) -> None:
        del self.conn
        self.pool.close()

    def _exec(self, sql: str) -> Any:
        cursor = self.conn.cursor()
        cursor.execute(sql)
        return cursor

    def test_in_transaction_tracks_the_transaction(self) -> None:
        self.assertFalse(self.engine.in_transaction(self.conn))
        # The first statement lazily opens a transaction.
        self._exec("SELECT 1")
        self.assertTrue(self.engine.in_transaction(self.conn))
        self.conn.commit()
        self.assertFalse(self.engine.in_transaction(self.conn))

    def test_is_connection_closed(self) -> None:
        self.assertFalse(self.engine.is_connection_closed(self.conn))
        self.conn.close()
        self.assertTrue(self.engine.is_connection_closed(self.conn))

    def test_attempt_to_set_autocommit(self) -> None:
        # In autocommit mode the shim issues no implicit BEGIN, so a statement
        # does not open a transaction.
        self.engine.attempt_to_set_autocommit(self.conn, True)
        self._exec("SELECT 1")
        self.assertFalse(self.engine.in_transaction(self.conn))

        # Turning it back off restores the transactional default.
        self.engine.attempt_to_set_autocommit(self.conn, False)
        self._exec("SELECT 1")
        self.assertTrue(self.engine.in_transaction(self.conn))
        self.conn.rollback()
