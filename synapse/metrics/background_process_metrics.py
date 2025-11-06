#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import logging
import threading
from contextlib import contextmanager, nullcontext
from functools import wraps
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ContextManager,
    Generator,
    Iterable,
    Optional,
    Protocol,
    TypeVar,
)

from prometheus_client import Metric
from prometheus_client.core import REGISTRY, Counter, Gauge
from typing_extensions import Concatenate, ParamSpec

from twisted.internet import defer

from synapse.logging.context import (
    ContextResourceUsage,
    LoggingContext,
    PreserveLoggingContext,
)
from synapse.logging.opentracing import (
    SynapseTags,
    active_span,
    start_active_span,
    start_active_span_follows_from,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.metrics._types import Collector

if TYPE_CHECKING:
    import resource

    # Old versions don't have `LiteralString`
    from typing_extensions import LiteralString

    from synapse.server import HomeServer

    try:
        import opentracing
    except ImportError:
        opentracing = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


_background_process_start_count = Counter(
    "synapse_background_process_start_count",
    "Number of background processes started",
    labelnames=["name", SERVER_NAME_LABEL],
)

_background_process_in_flight_count = Gauge(
    "synapse_background_process_in_flight_count",
    "Number of background processes in flight",
    labelnames=["name", SERVER_NAME_LABEL],
)

# we set registry=None in all of these to stop them getting registered with
# the default registry. Instead we collect them all via the CustomCollector,
# which ensures that we can update them before they are collected.
#
_background_process_ru_utime = Counter(
    "synapse_background_process_ru_utime_seconds",
    "User CPU time used by background processes, in seconds",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=None,
)

_background_process_ru_stime = Counter(
    "synapse_background_process_ru_stime_seconds",
    "System CPU time used by background processes, in seconds",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=None,
)

_background_process_db_txn_count = Counter(
    "synapse_background_process_db_txn_count",
    "Number of database transactions done by background processes",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=None,
)

_background_process_db_txn_duration = Counter(
    "synapse_background_process_db_txn_duration_seconds",
    (
        "Seconds spent by background processes waiting for database "
        "transactions, excluding scheduling time"
    ),
    labelnames=["name", SERVER_NAME_LABEL],
    registry=None,
)

_background_process_db_sched_duration = Counter(
    "synapse_background_process_db_sched_duration_seconds",
    "Seconds spent by background processes waiting for database connections",
    labelnames=["name", SERVER_NAME_LABEL],
    registry=None,
)

# map from description to a counter, so that we can name our logcontexts
# incrementally. (It actually duplicates _background_process_start_count, but
# it's much simpler to do so than to try to combine them.)
_background_process_counts: dict[str, int] = {}

# Set of all running background processes that became active active since the
# last time metrics were scraped (i.e. background processes that performed some
# work since the last scrape.)
#
# We do it like this to handle the case where we have a large number of
# background processes stacking up behind a lock or linearizer, where we then
# only need to iterate over and update metrics for the process that have
# actually been active and can ignore the idle ones.
_background_processes_active_since_last_scrape: "set[_BackgroundProcess]" = set()

# A lock that covers the above set and dict
_bg_metrics_lock = threading.Lock()


class _Collector(Collector):
    """A custom metrics collector for the background process metrics.

    Ensures that all of the metrics are up-to-date with any in-flight processes
    before they are returned.
    """

    def collect(self) -> Iterable[Metric]:
        global _background_processes_active_since_last_scrape

        # We swap out the _background_processes set with an empty one so that
        # we can safely iterate over the set without holding the lock.
        with _bg_metrics_lock:
            _background_processes_copy = _background_processes_active_since_last_scrape
            _background_processes_active_since_last_scrape = set()

        for process in _background_processes_copy:
            process.update_metrics()

        # now we need to run collect() over each of the static Counters, and
        # yield each metric they return.
        for m in (
            _background_process_ru_utime,
            _background_process_ru_stime,
            _background_process_db_txn_count,
            _background_process_db_txn_duration,
            _background_process_db_sched_duration,
        ):
            yield from m.collect()


# The `SERVER_NAME_LABEL` is included in the individual metrics added to this registry,
# so we don't need to worry about it on the collector itself.
REGISTRY.register(_Collector())  # type: ignore[missing-server-name-label]


class _BackgroundProcess:
    def __init__(self, *, desc: str, server_name: str, ctx: LoggingContext):
        self.desc = desc
        self.server_name = server_name
        self._context = ctx
        self._reported_stats: ContextResourceUsage | None = None

    def update_metrics(self) -> None:
        """Updates the metrics with values from this process."""
        new_stats = self._context.get_resource_usage()
        if self._reported_stats is None:
            diff = new_stats
        else:
            diff = new_stats - self._reported_stats
        self._reported_stats = new_stats

        # For unknown reasons, the difference in times can be negative. See comment in
        # synapse.http.request_metrics.RequestMetrics.update_metrics.
        _background_process_ru_utime.labels(
            name=self.desc, **{SERVER_NAME_LABEL: self.server_name}
        ).inc(max(diff.ru_utime, 0))
        _background_process_ru_stime.labels(
            name=self.desc, **{SERVER_NAME_LABEL: self.server_name}
        ).inc(max(diff.ru_stime, 0))
        _background_process_db_txn_count.labels(
            name=self.desc, **{SERVER_NAME_LABEL: self.server_name}
        ).inc(diff.db_txn_count)
        _background_process_db_txn_duration.labels(
            name=self.desc, **{SERVER_NAME_LABEL: self.server_name}
        ).inc(diff.db_txn_duration_sec)
        _background_process_db_sched_duration.labels(
            name=self.desc, **{SERVER_NAME_LABEL: self.server_name}
        ).inc(diff.db_sched_duration_sec)


R = TypeVar("R")


def run_as_background_process(
    desc: "LiteralString",
    server_name: str,
    func: Callable[..., Awaitable[R | None]],
    *args: Any,
    bg_start_span: bool = True,
    test_only_tracer: Optional["opentracing.Tracer"] = None,
    **kwargs: Any,
) -> "defer.Deferred[R | None]":
    """Run the given function in its own logcontext, with resource metrics

    This should be used to wrap processes which are fired off to run in the
    background, instead of being associated with a particular request.

    It returns a Deferred which completes when the function completes, but it doesn't
    follow the synapse logcontext rules, which makes it appropriate for passing to
    clock.looping_call and friends (or for firing-and-forgetting in the middle of a
    normal synapse async function).

    Because the returned Deferred does not follow the synapse logcontext rules, awaiting
    the result of this function will result in the log context being cleared (bad). In
    order to properly await the result of this function and maintain the current log
    context, use `make_deferred_yieldable`.

    Args:
        desc: a description for this background process type
        server_name: The homeserver name that this background process is being run for
            (this should be `hs.hostname`).
        func: a function, which may return a Deferred or a coroutine
        bg_start_span: Whether to start an opentracing span. Defaults to True.
            Should only be disabled for processes that will not log to or tag
            a span.
        test_only_tracer: Set the OpenTracing tracer to use. This is only useful for
            tests.
        args: positional args for func
        kwargs: keyword args for func

    Returns:
        Deferred which returns the result of func, or `None` if func raises.
        Note that the returned Deferred does not follow the synapse logcontext
        rules.
    """

    # Since we track the tracing scope in the `LoggingContext`, before we move to the
    # sentinel logcontext (or a new `LoggingContext`), grab the currently active
    # tracing span (if any) so that we can create a cross-link to the background process
    # trace.
    original_active_tracing_span = active_span(tracer=test_only_tracer)

    async def run() -> R | None:
        with _bg_metrics_lock:
            count = _background_process_counts.get(desc, 0)
            _background_process_counts[desc] = count + 1

        _background_process_start_count.labels(
            name=desc, **{SERVER_NAME_LABEL: server_name}
        ).inc()
        _background_process_in_flight_count.labels(
            name=desc, **{SERVER_NAME_LABEL: server_name}
        ).inc()

        with BackgroundProcessLoggingContext(
            name=desc, server_name=server_name, instance_id=count
        ) as logging_context:
            try:
                if bg_start_span:
                    # If there is already an active span (e.g. because this background
                    # process was started as part of handling a request for example),
                    # because this is a long-running background task that may serve a
                    # broader purpose than the request that kicked it off, we don't want
                    # it to be a direct child of the currently active trace connected to
                    # the request. We only want a loose reference to jump between the
                    # traces.
                    #
                    # For example, when making a `/messages` request, when approaching a
                    # gap, we may kick off a background process to fetch missing events
                    # from federation. The `/messages` request trace should't include
                    # the entire time taken and details around fetching the missing
                    # events since the request doesn't rely on the result, it was just
                    # part of the heuristic to initiate things.
                    #
                    # We don't care about the value from the context manager as it's not
                    # used (so we just use `Any` for the type). Ideally, we'd be able to
                    # mark this as unused like an `assert_never` of sorts.
                    tracing_scope: ContextManager[Any]
                    if original_active_tracing_span is not None:
                        # With the OpenTracing client that we're using, it's impossible to
                        # create a disconnected root span while also providing `references`
                        # so we first create a bare root span, then create a child span that
                        # includes the references that we want.
                        root_tracing_scope = start_active_span(
                            f"bgproc.{desc}",
                            tags={SynapseTags.REQUEST_ID: str(logging_context)},
                            # Create a root span for the background process (disconnected
                            # from other spans)
                            ignore_active_span=True,
                            tracer=test_only_tracer,
                        )

                        # Also add a span in the original request trace that cross-links
                        # to background process trace. We immediately finish the span as
                        # this is just a marker to follow where the real work is being
                        # done.
                        #
                        # In OpenTracing, `FOLLOWS_FROM` indicates parent-child
                        # relationship whereas we just want a cross-link to the
                        # downstream trace. This is a bit hacky, but the closest we
                        # can get to in OpenTracing land. If we ever migrate to
                        # OpenTelemetry, we should use a normal `Link` for this.
                        with start_active_span_follows_from(
                            f"start_bgproc.{desc}",
                            child_of=original_active_tracing_span,
                            ignore_active_span=True,
                            # Create the `FOLLOWS_FROM` reference to the background
                            # process span so there is a loose coupling between the two
                            # traces and it's easy to jump between.
                            contexts=[root_tracing_scope.span.context],
                            tracer=test_only_tracer,
                        ):
                            pass

                        # Then start the tracing scope that we're going to use for
                        # the duration of the background process within the root
                        # span we just created.
                        child_tracing_scope = start_active_span_follows_from(
                            f"bgproc_child.{desc}",
                            child_of=root_tracing_scope.span,
                            ignore_active_span=True,
                            tags={SynapseTags.REQUEST_ID: str(logging_context)},
                            # Create the `FOLLOWS_FROM` reference to the request's
                            # span so there is a loose coupling between the two
                            # traces and it's easy to jump between.
                            contexts=[original_active_tracing_span.context],
                            tracer=test_only_tracer,
                        )

                        # For easy usage down below, we create a context manager that
                        # combines both scopes.
                        @contextmanager
                        def combined_context_manager() -> Generator[None, None, None]:
                            with root_tracing_scope, child_tracing_scope:
                                yield

                        tracing_scope = combined_context_manager()

                    else:
                        # Otherwise, when there is no active span, we will be creating
                        # a disconnected root span already and we don't have to
                        # worry about cross-linking to anything.
                        tracing_scope = start_active_span(
                            f"bgproc.{desc}",
                            tags={SynapseTags.REQUEST_ID: str(logging_context)},
                            tracer=test_only_tracer,
                        )
                else:
                    tracing_scope = nullcontext()

                with tracing_scope:
                    return await func(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Background process '%s' threw an exception",
                    desc,
                )
                return None
            finally:
                _background_process_in_flight_count.labels(
                    name=desc, **{SERVER_NAME_LABEL: server_name}
                ).dec()

    # To explain how the log contexts work here:
    #  - When `run_as_background_process` is called, the current context is stored
    #    (using `PreserveLoggingContext`), we kick off the background task, and we
    #    restore the original context before returning (also part of
    #    `PreserveLoggingContext`).
    #  - The background task runs in its own new logcontext named after `desc`
    #  - When the background task finishes, we don't want to leak our background context
    #    into the reactor which would erroneously get attached to the next operation
    #    picked up by the event loop. We use `PreserveLoggingContext` to set the
    #    `sentinel` context and means the new `BackgroundProcessLoggingContext` will
    #    remember the `sentinel` context as its previous context to return to when it
    #    exits and yields control back to the reactor.
    #
    # TODO: Why can't we simplify to using `return run_in_background(run)`?
    with PreserveLoggingContext():
        # Note that we return a Deferred here so that it can be used in a
        # looping_call and other places that expect a Deferred.
        return defer.ensureDeferred(run())


P = ParamSpec("P")


class HasHomeServer(Protocol):
    hs: "HomeServer"
    """
    The homeserver that this cache is associated with (used to label the metric and
    track backgroun processes for clean shutdown).
    """


def wrap_as_background_process(
    desc: "LiteralString",
) -> Callable[
    [Callable[P, Awaitable[R | None]]],
    Callable[P, "defer.Deferred[R | None]"],
]:
    """Decorator that wraps an asynchronous function `func`, returning a synchronous
    decorated function. Calling the decorated version runs `func` as a background
    process, forwarding all arguments verbatim.

    That is,

        @wrap_as_background_process
        def func(*args): ...
        func(1, 2, third=3)

    is equivalent to:

        def func(*args): ...
        run_as_background_process(func, 1, 2, third=3)

    The former can be convenient if `func` needs to be run as a background process in
    multiple places.
    """

    def wrapper(
        func: Callable[Concatenate[HasHomeServer, P], Awaitable[R | None]],
    ) -> Callable[P, "defer.Deferred[R | None]"]:
        @wraps(func)
        def wrapped_func(
            self: HasHomeServer, *args: P.args, **kwargs: P.kwargs
        ) -> "defer.Deferred[R | None]":
            assert self.hs is not None, (
                "The `hs` attribute must be set on the object where `@wrap_as_background_process` decorator is used."
            )

            return self.hs.run_as_background_process(
                desc,
                func,
                self,
                *args,
                **kwargs,
            )

        # There are some shenanigans here, because we're decorating a method but
        # explicitly making use of the `self` parameter. The key thing here is that the
        # return type within the return type for `measure_func` itself describes how the
        # decorated function will be called.
        return wrapped_func  # type: ignore[return-value]

    return wrapper  # type: ignore[return-value]


class BackgroundProcessLoggingContext(LoggingContext):
    """A logging context that tracks in flight metrics for background
    processes.
    """

    __slots__ = ["_proc"]

    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        instance_id: int | str | None = None,
    ):
        """

        Args:
            name: The name of the background process. Each distinct `name` gets a
                separate prometheus time series.
            server_name: The homeserver name that this background process is being run for
                (this should be `hs.hostname`).
            instance_id: an identifer to add to `name` to distinguish this instance of
                the named background process in the logs. If this is `None`, one is
                made up based on id(self).
        """
        if instance_id is None:
            instance_id = id(self)
        super().__init__(name="%s-%s" % (name, instance_id), server_name=server_name)
        self._proc: _BackgroundProcess | None = _BackgroundProcess(
            desc=name, server_name=server_name, ctx=self
        )

    def start(self, rusage: "resource.struct_rusage | None") -> None:
        """Log context has started running (again)."""

        super().start(rusage)

        if self._proc is None:
            logger.error(
                "Background process re-entered without a proc: %s",
                self.name,
                stack_info=True,
            )
            return

        # We've become active again so we make sure we're in the list of active
        # procs. (Note that "start" here means we've become active, as opposed
        # to starting for the first time.)
        with _bg_metrics_lock:
            _background_processes_active_since_last_scrape.add(self._proc)

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Log context has finished."""

        super().__exit__(type, value, traceback)

        if self._proc is None:
            logger.error(
                "Background process exited without a proc: %s",
                self.name,
                stack_info=True,
            )
            return

        # The background process has finished. We explicitly remove and manually
        # update the metrics here so that if nothing is scraping metrics the set
        # doesn't infinitely grow.
        with _bg_metrics_lock:
            _background_processes_active_since_last_scrape.discard(self._proc)

        self._proc.update_metrics()

        # Set proc to None to break the reference cycle.
        self._proc = None
