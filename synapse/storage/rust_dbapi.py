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

from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from synapse.storage.types import SQLQueryParameters


def _quote_dsn_value(value: Any) -> str:
    """Quote a value for a libpq keyword/value connection string.

    Values with spaces or quotes must be single-quoted with `\\` and `'`
    backslash-escaped; an empty value must be `''`.
    """
    s = str(value)
    if s == "" or any(c in s for c in " '\\"):
        escaped = s.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return s


# psycopg2 accepts a few connection kwargs that are *not* libpq keywords and
# translates them itself. The Rust pool instead parses a strict libpq DSN
# (via ``tokio_postgres``), which only knows the real keywords, so we map the
# aliases here. ``database`` -> ``dbname`` is the important one: Synapse's
# sample config and most real deployments spell the database name ``database``.
_PSYCOPG2_KEY_ALIASES = {"database": "dbname"}


def build_dsn(params: Mapping[str, Any]) -> str:
    """Build a libpq keyword/value DSN from psycopg2-style connection kwargs.

    Synapse's database `args` are psycopg2 connection kwargs — mostly libpq
    keywords (``user``, ``host``, ``port``, ``password``, …), but ``database``
    is a psycopg2 alias for libpq's ``dbname`` (see ``_PSYCOPG2_KEY_ALIASES``).
    The Rust pool takes a strict libpq DSN string rather than kwargs, so join
    them into one, mapping any aliases to their real keyword. ``None`` values
    are skipped, matching psycopg2's handling of `None` kwargs (fall back to the
    libpq default).
    """
    return " ".join(
        f"{_PSYCOPG2_KEY_ALIASES.get(key, key)}={_quote_dsn_value(value)}"
        for key, value in params.items()
        if value is not None
    )


# libpq TLS keywords. psycopg2 forwards these to libpq; the Rust pool takes them
# as explicit params to :class:`ConnectionPool` rather than in the DSN, because
# ``tokio_postgres`` can't parse the cert-file paths and rejects the
# ``verify-ca`` / ``verify-full`` modes. See ``rust/src/database/postgres/tls.rs``.
SSL_PARAM_KEYS = ("sslmode", "sslrootcert", "sslcert", "sslkey", "sslpassword")


def split_ssl_params(
    args: Mapping[str, Any],
) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Split connection args into (DSN args, TLS params).

    The TLS params (the ``ssl*`` keys, ``None`` values dropped) are passed to the
    Rust pool separately; everything else goes into the DSN. Keeps existing
    psycopg2-style ``database.args`` — which may carry ``sslmode`` etc. — working
    on the Rust backend.
    """
    ssl_params = {key: args[key] for key in SSL_PARAM_KEYS if args.get(key) is not None}
    dsn_args = {key: value for key, value in args.items() if key not in SSL_PARAM_KEYS}
    return dsn_args, ssl_params


def connect(
    dsn: str,
    *,
    synchronous_commit: bool = True,
    statement_timeout_ms: int | None = None,
    checkout_timeout_ms: int | None = None,
    ssl_params: Mapping[str, Any] | None = None,
) -> "Connection":
    """Open a single standalone connection for bootstrap/one-off use.

    The Rust shim is pool-only, so a lone connection is a pool of one; the
    returned :class:`Connection` keeps that pool alive for its lifetime. Used by
    ``make_conn`` for the startup connection that runs schema preparation before
    the real pool exists. ``ssl_params`` are the libpq ``ssl*`` keys (see
    :func:`split_ssl_params`); ``checkout_timeout_ms`` bounds how long opening the
    connection may block (``None``/``0`` waits indefinitely).
    """
    pool = postgres.ConnectionPool(
        dsn,
        1,
        synchronous_commit=synchronous_commit,
        statement_timeout_ms=statement_timeout_ms,
        checkout_timeout_ms=checkout_timeout_ms,
        **(ssl_params or {}),
    )
    return Connection(pool.connect(), pool=pool, owns_pool=True)


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

    def executescript(self, script: str) -> None:
        # The shim runs a multi-statement script on the simple-query protocol
        # (no parameters, no fetchable rows).
        self._exhausted = False
        self._cursor.executescript(script)

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

    def __init__(self, conn: Any, pool: Any = None, owns_pool: bool = False) -> None:
        self._conn = conn
        # The pool this connection was checked out of, if any. Used to
        # `reconnect` (check out a fresh connection). When `owns_pool` is set the
        # pool belongs solely to this connection (a bootstrap "pool of one") and
        # is closed together with it.
        self._pool = pool
        self._owns_pool = owns_pool

    def cursor(self) -> Cursor:
        return Cursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def reconnect(self) -> None:
        """Replace the underlying connection with a fresh one from the pool.

        Mirrors ``adbapi.Connection.reconnect``: the transaction driver calls it
        when a connection is found closed, or to recycle one that has hit the
        per-connection transaction limit. The current connection is returned to
        the pool (or discarded if unusable) and a new one checked out.
        """
        if self._pool is None:
            raise RuntimeError("cannot reconnect a connection with no pool")
        self._conn.close()
        self._conn = self._pool.connect()

    def close(self) -> None:
        self._conn.close()
        if self._owns_pool and self._pool is not None:
            self._pool.close()

    # -- engine-facing methods (see RustPostgresEngine) ---------------------

    def set_autocommit(self, autocommit: bool) -> None:
        self._conn.set_autocommit(autocommit)

    @property
    def autocommit(self) -> bool:
        return bool(self._conn.autocommit)

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
