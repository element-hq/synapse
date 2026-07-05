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

"""Tests for the DBAPI2 adapter over the Rust Postgres shim
(:mod:`synapse.storage.rust_dbapi`), including driving a real
``LoggingTransaction`` through it."""

from synapse.config.database import DatabaseConnectionConfig
from synapse.storage import rust_dbapi
from synapse.storage.database import LoggingDatabaseConnection, make_conn
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


class BuildDsnTestCase(unittest.TestCase):
    """`build_dsn` turns psycopg2-style kwargs into a libpq DSN (no database)."""

    def test_joins_keywords(self) -> None:
        self.assertEqual(
            rust_dbapi.build_dsn(
                {"dbname": "synapse", "user": "u", "host": "db", "port": 5432}
            ),
            "dbname=synapse user=u host=db port=5432",
        )

    def test_maps_database_alias_to_dbname(self) -> None:
        # psycopg2 accepts `database` as an alias for libpq's `dbname` (Synapse's
        # sample config and most deployments use it); the strict libpq DSN the
        # Rust pool parses only knows `dbname`, so it must be translated.
        self.assertEqual(
            rust_dbapi.build_dsn({"database": "synapse", "user": "u"}),
            "dbname=synapse user=u",
        )

    def test_skips_none_values(self) -> None:
        # `None` kwargs (unset config) are omitted, as psycopg2 treats them.
        self.assertEqual(
            rust_dbapi.build_dsn({"dbname": "d", "host": None, "port": None}),
            "dbname=d",
        )

    def test_quotes_values_needing_it(self) -> None:
        # Spaces / quotes / backslashes get single-quoted and escaped; empty → ''.
        self.assertEqual(
            rust_dbapi.build_dsn({"password": "p a'ss\\x", "options": ""}),
            "password='p a\\'ss\\\\x' options=''",
        )

    def test_maps_keepalives_count_to_keepalives_retries(self) -> None:
        # libpq (and docs/postgres.md's example config) spell it
        # `keepalives_count`; tokio_postgres spells it `keepalives_retries`.
        self.assertEqual(
            rust_dbapi.build_dsn({"keepalives": 1, "keepalives_count": 3}),
            "keepalives=1 keepalives_retries=3",
        )

    def test_alias_colliding_with_its_target_raises(self) -> None:
        # Both spellings set: psycopg2 rejects database+dbname with a
        # TypeError; silently letting one win would be config-order lottery.
        with self.assertRaises(ValueError):
            rust_dbapi.build_dsn({"database": "a", "dbname": "b"})
        with self.assertRaises(ValueError):
            rust_dbapi.build_dsn({"keepalives_count": 3, "keepalives_retries": 5})

    def test_drops_known_harmless_keywords_with_a_warning(self) -> None:
        # These libpq keys can't change the connection target, auth, or
        # security posture, so psycopg2-era configs carrying them keep working.
        self.assertEqual(
            rust_dbapi.build_dsn(
                {"dbname": "d", "client_encoding": "UTF8", "sslcompression": 0}
            ),
            "dbname=d",
        )

    def test_rejects_keywords_that_could_change_target_or_security(self) -> None:
        # Silently dropping these would connect to the wrong database
        # (service/passfile) or downgrade security (sslcrl, gssencmode, ...):
        # fail loudly instead.
        for key, value in (
            ("service", "synapse-prod"),
            ("passfile", "/etc/pgpass"),
            ("sslcrl", "/etc/crl.pem"),
            ("gssencmode", "require"),
            ("ssl_min_protocol_version", "TLSv1.3"),
        ):
            with self.assertRaises(ValueError, msg=key) as ctx:
                rust_dbapi.build_dsn({"dbname": "d", key: value})
            self.assertIn(key, str(ctx.exception))

    def test_requiressl_maps_to_sslmode(self) -> None:
        # libpq's deprecated requiressl=1 spells sslmode=require; the
        # encryption requirement must not be silently dropped. An explicit
        # sslmode wins, as in libpq.
        dsn_args, ssl_params = rust_dbapi.split_ssl_params(
            {"dbname": "d", "requiressl": 1}
        )
        self.assertEqual(ssl_params, {"sslmode": "require"})
        self.assertEqual(dsn_args, {"dbname": "d"})

        _, ssl_params = rust_dbapi.split_ssl_params({"requiressl": 0})
        self.assertEqual(ssl_params, {"sslmode": "prefer"})

        _, ssl_params = rust_dbapi.split_ssl_params(
            {"requiressl": 1, "sslmode": "verify-full"}
        )
        self.assertEqual(ssl_params, {"sslmode": "verify-full"})

    def test_supported_dsn_keys_are_accepted_by_the_parser(self) -> None:
        # _SUPPORTED_DSN_KEYS mirrors tokio_postgres's Config::param keyword
        # set; if the crate is upgraded and a key is renamed or removed, this
        # catches the drift (the pool parses its DSN eagerly, no server
        # needed).
        samples = {
            "channel_binding": "disable",
            "connect_timeout": "5",
            "hostaddr": "127.0.0.1",
            "keepalives": "1",
            "load_balance_hosts": "disable",
            "sslnegotiation": "postgres",
            "target_session_attrs": "any",
        }
        for key in sorted(rust_dbapi._SUPPORTED_DSN_KEYS):
            value = samples.get(key, "1" if key.startswith("keepalives") else "x")
            if key in ("port", "tcp_user_timeout"):
                value = "5432"
            pool = postgres.ConnectionPool(f"host=h {key}={value}")
            pool.close()


@skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class RustStartupTestCase(unittest.TestCase):
    """The startup path: `make_conn` + `check_database` for the Rust engine."""

    def _db_config(self) -> DatabaseConnectionConfig:
        args: dict = {"dbname": POSTGRES_BASE_DB}
        if POSTGRES_USER is not None:
            args["user"] = POSTGRES_USER
        if POSTGRES_HOST is not None:
            args["host"] = POSTGRES_HOST
        if POSTGRES_PORT is not None:
            args["port"] = POSTGRES_PORT
        if POSTGRES_PASSWORD is not None:
            args["password"] = POSTGRES_PASSWORD
        # `name` must be a recognised engine; the Rust engine is selected by
        # passing a RustPostgresEngine to make_conn, not by the config name.
        return DatabaseConnectionConfig("master", {"name": "psycopg2", "args": args})

    def test_make_conn_check_database_and_query(self) -> None:
        engine = RustPostgresEngine({})
        db_conn = make_conn(
            db_config=self._db_config(),
            engine=engine,
            default_txn_name="startup",
            server_name="test",
        )
        try:
            # A bootstrap connection the engine can validate over a cursor.
            engine.check_database(db_conn)
            self.assertRegex(engine.server_version, r"^\d+\.\d+$")

            # And it's a working connection.
            with db_conn.cursor(txn_name="startup") as cur:
                cur.execute("SELECT 1")
                self.assertEqual(cur.fetchone(), (1,))
            db_conn.commit()
        finally:
            db_conn.close()


