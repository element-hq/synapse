#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""The wiring of the Rust sliding sync handler (`rust/src/sliding_sync/`)."""

from unittest import SkipTest

from prometheus_client import REGISTRY

from synapse.handlers.sliding_sync import observe_rust_sliding_sync_stats
from synapse.synapse_rust.sliding_sync import SlidingSyncStats

import tests.utils
from tests import unittest
from tests.unittest import override_config


class RustSlidingSyncHandlerTestCase(unittest.HomeserverTestCase):
    """The Rust sliding sync handler is only wired up on the native Rust
    driver."""

    @override_config({"experimental_features": {"rust_sliding_sync": "on"}})
    def test_needs_the_rust_driver(self) -> None:
        if tests.utils.RUST_SLIDING_SYNC_FOR_TESTS:
            raise SkipTest("SYNAPSE_TEST_RUST_SLIDING_SYNC overrides the config")
        handler = self.hs.get_sliding_sync_handler()
        self.assertEqual(handler._rust_sliding_sync_mode, "on")
        if tests.utils.USE_RUST_DRIVER_FOR_TESTS:
            self.assertIsNotNone(handler._rust_sliding_sync)
            self.assertIsNotNone(self.hs.get_rust_handlers().sliding_sync)
        else:
            # Falls back to the Python handler, with a warning.
            self.assertIsNone(handler._rust_sliding_sync)
            self.assertIsNone(self.hs.get_rust_handlers().sliding_sync)

    def test_off_by_default(self) -> None:
        handler = self.hs.get_sliding_sync_handler()
        # Unless the test run sets it for every homeserver.
        expected = tests.utils.RUST_SLIDING_SYNC_FOR_TESTS or "off"
        self.assertEqual(handler._rust_sliding_sync_mode, expected)
        if expected == "off":
            self.assertIsNone(handler._rust_sliding_sync)

    def test_observe_stats(self) -> None:
        def sample(name: str) -> float:
            value = REGISTRY.get_sample_value(name, {"server_name": "stats.test"})
            return value or 0.0

        before_db = sample("synapse_sliding_sync_rust_db_seconds_sum")
        before_queries = sample("synapse_sliding_sync_rust_db_queries_sum")
        before_rooms = sample("synapse_sliding_sync_rust_rooms_computed_count")

        stats = SlidingSyncStats(
            db_time=0.5, queries=12, transactions=2, rooms_computed=3
        )
        self.assertEqual(stats.queries, 12)
        self.assertEqual(stats.lazy_member_lookups, 0)
        observe_rust_sliding_sync_stats(stats, "stats.test")

        self.assertAlmostEqual(
            sample("synapse_sliding_sync_rust_db_seconds_sum") - before_db, 0.5
        )
        self.assertEqual(
            sample("synapse_sliding_sync_rust_db_queries_sum") - before_queries, 12
        )
        self.assertEqual(
            sample("synapse_sliding_sync_rust_rooms_computed_count") - before_rooms, 1
        )
