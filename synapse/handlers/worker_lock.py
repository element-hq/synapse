#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
import random
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    AsyncContextManager,
    Collection,
)
from weakref import WeakSet

import attr

from twisted.internet import defer

from synapse.logging.context import PreserveLoggingContext
from synapse.logging.opentracing import start_active_span
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage.databases.main.lock import Lock, LockStore
from synapse.util.async_helpers import timeout_deferred
from synapse.util.clock import Clock
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.logging.opentracing import opentracing
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)

# This lock is used to avoid creating an event while we are purging the room.
# We take a read lock when creating an event, and a write one when purging a room.
# This is because it is fine to create several events concurrently, since referenced events
# will not disappear under our feet as long as we don't delete the room.
NEW_EVENT_DURING_PURGE_LOCK_NAME = "new_event_during_purge_lock"

WORKER_LOCK_MAX_RETRY_INTERVAL = Duration(seconds=5)
"""
The maximum wait time before retrying to acquire the lock.

Better to retry more quickly than have workers wait around. 5 seconds is still a
reasonable gap in time to not overwhelm the CPU/Database.

This matters most when locks go stale as normally, when the lock holder releases, we
signal to other locks (with the same name/key) that they should try reacquiring the lock
immediately. But stale locks are never released and instead forcefully reaped behind the
scenes.
"""
WORKER_LOCK_EXCESSIVE_WAITING_WARN_DURATION = Duration(minutes=10)


