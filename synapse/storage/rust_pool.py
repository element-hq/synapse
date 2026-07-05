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

"""A Twisted connection-pool adapter backed by the native Rust Postgres pool.

Synapse's transaction functions are synchronous and expect a DBAPI2 connection.
This adapter lets them run unchanged against the Rust ``Connection`` / ``Cursor``
shim: it owns a dedicated Twisted thread pool and, for each call, checks a
connection out of the native Rust ``ConnectionPool``, wraps it in the DBAPI2
adapter (:mod:`synapse.storage.rust_dbapi`), runs the caller's function against
it on a worker thread, then returns the connection to the pool and hands the
result back to the reactor as a ``Deferred``.

It presents the slice of ``twisted.enterprise.adbapi.ConnectionPool`` that
``DatabasePool`` uses — ``runWithConnection``, ``threadID``, ``threadpool`` and
``running`` — plus the ``runQuery`` / ``runOperation`` / ``connect`` convenience
methods that some tests call on ``_db_pool`` directly, so it can stand in for it
(paired with :class:`~synapse.storage.engines.RustPostgresEngine`, which drives
the wrapped connection). ``make_pool`` returns one of these when the database is
configured with ``use_rust_driver``.
"""

import logging
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

from typing_extensions import Concatenate, ParamSpec

from twisted.internet import threads
from twisted.python.threadpool import ThreadPool

