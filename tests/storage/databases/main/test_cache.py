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
from unittest.mock import Mock, call

from synapse.storage.database import LoggingTransaction

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.unittest import HomeserverTestCase


class CacheInvalidationTestCase(HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.hs.get_datastores().main

    def test_bulk_invalidation(self) -> None:
        master_invalidate = Mock()

        self.store._get_cached_user_device.invalidate = master_invalidate

        keys_to_invalidate = [
            ("a", "b"),
            ("c", "d"),
            ("e", "f"),
            ("g", "h"),
        ]

        def test_txn(txn: LoggingTransaction) -> None:
            self.store._invalidate_cache_and_stream_bulk(
                txn,
                # This is an arbitrarily chosen cached store function. It was chosen
                # because it takes more than one argument. We'll use this later to
                # check that the invalidation was actioned over replication.
                cache_func=self.store._get_cached_user_device,
                key_tuples=keys_to_invalidate,
            )

        self.get_success(
            self.store.db_pool.runInteraction(
                "test_invalidate_cache_and_stream_bulk", test_txn
            )
        )

        master_invalidate.assert_has_calls(
            [call(key_list) for key_list in keys_to_invalidate],
            any_order=True,
        )


class CacheInvalidationOverReplicationTestCase(BaseMultiWorkerStreamTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.hs.get_datastores().main

    def test_bulk_invalidation_replicates(self) -> None:
        """Like test_bulk_invalidation, but also checks the invalidations replicate."""
        master_invalidate = Mock()
        worker_invalidate = Mock()

        self.store._get_cached_user_device.invalidate = master_invalidate
        worker = self.make_worker_hs("synapse.app.generic_worker")
        worker_ds = worker.get_datastores().main
        worker_ds._get_cached_user_device.invalidate = worker_invalidate

        keys_to_invalidate = [
            ("a", "b"),
            ("c", "d"),
            ("e", "f"),
            ("g", "h"),
        ]

        def test_txn(txn: LoggingTransaction) -> None:
            self.store._invalidate_cache_and_stream_bulk(
                txn,
                # This is an arbitrarily chosen cached store function. It was chosen
                # because it takes more than one argument. We'll use this later to
                # check that the invalidation was actioned over replication.
                cache_func=self.store._get_cached_user_device,
                key_tuples=keys_to_invalidate,
            )

        assert self.store._cache_id_gen is not None
        initial_token = self.store._cache_id_gen.get_current_token()
        self.get_success(
            self.database_pool.runInteraction(
                "test_invalidate_cache_and_stream_bulk", test_txn
            )
        )
        second_token = self.store._cache_id_gen.get_current_token()

        self.assertGreaterEqual(second_token, initial_token + len(keys_to_invalidate))

        self.get_success(
            worker.get_replication_data_handler().wait_for_stream_position(
                "master", "caches", second_token
            )
        )

        master_invalidate.assert_has_calls(
            [call(key_list) for key_list in keys_to_invalidate],
            any_order=True,
        )
        worker_invalidate.assert_has_calls(
            [call(key_list) for key_list in keys_to_invalidate],
            any_order=True,
        )
