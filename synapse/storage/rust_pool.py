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
connection out of the native Rust ``ConnectionPool``, runs the caller's function
against it on a worker thread, then returns the connection to the pool and hands
the result back to the reactor as a ``Deferred``.

This is deliberately a thin *execution bridge*. Slotting it into
``DatabasePool`` (so ``runInteraction`` flows through it) additionally needs
engine-level support for the shim connection — ``in_transaction``,
``is_connection_closed``, autocommit / isolation and ``reconnect`` — and is left
to a follow-up.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from typing_extensions import Concatenate, ParamSpec

from twisted.python.threadpool import ThreadPool

from synapse.logging.context import defer_to_threadpool
from synapse.synapse_rust.database import postgres

if TYPE_CHECKING:
    from twisted.internet.defer import Deferred

    from synapse.synapse_rust.runtime import RustRuntime
    from synapse.types import ISynapseReactor

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class RustConnectionPool:
    """Runs blocking database functions against pooled Rust connections.

    Each :meth:`run_with_connection` call runs its function on a worker thread
    with a connection checked out of the native Rust pool, and returns a
    ``Deferred`` that fires on the reactor thread with the result (or an
    errback if it raised). Log contexts are preserved across the hop, following
    the same rules as :func:`synapse.logging.context.defer_to_threadpool`.
    """

    def __init__(
        self,
        reactor: "ISynapseReactor",
        runtime: "RustRuntime",
        dsn: str,
        *,
        name: str,
        threads: int = 10,
    ) -> None:
        """
        Args:
            reactor: the reactor in whose main thread Deferreds are fired.
            runtime: the homeserver's ``RustRuntime``, whose tokio runtime
                drives the native connection pool.
            dsn: a libpq-style connection string for the Rust pool.
            name: a label for the thread pool (used in logs / metrics).
            threads: the maximum number of worker threads. The Rust connection
                pool is sized to match, since each worker holds at most one
                connection at a time — a 1:1 cap avoids both starvation and
                idle connections.

        The owner is responsible for the lifecycle: call :meth:`start` before
        use and :meth:`close` on shutdown (e.g. via the Synapse clock's
        ``add_system_event_trigger``). The pool does not register a shutdown
        hook itself, so it needs no clock and stays trivially testable.
        """
        self._reactor = reactor
        self._pool = postgres.ConnectionPool(runtime, dsn, threads)
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

    def run_with_connection(
        self,
        func: Callable[Concatenate[Any, P], R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> "Deferred[R]":
        """Run ``func(conn, *args, **kwargs)`` on a worker thread.

        ``conn`` is a connection checked out of the Rust pool for the duration
        of the call. As with ``adbapi.ConnectionPool.runWithConnection``, any
        transaction the function leaves open is committed if it returns and
        rolled back if it raises; the connection is returned to the pool
        afterwards regardless.

        Returns:
            A ``Deferred`` firing with ``func``'s result, following the Synapse
            logcontext rules (``yield`` / ``await`` it).
        """
        if not self.running:
            raise RuntimeError("connection pool is not running")

        return defer_to_threadpool(
            self._reactor, self.threadpool, self._run, func, args, kwargs
        )

    def _run(
        self,
        func: Callable[..., R],
        args: tuple,
        kwargs: dict,
    ) -> R:
        """Worker-thread body: check out a connection, run ``func``, return it.

        Matches adbapi's ``_runWithConnection`` transaction contract: commit on
        success, roll back on exception. The commit makes callers that rely on
        the pool's implicit commit (e.g. one-shot runners that execute DDL and
        return) durable, and is a no-op after functions like
        ``new_transaction`` that commit themselves. The rollback returns the
        connection to the pool *clean* so it is reused, rather than being
        discarded as mid-transaction (tearing down the TCP session) every time
        a transaction function raises.

        A checkout failure surfaces as the raised exception (→ errback) before
        there is any connection to release.
        """
        conn = self._pool.connect()
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
