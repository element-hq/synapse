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

Not yet adapted (the engine is not yet selectable via ``create_engine``, so
these are not reached): ``check_database`` / ``server_version`` still read
psycopg2 connection attributes, and per-transaction isolation-level overrides
are unimplemented. Both are follow-ups for the full ``make_pool`` wiring.
"""

import logging
from typing import TYPE_CHECKING, Any, Mapping

from synapse.storage.engines._base import AUTO_INCREMENT_PRIMARY_KEYPLACEHOLDER
from synapse.storage.engines.postgres import PostgresEngine
from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from synapse.storage.database import LoggingDatabaseConnection

logger = logging.getLogger(__name__)

# Deadlock / serialization-failure SQLSTATEs that Synapse retries.
_RETRYABLE_PGCODES = ("40001", "40P01")


class RustPostgresEngine(PostgresEngine):
    """A :class:`PostgresEngine` that talks to the Rust backend's shim."""

    def __init__(self, database_config: Mapping[str, Any]):
        super().__init__(database_config)
        # Route the DBAPI2 exception hierarchy (OperationalError, DatabaseError,
        # IntegrityError, …) to the Rust backend's classes; the transaction
        # driver catches `engine.module.<Error>`.
        self.module = postgres

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
        # Per-transaction isolation overrides are not implemented for the shim
        # yet; the connection's default level is set when the pool opens it.
        raise NotImplementedError(
            "per-transaction isolation levels are not supported by the Rust "
            "Postgres backend yet"
        )

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
