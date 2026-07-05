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

import logging
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from synapse.storage.types import SQLQueryParameters

logger = logging.getLogger(__name__)


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


# psycopg2 accepts a few connection kwargs that are *not* libpq keywords (or
# not tokio_postgres's spelling of them) and translates them itself. The Rust
# pool instead parses a strict libpq DSN (via ``tokio_postgres``), which only
# knows its own keywords, so we map the aliases here. ``database`` ->
# ``dbname`` is the important one: Synapse's sample config and most real
# deployments spell the database name ``database``. ``keepalives_count`` is
# libpq's name for what tokio_postgres calls ``keepalives_retries`` (and is
# what docs/postgres.md recommends setting).
_PSYCOPG2_KEY_ALIASES = {
    "database": "dbname",
    "keepalives_count": "keepalives_retries",
}

# The keywords tokio_postgres's DSN parser accepts (see ``Config::param`` in
# tokio-postgres 0.7.x, pinned by Cargo.lock; parity is asserted by
# ``test_supported_dsn_keys_are_accepted_by_the_parser``). It hard-errors on
# anything else, unlike libpq/psycopg2 which accept a much wider set — so only
# these keys may reach the DSN. ``sslmode`` is deliberately absent: the
# ``ssl*`` keys are split into TLS params before the DSN is built (see
# ``split_ssl_params``), and a DSN-level ``sslmode`` would be silently
# overridden by the pool's TLS setup — better to fail loudly.
_SUPPORTED_DSN_KEYS = frozenset(
    {
        "application_name",
        "channel_binding",
        "connect_timeout",
        "dbname",
        "host",
        "hostaddr",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_retries",
        "load_balance_hosts",
        "options",
        "password",
        "port",
        "sslnegotiation",
        "target_session_attrs",
        "tcp_user_timeout",
        "user",
    }
)

# libpq keywords whose absence cannot change where we connect, how we
# authenticate, or whether the connection is encrypted; these are dropped with
# a warning, for compatibility with psycopg2-era configs. Anything else
# unknown is a hard error: silently dropping e.g. ``service`` or ``sslcrl``
# could connect to the wrong database or downgrade security.
_DROPPABLE_DSN_KEYS = frozenset(
    {
        # tokio_postgres always talks UTF8 (and Synapse requires a UTF8 DB).
        "client_encoding",
        # Deprecated in libpq and a no-op since PostgreSQL 14.
        "sslcompression",
    }
)


def build_dsn(params: Mapping[str, Any]) -> str:
    """Build a libpq keyword/value DSN from psycopg2-style connection kwargs.

    Synapse's database `args` are psycopg2 connection kwargs — mostly libpq
    keywords (``user``, ``host``, ``port``, ``password``, …), but ``database``
    is a psycopg2 alias for libpq's ``dbname`` (see ``_PSYCOPG2_KEY_ALIASES``).
    The Rust pool takes a strict libpq DSN string rather than kwargs, so join
    them into one, mapping any aliases to their real keyword. ``None`` values
    are skipped, matching psycopg2's handling of `None` kwargs (fall back to the
    libpq default).

    Keywords tokio_postgres doesn't know cannot go into the DSN (its parser
    hard-errors with no hint about which key is at fault). The known-harmless
    ones (``_DROPPABLE_DSN_KEYS``) are dropped with a warning; anything else
    raises, because silently dropping a key like ``service`` or ``passfile``
    could change *which database we connect to* or quietly downgrade security.

    Raises:
        ValueError: for an arg the Rust driver can't honour, or when an alias
            and its target are both set (e.g. ``database`` and ``dbname``),
            which psycopg2 also rejects.
    """
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        mapped = _PSYCOPG2_KEY_ALIASES.get(key, key)
        if mapped != key and params.get(mapped) is not None:
            raise ValueError(
                f"database config args set both {key!r} and {mapped!r}; "
                "remove one of them"
            )
        if mapped not in _SUPPORTED_DSN_KEYS:
            if mapped in _DROPPABLE_DSN_KEYS:
                logger.warning(
                    "Ignoring database connection argument %r: "
                    "not supported by the native Rust driver",
                    key,
                )
                continue
            raise ValueError(
                f"database config arg {key!r} is not supported by the native "
                "Rust driver; remove it from `args` or disable "
                "`use_rust_driver`"
            )
        parts.append(f"{mapped}={_quote_dsn_value(value)}")
    return " ".join(parts)


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

    libpq's deprecated ``requiressl`` is translated to the ``sslmode`` it
    spells (``1`` → ``require``, else ``prefer``) rather than dropped — a
    config that demanded an encrypted connection must not silently lose that
    requirement. An explicit ``sslmode`` wins, as in libpq.
    """
    ssl_params = {key: args[key] for key in SSL_PARAM_KEYS if args.get(key) is not None}
    requiressl = args.get("requiressl")
    if requiressl is not None and "sslmode" not in ssl_params:
        truthy = str(requiressl).lower() in ("1", "true", "yes", "on")
        ssl_params["sslmode"] = "require" if truthy else "prefer"
    dsn_args = {
        key: value
        for key, value in args.items()
        if key not in SSL_PARAM_KEYS and key != "requiressl"
    }
    return dsn_args, ssl_params


def connect(
    dsn: str,
    *,
    synchronous_commit: bool = True,
    statement_timeout_ms: int | None = None,
    ssl_params: Mapping[str, Any] | None = None,
) -> "Connection":
    """Open a single standalone connection for bootstrap/one-off use.

    The Rust shim is pool-only, so a lone connection is a pool of one; the
    returned :class:`Connection` keeps that pool alive for its lifetime. Used by
    ``make_conn`` for the startup connection that runs schema preparation before
    the real pool exists. ``ssl_params`` are the libpq ``ssl*`` keys (see
    :func:`split_ssl_params`).
    """
    pool = postgres.ConnectionPool(
        dsn,
        1,
        synchronous_commit=synchronous_commit,
        statement_timeout_ms=statement_timeout_ms,
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

        Mirrors ``adbapi.Connection.reconnect`` — which closes the DBAPI
        connection and opens a brand-new one: the transaction driver calls it
        when a connection is found closed, or to recycle one that has hit the
        per-connection transaction limit (``txn_limit``, which exists to bound
        per-session server-side state). The current connection is therefore
        *discarded*, not returned to the pool — returning it would just hand
        the same live session back out on the next checkout.
        """
        if self._pool is None:
            raise RuntimeError("cannot reconnect a connection with no pool")
        self._conn.discard()
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
