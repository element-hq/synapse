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

"""A thin DBAPI2 adapter over the native Rust Postgres shim.

The Rust ``Connection`` / ``Cursor`` shim
(:mod:`synapse.synapse_rust.database.postgres`) is close to DBAPI2 but not
identical: its cursor exposes ``fetch_one`` / ``fetch_all`` /
``fetch_next_batch`` and ``rowcount()`` / ``description()`` as *methods*, whereas
Synapse's :class:`~synapse.storage.database.LoggingTransaction` drives a cursor
through the DBAPI2 spelling — ``fetchone`` / ``fetchmany`` / ``fetchall``,
iteration, and ``rowcount`` / ``description`` as *properties*.

Rather than reshape the Rust API, these small wrappers present the DBAPI2 shape
Synapse expects and delegate to the shim. :class:`Connection` also keeps the
connection-level methods the database engine calls (``in_transaction``,
``is_closed``, ``set_autocommit``) so a wrapped connection is a drop-in for the
raw one.

Not handled here: ``execute_batch`` / ``execute_values`` (psycopg2 extras that
``LoggingTransaction`` invokes directly for ``PostgresEngine``) still need a
routing change in ``LoggingTransaction`` to reach a shim-backed implementation;
that is a separate follow-up.
"""

from typing import TYPE_CHECKING, Any, Iterator, Sequence

if TYPE_CHECKING:
    from synapse.storage.types import SQLQueryParameters


class Cursor:
    """A DBAPI2 cursor wrapping a Rust shim cursor."""

    # DBAPI2 default number of rows `fetchmany` returns when no size is given.
    arraysize = 1

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        # The shim raises if a result set is fetched past exhaustion, whereas
        # DBAPI2 wants further fetches to keep returning "no more rows". Track
        # exhaustion here so repeated / mixed fetches stay well-behaved.
        self._exhausted = False

    def execute(self, sql: str, parameters: "SQLQueryParameters" = ()) -> None:
        # DBAPI2 passes an empty sequence when there are no parameters; the shim
        # wants `None` in that case (and a list of values otherwise).
        self._exhausted = False
        self._cursor.execute(sql, list(parameters) if parameters else None)

    def executemany(self, sql: str, seq_of_parameters: Sequence[Any]) -> None:
        self._exhausted = False
        self._cursor.executemany(sql, [list(p) for p in seq_of_parameters])

    def _next(self) -> Any:
        """Fetch one row, or `None` once the result set is exhausted."""
        if self._exhausted:
            return None
        row = self._cursor.fetch_one()
        if row is None:
            self._exhausted = True
        return row

    def fetchone(self) -> Any:
        return self._next()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        # DBAPI2: return at most `size` rows (defaulting to `arraysize`). The
        # shim's `fetch_next_batch` is a size *hint* rather than a hard limit, so
        # pull rows one at a time to honour the exact contract.
        if size is None:
            size = self.arraysize
        rows = []
        for _ in range(size):
            row = self._next()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self) -> list[Any]:
        if self._exhausted:
            return []
        rows = self._cursor.fetch_all()
        self._exhausted = True
        return rows

    def __iter__(self) -> Iterator[Any]:
        while True:
            row = self._next()
            if row is None:
                return
            yield row

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount()

    @property
    def description(self) -> Any:
        return self._cursor.description()

    def close(self) -> None:
        self._cursor.close()


class Connection:
    """A DBAPI2 connection wrapping a Rust shim connection.

    ``cursor()`` returns a DBAPI2 :class:`Cursor`; the transaction-control and
    engine-facing methods delegate straight to the shim.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def cursor(self) -> Cursor:
        return Cursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    # -- engine-facing methods (see RustPostgresEngine) ---------------------

    def set_autocommit(self, autocommit: bool) -> None:
        self._conn.set_autocommit(autocommit)

    def is_closed(self) -> bool:
        return bool(self._conn.is_closed())

    def in_transaction(self) -> bool:
        return bool(self._conn.in_transaction())

    # -- context-manager protocol (matches the shim / psycopg2) -------------

    def __enter__(self) -> "Connection":
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._conn.__exit__(exc_type, exc, tb)
