#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

"""Thread-local-alike tracking of log contexts within synapse

This module provides objects and utilities for tracking contexts through
synapse code, so that log lines can include a request identifier, and so that
CPU and database activity can be accounted for against the request that caused
them.

See doc/log_contexts.rst for details on how this works.
"""

import logging
import threading
import typing
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    TypeVar,
    Union,
    overload,
)

import attr
from typing_extensions import ParamSpec

from twisted.internet import defer, threads
from twisted.python.threadpool import ThreadPool

from synapse.logging.loggers import ExplicitlyConfiguredLogger
from synapse.util.stringutils import random_string_insecure_fast

if TYPE_CHECKING:
    from synapse.logging.scopecontextmanager import _LogContextScope
    from synapse.types import ISynapseReactor

logger = logging.getLogger(__name__)

original_logger_class = logging.getLoggerClass()
logging.setLoggerClass(ExplicitlyConfiguredLogger)
logcontext_debug_logger = logging.getLogger("synapse.logging.context.debug")
"""
A logger for debugging when the logcontext switches.

Because this is very noisy and probably something only developers want to see when
debugging logcontext problems, we want people to explictly opt-in before seeing anything
in the logs. Requires explicitly setting `synapse.logging.context.debug` in the logging
configuration and does not inherit the log level from the parent logger.
"""
# Restore the original logger class
logging.setLoggerClass(original_logger_class)

try:
    import resource

    # Python doesn't ship with a definition of RUSAGE_THREAD but it's defined
    # to be 1 on linux so we hard code it.
    RUSAGE_THREAD = 1

    # If the system doesn't support RUSAGE_THREAD then this should throw an
    # exception.
    resource.getrusage(RUSAGE_THREAD)

    is_thread_resource_usage_supported = True

    def get_thread_resource_usage() -> "resource.struct_rusage | None":
        return resource.getrusage(RUSAGE_THREAD)

except Exception:
    # If the system doesn't support resource.getrusage(RUSAGE_THREAD) then we
    # won't track resource usage.
    is_thread_resource_usage_supported = False

    def get_thread_resource_usage() -> "resource.struct_rusage | None":
        return None


# a hook which can be set during testing to assert that we aren't abusing logcontexts.
def logcontext_error(msg: str) -> None:
    logger.warning(msg)


# get an id for the current thread.
#
# threading.get_ident doesn't actually return an OS-level tid, and annoyingly,
# on Linux it actually returns the same value either side of a fork() call. However
# we only fork in one place, so it's not worth the hoop-jumping to get a real tid.
#
get_thread_id = threading.get_ident


class ContextResourceUsage:
    """Object for tracking the resources used by a log context

    Attributes:
        ru_utime (float): user CPU time (in seconds)
        ru_stime (float): system CPU time (in seconds)
        db_txn_count (int): number of database transactions done
        db_sched_duration_sec (float): amount of time spent waiting for a
            database connection
        db_txn_duration_sec (float): amount of time spent doing database
            transactions (excluding scheduling time)
        evt_db_fetch_count (int): number of events requested from the database
    """

    __slots__ = [
        "ru_stime",
        "ru_utime",
        "db_txn_count",
        "db_txn_duration_sec",
        "db_sched_duration_sec",
        "evt_db_fetch_count",
    ]

    def __init__(self, copy_from: "ContextResourceUsage | None" = None) -> None:
        """Create a new ContextResourceUsage

        Args:
            copy_from: if not None, an object to copy stats from
        """
        if copy_from is None:
            self.reset()
        else:
            # FIXME: mypy can't infer the types set via reset() above, so specify explicitly for now
            self.ru_utime: float = copy_from.ru_utime
            self.ru_stime: float = copy_from.ru_stime
            self.db_txn_count: int = copy_from.db_txn_count

            self.db_txn_duration_sec: float = copy_from.db_txn_duration_sec
            self.db_sched_duration_sec: float = copy_from.db_sched_duration_sec
            self.evt_db_fetch_count: int = copy_from.evt_db_fetch_count

    def copy(self) -> "ContextResourceUsage":
        return ContextResourceUsage(copy_from=self)

    def reset(self) -> None:
        self.ru_stime = 0.0
        self.ru_utime = 0.0
        self.db_txn_count = 0

        self.db_txn_duration_sec = 0.0
        self.db_sched_duration_sec = 0.0
        self.evt_db_fetch_count = 0

    def __repr__(self) -> str:
        return (
            "<ContextResourceUsage ru_stime='%r', ru_utime='%r', "
            "db_txn_count='%r', db_txn_duration_sec='%r', "
            "db_sched_duration_sec='%r', evt_db_fetch_count='%r'>"
        ) % (
            self.ru_stime,
            self.ru_utime,
            self.db_txn_count,
            self.db_txn_duration_sec,
            self.db_sched_duration_sec,
            self.evt_db_fetch_count,
        )

    def __iadd__(self, other: "ContextResourceUsage") -> "ContextResourceUsage":
        """Add another ContextResourceUsage's stats to this one's.

        Args:
            other: the other resource usage object
        """
        self.ru_utime += other.ru_utime
        self.ru_stime += other.ru_stime
        self.db_txn_count += other.db_txn_count
        self.db_txn_duration_sec += other.db_txn_duration_sec
        self.db_sched_duration_sec += other.db_sched_duration_sec
        self.evt_db_fetch_count += other.evt_db_fetch_count
        return self

    def __isub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage":
        self.ru_utime -= other.ru_utime
        self.ru_stime -= other.ru_stime
        self.db_txn_count -= other.db_txn_count
        self.db_txn_duration_sec -= other.db_txn_duration_sec
        self.db_sched_duration_sec -= other.db_sched_duration_sec
        self.evt_db_fetch_count -= other.evt_db_fetch_count
        return self

    def __add__(self, other: "ContextResourceUsage") -> "ContextResourceUsage":
        res = ContextResourceUsage(copy_from=self)
        res += other
        return res

    def __sub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage":
        res = ContextResourceUsage(copy_from=self)
        res -= other
        return res


