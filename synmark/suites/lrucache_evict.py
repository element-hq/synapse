#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from pyperf import perf_counter

from synapse.types import ISynapseReactor
from synapse.util.caches.lrucache import LruCache
from synapse.util.clock import Clock


async def main(reactor: ISynapseReactor, loops: int) -> float:
    """
    Benchmark `loops` number of insertions into LruCache where half of them are
    evicted.
    """
    # Ignore linter error here since we are running outside of the context of a
    # Synapse `HomeServer`.
    cache: LruCache[int, bool] = LruCache(
        max_size=loops // 2,
        clock=Clock(reactor, server_name="synmark_benchmark"),  # type: ignore[multiple-internal-clocks]
        server_name="synmark_benchmark",
    )

    start = perf_counter()

    for i in range(loops):
        cache[i] = True

    end = perf_counter() - start

    return end