class WorkerLocksHandler:
    """A class for waiting on taking out locks, rather than using the storage
    functions directly (which don't support awaiting).
    """

    def __init__(self, hs: "HomeServer") -> None:
        self.hs = hs  # nb must be called this for @wrap_as_background_process
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._notifier = hs.get_notifier()
        self._instance_name = hs.get_instance_name()

        # Map from lock name/key to set of `WaitingLock` that are active for
        # that lock.
        self._locks: dict[
            tuple[str, str],
            WeakSet[WaitingLock | WaitingMultiLock | WaitingLockSnapshot],
        ] = {}

        self._clock.looping_call(self._cleanup_locks, Duration(seconds=30))

        self._notifier.add_lock_released_callback(self._on_lock_released)

    def acquire_lock(self, lock_name: str, lock_key: str) -> "WaitingLock":
        """Acquire a standard lock, returns a context manager that will block
        until the lock is acquired.

        Note: Care must be taken to avoid deadlocks. In particular, this
        function does *not* timeout.

        Usage:
            async with handler.acquire_lock(name, key):
                # Do work while holding the lock...
        """

        lock = WaitingLock(
            clock=self._clock,
            store=self._store,
            handler=self,
            lock_name=lock_name,
            lock_key=lock_key,
            write=None,
        )

        self._locks.setdefault((lock_name, lock_key), WeakSet()).add(lock)

        return lock

    def acquire_read_write_lock(
        self,
        lock_name: str,
        lock_key: str,
        *,
        write: bool,
    ) -> "WaitingLock":
        """Acquire a read/write lock, returns a context manager that will block
        until the lock is acquired.

        Note: Care must be taken to avoid deadlocks. In particular, this
        function does *not* timeout.

        Usage:
            async with handler.acquire_read_write_lock(name, key, write=True):
                # Do work while holding the lock...
        """

        lock = WaitingLock(
            clock=self._clock,
            store=self._store,
            handler=self,
            lock_name=lock_name,
            lock_key=lock_key,
            write=write,
        )

        self._locks.setdefault((lock_name, lock_key), WeakSet()).add(lock)

        return lock

    def acquire_multi_read_write_lock(
        self,
        lock_names: Collection[tuple[str, str]],
        *,
        write: bool,
    ) -> "WaitingMultiLock":
        """Acquires multi read/write locks at once, returns a context manager
        that will block until all the locks are acquired.

        This will try and acquire all locks at once, and will never hold on to a
        subset of the locks. (This avoids accidentally creating deadlocks).

        Note: Care must be taken to avoid deadlocks. In particular, this
        function does *not* timeout.
        """

        lock = WaitingMultiLock(
            lock_names=lock_names,
            write=write,
            clock=self._clock,
            store=self._store,
            handler=self,
        )

        for lock_name, lock_key in lock_names:
            self._locks.setdefault((lock_name, lock_key), WeakSet()).add(lock)

        return lock

    async def wait_for_current_read_write_lock_holders(
        self,
        lock_name: str,
        lock_key: str,
        *,
        write: bool,
        timeout: Duration | None = None,
    ) -> bool:
        """Wait for the current holders of a read/write lock to release it.

        This snapshots the lock tokens which exist when the method is called and
        waits only for those tokens. Lock holders which arrive after the snapshot
        do not delay the wait.

        Note: this does not acquire the lock, so by the time this returns the
        lock may well have been taken out again by someone else.

        Args:
            timeout: How long to wait before giving up. `None` means wait
                forever, which should only be used by callers that are not
                blocking a request: lock holders can be slow (handling an
                inbound PDU holds `NEW_EVENT_DURING_PURGE_LOCK_NAME` while it
                fetches missing events over federation), and stale locks are
                only reaped after `_LOCK_TIMEOUT`.

        Returns:
            True if all the snapshotted holders released the lock, False if we
            gave up waiting first.
        """

        tokens = await self._store.get_read_write_lock_tokens(
            lock_name, lock_key, write=write
        )
        if not tokens:
            return True

        waiter = WaitingLockSnapshot(
            clock=self._clock,
            store=self._store,
            lock_name=lock_name,
            lock_key=lock_key,
            write=write,
            tokens=tokens,
            timeout=timeout,
        )
        self._locks.setdefault((lock_name, lock_key), WeakSet()).add(waiter)
        return await waiter.wait()

    def notify_lock_released(self, lock_name: str, lock_key: str) -> None:
        """Notify that a lock has been released.

        Pokes both the notifier and replication.
        """

        self._notifier.notify_lock_released(self._instance_name, lock_name, lock_key)

    def _on_lock_released(
        self, instance_name: str, lock_name: str, lock_key: str
    ) -> None:
        """Called when a lock has been released.

        Wakes up any locks that might be waiting on this.
        """
        locks = self._locks.get((lock_name, lock_key))
        if not locks:
            return

        def _wake_all_locks(
            locks: Collection[WaitingLock | WaitingMultiLock | WaitingLockSnapshot],
        ) -> None:
            for lock in locks:
                lock.release_lock()

        self._clock.call_later(
            Duration(seconds=0),
            _wake_all_locks,
            locks,
        )

    @wrap_as_background_process("_cleanup_locks")
    async def _cleanup_locks(self) -> None:
        """Periodically cleans out stale entries in the locks map"""
        self._locks = {key: value for key, value in self._locks.items() if value}


