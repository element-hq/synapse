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

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.databases.main.transactions import DestinationRetryTimings
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class TransactionStoreTestCase(HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main

    def test_get_set_transactions(self) -> None:
        """Tests that we can successfully get a non-existent entry for
        destination retries, as well as testing tht we can set and get
        correctly.
        """
        r = self.get_success(self.store.get_destination_retry_timings("example.com"))
        self.assertIsNone(r)

        self.get_success(
            self.store.set_destination_retry_timings("example.com", 1000, 50, 100)
        )

        r = self.get_success(self.store.get_destination_retry_timings("example.com"))

        self.assertEqual(
            DestinationRetryTimings(
                retry_last_ts=50, retry_interval=100, failure_ts=1000
            ),
            r,
        )

    def test_initial_set_transactions(self) -> None:
        """Tests that we can successfully set the destination retries (there
        was a bug around invalidating the cache that broke this)
        """
        d = self.store.set_destination_retry_timings("example.com", 1000, 50, 100)
        self.get_success(d)

    def test_large_destination_retry(self) -> None:
        max_retry_interval_ms = (
            self.hs.config.federation.destination_max_retry_interval_ms
        )
        d = self.store.set_destination_retry_timings(
            "example.com",
            max_retry_interval_ms,
            max_retry_interval_ms,
            max_retry_interval_ms,
        )
        self.get_success(d)

        d2 = self.store.get_destination_retry_timings("example.com")
        self.get_success(d2)
