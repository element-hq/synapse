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
from synapse.api.errors import Codes, SynapseError
from synapse.api.room_versions import (
    KNOWN_ROOM_VERSIONS,
    EventFormatVersions,
    RoomVersions,
)
from synapse.events import make_event_from_dict
from synapse.events.validator import EventValidator

from tests.unittest import HomeserverTestCase


class EventValidatorTestCase(HomeserverTestCase):
    def test_validate_new_with_mentions_succeed(self) -> None:
        """
        Test that `EventValidator.validate_new` accepts an event with valid `m.mentions`
        content.
        """
        event = make_event_from_dict(
            {
                "room_id": "!room:test",
                "type": "m.room.message",
                "sender": "@alice:example.com",
                "content": {
                    "msgtype": "m.text",
                    "body": "@alice:example.com hello",
                    "m.mentions": {"user_ids": ["@alice:example.com"]},
                },
                "auth_events": [],
                "prev_events": [],
                "hashes": {"sha256": "aGVsbG8="},
                "signatures": {},
                "depth": 1,
                "origin_server_ts": 1000,
            },
            room_version=RoomVersions.V9,
        )

        EventValidator().validate_new(event, self.hs.config)

    def test_validate_new_rejects_big_depth_for_strict_canonicaljson_rooms(
        self,
    ) -> None:
        """
        Test that `EventValidator.validate_new` rejects events with integers outside the
        canonical JSON range, in room versions which enforce it (v6+).
        """
        for room_version in KNOWN_ROOM_VERSIONS.values():
            with self.subTest(room_version=room_version.identifier):
                event_dict = {
                    "room_id": "!room:test",
                    "type": "m.room.message",
                    "sender": "@alice:example.com",
                    "content": {
                        "msgtype": "m.text",
                        "body": "hello",
                    },
                    "auth_events": [],
                    "prev_events": [],
                    "hashes": {"sha256": "aGVsbG8="},
                    "signatures": {},
                    "depth": 2**53,
                    "origin_server_ts": 1000,
                }

                if room_version.event_format == EventFormatVersions.ROOM_V1_V2:
                    event_dict["event_id"] = "$event:test"

                event = make_event_from_dict(
                    event_dict,
                    room_version=room_version,
                )

                # Check if this room version enforces strict canonical json.
                if room_version.strict_canonicaljson:
                    with self.assertRaises(SynapseError) as cm:
                        EventValidator().validate_new(event, self.hs.config)

                    self.assertEqual(cm.exception.errcode, Codes.BAD_JSON)
                    self.assertEqual(cm.exception.msg, "JSON integer out of range")
                else:
                    EventValidator().validate_new(event, self.hs.config)