@attr.s(slots=True, auto_attribs=True)
class ContextRequest:
    """
    A bundle of attributes from the SynapseRequest object.

    This exists to:

    * Avoid a cycle between LoggingContext and SynapseRequest.
    * Be a single variable that can be passed from parent LoggingContexts to
      their children.
    """

    request_id: str
    ip_address: str
    site_tag: str
    requester: str | None
    authenticated_entity: str | None
    method: str
    url: str
    protocol: str
    user_agent: str


LoggingContextOrSentinel = Union["LoggingContext", "_Sentinel"]


class _Sentinel:
    """
    Sentinel to represent the root context

    This should only be used for tasks outside of Synapse like when we yield control
    back to the Twisted reactor (event loop) so we don't leak the current logging
    context to other tasks that are scheduled next in the event loop.

    Nothing from the Synapse homeserver should be logged with the sentinel context. i.e.
    we should always know which server the logs are coming from.
    """

    __slots__ = [
        "previous_context",
        "finished",
        "scope",
        "server_name",
        "request",
        "tag",
    ]

    def __init__(self) -> None:
        # Minimal set for compatibility with LoggingContext
        self.previous_context = None
        self.finished = False
        self.server_name = "unknown_server_from_sentinel_context"
        self.request = None
        self.scope = None
        self.tag = None

    def __str__(self) -> str:
        return "sentinel"

    def start(self, rusage: "resource.struct_rusage | None") -> None:
        pass

    def stop(self, rusage: "resource.struct_rusage | None") -> None:
        pass

    def add_database_transaction(self, duration_sec: float) -> None:
        pass

    def add_database_scheduled(self, sched_sec: float) -> None:
        pass

    def record_event_fetch(self, event_count: int) -> None:
        pass

    def __bool__(self) -> Literal[False]:
        return False


SENTINEL_CONTEXT = _Sentinel()


