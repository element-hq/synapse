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

"""asyncio-native cache using asyncio.Future instead of Deferred.

Phase 7 of the Twisted → asyncio migration. FutureCache is the asyncio-native
equivalent of DeferredCache, using asyncio.Future and ObservableFuture
instead of Deferred and ObservableDeferred.

This module is unused until later phases switch the cache decorators to use it.
"""

import asyncio
import logging
from typing import Any, Callable, Generic, Hashable, TypeVar

from synapse.util.async_helpers import ObservableFuture

logger = logging.getLogger(__name__)

VT = TypeVar("VT")


class FutureCacheEntry(Generic[VT]):
    """A single pending cache entry backed by an ObservableFuture."""

    __slots__ = ["_observable", "_callbacks"]

    def __init__(self, future: "asyncio.Future[VT]") -> None:
        self._observable = ObservableFuture(future, consumeErrors=True)
        self._callbacks: list[Callable[[], None]] = []

    def observe(self) -> "asyncio.Future[VT]":
        """Return a new Future that resolves with the same value."""
        return self._observable.observe()

    def has_completed(self) -> bool:
        return self._observable.has_called()

    def has_succeeded(self) -> bool:
        return self._observable.has_succeeded()

    def get_result(self) -> VT:
        result = self._observable.get_result()
        if isinstance(result, BaseException):
            raise result
        return result

    def add_invalidation_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def run_invalidation_callbacks(self) -> None:
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Error running cache invalidation callback")
        self._callbacks.clear()


