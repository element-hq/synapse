#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generic,
    Iterable,
    TypeVar,
)

import attr

from twisted.internet import defer

from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import (
    active_span,
    start_active_span,
    start_active_span_follows_from,
)
from synapse.util.async_helpers import (
    ObservableDeferred,
    delay_cancellation,
)
from synapse.util.caches import EvictionReason, register_cache
from synapse.util.cancellation import cancellable, is_function_cancellable
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from synapse.util.wheel_timer import WheelTimer

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import opentracing

# the type of the key in the cache
KV = TypeVar("KV")

# the type of the result from the operation
RV = TypeVar("RV")


@attr.s(auto_attribs=True)
class ResponseCacheContext(Generic[KV]):
    """Information about a missed ResponseCache hit

    This object can be passed into the callback for additional feedback
    """

    cache_key: KV
    """The cache key that caused the cache miss

    This should be considered read-only.

    TODO: in attrs 20.1, make it frozen with an on_setattr.
    """

    should_cache: bool = True
    """Whether the result should be cached once the request completes.

    This can be modified by the callback if it decides its result should not be cached.
    """


@attr.s(auto_attribs=True)
class ResponseCacheEntry(Generic[KV]):
    result: ObservableDeferred[KV]
    """The (possibly incomplete) result of the operation.

    Note that we continue to store an ObservableDeferred even after the operation
    completes (rather than switching to an immediate value), since that makes it
    easier to cache Failure results.
    """

    opentracing_span_context: "opentracing.SpanContext | None"
    """The opentracing span which generated/is generating the result"""

    cancellable: bool
    """Whether the deferred is safe to be cancelled."""

    last_observer_removed_time_ms: int | None = None
    """The last time that an observer was removed from this entry.

    Used to determine when to evict the entry if it has no observers.
    """


