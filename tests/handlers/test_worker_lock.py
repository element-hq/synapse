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

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest
from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.utils import test_timeout


class WorkerLockTestCase(unittest.HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.worker_lock_handler = self.hs.get_worker_locks_handler()

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

    def test_lock_contention(self) -> None:
        """Test lock contention when a lot of locks wait on a single worker"""

        # It takes around 0.5s on a 5+ years old laptop
        with test_timeout(5):
            nb_locks = 500
            d = self._take_locks(nb_locks)
            self.assertEqual(self.get_success(d), nb_locks)

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
