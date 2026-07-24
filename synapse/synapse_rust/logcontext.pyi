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
from typing import TYPE_CHECKING, Optional

from synapse.logging.context import ContextRequest

if TYPE_CHECKING:
    from synapse.logging.scopecontextmanager import _LogContextScope

DEBUG_LOGGER_NAME: str
"""Name of the opt-in logger for logcontext switch tracing
(`synapse.logging.context.debug`). Shared with the Rust `debug!` target so the
names cannot drift."""

class ContextResourceUsage:
    """Tracks the resources used by a log context."""

    ru_stime: float
    ru_utime: float
    db_txn_count: int
    db_txn_duration_sec: float
    db_sched_duration_sec: float
    evt_db_fetch_count: int

    def __init__(self, copy_from: "Optional[ContextResourceUsage]" = None) -> None: ...
    def copy(self) -> "ContextResourceUsage": ...
    def reset(self) -> None: ...
    def __iadd__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __isub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __add__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __sub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...

class LoggingContext:
    """Additional context for log formatting. Contexts are scoped within a
    "with" block.

    If a parent is given when creating a new context, then:
        - logging fields are copied from the parent to the new context on entry
        - when the new context exits, the cpu usage stats are copied from the
          child to the parent
    """

    # The context that was current when this one was created; None means the
    # sentinel (the Rust storage cannot hold the pure-Python `SENTINEL_CONTEXT`
    # object, so this is narrower than the pure-Python attribute used to be).
    previous_context: Optional[LoggingContext]
    name: str
    server_name: str
    parent_context: "Optional[LoggingContext]"
    main_thread: int
    finished: bool
    request: Optional[ContextRequest]
    # Narrower than the runtime: the setter also accepts None (see the Rust
    # field docs for why), but all in-tree code treats this as str.
    tag: str
    scope: "Optional[_LogContextScope]"

    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        parent_context: "Optional[LoggingContext]" = None,
        request: Optional[ContextRequest] = None,
    ) -> None:
        """
        Args:
            name: Name for the context for logging.
            server_name: The name of the server this context is associated with
                (`config.server.server_name` or `hs.hostname`).
            parent_context: The parent of the new context.
            request: Synapse Request Context object. Useful to associate all the
                logs happening to a given request.
        """

    def __str__(self) -> str: ...
    def __enter__(self) -> "LoggingContext": ...
    def __exit__(
        self,
        type: Optional[type[BaseException]],
        value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None: ...
    def start(self, rusage: "Optional[tuple[float, float]]") -> None:
        """Record that this logcontext is currently running.

        Should not be called directly: use `set_current_context`.

        Args:
            rusage: The thread CPU usage `(ru_utime, ru_stime)` at the point of
                switching to this context, or None if the platform doesn't
                track it.
        """

    def stop(self, rusage: "Optional[tuple[float, float]]") -> None:
        """Record that this logcontext is no longer running.

        Should not be called directly: use `set_current_context`.

        Args:
            rusage: The thread CPU usage `(ru_utime, ru_stime)` at the point of
                switching away from this context, or None if the platform
                doesn't track it.
        """

    def get_resource_usage(self) -> ContextResourceUsage:
        """Get the resources used by this logcontext so far.

        Returns:
            A *copy* of the object tracking resource usage so far.
        """

    def add_cputime(self, utime_delta: float, stime_delta: float) -> None:
        """Update the CPU time usage of this context (and any parents, recursively)."""

    def add_database_transaction(self, duration_sec: float) -> None:
        """Record the use of a database transaction and how long it took."""

    def add_database_scheduled(self, sched_sec: float) -> None:
        """Record a use of the database pool (the time taken to get a connection)."""

    def record_event_fetch(self, event_count: int) -> None:
        """Record a number of events being fetched from the db."""

def current_context() -> Optional[LoggingContext]:
    """Get the current logging context, or None for the sentinel.

    Resolves the tokio task-local first (so logging emitted while a Rust task is
    being polled is attributed to the context that was current when the task was
    spawned), then this OS thread's slot. This is not the Python-facing API:
    `synapse.logging.context.current_context` wraps this and returns
    `SENTINEL_CONTEXT` instead of `None`.
    """

def set_current_context(
    context: Optional[LoggingContext],
) -> Optional[LoggingContext]:
    """Set the current logging context, returning the context that was previously
    current. `None` means the sentinel, in both directions.

    Reads the thread CPU usage once via `getrusage(RUSAGE_THREAD)` and does the
    `stop`/`start` accounting natively. The annotated type is enforced: raises
    `TypeError` unless `context` is a `LoggingContext` (or subclass) or `None`.
    This is not the Python-facing API: `synapse.logging.context.set_current_context`
    wraps this with the `SENTINEL_CONTEXT` <-> `None` mapping.
    """
