# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import List

import attr

from tests.unittest import TestCase

from synapse.storage.controllers.persist_events import find_predecessors

class FindPredecessorsTestCase(TestCase):
    def test_predecessors_finds_nothing_if_event_is_not_in_batch(self) -> None:
        batch = [
            (FakeEvent(event_id="B", prev_event_ids=["C"]), None),
        ]

        predecessors = find_predecessors({"A"}, batch)  # type: ignore[arg-type]
        self.assertEqual(predecessors, set())

    def test_predecessors_finds_only_event_if_it_has_no_predecessors(self) -> None:
        batch = [
            (FakeEvent(event_id="E1", prev_event_ids=[]), None),
            (FakeEvent(event_id="E2", prev_event_ids=["E3"]), None),
        ]

        predecessors = find_predecessors({"E1"}, batch)  # type: ignore[arg-type]
        self.assertEqual(predecessors, {"E1"})

    def test_predecessors_finds_all_ancestors(self) -> None:
        batch = [
            (FakeEvent(event_id="A", prev_event_ids=["B", "C"]), None),
            (FakeEvent(event_id="B", prev_event_ids=["D"]), None),
            (FakeEvent(event_id="C", prev_event_ids=["D"]), None),
            (FakeEvent(event_id="D", prev_event_ids=["E"]), None),
            (FakeEvent(event_id="E", prev_event_ids=[]), None),
            (FakeEvent(event_id="F", prev_event_ids=["G", "H"]), None),
            (FakeEvent(event_id="G", prev_event_ids=[]), None),
        ]
        predecessors = find_predecessors({"A"}, batch)  # type: ignore[arg-type]
        self.assertEqual(
            predecessors, {"A", "B", "C", "D", "E"}
        )

    def test_predecessors_ignores_cycles(self) -> None:
        batch = [
            (FakeEvent(event_id="E1", prev_event_ids=["E2"]), None),
            (FakeEvent(event_id="E2", prev_event_ids=["E1"]), None),
        ]

        predecessors = find_predecessors({"E1"}, batch)  # type: ignore[arg-type]
        self.assertEqual(predecessors, {"E1", "E2"})

    def test_predecessors_ignores_self_reference_cycles(self) -> None:
        batch = [
            (FakeEvent(event_id="E1", prev_event_ids=["E2"]), None),
            (FakeEvent(event_id="E2", prev_event_ids=["E2"]), None),
        ]

        predecessors = find_predecessors({"E1"}, batch)  # type: ignore[arg-type]
        self.assertEqual(predecessors, {"E1", "E2"})

    def test_predecessors_finds_ancestors_of_multiple_starting_events(self) -> None:
        batch = [
            (FakeEvent(event_id="A", prev_event_ids=["B"]), None),
            (FakeEvent(event_id="B", prev_event_ids=[]), None),
            (FakeEvent(event_id="C", prev_event_ids=["D"]), None),
            (FakeEvent(event_id="D", prev_event_ids=["E"]), None),
            (FakeEvent(event_id="E", prev_event_ids=[]), None),
            (FakeEvent(event_id="F", prev_event_ids=["G"]), None),
            (FakeEvent(event_id="G", prev_event_ids=[]), None),
        ]
        predecessors = find_predecessors({"A", "C"}, batch)  # type: ignore[arg-type]
        self.assertEqual(
            predecessors, {"A", "B", "C", "D", "E"}
        )



@attr.s(auto_attribs=True)
class FakeEvent:
    event_id: str
    _prev_event_ids: List[str]

    def prev_event_ids(self) -> List[str]:
        return self._prev_event_ids