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

from types import TracebackType
from typing import Any, Optional, Sequence

from synapse.synapse_rust.runtime import RustRuntime

class ConnectionPool:
    """A pool of native `tokio_postgres` connections.

    Built once from a libpq-style DSN; connections are opened lazily. This is
    the only way to obtain a :class:`Connection`.
    """

    def __init__(
        self,
        runtime: RustRuntime,
        dsn: str,
        max_size: int = 10,
        *,
        synchronous_commit: bool = True,
        statement_timeout_ms: int | None = None,
        checkout_timeout_ms: int | None = None,
        sslmode: str | None = None,
        sslrootcert: str | None = None,
        sslcert: str | None = None,
        sslkey: str | None = None,
        sslpassword: str | None = None,
    ) -> None:
        """
        ``runtime`` is the homeserver's ``RustRuntime`` (``hs.get_rust_runtime()``);
        its tokio runtime is started on first use, so this works during schema
        setup and in tests.

        Each connection gets the same session setup when opened: the default
        (REPEATABLE READ) isolation level, plus ``synchronous_commit`` off and
        ``statement_timeout`` (milliseconds) if configured.
        ``checkout_timeout_ms`` bounds how long ``connect`` may wait for a
        connection; ``None`` or ``0`` waits indefinitely.

        The ``ssl*`` keyword arguments are the libpq TLS settings; see
        ``rust/src/database/postgres/tls.rs``.
        """

    def connect(self) -> Connection:
        """Check a connection out of the pool, blocking until one is available.

        A failure to establish the connection raises the same DBAPI2 exceptions
        a query error would.
        """

    def close(self) -> None: ...

class Connection:
    """A connection checked out of a :class:`ConnectionPool`."""

    @property
    def autocommit(self) -> bool: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...
    def discard(self) -> None:
        """Close the underlying session instead of returning it to the pool
        (which shrinks, growing back on demand). For forced recycling, where a
        fresh server session is wanted."""

    def is_closed(self) -> bool: ...
    def in_transaction(self) -> bool: ...
    def set_autocommit(self, autocommit: bool) -> None: ...
    def __enter__(self) -> Connection: ...
    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool: ...

class Cursor:
    """A PEP-249-style cursor over a connection's current transaction.

    Result-fetching uses ``fetch_one`` / ``fetch_all`` / ``fetch_next_batch`` and
    ``rowcount`` / ``description`` as *methods* (not DBAPI2 properties); the
    :class:`synapse.storage.rust_dbapi.Cursor` adapter presents the DBAPI2 shape.
    """

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> None: ...
    def executemany(self, query: str, params_seq: Sequence[Sequence[Any]]) -> None: ...
    def executescript(self, script: str) -> None: ...
    def fetch_one(self) -> Optional[tuple[Any, ...]]: ...
    def fetch_all(self) -> list[tuple[Any, ...]]: ...
    def fetch_next_batch(self, capacity: int = 100) -> list[tuple[Any, ...]]: ...
    def rowcount(self) -> int: ...
    def description(self) -> Optional[list[tuple[Any, ...]]]: ...
    def close(self) -> None: ...

# The PEP-249 exception hierarchy raised by the backend. `DatabaseError` and its
# subclasses carry the SQLSTATE as `pgcode` (a 5-character string, or `None`),
# matching psycopg2 so engine code such as `is_deadlock` can read `error.pgcode`.
class Error(Exception): ...

class DatabaseError(Error):
    pgcode: Optional[str]

class OperationalError(DatabaseError): ...
class IntegrityError(DatabaseError): ...
class ProgrammingError(DatabaseError): ...
