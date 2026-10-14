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

"""`extract_heroes_from_room_summary` against `rust/src/sync/summary.rs`."""

import random

from parameterized import parameterized

from synapse.api.constants import Membership
from synapse.storage.databases.main.roommember import extract_heroes_from_room_summary
from synapse.storage.roommember import MemberSummary
from synapse.synapse_rust.sync import extract_heroes_for_test

from tests import unittest
from tests.sync.fixtures import (
    ME,
    SEEDS,
)


class HeroesTestCase(unittest.TestCase):
    """`extract_heroes_from_room_summary` against `rust/src/sync/summary.rs`."""

    @parameterized.expand(SEEDS)
    def test_random_summaries_agree(self, seed: int) -> None:
        rng = random.Random(seed)
        users = [ME] + [f"@u{i}:test" for i in range(8)]
        for _ in range(100):
            details: dict[str, MemberSummary] = {}
            for membership in rng.sample(
                [
                    Membership.JOIN,
                    Membership.INVITE,
                    Membership.LEAVE,
                    Membership.BAN,
                    "knock",
                ],
                rng.randint(0, 5),
            ):
                members = [
                    (user, f"$m-{user}")
                    for user in rng.sample(users, rng.randint(0, 7))
                ]
                details[membership] = MemberSummary(
                    members, len(members) + rng.randint(0, 3)
                )
            expected = extract_heroes_from_room_summary(details, ME)
            actual = extract_heroes_for_test(
                {m: (s.members, s.count) for m, s in details.items()}, ME
            )
            self.assertEqual(actual, expected, str(details))
