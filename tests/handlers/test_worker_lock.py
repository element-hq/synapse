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
import platform

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.handlers.worker_lock import WORKER_LOCK_MAX_RETRY_INTERVAL
from synapse.server import HomeServer
from synapse.storage.databases.main.lock import (
    _LOCK_REAP_INTERVAL,
    _LOCK_TIMEOUT,
    _RENEWAL_INTERVAL,
)
from synapse.util.clock import Clock
from synapse.util.duration import Duration

from tests import unittest
from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.utils import test_timeout

logger = logging.getLogger(__name__)


class WorkerLockTestCase(unittest.HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.worker_lock_handler = self.hs.get_worker_locks_handler()
        self.store = self.hs.get_datastores().main

    def test_wait_for_lock_locally(self) -> None:
        """Test waiting for a lock on a single worker"""

        lock1 = self.worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        lock2 = self.worker_lock_handler.acquire_lock("name", "key")
        d2 = defer.ensureDeferred(lock2.__aenter__())
        self.assertNoResult(d2)

        self.get_success(lock1.__aexit__(None, None, None))

        self.get_success(d2)
        self.get_success(lock2.__aexit__(None, None, None))

    def test_timeouts_for_lock_locally(self) -> None:
        """
        Test that we regularly retry to reacquire locks.

        This is a regression test to make sure the lock retry time doesn't balloon to a value
        so large it can't even be printed reliably anymore.
        """

        # Create and acquire the first lock
        lock1 = self.worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        # Create and try to acquire the second lock
        lock2 = self.worker_lock_handler.acquire_lock("name", "key")
        d2 = defer.ensureDeferred(lock2.__aenter__())
        # Make sure we haven't acquired the lock yet (`lock1` still holds it)
        self.assertNoResult(d2)

        # Advance time by an hour (some duration that would previously cause our timeout
        # to balloon if it weren't constrained). Max back-off (saturate)
        #
        # Note: We use `_pump_by` instead of `pump`/`advance` as the `Lock` has an
        # internal background looping call that runs every 30 seconds
        # (`_RENEWAL_INTERVAL`) to renew the `Lock` and push it's "drop timeout" value
        # further out by 2 minutes (`_LOCK_TIMEOUT`). The `Lock` will prematurely
        # drop if this renewal is not allowed to run, which sours the test.
        # self.pump(amount=Duration(hours=1))
        self._pump_by(amount=Duration(hours=1), by=_RENEWAL_INTERVAL)

        # Make sure we haven't acquired the `lock2` yet (`lock1` still holds it)
        self.assertNoResult(d2)

        # Release the first lock (`lock1`). The second lock(`lock2`) should be
        # automatically acquired by the `pump()` inside `get_success()`
        self.get_success(lock1.__aexit__(None, None, None))

        # We should now have the lock
        self.successResultOf(d2)

    def _pump_by(
        self,
        *,
        amount: Duration = Duration(seconds=0),
        by: Duration = Duration(seconds=0.1),
    ) -> None:
        """
        Like `self.pump()` but you can specify the time increment to advance with until
        you reach the time amount.

        Unlike `self.pump()`, this doesn't multiply the time at all.

        Args:
            amount: The amount of time to advance
            by: The time increment in seconds to advance time by until we reach the `amount`
        """
        end_time_s = self.reactor.seconds() + amount.as_secs()

        while self.reactor.seconds() < end_time_s:
            self.reactor.advance(by.as_secs())

    def test_lock_contention(self) -> None:
        """Test lock contention when a lot of locks wait on a single worker"""
        nb_locks_to_test = 500
        current_machine = platform.machine().lower()
        if current_machine.startswith("riscv"):
            # RISC-V specific settings
            timeout_seconds = 15  # Increased timeout for RISC-V
            # add a print or log statement here for visibility in CI logs
            logger.info(  # use logger.info
                "Detected RISC-V architecture (%s). "
                "Adjusting test_lock_contention: timeout=%ss",
                current_machine,
                timeout_seconds,
            )
        else:
            # Settings for other architectures
            timeout_seconds = 5
        # It takes around 0.5s on a 5+ years old laptop
        with test_timeout(timeout_seconds):  # Use the dynamically set timeout
            d = self._take_locks(
                nb_locks_to_test
            )  # Use the (potentially adjusted) number of locks
            self.assertEqual(
                self.get_success(d), nb_locks_to_test
            )  # Assert against the used number of locks

    async def _take_locks(self, nb_locks: int) -> int:
        locks = [
            self.hs.get_worker_locks_handler().acquire_lock("test_lock", "")
            for _ in range(nb_locks)
        ]

        nb_locks_taken = 0
        for lock in locks:
            async with lock:
                nb_locks_taken += 1

        return nb_locks_taken


class WorkerLockWorkersTestCase(BaseMultiWorkerStreamTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.main_worker_lock_handler = self.hs.get_worker_locks_handler()

    def test_wait_for_lock_worker(self) -> None:
        """Test waiting for a lock on another worker"""

        worker = self.make_worker_hs(
            "synapse.app.generic_worker",
            extra_config={
                "redis": {"enabled": True},
            },
        )
        worker_lock_handler = worker.get_worker_locks_handler()

        lock1 = self.main_worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        lock2 = worker_lock_handler.acquire_lock("name", "key")
        d2 = defer.ensureDeferred(lock2.__aenter__())
        self.assertNoResult(d2)

        self.get_success(lock1.__aexit__(None, None, None))

        self.get_success(d2)
        self.get_success(lock2.__aexit__(None, None, None))

    def test_timeouts_for_lock_worker(self) -> None:
        """
        Test that we regularly retry to reacquire locks.

        This is a regression test to make sure the lock retry time doesn't balloon to a value
        so large it can't even be printed reliably anymore.
        """
        worker = self.make_worker_hs(
            "synapse.app.generic_worker",
            extra_config={
                "redis": {"enabled": True},
            },
        )
        worker_lock_handler = worker.get_worker_locks_handler()

        # Create and acquire the first lock on the main process
        lock1 = self.main_worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        # Create and try to acquire the second lock on the worker
        lock2 = worker_lock_handler.acquire_lock("name", "key")
        d2 = defer.ensureDeferred(lock2.__aenter__())
        # Make sure we haven't acquired the lock yet (`lock1` still holds it)
        self.assertNoResult(d2)

        # Advance time by an hour (some duration that would previously cause our timeout
        # to balloon if it weren't constrained). Max back-off (saturate)
        #
        # Note: We use `_pump_by` instead of `pump`/`advance` as the `Lock` has an
        # internal background looping call that runs every 30 seconds
        # (`_RENEWAL_INTERVAL`) to renew the `Lock` and push it's "drop timeout" value
        # further out by 2 minutes (`_LOCK_TIMEOUT`). The `Lock` will prematurely
        # drop if this renewal is not allowed to run, which sours the test.
        # self.pump(amount=Duration(hours=1))
        self._pump_by(amount=Duration(hours=1), by=_RENEWAL_INTERVAL)

        # Make sure we haven't acquired the `lock2` yet (`lock1` still holds it)
        self.assertNoResult(d2)

        # Drop the lock without releasing it. If we just normally released the lock
        # (`self.get_success(lock1.__aexit__(None, None, None))`), the
        # `add_lock_released_callback`/`notify_lock_released` cycle would signal that we
        # should re-aquire the lock right away (on the next reactor tick). And we want
        # to avoid that as the point of this test is to stress the retry timeout
        # interval and `WORKER_LOCK_MAX_RETRY_INTERVAL`.
        del lock1

        # Wait for `lock1` to go stale (it won't be renewed anymore because we deleted
        # it just above)
        self._pump_by(
            amount=_LOCK_TIMEOUT,
            by=_RENEWAL_INTERVAL,
        )

        # Wait just enough time so `lock1` is reaped (found stale and forcefully drops
        # the lock its holding)
        self._pump_by(
            amount=_LOCK_REAP_INTERVAL,
            by=_RENEWAL_INTERVAL,
        )

        # Wait just enough time so `lock2` tries re-acquiring the lock. Should be no
        # longer than our `WORKER_LOCK_MAX_RETRY_INTERVAL`.
        self._pump_by(
            amount=WORKER_LOCK_MAX_RETRY_INTERVAL,
            by=_RENEWAL_INTERVAL,
        )

        # We should now have the lock
        self.successResultOf(d2)

    def _pump_by(
        self,
        *,
        amount: Duration = Duration(seconds=0),
        by: Duration = Duration(seconds=0.1),
    ) -> None:
        """
        Like `self.pump()` but you can specify the time increment to advance with until
        you reach the time amount.

        Unlike `self.pump()`, this doesn't multiply the time at all.

        Args:
            amount: The amount of time to advance
            by: The time increment in seconds to advance time by until we reach the `amount`
        """
        end_time_s = self.reactor.seconds() + amount.as_secs()

        while self.reactor.seconds() < end_time_s:
            self.reactor.advance(by.as_secs())
