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

from typing import Any, Callable, Iterable, Optional
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.util import Clock

from tests.replication._base import BaseStreamTestCase


class BaseWorkerStoreTestCase(BaseStreamTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver(federation_client=Mock())

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.reconnect()

        self.master_store = hs.get_datastores().main
        self.worker_store = self.worker_hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistance = persistence

    def replicate(self) -> None:
        """Tell the master side of replication that something has happened, and then
        wait for the replication to occur.
        """
        self.streamer.on_notifier_poke()
        self.pump(0.1)

    def check(
        self,
        method: str,
        args: Iterable[Any],
        expected_result: Optional[Any] = None,
        asserter: Optional[Callable[[Any, Any, Optional[Any]], None]] = None,
    ) -> None:
        if asserter is None:
            asserter = self.assertEqual

        master_result = self.get_success(getattr(self.master_store, method)(*args))
        worker_result = self.get_success(getattr(self.worker_store, method)(*args))
        if expected_result is not None:
            asserter(
                master_result,
                expected_result,
                "Expected master result to be %r but was %r"
                % (expected_result, master_result),
            )
            asserter(
                worker_result,
                expected_result,
                "Expected worker result to be %r but was %r"
                % (expected_result, worker_result),
            )
        asserter(
            master_result,
            worker_result,
            "Worker result %r does not match master result %r"
            % (worker_result, master_result),
        )
