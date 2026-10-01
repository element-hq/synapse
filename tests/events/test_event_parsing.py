#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.


from typing import Any

from parameterized import parameterized_class

from synapse.api.room_versions import RoomVersions
from synapse.events import make_event_from_dict
from synapse.synapse_rust.events import redact_event
from synapse.types import JsonDict

from tests.test_utils.event_injection import EventTypes
from tests.unittest import TestCase


def create_minimal_event_dict(**fields: Any) -> JsonDict:
    """Create a minimal event dict that will parse correctly."""
    return {
        "type": EventTypes.Message,
        "content": {},
        "room_id": "!room:id",
        "sender": "@user:id",
        "event_id": "$event:id",
        "origin_server_ts": 0,
        "auth_events": [],
        "prev_events": [],
        "hashes": {},
        "signatures": {},
        "depth": 0,
        **fields,
    }


@parameterized_class(
    [
        {"test_int": 2**7 - 1},
        {"test_int": 2**15 - 1},
        {"test_int": 2**31 - 1},
        {"test_int": 2**63 - 1},
        {"test_int": 2**127 - 1},
        {"test_int": 2**200},
    ]
)
class LargeIntTestCase(TestCase):
    """Test that we can handle various sized integers in events.

    This is a regression test where we had issues handling integers that fit in
    a Rust `i128`.
    """

    test_int: int
    """The integer to test with. This will be set by the parameterized_class decorator."""

    def test_very_large_int_in_event_content(self) -> None:
        """Test that we can handle integers in the event content, which is a
        JsonObject and thus can contain arbitrary JSON."""

        event_dict = create_minimal_event_dict(content={"some_field": self.test_int})

        event = make_event_from_dict(event_dict, RoomVersions.V1)

        self.assertEqual(event.content["some_field"], self.test_int)

    def test_large_int_in_unsigned(self) -> None:
        """Test that we can handle integers in the unsigned data, which is an
        Unsigned and thus can contain arbitrary JSON."""

        event_dict = create_minimal_event_dict(
            unsigned={"prev_content": {"some_field": self.test_int}}
        )

        event = make_event_from_dict(event_dict, RoomVersions.V1)

        self.assertEqual(event.unsigned["prev_content"]["some_field"], self.test_int)

    def test_large_int_redacted(self) -> None:
        """Test that redact events that have an unsigned field with a large
        integer in a protected field"""

        event_dict = create_minimal_event_dict(
            type=EventTypes.PowerLevels,
            state_key="",
            content={"users": {"@user:id": self.test_int}},
        )

        event = make_event_from_dict(event_dict, RoomVersions.V1)

        redacted_event = redact_event(event)

        self.assertEqual(redacted_event.content["users"]["@user:id"], self.test_int)
