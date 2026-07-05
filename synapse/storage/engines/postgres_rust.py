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

"""A database engine for the native Rust ``tokio-postgres`` backend.

This drives the Rust ``Connection`` / ``Cursor`` shim
(:mod:`synapse.synapse_rust.database.postgres`) rather than psycopg2. It reuses
:class:`PostgresEngine` for everything that is pure SQL generation or
configuration (``supports_using_any_list``, ``lock_table``, ``row_id_name``, …)
and overrides only the parts that touch a live connection or that are wired to
psycopg2 internals:

  - the DBAPI2 exception ``module`` — the Rust backend has its own hierarchy;
  - ``convert_param_style`` — the shim binds ``$1, $2, …`` placeholders, not
    psycopg2's ``%s``;
  - ``in_transaction`` / ``is_connection_closed`` / ``attempt_to_set_autocommit``
    — served by the shim's own methods;
  - ``is_deadlock`` — matches the Rust ``DatabaseError`` and its ``pgcode``;
  - ``executescript`` — uses the shim's dedicated multi-statement primitive.

Per-connection session setup (isolation level, ``synchronous_commit``,
``statement_timeout``) lives in the Rust connection pool rather than in
``on_new_connection``, so that hook is a no-op here.

"""

import logging
from typing import TYPE_CHECKING, Any, Mapping

from synapse.storage.engines._base import (
    AUTO_INCREMENT_PRIMARY_KEYPLACEHOLDER,
    IncorrectDatabaseSetup,
    IsolationLevel,
)
from synapse.storage.engines.postgres_base import PostgresEngine
from synapse.storage.types import Connection, Cursor
from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from synapse.storage.database import LoggingDatabaseConnection

logger = logging.getLogger(__name__)

# Deadlock / serialization-failure SQLSTATEs that Synapse retries.
_RETRYABLE_PGCODES = ("40001", "40P01")


