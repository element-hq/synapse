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

"""Shared pieces of the differential tests of the Rust sync handler's pure
parts (`test_rust_*.py` in this package): random inputs the Python and Rust
implementations must agree on.

The random cases are seeded, so a failure reproduces.
"""

import random

from immutabledict import immutabledict

from synapse.types import RoomStreamToken

ME = "@me:test"
USERS = [ME, "@alice:test", "@bob:test", "@spam:example.com"]
ROOMS = ["!a:test", "!b:test", "!c:example.com"]
TYPES = [
    "m.room.message",
    "m.room.member",
    "m.room.topic",
    "m.receipt",
    "m.typing",
    "org.example.custom",
]
WRITERS = ["master", "w1", "w2", "w3"]

SEEDS = [(seed,) for seed in range(20)]


def pick_list(
    rng: random.Random, values: list[str], wildcard: str | None = None
) -> list[str]:
    """A random sub-list of `values`, sometimes with a wildcard in it."""
    chosen = rng.sample(values, rng.randint(0, len(values)))
    if wildcard and rng.random() < 0.15:
        chosen.append(wildcard)
    return chosen


def random_live_token(rng: random.Random) -> RoomStreamToken:
    """A live token with a random instance map over `WRITERS`."""
    stream = rng.randint(0, 20)
    instance_map = {
        name: stream + rng.randint(1, 10)
        for name in rng.sample(WRITERS, rng.randint(0, len(WRITERS)))
    }
    return RoomStreamToken(stream=stream, instance_map=immutabledict(instance_map))