class ResponseCache(Generic[KV]):
    """
    This caches a deferred response. Until the deferred completes it will be
    returned from the cache. This means that if the client retries the request
    while the response is still being computed, that original response will be
    used rather than trying to compute a new response.

    If a timeout is not specified then the cache entry will be kept while the
    wrapped function is still running, and will be removed immediately once it
    completes.

    If a timeout is specified then the cache entry will be kept for the duration
    of the timeout after the wrapped function completes. If the wrapped function
    is cancellable and during processing nothing waits on the result for longer
    than the timeout then the wrapped function will be cancelled and the cache
    entry will be removed.

    This behaviour is useful for caching responses to requests which are
    expensive to compute, but which may be retried by clients if they time out.
    For example, /sync requests which may take a long time to compute, and which
    clients will retry. However, if the client stops retrying for a while then
    we want to stop processing the request and free up the resources.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        name: str,
        server_name: str,
        timeout: Duration | None = None,
        enable_logging: bool = True,
    ):
        """
        Args:
            clock
            name
            server_name: The homeserver name that this cache is associated
                with (used to label the metric) (`hs.hostname`).
            timeout_ms
            enable_logging
        """
        self._result_cache: dict[KV, ResponseCacheEntry] = {}

        self.clock = clock
        self.timeout = timeout

        self._name = name
        self._metrics = register_cache(
            cache_type="response_cache",
            cache_name=name,
            cache=self,
            server_name=server_name,
            resizable=False,
        )
        self._enable_logging = enable_logging

        self._prune_timer: WheelTimer[KV] | None = None
        if self.timeout:
            # Set up the timers for pruning inflight entries. The times here are
            # how often we check for entries to prune.
            self._prune_timer = WheelTimer(bucket_size=self.timeout / 10)
            self.clock.looping_call(self._prune_inflight_entries, self.timeout / 10)

    def size(self) -> int:
        return len(self._result_cache)

    def __len__(self) -> int:
        return self.size()

    def keys(self) -> Iterable[KV]:
        """Get the keys currently in the result cache

        Returns both incomplete entries, and (if the timeout on this cache is non-zero),
        complete entries which are still in the cache.

        Note that the returned iterator is not safe in the face of concurrent execution:
        behaviour is undefined if `wrap` is called during iteration.
        """
        return self._result_cache.keys()

    def _get(self, key: KV) -> ResponseCacheEntry | None:
        """Look up the given key.

        Args:
            key: key to get in the cache

        Returns:
            The entry for this key, if any; else None.
        """
        entry = self._result_cache.get(key)
        if entry is not None:
            self._metrics.inc_hits()
            return entry
        else:
            self._metrics.inc_misses()
            return None

    def _set(
        self,
        context: ResponseCacheContext[KV],
        deferred: "defer.Deferred[RV]",
        opentracing_span_context: "opentracing.SpanContext | None",
        cancellable: bool,
    ) -> ResponseCacheEntry:
        """Set the entry for the given key to the given deferred.

        *deferred* should run its callbacks in the sentinel logcontext (ie,
        you should wrap normal synapse deferreds with
        synapse.logging.context.run_in_background).

        Args:
            context: Information about the cache miss
            deferred: The deferred which resolves to the result.
            opentracing_span_context: An opentracing span wrapping the calculation
            cancellable: Whether the deferred is safe to be cancelled

        Returns:
            The cache entry object.
        """
        result = ObservableDeferred(deferred, consumeErrors=True)
        key = context.cache_key
        entry = ResponseCacheEntry(
            result, opentracing_span_context, cancellable=cancellable
        )
        self._result_cache[key] = entry

        def on_complete(r: RV) -> RV:
            # if this cache has a non-zero timeout, and the callback has not cleared
            # the should_cache bit, we leave it in the cache for now and schedule
            # its removal later.
            if self.timeout and context.should_cache:
                self.clock.call_later(
                    self.timeout,
                    self._entry_timeout,
                    key,
                    # We don't need to track these calls since they don't hold any strong
                    # references which would keep the `HomeServer` in memory after shutdown.
                    # We don't want to track these because they can get cancelled really
                    # quickly and thrash the tracking mechanism, ie. during repeated calls
                    # to /sync.
                    call_later_cancel_on_shutdown=False,
                )
            else:
                # otherwise, remove the result immediately.
                self.unset(key)
            return r

        # make sure we do this *after* adding the entry to result_cache,
        # in case the result is already complete (in which case flipping the order would
        # leave us with a stuck entry in the cache).
        result.addBoth(on_complete)
        return entry

    def unset(self, key: KV) -> None:
        """Remove the cached value for this key from the cache, if any.

        Args:
            key: key used to remove the cached value
        """
        self._metrics.inc_evictions(EvictionReason.invalidation)
        self._result_cache.pop(key, None)

    def _entry_timeout(self, key: KV) -> None:
        """For the call_later to remove from the cache"""
        self._metrics.inc_evictions(EvictionReason.time)
        self._result_cache.pop(key, None)

    @cancellable
    async def wrap(
        self,
        key: KV,
        callback: Callable[..., Awaitable[RV]],
        *args: Any,
        cache_context: bool = False,
        **kwargs: Any,
    ) -> RV:
        """Wrap together a *get* and *set* call, taking care of logcontexts

        First looks up the key in the cache, and if it is present makes it
        follow the synapse logcontext rules and returns it.

        Otherwise, makes a call to *callback(*args, **kwargs)*, which should
        follow the synapse logcontext rules, and adds the result to the cache.

        Example usage:

            async def handle_request(request):
                # etc
                return result

            result = await response_cache.wrap(
                key,
                handle_request,
                request,
            )

        Args:
            key: key to get/set in the cache

            callback: function to call if the key is not found in
                the cache

            *args: positional parameters to pass to the callback, if it is used

            cache_context: if set, the callback will be given a `cache_context` kw arg,
                which will be a ResponseCacheContext object.

            **kwargs: named parameters to pass to the callback, if it is used

        Returns:
            The result of the callback (from the cache, or otherwise)
        """
        entry = self._get(key)
        if not entry:
            if self._enable_logging:
                logger.debug(
                    "[%s]: no cached result for [%s], calculating new one",
                    self._name,
                    key,
                )
            context = ResponseCacheContext(cache_key=key)
            if cache_context:
                kwargs["cache_context"] = context

            span_context: opentracing.SpanContext | None = None

            async def cb() -> RV:
                # NB it is important that we do not `await` before setting span_context!
                nonlocal span_context
                with start_active_span(f"ResponseCache[{self._name}].calculate"):
                    span = active_span()
                    if span:
                        span_context = span.context
                    return await callback(*args, **kwargs)

            d = run_in_background(cb)
            entry = self._set(
                context, d, span_context, cancellable=is_function_cancellable(callback)
            )
            try:
                return await make_deferred_yieldable(entry.result.observe())
            except defer.CancelledError:
                pass

            # We've been cancelled.
            #
            # Since we've kicked off the background operation, we can't just
            # give up and return here and need to wait for the background
            # operation to stop. We don't want to stop the background process
            # immediately to give a chance for retries to come in and wait for
            # the result.
            #
            # Instead, we temporarily swallow the cancellation and mark the
            # cache key as one to potentially timeout.

            # Update the `last_observer_removed_time_ms` so that the pruning
            # mechanism can kick in if needed.
            now = self.clock.time_msec()
            entry.last_observer_removed_time_ms = now
            if self._prune_timer is not None and self.timeout:
                self._prune_timer.insert(now, key, now + self.timeout.as_millis())

            # Wait on the original deferred, which will continue to run in the
            # background until it completes. We don't want to add an observer as
            # this would prevent the entry from being pruned.
            #
            # Note that this deferred has been consumed by the
            # ObservableDeferred, so we don't know what it will return. That
            # doesn't matter as we just want to throw a CancelledError once it completes anyway.
            try:
                await make_deferred_yieldable(delay_cancellation(d))
            except Exception:
                pass
            raise defer.CancelledError()

        result = entry.result.observe()
        if self._enable_logging:
            if result.called:
                logger.info(
                    "[%s]: using completed cached result for [%s]", self._name, key
                )
            else:
                logger.info(
                    "[%s]: using incomplete cached result for [%s]", self._name, key
                )

        span_context = entry.opentracing_span_context
        with start_active_span_follows_from(
            f"ResponseCache[{self._name}].wait",
            contexts=(span_context,) if span_context else (),
        ):
            try:
                return await make_deferred_yieldable(result)
            except defer.CancelledError:
                # If we're cancelled then we update the
                # `last_observer_removed_time_ms` so that the pruning mechanism
                # can kick in if needed.
                now = self.clock.time_msec()
                entry.last_observer_removed_time_ms = now
                if self._prune_timer is not None and self.timeout:
                    self._prune_timer.insert(now, key, now + self.timeout.as_millis())
                raise

    def _prune_inflight_entries(self) -> None:
        """Prune entries which have been in the cache for too long without
        observers"""
        assert self._prune_timer is not None
        assert self.timeout is not None

        now = self.clock.time_msec()
        keys_to_check = self._prune_timer.fetch(now)

        # Loop through the keys and check if they should be evicted. We evict
        # entries which have no active observers, and which have been in the
        # cache for longer than the timeout since the last observer was removed.
        for key in keys_to_check:
            entry = self._result_cache.get(key)
            if not entry:
                continue

            if not entry.cancellable:
                # this entry is not cancellable, so we should keep it in the cache until it completes.
                continue

            if entry.result.has_called():
                # this entry has already completed, so we should have scheduled it for
                # removal at the right time. We can just skip it here and wait for the
                # scheduled call to remove it.
                continue

            if entry.result.has_observers():
                # this entry has observers, so we should keep it in the cache for now.
                continue

            if entry.last_observer_removed_time_ms is None:
                # this should never happen, but just in case, we should keep the entry
                # in the cache until we have a valid last_observer_removed_time_ms to
                # compare against.
                continue

            if now - entry.last_observer_removed_time_ms > self.timeout.as_millis():
                self._metrics.inc_evictions(EvictionReason.time)
                self._result_cache.pop(key, None)
                try:
                    entry.result.cancel()
                except Exception:
                    # we ignore exceptions from cancel, as it is best effort anyway.
                    pass
