#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""asyncio-native database connection pool.

Phase 3 of the Twisted → asyncio migration. Provides NativeConnectionPool
as a replacement for twisted.enterprise.adbapi.ConnectionPool, using
concurrent.futures.ThreadPoolExecutor + asyncio.loop.run_in_executor()
instead of Twisted's thread pool.

This module is unused until later phases switch the DatabasePool to use it.
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from synapse.config.database import DatabaseConnectionConfig
from synapse.logging.context import LoggingContext
from synapse.storage.database import LoggingDatabaseConnection
from synapse.storage.engines import BaseDatabaseEngine
from synapse.storage.types import Connection

logger = logging.getLogger(__name__)

R = TypeVar("R")


class NativeConnectionPool:
    """asyncio-native database connection pool.

    Uses a ThreadPoolExecutor for running blocking database operations, with
    thread-local connection management. Each thread in the pool gets its own
    persistent database connection, which is reused across calls.

    This is the asyncio-native equivalent of twisted.enterprise.adbapi.ConnectionPool
    and will replace it in DatabasePool once the migration is complete.
    """

    def __init__(
        self,
        db_config: DatabaseConnectionConfig,
        engine: BaseDatabaseEngine,
        server_name: str,
        max_workers: int = 5,
    ) -> None:
        self._db_config = db_config
        self._engine = engine
        self._server_name = server_name

        # Extract DB connection arguments (filter out cp_* Twisted pool args)
        self._db_args: dict[str, Any] = {
            k: v
            for k, v in db_config.config.get("args", {}).items()
            if not k.startswith("cp_")
        }

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"synapse-db-{db_config.name}",
        )

        # Thread-local storage for per-thread connections
        self._thread_local = threading.local()

        self._closed = False

    def _get_connection(self) -> Connection:
        """Get or create a connection for the current thread.

        Each thread in the pool maintains its own persistent connection.
        If the connection is closed or doesn't exist, a new one is created
        and initialized via the engine's on_new_connection callback.
        """
        conn = getattr(self._thread_local, "conn", None)

        if conn is not None and not self._engine.is_connection_closed(conn):
            return conn

        # Create a new raw connection
        raw_conn = self._engine.module.connect(**self._db_args)

        # Initialize it via the engine (sets isolation level, PRAGMAs, etc.)
        with LoggingContext(
            name="db.on_new_connection", server_name=self._server_name
        ):
            db_conn = LoggingDatabaseConnection(
                conn=raw_conn,
                engine=self._engine,
                default_txn_name="on_new_connection",
                server_name=self._server_name,
            )
            self._engine.on_new_connection(db_conn)

        self._thread_local.conn = raw_conn
        return raw_conn

    def _reconnect(self) -> Connection:
        """Close the current thread's connection and create a new one."""
        old_conn = getattr(self._thread_local, "conn", None)
        if old_conn is not None:
            try:
                old_conn.close()
            except Exception:
                pass
        self._thread_local.conn = None
        return self._get_connection()

    async def runWithConnection(
        self,
        func: Callable[..., R],
        *args: Any,
        **kwargs: Any,
    ) -> R:
        """Run a function with a database connection on a thread pool thread.

        The function receives a raw database connection as its first argument.
        This is the asyncio-native equivalent of adbapi.ConnectionPool.runWithConnection.

        Args:
            func: Function to call with (connection, *args, **kwargs).
            *args: Additional positional arguments for func.
            **kwargs: Additional keyword arguments for func.

        Returns:
            The return value of func.
        """
        if self._closed:
            raise Exception("Connection pool is closed")

        def _inner() -> R:
            conn = self._get_connection()
            return func(conn, *args, **kwargs)

        # Submit to thread pool. Use asyncio if available, Twisted otherwise.
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, _inner)
        except RuntimeError:
            # No running asyncio loop — use Twisted's thread pool
            try:
                from twisted.internet import threads
                return await threads.deferToThread(_inner)
            except ImportError:
                # Last resort: blocking call
                return self._executor.submit(_inner).result()

    async def runInteraction(
        self,
        func: Callable[..., R],
        *args: Any,
        **kwargs: Any,
    ) -> R:
        """Run a function within a database transaction on a thread pool thread.

        The function receives a raw database connection as its first argument.
        The transaction is committed on success and rolled back on failure.

        Args:
            func: Function to call with (connection, *args, **kwargs).
            *args: Additional positional arguments for func.
            **kwargs: Additional keyword arguments for func.

        Returns:
            The return value of func.
        """
        if self._closed:
            raise Exception("Connection pool is closed")

        def _inner() -> R:
            conn = self._get_connection()
            try:
                result = func(conn, *args, **kwargs)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

        # Submit to thread pool. Use asyncio if available, Twisted otherwise.
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, _inner)
        except RuntimeError:
            # No running asyncio loop — use Twisted's thread pool
            try:
                from twisted.internet import threads
                return await threads.deferToThread(_inner)
            except ImportError:
                # Last resort: blocking call
                return self._executor.submit(_inner).result()

    def close(self) -> None:
        """Shut down the connection pool.

        Closes all thread-local connections and shuts down the executor.
        """
        self._closed = True
        self._executor.shutdown(wait=False)

    def threadID(self) -> int:
        """Return the current thread's ID (for compatibility with adbapi pool)."""
        return threading.get_ident()
