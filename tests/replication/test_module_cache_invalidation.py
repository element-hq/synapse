#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

import synapse
from synapse.module_api import cached

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import get_clock

logger = logging.getLogger(__name__)

FIRST_VALUE = "one"
SECOND_VALUE = "two"

KEY = "mykey"


class TestCache:
    current_value = FIRST_VALUE
    server_name = "test_server"  # nb must be called this for @cached
    _, clock = get_clock()  # nb must be called this for @cached

    @cached()
    async def cached_function(self, user_id: str) -> str:
        return self.current_value


class ModuleCacheInvalidationTestCase(BaseMultiWorkerStreamTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
    ]

    def test_module_cache_full_invalidation(self) -> None:
        main_cache = TestCache()
        self.hs.get_module_api().register_cached_function(main_cache.cached_function)

        worker_hs = self.make_worker_hs("synapse.app.generic_worker")

        worker_cache = TestCache()
        worker_hs.get_module_api().register_cached_function(
            worker_cache.cached_function
        )

        self.assertEqual(FIRST_VALUE, self.get_success(main_cache.cached_function(KEY)))
        self.assertEqual(
            FIRST_VALUE, self.get_success(worker_cache.cached_function(KEY))
        )

        main_cache.current_value = SECOND_VALUE
        worker_cache.current_value = SECOND_VALUE
        # No invalidation yet, should return the cached value on both the main process and the worker
        self.assertEqual(FIRST_VALUE, self.get_success(main_cache.cached_function(KEY)))
        self.assertEqual(
            FIRST_VALUE, self.get_success(worker_cache.cached_function(KEY))
        )

        # Full invalidation on the main process, should be replicated on the worker that
        # should returned the updated value too
        self.get_success(
            self.hs.get_module_api().invalidate_cache(
                main_cache.cached_function, (KEY,)
            )
        )

        self.assertEqual(
            SECOND_VALUE, self.get_success(main_cache.cached_function(KEY))
        )
        self.assertEqual(
            SECOND_VALUE, self.get_success(worker_cache.cached_function(KEY))
        )
