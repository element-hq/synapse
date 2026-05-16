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
#

from unittest.mock import Mock

from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.events import EventBase, FrozenEvent, make_event_from_dict
from synapse.events.py_protocol import (
    EventProtocol,
    MSC4242Event,
    all_supports_msc4242_state_dag,
    supports_msc4242_state_dag,
)

from tests.unittest import TestCase


def _make_event(room_version: RoomVersion) -> EventBase:
    """Helper to make an EventBase with the given room version."""
    event_dict = {
        "content": {},
        "sender": "@user:example.com",
        "type": "m.room.message",
        "room_id": "!room:example.com",
    }
    if room_version.msc4242_state_dags:
        event_dict["prev_state_events"] = []
    return make_event_from_dict(event_dict, room_version=room_version)


class TestMetaClass(TestCase):
    def test_is_instance(self) -> None:
        """Test that isinstance checks on EventProtocol raise
        NotImplementedError, but that isinstance checks on EventBase and
        FrozenEvent still work as normal.
        """
        # EventBase and FrozenEvent should work as normal
        self.assertFalse(isinstance(object(), EventBase))
        self.assertFalse(isinstance(object(), FrozenEvent))

        with self.assertRaises(NotImplementedError):
            isinstance(object(), EventProtocol)

        with self.assertRaises(NotImplementedError):
            isinstance(object(), MSC4242Event)


class SupportsMSC4242StateDagTestCase(TestCase):
    def test_single_event_msc4242(self) -> None:
        """A single event in an MSC4242 room is recognised."""
        ev = _make_event(RoomVersions.MSC4242v12)
        self.assertTrue(supports_msc4242_state_dag(ev))

    def test_single_event_non_msc4242(self) -> None:
        """A single event in a non-MSC4242 room is not recognised."""
        ev = _make_event(RoomVersions.V11)
        self.assertFalse(supports_msc4242_state_dag(ev))

    def test_sequence_all_msc4242(self) -> None:
        """A sequence of MSC4242 (event, context) pairs is recognised."""
        pairs = [(_make_event(RoomVersions.MSC4242v12), Mock()) for _ in range(3)]
        self.assertTrue(all_supports_msc4242_state_dag(pairs))

    def test_sequence_mixed(self) -> None:
        """A sequence containing any non-MSC4242 event is not recognised."""
        pairs = [
            (_make_event(RoomVersions.MSC4242v12), Mock()),
            (_make_event(RoomVersions.V11), Mock()),
        ]
        self.assertFalse(all_supports_msc4242_state_dag(pairs))
