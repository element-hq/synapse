#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from http import HTTPStatus

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes
from synapse.rest import admin
from synapse.rest.client import room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest


class EphemeralMessageTestCase(unittest.HomeserverTestCase):
    user_id = "@user:test"

    servlets = [
        admin.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        config["enable_ephemeral_messages"] = True

        self.hs = self.setup_test_homeserver(config=config)
        return self.hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.room_id = self.helper.create_room_as(self.user_id)

    def test_message_expiry_no_delay(self) -> None:
        """Tests that sending a message sent with a m.self_destruct_after field set to the
        past results in that event being deleted right away.
        """
        # Send a message in the room that has expired. From here, the reactor clock is
        # at 200ms, so 0 is in the past, and even if that wasn't the case and the clock
        # is at 0ms the code path is the same if the event's expiry timestamp is the
        # current timestamp.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "hello",
                EventContentFields.SELF_DESTRUCT_AFTER: 0,
            },
        )
        event_id = res["event_id"]

        # Check that we can't retrieve the content of the event.
        event_content = self.get_event(self.room_id, event_id)["content"]
        self.assertFalse(bool(event_content), event_content)

    def test_message_expiry_delay(self) -> None:
        """Tests that sending a message with a m.self_destruct_after field set to the
        future results in that event not being deleted right away, but advancing the
        clock to after that expiry timestamp causes the event to be deleted.
        """
        # Send a message in the room that'll expire in 1s.
        res = self.helper.send_event(
            room_id=self.room_id,
            type=EventTypes.Message,
            content={
                "msgtype": "m.text",
                "body": "hello",
                EventContentFields.SELF_DESTRUCT_AFTER: self.clock.time_msec() + 1000,
            },
        )
        event_id = res["event_id"]

        # Check that we can retrieve the content of the event before it has expired.
        event_content = self.get_event(self.room_id, event_id)["content"]
        self.assertTrue(bool(event_content), event_content)

        # Advance the clock to after the deletion.
        self.reactor.advance(1)

        # Check that we can't retrieve the content of the event anymore.
        event_content = self.get_event(self.room_id, event_id)["content"]
        self.assertFalse(bool(event_content), event_content)

    def get_event(
        self, room_id: str, event_id: str, expected_code: int = HTTPStatus.OK
    ) -> JsonDict:
        url = "/_matrix/client/r0/rooms/%s/event/%s" % (room_id, event_id)

        channel = self.make_request("GET", url)

        self.assertEqual(channel.code, expected_code, channel.result)

        return channel.json_body
