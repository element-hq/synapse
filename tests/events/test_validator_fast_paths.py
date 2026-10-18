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

"""The Rust fast paths of event validation must never call valid something the
Python checks reject."""

import random
from typing import Any

from canonicaljson import encode_canonical_json

from synapse.api.errors import SynapseError
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.events import EventBase, make_event_from_dict, make_event_from_json
from synapse.events.utils import validate_canonicaljson
from synapse.events.validator import (
    _MENTIONS_SCHEMA_VALIDATOR,
    _POWER_LEVELS_SCHEMA_VALIDATOR,
    _validate_against_schema,
)
from synapse.synapse_rust.events import (
    mentions_content_is_valid,
    pdu_json_is_canonical,
    pdu_json_size,
    power_levels_content_is_valid,
)

from tests import unittest

# Values the generators pick from: valid integers at and beyond the canonical
# range, and everything else JSON can hold.
AWKWARD_VALUES: list[Any] = [
    0,
    -1,
    100,
    2**53 - 1,
    -(2**53) + 1,
    2**53,
    -(2**53),
    2**70,
    1.0,
    50.5,
    -0.0,
    1e100,
    True,
    False,
    None,
    "50",
    "",
    [],
    [1],
    {},
    {"a": 1},
]

PL_FIELDS = [
    "ban",
    "events_default",
    "invite",
    "kick",
    "redact",
    "state_default",
    "users_default",
    "events",
    "notifications",
    "users",
    "historical",
]


def _random_value(rng: random.Random) -> Any:
    value = rng.choice(AWKWARD_VALUES)
    if value == {"a": 1} and rng.random() < 0.5:
        # An object of mostly integers, for the object-of-ints fields.
        return {f"k{i}": rng.choice(AWKWARD_VALUES[:8]) for i in range(3)}
    return value


def _schema_accepts(validator: Any, instance: Any) -> bool:
    try:
        _validate_against_schema(validator, instance)
    except SynapseError:
        return False
    return True


def _make_event(room_version: RoomVersion, content: dict) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": "!room:test",
            "type": "m.room.message",
            "sender": "@alice:test",
            "content": content,
            "auth_events": [],
            "prev_events": [],
            "depth": 1,
            "hashes": {"sha256": "abc"},
            "signatures": {},
            "origin_server_ts": 1,
            "unsigned": {"age_ts": 1},
            **(
                {"event_id": "$e:test"} if room_version.identifier in ("1", "2") else {}
            ),
        },
        room_version,
    )


class ValidatorFastPathTestCase(unittest.TestCase):
    def test_power_levels(self) -> None:
        rng = random.Random(1)
        accepted = 0
        for _ in range(3000):
            content = {
                field: _random_value(rng)
                for field in rng.sample(PL_FIELDS, rng.randint(0, 4))
            }
            if power_levels_content_is_valid(content):
                accepted += 1
                self.assertTrue(
                    _schema_accepts(_POWER_LEVELS_SCHEMA_VALIDATOR, content),
                    content,
                )
        # The generator does produce valid content.
        self.assertGreater(accepted, 50)

        # Default power levels go the fast way.
        self.assertTrue(
            power_levels_content_is_valid(
                {
                    "users": {"@alice:test": 100},
                    "users_default": 0,
                    "events": {"m.room.name": 50},
                    "events_default": 0,
                    "state_default": 50,
                    "ban": 50,
                    "kick": 50,
                    "redact": 50,
                    "invite": 0,
                    "historical": 100,
                }
            )
        )

    def test_mentions(self) -> None:
        rng = random.Random(2)
        accepted = 0
        for _ in range(3000):
            mentions: Any
            if rng.random() < 0.8:
                mentions = {}
                if rng.random() < 0.6:
                    mentions["user_ids"] = rng.choice(
                        [
                            [],
                            ["@a:test"],
                            ["@a:test", 1],
                            "@a:test",
                            None,
                            [None],
                        ]
                    )
                if rng.random() < 0.6:
                    mentions["room"] = _random_value(rng)
            else:
                mentions = _random_value(rng)
            content = {"body": "hi", "m.mentions": mentions}
            if mentions_content_is_valid(content):
                accepted += 1
                self.assertTrue(
                    _schema_accepts(_MENTIONS_SCHEMA_VALIDATOR, mentions), mentions
                )
        self.assertGreater(accepted, 50)

        self.assertTrue(mentions_content_is_valid({"body": "no mentions"}))

    def test_canonical_json_and_size(self) -> None:
        rng = random.Random(3)
        accepted = 0
        sized = 0
        for room_version in (RoomVersions.V1, RoomVersions.V5, RoomVersions.V10):
            for _ in range(500):
                content = {
                    f"k{i}": _random_value(rng) for i in range(rng.randint(0, 3))
                }
                content["nested"] = [_random_value(rng)]
                content["s"] = 'é \x01"\\ 🐈'
                event = _make_event(room_version, content)

                size = pdu_json_size(event)
                if size is not None:
                    sized += 1
                    self.assertEqual(
                        size, len(encode_canonical_json(event.get_pdu_json()))
                    )

                if pdu_json_is_canonical(event):
                    accepted += 1
                    validate_canonicaljson(event.get_pdu_json())
        self.assertGreater(accepted, 50)
        self.assertGreater(sized, 50)

    def test_size_of_numbers_python_prints_differently(self) -> None:
        """Numbers whose JSON text is not how Python prints them leave the size
        to Python, or get the size Python would give."""
        for number in ("1e3", "1.50", "1e-7", "-0", "1e400"):
            event = make_event_from_json(
                '{"room_id": "!room:test", "type": "m.room.message",'
                ' "sender": "@alice:test", "event_id": "$e:test",'
                f' "content": {{"x": {number}}}, "auth_events": [],'
                ' "prev_events": [], "depth": 1, "hashes": {"sha256": "abc"},'
                ' "signatures": {}, "origin_server_ts": 1}',
                RoomVersions.V1,
            )
            size = pdu_json_size(event)
            if number == "1e3":
                self.assertIsNone(size)
            if size is not None:
                self.assertEqual(
                    size, len(encode_canonical_json(event.get_pdu_json())), number
                )
