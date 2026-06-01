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
from synapse.api.room_versions import RoomVersions
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