class LoggingContext:
    """Additional context for log formatting. Contexts are scoped within a
    "with" block.

    If a parent is given when creating a new context, then:
        - logging fields are copied from the parent to the new context on entry
        - when the new context exits, the cpu usage stats are copied from the
          child to the parent

    Args:
        name: Name for the context for logging.
        server_name: The name of the server this context is associated with
            (`config.server.server_name` or `hs.hostname`)
        parent_context (LoggingContext|None): The parent of the new context
        request: Synapse Request Context object. Useful to associate all the logs
            happening to a given request.

    """

    __slots__ = [
        "previous_context",
        "name",
        "server_name",
        "parent_context",
        "_resource_usage",
        "usage_start",
        "main_thread",
        "finished",
        "request",
        "tag",
        "scope",
    ]

    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        parent_context: "LoggingContext | None" = None,
        request: ContextRequest | None = None,
    ) -> None:
        self.previous_context = current_context()

        # track the resources used by this context so far
        self._resource_usage = ContextResourceUsage()

        # The thread resource usage when the logcontext became active. None
        # if the context is not currently active.
        self.usage_start: resource.struct_rusage | None = None

        self.name = name
        self.server_name = server_name
        self.main_thread = get_thread_id()
        self.request = None
        self.tag = ""
        self.scope: "_LogContextScope" | None = None

        # keep track of whether we have hit the __exit__ block for this context
        # (suggesting that the the thing that created the context thinks it should
        # be finished, and that re-activating it would suggest an error).
        self.finished = False

        self.parent_context = parent_context

        # Inherit some fields from the parent context
        if self.parent_context is not None:
            # which request this corresponds to
            self.request = self.parent_context.request

            # we also track the current scope:
            self.scope = self.parent_context.scope

        if request is not None:
            # the request param overrides the request from the parent context
            self.request = request

    def __str__(self) -> str:
        return self.name

    def __enter__(self) -> "LoggingContext":
        """Enters this logging context into thread local storage"""
        logcontext_debug_logger.debug("LoggingContext(%s).__enter__", self.name)
        old_context = set_current_context(self)
        if self.previous_context != old_context:
            logcontext_error(
                "Expected previous context %r, found %r"
                % (
                    self.previous_context,
                    old_context,
                )
            )
        return self

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Restore the logging context in thread local storage to the state it
        was before this context was entered.
        Returns:
            None to avoid suppressing any exceptions that were thrown.
        """
        logcontext_debug_logger.debug(
            "LoggingContext(%s).__exit__ --> %s", self.name, self.previous_context
        )
        current = set_current_context(self.previous_context)
        if current is not self:
            if current is SENTINEL_CONTEXT:
                logcontext_error("Expected logging context %s was lost" % (self,))
            else:
                logcontext_error(
                    "Expected logging context %s but found %s" % (self, current)
                )

        # the fact that we are here suggests that the caller thinks that everything
        # is done and dusted for this logcontext, and further activity will not get
        # recorded against the correct metrics.
        self.finished = True

    def start(self, rusage: "resource.struct_rusage | None") -> None:
        """
        Record that this logcontext is currently running.

        This should not be called directly: use set_current_context

        Args:
            rusage: the resources used by the current thread, at the point of
                switching to this logcontext. May be None if this platform doesn't
                support getrusuage.
        """
        if get_thread_id() != self.main_thread:
            logcontext_error("Started logcontext %s on different thread" % (self,))
            return

        if self.finished:
            logcontext_error("Re-starting finished log context %s" % (self,))

        # If we haven't already started record the thread resource usage so
        # far
        if self.usage_start:
            logcontext_error("Re-starting already-active log context %s" % (self,))
        else:
            self.usage_start = rusage

    def stop(self, rusage: "resource.struct_rusage | None") -> None:
        """
        Record that this logcontext is no longer running.

        This should not be called directly: use set_current_context

        Args:
            rusage: the resources used by the current thread, at the point of
                switching away from this logcontext. May be None if this platform
                doesn't support getrusuage.
        """

        try:
            if get_thread_id() != self.main_thread:
                logcontext_error("Stopped logcontext %s on different thread" % (self,))
                return

            if not rusage:
                return

            # Record the cpu used since we started
            if not self.usage_start:
                logcontext_error(
                    "Called stop on logcontext %s without recording a start rusage"
                    % (self,)
                )
                return

            utime_delta, stime_delta = self._get_cputime(rusage)
            self.add_cputime(utime_delta, stime_delta)
        finally:
            self.usage_start = None

    def get_resource_usage(self) -> ContextResourceUsage:
        """Get resources used by this logcontext so far.

        Returns:
            A *copy* of the object tracking resource usage so far
        """
        # we always return a copy, for consistency
        res = self._resource_usage.copy()

        # If we are on the correct thread and we're currently running then we
        # can include resource usage so far.
        is_main_thread = get_thread_id() == self.main_thread
        if self.usage_start and is_main_thread:
            rusage = get_thread_resource_usage()
            assert rusage is not None
            utime_delta, stime_delta = self._get_cputime(rusage)
            res.ru_utime += utime_delta
            res.ru_stime += stime_delta

        return res

    def _get_cputime(self, current: "resource.struct_rusage") -> tuple[float, float]:
        """Get the cpu usage time between start() and the given rusage

        Args:
            rusage: the current resource usage

        Returns: tuple[float, float]: seconds in user mode, seconds in system mode
        """
        assert self.usage_start is not None

        utime_delta = current.ru_utime - self.usage_start.ru_utime
        stime_delta = current.ru_stime - self.usage_start.ru_stime

        # sanity check
        if utime_delta < 0:
            logger.error(
                "utime went backwards! %f < %f",
                current.ru_utime,
                self.usage_start.ru_utime,
            )
            utime_delta = 0

        if stime_delta < 0:
            logger.error(
                "stime went backwards! %f < %f",
                current.ru_stime,
                self.usage_start.ru_stime,
            )
            stime_delta = 0

        return utime_delta, stime_delta

    def add_cputime(self, utime_delta: float, stime_delta: float) -> None:
        """Update the CPU time usage of this context (and any parents, recursively).

        Args:
            utime_delta: additional user time, in seconds, spent in this context.
            stime_delta: additional system time, in seconds, spent in this context.
        """
        self._resource_usage.ru_utime += utime_delta
        self._resource_usage.ru_stime += stime_delta
        if self.parent_context:
            self.parent_context.add_cputime(utime_delta, stime_delta)

    def add_database_transaction(self, duration_sec: float) -> None:
        """Record the use of a database transaction and the length of time it took.

        Args:
            duration_sec: The number of seconds the database transaction took.
        """
        if duration_sec < 0:
            raise ValueError("DB txn time can only be non-negative")
        self._resource_usage.db_txn_count += 1
        self._resource_usage.db_txn_duration_sec += duration_sec
        if self.parent_context:
            self.parent_context.add_database_transaction(duration_sec)

    def add_database_scheduled(self, sched_sec: float) -> None:
        """Record a use of the database pool

        Args:
            sched_sec: number of seconds it took us to get a connection
        """
        if sched_sec < 0:
            raise ValueError("DB scheduling time can only be non-negative")
        self._resource_usage.db_sched_duration_sec += sched_sec
        if self.parent_context:
            self.parent_context.add_database_scheduled(sched_sec)

    def record_event_fetch(self, event_count: int) -> None:
        """Record a number of events being fetched from the db

        Args:
            event_count: number of events being fetched
        """
        self._resource_usage.evt_db_fetch_count += event_count
        if self.parent_context:
            self.parent_context.record_event_fetch(event_count)


class LoggingContextFilter(logging.Filter):
    """Logging filter that adds values from the current logging context to each
    record.
    """

    def __init__(
        self,
        # `request` is here for backwards compatibility since we previously recommended
        # people manually configure `LoggingContextFilter` like the following.
        #
        # ```yaml
        # filters:
        #   context:
        #       (): synapse.logging.context.LoggingContextFilter
        #       request: ""
        # ```
        #
        # TODO: Since we now configure `LoggingContextFilter` automatically since #8051
        # (2020-08-11), we could consider removing this useless parameter. This would
        # require people to remove their own manual configuration of
        # `LoggingContextFilter` as it would cause `TypeError: Filter.__init__() got an
        # unexpected keyword argument 'request'` -> `ValueError: Unable to configure
        # filter 'context'`
        request: str = "",
    ):
        self._default_request = request

    def filter(self, record: logging.LogRecord) -> Literal[True]:
        """
        Add each field from the logging context to the record.

        Please be mindful of 3rd-party code outside of Synapse (like in the case of
        Synapse Pro for small hosts) as this is running as a global log record filter.
        Other code may have set their own attributes on the record and the log record
        may not be relevant to Synapse at all so we should not mangle it.

        We can have some defaults but we should avoid overwriting existing attributes on
        any log record unless we actually have a Synapse logcontext (not just the
        default sentinel logcontext).

        Returns:
            True to include the record in the log output.
        """
        context = current_context()
        # type-ignore: `context` should never be `None`, but if it somehow ends up
        # being, then we end up in a death spiral of infinite loops, so let's check, for
        # robustness' sake.
        #
        # Add some default values to avoid log formatting errors.
        if context is None:
            record.request = self._default_request  # type: ignore[unreachable]

            # Avoid overwriting an existing `server_name` on the record. This is running in
            # the context of a global log record filter so there may be 3rd-party code that
            # adds their own `server_name` and we don't want to interfere with that
            # (clobber).
            if not hasattr(record, "server_name"):
                record.server_name = "unknown_server_from_no_logcontext"

        # Otherwise, in the normal, expected case, fill in the log record attributes
        # from the logcontext.
        else:

            def safe_set(attr: str, value: Any) -> None:
                """
                Only write the attribute if it hasn't already been set or we actually have
                a Synapse logcontext (indicating that this log record is relevant to
                Synapse).
                """
                if context is not SENTINEL_CONTEXT or not hasattr(record, attr):
                    setattr(record, attr, value)

            safe_set("server_name", context.server_name)

            # Logging is interested in the request ID. Note that for backwards
            # compatibility this is stored as the "request" on the record.
            safe_set("request", str(context))

            # Add some data from the HTTP request.
            request = context.request
            # The sentinel logcontext has no request so if we get past this point, we
            # know we have some actual Synapse logcontext and don't need to worry about
            # using `safe_set`. We'll consider this an optimization since this is a
            # pretty hot-path.
            if request is None:
                return True

            record.ip_address = request.ip_address
            record.site_tag = request.site_tag
            record.requester = request.requester
            record.authenticated_entity = request.authenticated_entity
            record.method = request.method
            record.url = request.url
            record.protocol = request.protocol
            record.user_agent = request.user_agent

        return True


class PreserveLoggingContext:
    """
    Context manager which replaces the logging context

    The previous logging context is restored on exit.

    `make_deferred_yieldable` is pretty equivalent to using `with
    PreserveLoggingContext():` (using the default sentinel context), i.e. it clears the
    logcontext before awaiting (and so before execution passes back to the reactor) and
    restores the old context once the awaitable completes (execution passes from the
    reactor back to the code).
    """

    __slots__ = ["_old_context", "_new_context", "_instance_id"]

    def __init__(
        self, new_context: LoggingContextOrSentinel = SENTINEL_CONTEXT
    ) -> None:
        self._new_context = new_context
        self._instance_id = random_string_insecure_fast(5)

    def __enter__(self) -> None:
        logcontext_debug_logger.debug(
            "PreserveLoggingContext(%s).__enter__ %s --> %s",
            self._instance_id,
            current_context(),
            self._new_context,
        )
        self._old_context = set_current_context(self._new_context)

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        logcontext_debug_logger.debug(
            "PreserveLoggingContext(%s).__exit %s --> %s",
            self._instance_id,
            current_context(),
            self._old_context,
        )
        context = set_current_context(self._old_context)

        if context != self._new_context:
            if not context:
                logcontext_error(
                    "Expected logging context %s was lost" % (self._new_context,)
                )
            else:
                logcontext_error(
                    "Expected logging context %s but found %s"
                    % (
                        self._new_context,
                        context,
                    )
                )


_thread_local = threading.local()
_thread_local.current_context = SENTINEL_CONTEXT


def current_context() -> LoggingContextOrSentinel:
    """Get the current logging context from thread local storage"""
    return getattr(_thread_local, "current_context", SENTINEL_CONTEXT)


def set_current_context(context: LoggingContextOrSentinel) -> LoggingContextOrSentinel:
    """Set the current logging context in thread local storage
    Args:
        context: The context to activate.

    Returns:
        The context that was previously active
    """
    # everything blows up if we allow current_context to be set to None, so sanity-check
    # that now.
    if context is None:
        raise TypeError("'context' argument may not be None")

    current = current_context()

    if current is not context:
        rusage = get_thread_resource_usage()
        current.stop(rusage)
        _thread_local.current_context = context
        context.start(rusage)

    return current


def nested_logging_context(suffix: str) -> LoggingContext:
    """Creates a new logging context as a child of another.

    The nested logging context will have a 'name' made up of the parent context's
    name, plus the given suffix.

    CPU/db usage stats will be added to the parent context's on exit.

    Normal usage looks like:

        with nested_logging_context(suffix):
            # ... do stuff

    Args:
        suffix: suffix to add to the parent context's 'name'.

    Returns:
        A new logging context.
    """
    curr_context = current_context()
    if not curr_context:
        logger.warning(
            "Starting nested logging context from sentinel context: metrics will be lost"
        )
        parent_context = None
        server_name = "unknown_server_from_sentinel_context"
    else:
        assert isinstance(curr_context, LoggingContext)
        parent_context = curr_context
        server_name = parent_context.server_name
    prefix = str(curr_context)
    return LoggingContext(
        name=prefix + "-" + suffix,
        server_name=server_name,
        parent_context=parent_context,
    )


P = ParamSpec("P")
R = TypeVar("R")


async def _unwrap_awaitable(awaitable: Awaitable[R]) -> R:
    """Unwraps an arbitrary awaitable by awaiting it."""
    return await awaitable


@overload
def preserve_fn(
    f: Callable[P, Awaitable[R]],
) -> Callable[P, "defer.Deferred[R]"]:
    # The `type: ignore[misc]` above suppresses
    # "Overloaded function signatures 1 and 2 overlap with incompatible return types"
    ...


@overload
def preserve_fn(f: Callable[P, R]) -> Callable[P, "defer.Deferred[R]"]: ...


def preserve_fn(
    f: Callable[P, R] | Callable[P, Awaitable[R]],
) -> Callable[P, "defer.Deferred[R]"]:
    """Function decorator which wraps the function with run_in_background"""

    def g(*args: P.args, **kwargs: P.kwargs) -> "defer.Deferred[R]":
        return run_in_background(f, *args, **kwargs)

    return g


@overload
def run_in_background(
    f: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs
) -> "defer.Deferred[R]":
    # The `type: ignore[misc]` above suppresses
    # "Overloaded function signatures 1 and 2 overlap with incompatible return types"
    ...


@overload
def run_in_background(
    f: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> "defer.Deferred[R]": ...


def run_in_background(
    f: Callable[P, R] | Callable[P, Awaitable[R]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> "defer.Deferred[R]":
    """Calls a function, ensuring that the current context is restored after
    return from the function, and that the sentinel context is set once the
    deferred returned by the function completes.

    To explain how the log contexts work here:
     - When `run_in_background` is called, the calling logcontext is stored
       ("original"), we kick off the background task in the current context, and we
       restore that original context before returning.
     - For a completed deferred, that's the end of the story.
     - For an incomplete deferred, when the background task finishes, we don't want to
       leak our context into the reactor which would erroneously get attached to the
       next operation picked up by the event loop. We add a callback to the deferred
       which will clear the logging context after it finishes and yields control back to
       the reactor.

    Useful for wrapping functions that return a deferred or coroutine, which you don't
    yield or await on (for instance because you want to pass it to
    deferred.gatherResults()).

    If f returns a Coroutine object, it will be wrapped into a Deferred (which will have
    the side effect of executing the coroutine).

    Note that if you completely discard the result, you should make sure that
    `f` doesn't raise any deferred exceptions, otherwise a scary-looking
    CRITICAL error about an unhandled error will be logged without much
    indication about where it came from.

    Returns:
        Deferred which returns the result of func, or `None` if func raises.
        Note that the returned Deferred does not follow the synapse logcontext
        rules.
    """
    instance_id = random_string_insecure_fast(5)
    calling_context = current_context()
    logcontext_debug_logger.debug(
        "run_in_background(%s): called with logcontext=%s", instance_id, calling_context
    )
    try:
        # (kick off the task in the current context)
        res = f(*args, **kwargs)
    except Exception:
        # the assumption here is that the caller doesn't want to be disturbed
        # by synchronous exceptions, so let's turn them into Failures.
        return defer.fail()

    # `res` may be a coroutine, `Deferred`, some other kind of awaitable, or a plain
    # value. Convert it to a `Deferred`.
    #
    # Wrapping the value in a deferred has the side effect of executing the coroutine,
    # if it is one. If it's already a deferred, then we can just use that.
    d: "defer.Deferred[R]"
    if isinstance(res, typing.Coroutine):
        # Wrap the coroutine in a `Deferred`.
        d = defer.ensureDeferred(res)
    elif isinstance(res, defer.Deferred):
        d = res
    elif isinstance(res, Awaitable):
        # `res` is probably some kind of completed awaitable, such as a `DoneAwaitable`
        # or `Future` from `make_awaitable`.
        d = defer.ensureDeferred(_unwrap_awaitable(res))
    else:
        # `res` is a plain value. Wrap it in a `Deferred`.
        d = defer.succeed(res)

    # The deferred has already completed
    if d.called and not d.paused:
        # If the function messes with logcontexts, we can assume it follows the Synapse
        # logcontext rules (Rules for functions returning awaitables: "If the awaitable
        # is already complete, the function returns with the same logcontext it started
        # with."). If the function doesn't touch logcontexts at all, we can also assume
        # the logcontext is unchanged.
        #
        # Either way, the function should have maintained the calling logcontext, so we
        # can avoid messing with it further. Additionally, if the deferred has already
        # completed, then it would be a mistake to then add a deferred callback (below)
        # to reset the logcontext to the sentinel logcontext as that would run
        # immediately (remember our goal is to maintain the calling logcontext when we
        # return).
        if current_context() != calling_context:
            logcontext_error(
                "run_in_background(%s): deferred already completed but the function did not maintain the calling logcontext %s (found %s)"
                % (
                    instance_id,
                    calling_context,
                    current_context(),
                )
            )
        else:
            logcontext_debug_logger.debug(
                "run_in_background(%s): deferred already completed (maintained the calling logcontext %s)",
                instance_id,
                calling_context,
            )
        return d

    # Since the function we called may follow the Synapse logcontext rules (Rules for
    # functions returning awaitables: "If the awaitable is incomplete, the function
    # clears the logcontext before returning"), the function may have reset the
    # logcontext before returning, so we need to restore the calling logcontext now
    # before we return ourselves.
    #
    # Our goal is to have the caller logcontext unchanged after firing off the
    # background task and returning.
    logcontext_debug_logger.debug(
        "run_in_background(%s): restoring calling logcontext %s",
        instance_id,
        calling_context,
    )
    set_current_context(calling_context)

    # If the function we called is playing nice and following the Synapse logcontext
    # rules, it will restore original calling logcontext when the deferred completes;
    # but there is nothing waiting for it, so it will get leaked into the reactor (which
    # would then get picked up by the next thing the reactor does). We therefore need to
    # reset the logcontext here (set the `sentinel` logcontext) before yielding control
    # back to the reactor.
    #
    # (If this feels asymmetric, consider it this way: we are
    # effectively forking a new thread of execution. We are
    # probably currently within a ``with LoggingContext()`` block,
    # which is supposed to have a single entry and exit point. But
    # by spawning off another deferred, we are effectively
    # adding a new exit point.)
    if logcontext_debug_logger.isEnabledFor(logging.DEBUG):

        def _log_set_context_cb(
            result: ResultT, context: LoggingContextOrSentinel
        ) -> ResultT:
            logcontext_debug_logger.debug(
                "run_in_background(%s): resetting logcontext to %s",
                instance_id,
                context,
            )
            set_current_context(context)
            return result

        d.addBoth(_log_set_context_cb, SENTINEL_CONTEXT)
    else:
        d.addBoth(_set_context_cb, SENTINEL_CONTEXT)

    return d


def run_coroutine_in_background(
    coroutine: typing.Coroutine[Any, Any, R],
) -> "defer.Deferred[R]":
    """Run the coroutine, ensuring that the current context is restored after
    return from the function, and that the sentinel context is set once the
    deferred returned by the function completes.

    Useful for wrapping coroutines that you don't yield or await on (for
    instance because you want to pass it to deferred.gatherResults()).

    This is a special case of `run_in_background` where we can accept a coroutine
    directly rather than a function. We can do this because coroutines do not continue
    running once they have yielded.

    This is an ergonomic helper so we can do this:
    ```python
    run_coroutine_in_background(func1(arg1))
    ```
    Rather than having to do this:
    ```python
    run_in_background(lambda: func1(arg1))
    ```
    """
    return run_in_background(lambda: coroutine)


T = TypeVar("T")


def make_deferred_yieldable(deferred: "defer.Deferred[T]") -> "defer.Deferred[T]":
    """
    Given a deferred, make it follow the Synapse logcontext rules:

    - If the deferred has completed, essentially does nothing (just returns another
      completed deferred with the result/failure).
    - If the deferred has not yet completed, resets the logcontext before returning a
      incomplete deferred. Then, when the deferred completes, restores the current
      logcontext before running callbacks/errbacks.

    This means the resultant deferred can be awaited without leaking the current
    logcontext to the reactor (which would then get erroneously picked up by the next
    thing the reactor does), and also means that the logcontext is preserved when the
    deferred completes.

    (This is more-or-less the opposite operation to run_in_background in terms of how it
    handles log contexts.)

    Pretty much equivalent to using `with PreserveLoggingContext():`, i.e. it clears the
    logcontext before awaiting (and so before execution passes back to the reactor) and
    restores the old context once the awaitable completes (execution passes from the
    reactor back to the code).
    """
    instance_id = random_string_insecure_fast(5)
    logcontext_debug_logger.debug(
        "make_deferred_yieldable(%s): called with logcontext=%s",
        instance_id,
        current_context(),
    )

    # The deferred has already completed
    if deferred.called and not deferred.paused:
        # it looks like this deferred is ready to run any callbacks we give it
        # immediately. We may as well optimise out the logcontext faffery.
        logcontext_debug_logger.debug(
            "make_deferred_yieldable(%s): deferred already completed and the function should have maintained the logcontext",
            instance_id,
        )
        return deferred

    # Our goal is to have the caller logcontext unchanged after they yield/await the
    # returned deferred.
    #
    # When the caller yield/await's the returned deferred, it may yield
    # control back to the reactor. To avoid leaking the current logcontext to the
    # reactor (which would then get erroneously picked up by the next thing the reactor
    # does) while the deferred runs in the reactor event loop, we reset the logcontext
    # and add a callback to the deferred to restore it so the caller's logcontext is
    # active when the deferred completes.

    logcontext_debug_logger.debug(
        "make_deferred_yieldable(%s): resetting logcontext to %s",
        instance_id,
        SENTINEL_CONTEXT,
    )
    calling_context = set_current_context(SENTINEL_CONTEXT)

    if logcontext_debug_logger.isEnabledFor(logging.DEBUG):

        def _log_set_context_cb(
            result: ResultT, context: LoggingContextOrSentinel
        ) -> ResultT:
            logcontext_debug_logger.debug(
                "make_deferred_yieldable(%s): restoring calling logcontext to %s",
                instance_id,
                context,
            )
            set_current_context(context)
            return result

        deferred.addBoth(_log_set_context_cb, calling_context)
    else:
        deferred.addBoth(_set_context_cb, calling_context)

    return deferred


ResultT = TypeVar("ResultT")


def _set_context_cb(result: ResultT, context: LoggingContextOrSentinel) -> ResultT:
    """A callback function which just sets the logging context"""
    set_current_context(context)
    return result


def defer_to_thread(
    reactor: "ISynapseReactor", f: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> "defer.Deferred[R]":
    """
    Calls the function `f` using a thread from the reactor's default threadpool and
    returns the result as a Deferred.

    Creates a new logcontext for `f`, which is created as a child of the current
    logcontext (so its CPU usage metrics will get attributed to the current
    logcontext). `f` should preserve the logcontext it is given.

    The result deferred follows the Synapse logcontext rules: you should `yield`
    on it.

    Args:
        reactor: The reactor in whose main thread the Deferred will be invoked,
            and whose threadpool we should use for the function.

            Normally this will be hs.get_reactor().

        f: The function to call.

        args: positional arguments to pass to f.

        kwargs: keyword arguments to pass to f.

    Returns:
        A Deferred which fires a callback with the result of `f`, or an
            errback if `f` throws an exception.
    """
    return defer_to_threadpool(reactor, reactor.getThreadPool(), f, *args, **kwargs)


def defer_to_threadpool(
    reactor: "ISynapseReactor",
    threadpool: ThreadPool,
    f: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> "defer.Deferred[R]":
    """
    A wrapper for twisted.internet.threads.deferToThreadpool, which handles
    logcontexts correctly.

    Calls the function `f` using a thread from the given threadpool and returns
    the result as a Deferred.

    Creates a new logcontext for `f`, which is created as a child of the current
    logcontext (so its CPU usage metrics will get attributed to the current
    logcontext). `f` should preserve the logcontext it is given.

    The result deferred follows the Synapse logcontext rules: you should `yield`
    on it.

    Args:
        reactor: The reactor in whose main thread the Deferred will be invoked.
            Normally this will be hs.get_reactor().

        threadpool: The threadpool to use for running `f`. Normally this will be
            hs.get_reactor().getThreadPool().

        f: The function to call.

        args: positional arguments to pass to f.

        kwargs: keyword arguments to pass to f.

    Returns:
        A Deferred which fires a callback with the result of `f`, or an
            errback if `f` throws an exception.
    """
    curr_context = current_context()
    if not curr_context:
        logger.warning(
            "Calling defer_to_threadpool from sentinel context: metrics will be lost"
        )
        parent_context = None
        server_name = "unknown_server_from_sentinel_context"
    else:
        assert isinstance(curr_context, LoggingContext)
        parent_context = curr_context
        server_name = parent_context.server_name

    def g() -> R:
        with LoggingContext(
            name=str(curr_context),
            server_name=server_name,
            parent_context=parent_context,
        ):
            return f(*args, **kwargs)

    return make_deferred_yieldable(threads.deferToThreadPool(reactor, threadpool, g))