class _LockWaiter:
    """Shared retry behaviour for the waiters below.

    Waiters are woken up by `WorkerLocksHandler._on_lock_released`, but also
    retry periodically since not every release comes with a notification (e.g.
    stale locks being reaped, or direct users of the storage layer).

    Subclasses are `attrs` classes which declare the fields used here.
    """

    clock: Clock
    start_ts_ms: int
    deferred: "defer.Deferred[None]"
    _timeout_interval: float

    def release_lock(self) -> None:
        """Wake the waiter up (by resolving the deferred)"""
        if not self.deferred.called:
            with PreserveLoggingContext():
                self.deferred.callback(None)

    async def _wait_to_retry(self) -> None:
        """Wait until we are woken up, or until it is time to retry anyway.

        The caller is responsible for creating a fresh `self.deferred` *before*
        checking whatever it is waiting on, so that a notification arriving
        between the check and the wait is not missed.
        """
        try:
            with PreserveLoggingContext():
                await timeout_deferred(
                    deferred=self.deferred,
                    timeout=self._timeout_interval,
                    clock=self.clock,
                )
        except defer.TimeoutError:
            # Only increment the timeout value if this was an actual timeout
            # (defer.TimeoutError)
            self._increment_timeout_interval()

            time_spent_waiting = Duration(
                milliseconds=self.clock.time_msec() - self.start_ts_ms
            )
            if (
                time_spent_waiting.as_millis()
                > WORKER_LOCK_EXCESSIVE_WAITING_WARN_DURATION.as_millis()
            ):
                self._log_excessive_wait(time_spent_waiting)

        except Exception as e:
            self._log_wait_exception(e)

    def _increment_timeout_interval(self) -> float:
        next_interval = min(
            WORKER_LOCK_MAX_RETRY_INTERVAL.as_secs(), self._timeout_interval * 2
        )

        # The jitter value is maintained for the timeout, to help avoid a "thundering
        # herd" situation when all locks may time out at the same time.
        self._timeout_interval = next_interval * random.uniform(0.9, 1.1)
        return self._timeout_interval

    def _log_excessive_wait(self, time_spent_waiting: Duration) -> None:
        raise NotImplementedError()

    def _log_wait_exception(self, e: Exception) -> None:
        raise NotImplementedError()


@attr.s(auto_attribs=True, eq=False)
class WaitingLockSnapshot(_LockWaiter):
    """Waits for a snapshot of read/write lock tokens to be released.

    Unlike `WaitingLock` this never acquires anything, it only waits for the
    given tokens to go away. Holders which take the lock out *after* the
    snapshot was taken are ignored.
    """

    clock: Clock
    store: LockStore
    lock_name: str
    lock_key: str
    write: bool
    tokens: set[str]
    timeout: Duration | None = None
    start_ts_ms: int = 0
    deferred: "defer.Deferred[None]" = attr.Factory(defer.Deferred)
    _timeout_interval: float = 0.1

    async def wait(self) -> bool:
        """Wait for the snapshotted tokens to be released.

        Returns:
            True if they were all released, False if we hit the timeout first.
        """
        self.start_ts_ms = self.clock.time_msec()
        deadline_ts_ms = (
            self.start_ts_ms + self.timeout.as_millis()
            if self.timeout is not None
            else None
        )

        with start_active_span("WaitingLockSnapshot.wait"):
            while True:
                # Create the deferred before querying the database so a release
                # notification cannot be missed between the query and the wait.
                self.deferred = defer.Deferred()

                current_tokens = await self.store.get_read_write_lock_tokens(
                    self.lock_name, self.lock_key, write=self.write
                )
                self.tokens.intersection_update(current_tokens)
                if not self.tokens:
                    return True

                if deadline_ts_ms is not None:
                    remaining_ms = deadline_ts_ms - self.clock.time_msec()
                    if remaining_ms <= 0:
                        return False

                    # Don't sleep past the deadline.
                    self._timeout_interval = min(
                        self._timeout_interval, remaining_ms / 1000
                    )

                await self._wait_to_retry()

    def _log_excessive_wait(self, time_spent_waiting: Duration) -> None:
        logger.warning(
            "(WaitingLockSnapshot (%s, %s)) Time spent waiting for lock holders "
            "is getting excessive: %ss.",
            self.lock_name,
            self.lock_key,
            time_spent_waiting.as_secs(),
        )

    def _log_wait_exception(self, e: Exception) -> None:
        logger.warning(
            "Caught an exception while waiting on WaitingLockSnapshot(lock_name=%s, lock_key=%s): %r",
            self.lock_name,
            self.lock_key,
            e,
        )


