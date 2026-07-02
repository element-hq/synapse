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
``running`` — so it can stand in for ``_db_pool`` (paired with
:class:`~synapse.storage.engines.RustPostgresEngine`, which drives the wrapped
connection). What remains before ``make_pool`` can return it: ``reconnect`` on
the connection (only used on the transaction-limit / closed-connection paths)
and the startup path (``make_conn`` / ``check_database``).
"""

import logging
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

from typing_extensions import Concatenate, ParamSpec

from twisted.internet import threads
from twisted.python.threadpool import ThreadPool

from synapse.logging.context import defer_to_threadpool
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
    returns a ``Deferred`` that fires on the reactor thread with the result (or
    an errback if it raised). Log contexts are preserved across the hop,
    following the same rules as
    :func:`synapse.logging.context.defer_to_threadpool`.
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

        The owner is responsible for the lifecycle: call :meth:`start` before
        use and :meth:`close` on shutdown (e.g. via the Synapse clock's
        ``add_system_event_trigger``). The pool does not register a shutdown
        hook itself, so it needs no clock and stays trivially testable.
        """
        self._reactor = reactor
        self._pool = postgres.ConnectionPool(
            dsn,
            threads,
            synchronous_commit=synchronous_commit,
            statement_timeout_ms=statement_timeout_ms,
        )
        self.threadpool = ThreadPool(minthreads=1, maxthreads=threads, name=name)
        self.running = False

    def start(self) -> None:
        """Start the thread pool. Idempotent."""
        if self.running:
            return
        self.threadpool.start()
        self.running = True

    def close(self) -> None:
        """Stop the thread pool and close the connection pool. Idempotent.

        Stops the thread pool first (waiting for in-flight work to finish, which
        returns its connection to the pool), then closes the Rust pool so its
        server connections are dropped promptly rather than lingering until
        garbage collection.
        """
        if not self.running:
            return
        self.running = False
        self.threadpool.stop()
        self._pool.close()

    def runWithConnection(  # noqa: N802 (implements adbapi's interface)
        self,
        func: Callable[Concatenate[Any, P], R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> "Deferred[R]":
        """Run ``func(conn, *args, **kwargs)`` on a worker thread.

        ``conn`` is a DBAPI2-adapter connection wrapping one checked out of the
        Rust pool for the duration of the call. The function is responsible for
        committing or rolling back (as Synapse's ``new_transaction`` does); the
        connection is returned to the pool afterwards regardless.

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

    def _run(
        self,
        func: Callable[..., R],
        args: tuple,
        kwargs: dict,
    ) -> R:
        """Worker-thread body: check out a connection, run ``func``, return it.

        A checkout failure surfaces as the raised exception (→ errback) before
        there is any connection to release.
        """
        conn = DBAPI2Connection(self._pool.connect())
        try:
            return func(conn, *args, **kwargs)
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