from synapse.storage.rust_dbapi import Connection as DBAPI2Connection
from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from twisted.internet.defer import Deferred

    from synapse.types import ISynapseReactor

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class RustConnectionPool:
    """Runs blocking database functions against pooled Rust connections.

    Each :meth:`runWithConnection` call runs its function on a worker thread
    with a (DBAPI2-adapter) connection checked out of the native Rust pool, and
    returns a *raw* ``Deferred`` that fires on the reactor thread with the
    result (or an errback if it raised) — like
    ``adbapi.ConnectionPool.runWithConnection``, logcontext handling is the
    caller's job (``DatabasePool.runWithConnection`` supplies the single
    ``make_deferred_yieldable``; see :meth:`runWithConnection`).
    """

    def __init__(
        self,
        reactor: "ISynapseReactor",
        dsn: str,
        *,
        name: str,
        threads: int = 10,
        synchronous_commit: bool = True,
        statement_timeout_ms: int | None = None,
        ssl_params: "dict[str, Any] | None" = None,
    ) -> None:
        """
        Args:
            reactor: the reactor in whose main thread Deferreds are fired.
            dsn: a libpq-style connection string for the Rust pool.
            name: a label for the thread pool (used in logs / metrics).
            threads: the maximum number of worker threads. The Rust connection
                pool is sized to match, since each worker holds at most one
                connection at a time — a 1:1 cap avoids both starvation and
                idle connections.
            synchronous_commit: passed to each pooled connection's session setup.
            statement_timeout_ms: passed to each pooled connection's session
                setup (statements running longer are aborted).
            ssl_params: the libpq ``ssl*`` keys (see
                :func:`synapse.storage.rust_dbapi.split_ssl_params`), passed to
                each pooled connection's TLS setup.

        The owner is responsible for the lifecycle: call :meth:`start` before
        use and :meth:`close` on shutdown (e.g. via the Synapse clock's
        ``add_system_event_trigger``). The pool does not register a shutdown
        hook itself, so it needs no clock and stays trivially testable.
        """
        self._reactor = reactor
        self._dsn = dsn
        self._synchronous_commit = synchronous_commit
        self._statement_timeout_ms = statement_timeout_ms
        self._ssl_params = dict(ssl_params or {})
        self._threads = threads
        self._pool: Any = self._open_pool()
        self.threadpool = ThreadPool(minthreads=1, maxthreads=threads, name=name)
        self.running = False
        # Connections handed out by `connect()` for direct synchronous use, one
        # cached per thread (adbapi's model). Closed together with the pool.
        self._connections: dict[int, DBAPI2Connection] = {}
        # How a connection is obtained from the pool (adbapi interface). Held as
        # a swappable attribute so tests can replace it to simulate a database
        # outage; called as `connectionFactory(self)`.
        self.connectionFactory = self._default_connection_factory

    def _open_pool(self) -> Any:
        """Open a fresh native Rust connection pool from the stored config."""
        return postgres.ConnectionPool(
            self._dsn,
            self._threads,
            synchronous_commit=self._synchronous_commit,
            statement_timeout_ms=self._statement_timeout_ms,
            **self._ssl_params,
        )

    def _default_connection_factory(
        self, _pool: "RustConnectionPool"
    ) -> DBAPI2Connection:
        # Check a connection out of the native pool and wrap it in the DBAPI2
        # adapter. `owns_pool=False`: the pool is shared and outlives the
        # checkout. Mirrors `adbapi.ConnectionPool.connectionFactory`.
        pool = self._pool
        if pool is None:
            # `close()` dropped the native pool and no `start()` has reopened
            # it; a clear error beats an AttributeError on None.
            raise RuntimeError("connection pool has been closed")
        return DBAPI2Connection(pool.connect(), pool=pool)

    def start(self) -> None:
        """Start the thread pool. Idempotent.

        Reopens the native connection pool if a previous :meth:`close` shut it
        down, so a closed pool can be brought back up (as the database-outage
        tests do with a `close()`/`start()` cycle).
        """
        if self.running:
            return
        if self._pool is None:
            self._pool = self._open_pool()
        self.threadpool.start()
        self.running = True

    def close(self) -> None:
        """Stop the thread pool and close the connection pool. Idempotent.

        Stops the thread pool first (waiting for in-flight work to finish, which
        returns its connection to the pool), then closes the Rust pool so its
        server connections are dropped promptly rather than lingering until
        garbage collection. The pool can be reopened by a later :meth:`start`.
        """
        if not self.running:
            return
        self.running = False
        self.threadpool.stop()
        # Snapshot: a concurrent `connect()` on another thread must not change
        # the dict's size under this iteration.
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def runWithConnection(  # noqa: N802 (implements adbapi's interface)
        self,
        func: Callable[Concatenate[Any, P], R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> "Deferred[R]":
        """Run ``func(conn, *args, **kwargs)`` on a worker thread.

        ``conn`` is a DBAPI2-adapter connection wrapping one checked out of the
        Rust pool for the duration of the call. As with adbapi, any transaction
        the function leaves open is committed on success and rolled back on
        error; the connection is returned to the pool afterwards regardless.
        (One carve-out: ``DatabasePool.runWithConnection`` callers passing
        ``isolation_level`` have anything uncommitted rolled back by the
        isolation-level reset *before* this commit runs — psycopg2-matching
        semantics; such functions must commit their own work.)

        Named to match ``twisted.enterprise.adbapi.ConnectionPool`` so this can
        stand in for ``DatabasePool._db_pool``.

        Returns:
            A raw ``Deferred`` — like ``adbapi.ConnectionPool.runWithConnection``
            (and unlike a `make_deferred_yieldable`-wrapped one), because the
            caller wraps it: ``DatabasePool.runWithConnection`` supplies the
            single ``make_deferred_yieldable``, and the DB function sets up its
            own ``LoggingContext``. Wrapping it here as well (double
            ``make_deferred_yieldable``, plus a nested logcontext) breaks
            logcontext handling when the awaiting request is cancelled.
        """
        if not self.running:
            raise RuntimeError("connection pool is not running")

        return threads.deferToThreadPool(
            self._reactor, self.threadpool, self._run, func, args, kwargs
        )

    def threadID(self) -> int:  # noqa: N802 (implements adbapi's interface)
        """Identify the current worker thread (adbapi interface).

        Used by ``DatabasePool`` only when a per-connection transaction limit is
        configured, to count transactions per thread.
        """
        return threading.get_ident()

    def runQuery(  # noqa: N802 (implements adbapi's interface)
        self, *args: Any, **kwargs: Any
    ) -> "Deferred[list[Any]]":
        """Run a single query in its own transaction and return all its rows.

        Mirrors ``twisted.enterprise.adbapi.ConnectionPool.runQuery``: the
        arguments are passed straight to the cursor's ``execute``, the
        transaction is committed on success (rolled back on error), and the
        result is the ``fetchall()``. Synapse itself goes through
        ``DatabasePool``; this is here for callers that use ``_db_pool``
        directly (e.g. tests).
        """
        return self.runWithConnection(self._run_query, args, kwargs)

    def runOperation(  # noqa: N802 (implements adbapi's interface)
        self, *args: Any, **kwargs: Any
    ) -> "Deferred[None]":
        """Run a single statement in its own transaction, discarding any rows.

        Mirrors ``twisted.enterprise.adbapi.ConnectionPool.runOperation``.
        """
        return self.runWithConnection(self._run_operation, args, kwargs)

    def connect(self) -> DBAPI2Connection:
        """Return a connection for direct, synchronous use on the caller's thread.

        Mirrors ``twisted.enterprise.adbapi.ConnectionPool.connect``: one
        connection is cached per thread and reused (recreated if it has been
        closed), and all cached connections are closed when the pool closes.
        Unlike ``runWithConnection`` this does no thread hop — the caller drives
        the connection itself, as schema-preparation code and some tests do.
        """
        tid = self.threadID()
        conn = self._connections.get(tid)
        if conn is None or conn.is_closed():
            conn = self.connectionFactory(self)
            self._connections[tid] = conn
        return conn

    def _run_query(
        self, conn: DBAPI2Connection, args: tuple, kwargs: dict
    ) -> list[Any]:
        def body(cursor: Any) -> list[Any]:
            cursor.execute(*args, **kwargs)
            return cursor.fetchall()

        return self._in_transaction(conn, body)

    def _run_operation(self, conn: DBAPI2Connection, args: tuple, kwargs: dict) -> None:
        def body(cursor: Any) -> None:
            cursor.execute(*args, **kwargs)

        self._in_transaction(conn, body)

    @staticmethod
    def _in_transaction(conn: DBAPI2Connection, body: Callable[[Any], R]) -> R:
        """Run ``body(cursor)`` in a transaction: commit on success, else roll back.

        Matches adbapi's ``_runInteraction`` ordering — the commit is part of the
        protected region, so a failure to commit also rolls back.
        """
        cursor = conn.cursor()
        try:
            result = body(cursor)
            cursor.close()
            conn.commit()
            return result
        except BaseException:  # matching adbapi, as in `_run`
            conn.rollback()
            raise

    def _run(
        self,
        func: Callable[..., R],
        args: tuple,
        kwargs: dict,
    ) -> R:
        """Worker-thread body: check out a connection, run ``func``, return it.

        Matches adbapi's ``_runWithConnection`` transaction contract: commit on
        success, roll back on exception. The commit makes callers that rely on
        the pool's implicit commit (e.g. one-shot background-update runners
        that execute DDL and return) durable, and is a no-op after functions
        like ``new_transaction`` that commit themselves. The rollback returns
        the connection to the pool *clean* so it is reused, rather than being
        discarded as mid-transaction (destroying the TCP/TLS session) every
        time a transaction function raises.

        A checkout failure surfaces as the raised exception (→ errback) before
        there is any connection to release.
        """
        # Check a connection out via `connectionFactory` (see
        # `_default_connection_factory`); the wrapper keeps the pool so `func`
        # can `reconnect`. Routing through the factory lets tests swap it to
        # simulate a checkout failure. A checkout failure surfaces as the raised
        # exception (→ errback) before there is any connection to release.
        conn = self.connectionFactory(self)
        try:
            result = func(conn, *args, **kwargs)
            conn.commit()
            return result
        except BaseException:
            # BaseException, matching adbapi: even for e.g. SystemExit the
            # transaction is rolled back so the connection goes back to the
            # pool clean (reusable) rather than mid-transaction (discarded).
            try:
                conn.rollback()
            except Exception:
                # Re-raise the original error, not the rollback failure; the
                # shim has already discarded the connection as unusable.
                logger.warning("Rollback failed on connection check-in", exc_info=True)
            raise
        finally:
            # Return the connection to the pool. The Rust shim returns a clean
            # connection for reuse and discards one left mid-transaction or
            # otherwise unusable (see the shim's disposal docs).
            conn.close()

    def __enter__(self) -> "RustConnectionPool":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()