class RustPostgresEngine(PostgresEngine[Connection, Cursor]):
    """A :class:`PostgresEngine` that talks to the Rust backend's shim."""

    # SQL isolation-level names for each `IsolationLevel`. The shim has no
    # psycopg2-style `set_isolation_level`, so a per-transaction override is
    # applied as a `SET SESSION CHARACTERISTICS` statement (see
    # `attempt_to_set_isolation_level`). REPEATABLE READ is the default that the
    # pool applies at connection setup, matching `PostgresEngine`.
    _ISOLATION_LEVEL_SQL: Mapping[int, str] = {
        IsolationLevel.READ_COMMITTED: "READ COMMITTED",
        IsolationLevel.REPEATABLE_READ: "REPEATABLE READ",
        IsolationLevel.SERIALIZABLE: "SERIALIZABLE",
    }
    _DEFAULT_ISOLATION_LEVEL_SQL = "REPEATABLE READ"

    def __init__(self, database_config: Mapping[str, Any]):
        # The module is the Rust backend's DBAPI2 exception hierarchy
        # (OperationalError, DatabaseError, IntegrityError, …); the transaction
        # driver catches `engine.module.<Error>`. It is an intentionally *partial*
        # `DBAPI2Module`: it exposes only the exception subset Synapse actually
        # uses and has no module-level `connect` (connections come from the pool,
        # via `rust_dbapi.connect`), so it doesn't structurally satisfy the
        # protocol — hence the ignore.
        super().__init__(postgres, database_config)  # type: ignore[arg-type]
        self._version: int | None = None  # set by check_database

        # How long a connection checkout may block before failing with an
        # operational error, rather than waiting indefinitely for the pool (a
        # free slot or a new connection). In milliseconds; `0` disables it.
        # Rust-only: the psycopg2/adbapi path has no equivalent hook.
        self.pool_checkout_timeout_ms: int = database_config.get(
            "pool_checkout_timeout", 30000
        )

    def convert_param_style(self, sql: str) -> str:
        # The shim binds positional `$1, $2, ...` placeholders (like libpq),
        # not psycopg2's `%s`. Rewrite `?` left-to-right, matching the Rust-side
        # `convert_placeholders`; callers must parameterise rather than embed a
        # literal `?`.
        out = []
        n = 0
        for ch in sql:
            if ch == "?":
                n += 1
                out.append(f"${n}")
            else:
                out.append(ch)
        return "".join(out)

    def on_new_connection(self, db_conn: "LoggingDatabaseConnection") -> None:
        # No-op: per-connection session setup happens in the Rust connection
        # pool's connection manager, not here.
        pass

    def is_deadlock(self, error: Exception) -> bool:
        if isinstance(error, postgres.DatabaseError):
            return getattr(error, "pgcode", None) in _RETRYABLE_PGCODES
        return False

    def is_connection_closed(self, conn: Any) -> bool:
        return bool(conn.is_closed())

    def in_transaction(self, conn: Any) -> bool:
        return bool(conn.in_transaction())

    def attempt_to_set_autocommit(self, conn: Any, autocommit: bool) -> None:
        conn.set_autocommit(autocommit)

    def attempt_to_set_isolation_level(
        self, conn: Any, isolation_level: int | None
    ) -> None:
        # psycopg2 has `conn.set_isolation_level`; the shim does not, so set the
        # session's default transaction isolation directly. It applies to the
        # next transaction the caller opens — `new_transaction` sets it before
        # the transaction and resets it (`isolation_level=None`) afterwards, so
        # the override is scoped to that one transaction. `isolation_level` is a
        # fixed `IsolationLevel`, so the interpolated name is not user-controlled.
        if isolation_level is None:
            level_sql = self._DEFAULT_ISOLATION_LEVEL_SQL
        else:
            level_sql = self._ISOLATION_LEVEL_SQL[isolation_level]

        # psycopg2's `set_isolation_level` implicitly rolls back any pending
        # transaction before changing the session; mirror that. This matters
        # when the reset runs from `runWithConnection`'s `finally` after the
        # transaction function raised without `new_transaction` rolling back
        # (anything but an OperationalError/deadlock): without the rollback the
        # `SET` would either fail with 25P02 on an aborted transaction (masking
        # the original error) or — worse — the commit below would persist the
        # half-finished writes.
        conn.rollback()

        # `SET SESSION CHARACTERISTICS` only affects *subsequent* transactions,
        # so commit it (SET is transactional — a rollback would undo it) before
        # the caller's transaction begins. The rollback above ensures the
        # transaction being committed contains nothing but the `SET`.
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL "
                + level_sql
            )
        finally:
            cursor.close()
        conn.commit()

    @staticmethod
    def executescript(cursor: Any, script: str) -> None:
        # Use the shim's dedicated multi-statement primitive rather than
        # psycopg2's "just execute it" behaviour. The script runs in the
        # connection's current transaction (opened lazily) and is left open for
        # the caller to commit — intentionally *without* psycopg2's
        # `COMMIT; BEGIN TRANSACTION;` prefix, so a whole schema-upgrade run
        # commits atomically at the end (see BaseDatabaseEngine.executescript).
        script = script.replace(
            AUTO_INCREMENT_PRIMARY_KEYPLACEHOLDER,
            "BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY",
        )
        cursor.executescript(script)

    def check_database(
        self, db_conn: Any, allow_outdated_version: bool = False
    ) -> None:
        # The shim has no psycopg2-style `conn.server_version`, so read the
        # version (and encoding) over a cursor instead.
        allow_unsafe_locale = self.config.get("allow_unsafe_locale", False)

        with db_conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            self._version = int(cur.fetchone()[0])

            # Are we on a supported PostgreSQL version?
            if not allow_outdated_version and self._version < 140000:
                raise RuntimeError("Synapse requires PostgreSQL 14 or above.")

            cur.execute("SHOW SERVER_ENCODING")
            rows = cur.fetchall()
            if rows and rows[0][0] != "UTF8":
                raise IncorrectDatabaseSetup(
                    "Database has incorrect encoding: '%s' instead of 'UTF8'\n"
                    "See docs/postgres.md for more information." % (rows[0][0],)
                )

            collation, ctype = self.get_db_locale(cur)
            if collation != "C":
                logger.warning(
                    "Database has incorrect collation of %r. Should be 'C'",
                    collation,
                )
                if not allow_unsafe_locale:
                    raise IncorrectDatabaseSetup(
                        "Database has incorrect collation of %r. Should be 'C'\n"
                        "See docs/postgres.md for more information. You can override this check by"
                        "setting 'allow_unsafe_locale' to true in the database config.",
                        collation,
                    )

            if ctype != "C" and not allow_unsafe_locale:
                logger.warning(
                    "Database has incorrect ctype of %r. Should be 'C'",
                    ctype,
                )
                raise IncorrectDatabaseSetup(
                    "Database has incorrect ctype of %r. Should be 'C'\n"
                    "See docs/postgres.md for more information. You can override this check by"
                    "setting 'allow_unsafe_locale' to true in the database config.",
                    ctype,
                )

    @property
    def server_version(self) -> str:
        """Returns a string giving the server version. For example: '14.4'."""
        numver = self._version
        assert numver is not None, "check_database must be called first"
        # Supported versions are all >= 10, so use the two-part form.
        # https://www.postgresql.org/docs/current/libpq-status.html#LIBPQ-PQSERVERVERSION
        return "%i.%i" % (numver / 10000, numver % 10000)