class FutureCache(Generic[VT]):
    """asyncio-native cache using asyncio.Future.

    This is the asyncio-native equivalent of DeferredCache. It uses a
    two-layer architecture:

    1. _pending: Stores FutureCacheEntry for in-flight operations
       (futures that haven't completed yet). Multiple callers can observe
       the same pending entry.

    2. _completed: Stores completed values (plain Python objects, not futures).
       This is the actual LRU cache.

    The lifecycle is:
    - set(key, future) → entry goes into _pending
    - When future completes successfully → value moves to _completed, removed from _pending
    - When future fails → entry removed from _pending (no caching of errors)
    - get(key) → returns from _completed, or observer of _pending, or raises KeyError

    Unlike DeferredCache, this does NOT need make_deferred_yieldable() wrapping
    since asyncio Futures are directly awaitable.
    """

    def __init__(
        self,
        name: str = "",
        max_entries: int = 1000,
        # Extra args accepted for DeferredCache compatibility
        clock: Any = None,
        server_name: str = "",
        tree: bool = False,
        iterable: bool = False,
        apply_cache_factor_from_config: bool = True,
        prune_unread_entries: bool = True,
    ) -> None:
        self.name = name
        self._max_entries = max_entries

        # Pending (in-flight) futures
        self._pending: dict[Hashable, FutureCacheEntry[VT]] = {}

        # Completed values (simple LRU approximation using ordered dict)
        self._completed: dict[Hashable, VT] = {}

        # Invalidation callbacks for completed entries
        self._completed_callbacks: dict[Hashable, list[Callable[[], None]]] = {}

    def get(
        self,
        key: Hashable,
        callback: Callable[[], None] | None = None,
    ) -> "asyncio.Future[VT]":
        """Get a value from the cache.

        Returns a Future that resolves with the cached value.
        If the value is pending (in-flight), returns an observer of the
        pending future.

        Args:
            key: Cache key.
            callback: Optional invalidation callback, fired when this key is
                invalidated.

        Returns:
            A Future resolving to the cached value.

        Raises:
            KeyError: If the key is not in the cache (neither pending nor completed).
        """
        # Check pending first
        entry = self._pending.get(key)
        if entry is not None:
            if callback:
                entry.add_invalidation_callback(callback)
            return entry.observe()

        # Check completed
        if key in self._completed:
            if callback:
                self._completed_callbacks.setdefault(key, []).append(callback)
            loop = asyncio.get_running_loop()
            f: asyncio.Future[VT] = loop.create_future()
            f.set_result(self._completed[key])
            return f

        raise KeyError(key)

    def set(
        self,
        key: Hashable,
        future: "asyncio.Future[VT]",
        callback: Callable[[], None] | None = None,
    ) -> "asyncio.Future[VT]":
        """Store a future in the cache.

        If the future completes successfully, the result is moved to the
        completed cache. If it fails, the entry is removed.

        Args:
            key: Cache key.
            future: The future producing the value.
            callback: Optional invalidation callback.

        Returns:
            An observer future that resolves with the same value.
        """
        # Invalidate any existing entry
        self.invalidate(key)

        entry = FutureCacheEntry(future)
        if callback:
            entry.add_invalidation_callback(callback)
        self._pending[key] = entry

        def _on_complete(f: "asyncio.Future[VT]") -> None:
            # Remove from pending
            if self._pending.get(key) is entry:
                del self._pending[key]

            if f.cancelled():
                return

            exc = f.exception()
            if exc is not None:
                # Don't cache errors
                return

            # Move to completed cache
            result = f.result()
            self._completed[key] = result

            # Move callbacks from entry to completed callbacks
            if entry._callbacks:
                self._completed_callbacks.setdefault(key, []).extend(
                    entry._callbacks
                )
                entry._callbacks.clear()

            # Evict old entries if over limit
            self._maybe_evict()

        future.add_done_callback(_on_complete)
        return entry.observe()

    def invalidate(self, key: Hashable) -> None:
        """Remove an entry from the cache and fire invalidation callbacks."""
        # Invalidate pending
        entry = self._pending.pop(key, None)
        if entry is not None:
            entry.run_invalidation_callbacks()

        # Invalidate completed
        self._completed.pop(key, None)
        callbacks = self._completed_callbacks.pop(key, [])
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Error running cache invalidation callback")

    def invalidate_all(self) -> None:
        """Remove all entries and fire all invalidation callbacks."""
        # Pending
        for entry in self._pending.values():
            entry.run_invalidation_callbacks()
        self._pending.clear()

        # Completed
        for callbacks in self._completed_callbacks.values():
            for cb in callbacks:
                try:
                    cb()
                except Exception:
                    logger.exception("Error running cache invalidation callback")
        self._completed.clear()
        self._completed_callbacks.clear()

    def __contains__(self, key: Hashable) -> bool:
        return key in self._pending or key in self._completed

    def __len__(self) -> int:
        return len(self._pending) + len(self._completed)

    def prefill(
        self, key: Hashable, value: VT, callback: Callable[[], None] | None = None
    ) -> None:
        """Insert a completed value directly into the cache."""
        self.invalidate(key)
        self._completed[key] = value
        if callback:
            self._completed_callbacks.setdefault(key, []).append(callback)
        self._maybe_evict()

    def get_immediate(
        self, key: Hashable, default: Any = None, update_metrics: bool = True
    ) -> Any:
        """Get a completed value synchronously, or return default."""
        if key in self._completed:
            return self._completed[key]
        return default

    def get_bulk(
        self, keys: list[Hashable]
    ) -> tuple[dict[Hashable, VT], "asyncio.Future[dict[Hashable, VT]] | None", list[Hashable]]:
        """Look up multiple keys, returning cached, pending, and missing.

        Returns:
            Tuple of (cached_results, pending_future_or_None, missing_keys)
        """
        cached: dict[Hashable, VT] = {}
        pending_keys: list[Hashable] = []
        missing: list[Hashable] = []

        for key in keys:
            if key in self._completed:
                cached[key] = self._completed[key]
            elif key in self._pending:
                pending_keys.append(key)
            else:
                missing.append(key)

        pending_future = None
        if pending_keys:
            # Create a future that resolves when all pending keys resolve
            async def _gather_pending() -> dict[Hashable, VT]:
                results: dict[Hashable, VT] = {}
                for k in pending_keys:
                    entry = self._pending.get(k)
                    if entry:
                        results[k] = await entry.observe()
                return results

            pending_future = asyncio.ensure_future(_gather_pending())

        return cached, pending_future, missing

    def start_bulk_input(
        self, keys: list[Hashable], callback: Callable[[], None] | None = None
    ) -> "FutureCacheEntry[VT]":
        """Start a bulk insert operation for the given keys.

        Returns a FutureCacheEntry that can be resolved with the results.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[Hashable, VT]] = loop.create_future()

        entry = FutureCacheEntry(future)
        if callback:
            entry.add_invalidation_callback(callback)

        # Store pending entries for each key
        for key in keys:
            self._pending[key] = entry

        def _on_complete(f: asyncio.Future) -> None:  # type: ignore[type-arg]
            if f.cancelled():
                for key in keys:
                    self._pending.pop(key, None)
                return

            exc = f.exception()
            if exc is not None:
                for key in keys:
                    self._pending.pop(key, None)
                return

            results = f.result()
            for key in keys:
                self._pending.pop(key, None)
                if key in results:
                    self._completed[key] = results[key]
            self._maybe_evict()

        future.add_done_callback(_on_complete)
        return entry

    def _maybe_evict(self) -> None:
        """Evict oldest completed entries if over the max size."""
        while len(self._completed) > self._max_entries:
            # Pop the first (oldest) item
            oldest_key = next(iter(self._completed))
            self._completed.pop(oldest_key)
            callbacks = self._completed_callbacks.pop(oldest_key, [])
            for cb in callbacks:
                try:
                    cb()
                except Exception:
                    pass