@skip_unless(
    bool(USE_POSTGRES_FOR_TESTS), "requires a Postgres server (set SYNAPSE_POSTGRES)"
)
class RustDBAPIAdapterTestCase(unittest.TestCase):
    """The adapter presents the DBAPI2 shape over the shim."""

    def setUp(self) -> None:
        self._pool = postgres.ConnectionPool(_build_dsn())
        # Pass the pool (owns_pool=False) so `reconnect` can check out a fresh
        # connection; tearDown closes the pool itself.
        self.conn = rust_dbapi.Connection(self._pool.connect(), pool=self._pool)
        self.engine = RustPostgresEngine({})

    def tearDown(self) -> None:
        del self.conn
        self._pool.close()

    def test_execute_and_fetchone(self) -> None:
        cursor = self.conn.cursor()
        # The adapter passes parameters straight through; the shim binds `$n`.
        cursor.execute("SELECT $1::int", (7,))
        self.assertEqual(cursor.fetchone(), (7,))
        # Exhausted → None.
        self.assertIsNone(cursor.fetchone())
        self.conn.commit()

    def test_fetchall(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT g FROM generate_series(1, 3) AS g ORDER BY g")
        self.assertEqual(cursor.fetchall(), [(1,), (2,), (3,)])
        self.conn.commit()

    def test_fetchmany_returns_at_most_size(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT g FROM generate_series(1, 3) AS g ORDER BY g")
        self.assertEqual(cursor.fetchmany(2), [(1,), (2,)])
        self.assertEqual(cursor.fetchmany(2), [(3,)])
        self.assertEqual(cursor.fetchmany(2), [])
        self.conn.commit()

    def test_iteration(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT g FROM generate_series(1, 3) AS g ORDER BY g")
        self.assertEqual(list(cursor), [(1,), (2,), (3,)])
        self.conn.commit()

    def test_description_exposes_column_names(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 AS a, 2 AS b")
        assert cursor.description is not None
        self.assertEqual([col[0] for col in cursor.description], ["a", "b"])
        self.conn.commit()

    def test_rowcount_and_executemany(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("CREATE TEMP TABLE t (id int)")
        cursor.executemany("INSERT INTO t VALUES ($1)", [(1,), (2,), (3,)])
        self.assertEqual(cursor.rowcount, 3)
        cursor.execute("SELECT id FROM t ORDER BY id")
        self.assertEqual(cursor.fetchall(), [(1,), (2,), (3,)])
        self.conn.commit()

    def test_drives_a_logging_transaction(self) -> None:
        # The whole point: a real LoggingTransaction (which converts `?` to `$n`
        # via the engine, then drives the cursor via the DBAPI2 spelling) runs
        # unchanged against the adapter.
        engine = RustPostgresEngine({})
        db_conn = LoggingDatabaseConnection(
            conn=self.conn,
            engine=engine,
            default_txn_name="test",
            server_name="test",
        )

        txn = db_conn.cursor(txn_name="test")
        txn.execute("SELECT ?::int + ?::int", (2, 3))
        self.assertEqual(txn.fetchone(), (5,))

        txn.execute("SELECT g FROM generate_series(1, 2) AS g ORDER BY g")
        self.assertEqual(list(txn), [(1,), (2,)])

        txn.execute("SELECT 1 AS only")
        assert txn.description is not None
        self.assertEqual(txn.description[0][0], "only")

        db_conn.commit()

    def test_array_parameter(self) -> None:
        # A list parameter binds as a Postgres array, for `= ANY($1)` queries.
        cursor = self.conn.cursor()
        cursor.execute("SELECT 5 = ANY($1::int[])", ([1, 5, 9],))
        self.assertEqual(cursor.fetchone(), (True,))
        cursor.execute("SELECT 7 = ANY($1::int[])", ([1, 5, 9],))
        self.assertEqual(cursor.fetchone(), (False,))
        self.conn.commit()

    def test_executescript(self) -> None:
        # A multi-statement script runs via the shim's simple-query path.
        cursor = self.conn.cursor()
        cursor.executescript(
            "CREATE TEMP TABLE s (a int); INSERT INTO s VALUES (1), (2);"
        )
        cursor.execute("SELECT count(*) FROM s")
        self.assertEqual(cursor.fetchone(), (2,))
        self.conn.commit()

    def test_autocommit_property(self) -> None:
        self.assertFalse(self.conn.autocommit)
        self.conn.set_autocommit(True)
        self.assertTrue(self.conn.autocommit)
        self.conn.set_autocommit(False)
        self.assertFalse(self.conn.autocommit)

    def test_reconnect(self) -> None:
        # `reconnect` swaps in a fresh pooled connection; the connection is still
        # usable afterwards.
        self.conn.reconnect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT 1")
        self.assertEqual(cursor.fetchone(), (1,))
        self.conn.commit()

    def test_reconnect_gets_a_fresh_server_session(self) -> None:
        # `reconnect` exists to recycle a connection (txn_limit bounds
        # per-session server state), so it must discard the old session rather
        # than return it to the pool — where, like adbapi's close-and-reopen,
        # the next checkout would just get the same session back.
        def backend_pid(conn: rust_dbapi.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_backend_pid()")
            (pid,) = cursor.fetchone()  # type: ignore[misc]
            conn.commit()
            return pid

        pool = postgres.ConnectionPool(_build_dsn(), 1)
        self.addCleanup(pool.close)
        conn = rust_dbapi.Connection(pool.connect(), pool=pool)
        self.addCleanup(conn.close)

        before = backend_pid(conn)
        conn.reconnect()
        self.assertNotEqual(backend_pid(conn), before)
