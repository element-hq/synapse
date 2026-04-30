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
from unittest.mock import patch

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.handlers.worker_lock import WORKER_LOCK_MAX_RETRY_INTERVAL
from synapse.server import HomeServer
from synapse.storage.databases.main.lock import _LOCK_TIMEOUT_MS
from synapse.util.clock import Clock

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
        """Test timeouts are incremented for a lock on a single worker"""
        lock1 = self.worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        lock2 = self.worker_lock_handler.acquire_lock("name", "key")

        d2 = defer.ensureDeferred(lock2.__aenter__())

        self.assertNoResult(d2)

        # Wrap the database call to attempt to acquire the lock, so we can detect how
        # many times it is called on. With lock1 having already been entered, any other
        # calls to try_acquire_lock() should only be for lock2
        with patch.object(
            self.store,
            "try_acquire_lock",
            wraps=self.store.try_acquire_lock,
        ) as wrapped_try_acquire_lock_method:
            # A Lock has an internal background looping call that runs every 30 seconds
            # to renew the Lock and push it's "drop timeout" value further out by 2
            # minutes. The Lock will prematurely drop if this renewal is not allowed to
            # run, which sours the test.

            # Advance time by a bit over 3 hours. _LOCK_TIMEOUT_MS is 2 minutes. Remove
            # 1 second from that to give it the barest minimum time to still renew
            # itself(the test fails if we do not remove that second and the Lock will
            # drop)
            # 2 * 60 = 120 seconds
            # 120 - 1 = 119 seconds to actually advance
            pump_fraction = (_LOCK_TIMEOUT_MS / 1000) - 1
            # 119 * 100 = 11_900 seconds for the entire pump call
            # 11_900 / 60 = 180 minutes and 18.33 seconds
            self.pump(pump_fraction)  # iterates 100 times, see the math above
            # The actual Lock should not exist still
            assert lock2._inner_lock is None

            wrapped_try_acquire_lock_method.reset_mock()

            # By this point, the timeout on the WaitingLock should be maxed out at
            # WORKER_LOCK_MAX_RETRY_INTERVAL. Wait twice that long using pump() so lock1
            # doesn't drop in the background causing an incorrect pass for the test.
            # WORKER_LOCK_MAX_RETRY_INTERVAL = 900 seconds
            # 900 * 2 = 1800 seconds
            # 1800 seconds / 100 iterations = 18 seconds per iteration
            pump_fraction = 2 * WORKER_LOCK_MAX_RETRY_INTERVAL / 100
            # In case later adjustments to constants causes a drift in the calculation,
            # let future us know this is not necessarily a fault
            assert pump_fraction < _LOCK_TIMEOUT_MS, (
                "Please adjust this test, the calculated pump() iteration exceeds the "
                f"time the Lock will drop by: {pump_fraction} > {_LOCK_TIMEOUT_MS}"
            )
            self.pump(pump_fraction)

            # Should be called 1 or 2 times, there is a jitter to account for
            call_count = wrapped_try_acquire_lock_method.call_count
            assert 0 < call_count < 3, (
                f"Count of times try_to_acquire() was called was out of presumed bounds(> 0 but < 3): {call_count}"
            )
            self.get_success(lock1.__aexit__(None, None, None))

            self.get_success(d2)
            self.get_success(lock2.__aexit__(None, None, None))

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
        """Test timeouts are incremented for a lock on another worker"""
        worker = self.make_worker_hs(
            "synapse.app.generic_worker",
            extra_config={
                "redis": {"enabled": True},
            },
        )
        worker_lock_handler = worker.get_worker_locks_handler()
        worker_data_store = worker.get_datastores().main

        lock1 = self.main_worker_lock_handler.acquire_lock("name", "key")
        self.get_success(lock1.__aenter__())

        lock2 = worker_lock_handler.acquire_lock("name", "key")

        d2 = defer.ensureDeferred(lock2.__aenter__())
        self.assertNoResult(d2)

        # Wrap the database call to attempt to acquire the lock, so we can detect how
        # many times it is called on. With lock1 having already been entered, any other
        # calls to try_acquire_lock() should only be for lock2
        with patch.object(
            worker_data_store,
            "try_acquire_lock",
            wraps=worker_data_store.try_acquire_lock,
        ) as wrapped_worker_try_acquire_lock_method:
            # A Lock has an internal background looping call that runs every 30 seconds
            # to renew the Lock and push it's "drop timeout" value further out by 2
            # minutes. The Lock will prematurely drop if this renewal is not allowed to
            # run, which sours the test.

            # Advance time by a bit over 3 hours. _LOCK_TIMEOUT_MS is 2 minutes. Remove
            # 1 second from that to give it the barest minimum time to still renew
            # itself(the test fails if we do not remove that second and the Lock will
            # drop)
            # 2 * 60 = 120 seconds
            # 120 - 1 = 119 seconds to actually advance
            pump_fraction = (_LOCK_TIMEOUT_MS / 1000) - 1
            # 119 * 100 = 11_900 seconds for the entire pump call
            # 11_900 / 60 = 180 minutes and 18.33 seconds
            self.pump(pump_fraction)  # iterates 100 times, see the math above
            # The actual Lock should not exist still
            assert lock2._inner_lock is None

            wrapped_worker_try_acquire_lock_method.reset_mock()

            # By this point, the timeout on the WaitingLock should be maxed out at
            # WORKER_LOCK_MAX_RETRY_INTERVAL. Wait twice that long using pump() so lock1
            # doesn't drop in the background causing an incorrect pass for the test.
            # WORKER_LOCK_MAX_RETRY_INTERVAL = 900 seconds
            # 900 * 2 = 1800 seconds
            # 1800 seconds / 100 iterations = 18 seconds per iteration
            pump_fraction = 2 * WORKER_LOCK_MAX_RETRY_INTERVAL / 100
            # In case later adjustments to constants causes a drift in the calculation,
            # let future us know this is not necessarily a fault
            assert pump_fraction < _LOCK_TIMEOUT_MS, (
                "Please adjust this test, the calculated pump() iteration exceeds the "
                f"time the Lock will drop by: {pump_fraction} > {_LOCK_TIMEOUT_MS}"
            )
            self.pump(pump_fraction)

            # Should be called 1 or 2 times, there is a jitter to account for
            call_count = wrapped_worker_try_acquire_lock_method.call_count
            assert 0 < call_count < 3, (
                f"Count of times try_to_acquire() was called was out of presumed bounds(> 0 but < 3): {call_count}"
            )
            self.get_success(lock1.__aexit__(None, None, None))

            self.get_success(d2)
            self.get_success(lock2.__aexit__(None, None, None))
