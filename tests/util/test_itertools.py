#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import Iterable, Sequence

from synapse.util.iterutils import (
    chunk_seq,
    sorted_topologically,
    sorted_topologically_batched,
)

from tests.unittest import TestCase


class ChunkSeqTests(TestCase):
    def test_short_seq(self) -> None:
        parts = chunk_seq("123", 8)

        self.assertEqual(
            list(parts),
            ["123"],
        )

    def test_long_seq(self) -> None:
        parts = chunk_seq("abcdefghijklmnop", 8)

        self.assertEqual(
            list(parts),
            ["abcdefgh", "ijklmnop"],
        )

    def test_uneven_parts(self) -> None:
        parts = chunk_seq("abcdefghijklmnop", 5)

        self.assertEqual(
            list(parts),
            ["abcde", "fghij", "klmno", "p"],
        )

    def test_empty_input(self) -> None:
        parts: Iterable[Sequence] = chunk_seq([], 5)

        self.assertEqual(
            list(parts),
            [],
        )


class SortTopologically(TestCase):
    def test_empty(self) -> None:
        "Test that an empty graph works correctly"

        graph: dict[int, list[int]] = {}
        self.assertEqual(list(sorted_topologically([], graph)), [])

    def test_handle_empty_graph(self) -> None:
        "Test that a graph where a node doesn't have an entry is treated as empty"

        graph: dict[int, list[int]] = {}

        # For disconnected nodes the output is simply sorted.
        self.assertEqual(list(sorted_topologically([1, 2], graph)), [1, 2])

    def test_disconnected(self) -> None:
        "Test that a graph with no edges work"

        graph: dict[int, list[int]] = {1: [], 2: []}

        # For disconnected nodes the output is simply sorted.
        self.assertEqual(list(sorted_topologically([1, 2], graph)), [1, 2])

    def test_linear(self) -> None:
        "Test that a simple `4 -> 3 -> 2 -> 1` graph works"

        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3]}

        self.assertEqual(list(sorted_topologically([4, 3, 2, 1], graph)), [1, 2, 3, 4])

    def test_subset(self) -> None:
        "Test that only sorting a subset of the graph works"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3]}

        self.assertEqual(list(sorted_topologically([4, 3], graph)), [3, 4])

    def test_fork(self) -> None:
        "Test that a forked graph works"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [1], 4: [2, 3]}

        # Valid orderings are `[1, 3, 2, 4]` or `[1, 2, 3, 4]`, but we should
        # always get the same one.
        self.assertEqual(list(sorted_topologically([4, 3, 2, 1], graph)), [1, 2, 3, 4])

    def test_duplicates(self) -> None:
        "Test that a graph with duplicate edges work"
        graph: dict[int, list[int]] = {1: [], 2: [1, 1], 3: [2, 2], 4: [3]}

        self.assertEqual(list(sorted_topologically([4, 3, 2, 1], graph)), [1, 2, 3, 4])

    def test_multiple_paths(self) -> None:
        "Test that a graph with multiple paths between two nodes work"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3, 2, 1]}

        self.assertEqual(list(sorted_topologically([4, 3, 2, 1], graph)), [1, 2, 3, 4])


class SortTopologicallyBatched(TestCase):
    "Test cases for `sorted_topologically_batched`"

    def test_empty(self) -> None:
        "Test that an empty graph works correctly"

        graph: dict[int, list[int]] = {}
        self.assertEqual(list(sorted_topologically_batched([], graph)), [])

    def test_handle_empty_graph(self) -> None:
        "Test that a graph where a node doesn't have an entry is treated as empty"

        graph: dict[int, list[int]] = {}

        # For disconnected nodes the output is simply sorted.
        self.assertEqual(list(sorted_topologically_batched([1, 2], graph)), [[1, 2]])

    def test_disconnected(self) -> None:
        "Test that a graph with no edges work"

        graph: dict[int, list[int]] = {1: [], 2: []}

        # For disconnected nodes the output is simply sorted.
        self.assertEqual(list(sorted_topologically_batched([1, 2], graph)), [[1, 2]])

    def test_linear(self) -> None:
        "Test that a simple `4 -> 3 -> 2 -> 1` graph works"

        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3]}

        self.assertEqual(
            list(sorted_topologically_batched([4, 3, 2, 1], graph)),
            [[1], [2], [3], [4]],
        )

    def test_subset(self) -> None:
        "Test that only sorting a subset of the graph works"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3]}

        self.assertEqual(list(sorted_topologically_batched([4, 3], graph)), [[3], [4]])

    def test_fork(self) -> None:
        "Test that a forked graph works"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [1], 4: [2, 3]}

        # Valid orderings are `[1, 3, 2, 4]` or `[1, 2, 3, 4]`, but we should
        # always get the same one.
        self.assertEqual(
            list(sorted_topologically_batched([4, 3, 2, 1], graph)), [[1], [2, 3], [4]]
        )

    def test_duplicates(self) -> None:
        "Test that a graph with duplicate edges work"
        graph: dict[int, list[int]] = {1: [], 2: [1, 1], 3: [2, 2], 4: [3]}

        self.assertEqual(
            list(sorted_topologically_batched([4, 3, 2, 1], graph)),
            [[1], [2], [3], [4]],
        )

    def test_multiple_paths(self) -> None:
        "Test that a graph with multiple paths between two nodes work"
        graph: dict[int, list[int]] = {1: [], 2: [1], 3: [2], 4: [3, 2, 1]}

        self.assertEqual(
            list(sorted_topologically_batched([4, 3, 2, 1], graph)),
            [[1], [2], [3], [4]],
        )