@attr.s(auto_attribs=True, eq=False)
class WaitingLock(_LockWaiter):
    clock: Clock
    store: LockStore
    handler: WorkerLocksHandler
    lock_name: str
    lock_key: str
    write: bool | None
    start_ts_ms: int = 0
    deferred: "defer.Deferred[None]" = attr.Factory(defer.Deferred)
    _inner_lock: Lock | None = None
    _timeout_interval: float = 0.1
    _lock_span: "opentracing.Scope" = attr.Factory(
        lambda: start_active_span("WaitingLock.lock")
    )

    async def __aenter__(self) -> None:
        self.start_ts_ms = self.clock.time_msec()
        self._lock_span.__enter__()

        with start_active_span("WaitingLock.waiting_for_lock"):
            while self._inner_lock is None:
                self.deferred = defer.Deferred()

                if self.write is not None:
                    lock = await self.store.try_acquire_read_write_lock(
                        self.lock_name, self.lock_key, write=self.write
                    )
                else:
                    lock = await self.store.try_acquire_lock(
                        self.lock_name, self.lock_key
                    )

                if lock:
                    self._inner_lock = lock
                    break

                # Wait until the notification that the lock might have been
                # released (by the deferred being resolved). We also
                # periodically wake up in case the lock was released, but we
                # weren't notified.
                await self._wait_to_retry()

        return await self._inner_lock.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        assert self._inner_lock

        self.handler.notify_lock_released(self.lock_name, self.lock_key)

        try:
            r = await self._inner_lock.__aexit__(exc_type, exc, tb)
        finally:
            self._lock_span.__exit__(exc_type, exc, tb)

        return r

    def _log_excessive_wait(self, time_spent_waiting: Duration) -> None:
        logger.warning(
            "(WaitingLock (%s, %s)) Time spent waiting to acquire lock "
            "is getting excessive: %ss. There may be a deadlock.",
            self.lock_name,
            self.lock_key,
            time_spent_waiting.as_secs(),
        )

    def _log_wait_exception(self, e: Exception) -> None:
        logger.warning(
            "Caught an exception while waiting on WaitingLock(lock_name=%s, lock_key=%s): %r",
            self.lock_name,
            self.lock_key,
            e,
        )


@attr.s(auto_attribs=True, eq=False)
class WaitingMultiLock(_LockWaiter):
    lock_names: Collection[tuple[str, str]]

    write: bool

    clock: Clock
    store: LockStore
    handler: WorkerLocksHandler

    start_ts_ms: int = 0
    deferred: "defer.Deferred[None]" = attr.Factory(defer.Deferred)

    _inner_lock_cm: AsyncContextManager | None = None
    _timeout_interval: float = 0.1
    _lock_span: "opentracing.Scope" = attr.Factory(
        lambda: start_active_span("WaitingLock.lock")
    )

    async def __aenter__(self) -> None:
        self.start_ts_ms = self.clock.time_msec()
        self._lock_span.__enter__()

        with start_active_span("WaitingLock.waiting_for_lock"):
            while self._inner_lock_cm is None:
                self.deferred = defer.Deferred()

                lock_cm = await self.store.try_acquire_multi_read_write_lock(
                    self.lock_names, write=self.write
                )

                if lock_cm:
                    self._inner_lock_cm = lock_cm
                    break

                # Wait until the notification that the lock might have been
                # released (by the deferred being resolved). We also
                # periodically wake up in case the lock was released, but we
                # weren't notified.
                await self._wait_to_retry()

        assert self._inner_lock_cm
        await self._inner_lock_cm.__aenter__()
        return

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        assert self._inner_lock_cm

        for lock_name, lock_key in self.lock_names:
            self.handler.notify_lock_released(lock_name, lock_key)

        try:
            r = await self._inner_lock_cm.__aexit__(exc_type, exc, tb)
        finally:
            self._lock_span.__exit__(exc_type, exc, tb)

        return r

    def _log_excessive_wait(self, time_spent_waiting: Duration) -> None:
        logger.warning(
            "(WaitingMultiLock (%r)) Time spent waiting to acquire lock "
            "is getting excessive: %ss. There may be a deadlock.",
            self.lock_names,
            time_spent_waiting.as_secs(),
        )

    def _log_wait_exception(self, e: Exception) -> None:
        logger.warning(
            "Caught an exception while waiting on WaitingMultiLock(lock_names=%r): %r",
            self.lock_names,
            e,
        )
