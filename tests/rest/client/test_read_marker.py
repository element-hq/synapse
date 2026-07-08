#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 Beeper
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
from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes
from synapse.rest import admin
from synapse.rest.client import login, read_marker, register, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest

ONE_HOUR_MS = 3600000
ONE_DAY_MS = ONE_HOUR_MS * 24


class ReadMarkerTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        register.register_servlets,
        read_marker.register_servlets,
        room.register_servlets,
        synapse.rest.admin.register_servlets,
        admin.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        # merge this default retention config with anything that was specified in
        # @override_config
        retention_config = {
            "enabled": True,
            "allowed_lifetime_min": ONE_DAY_MS,
            "allowed_lifetime_max": ONE_DAY_MS * 3,
        }
        retention_config.update(config.get("retention", {}))
        config["retention"] = retention_config

        self.hs = self.setup_test_homeserver(config=config)

        return self.hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.owner = self.register_user("owner", "pass")
        self.owner_tok = self.login("owner", "pass")
        self.store = self.hs.get_datastores().main
        self.clock = self.hs.get_clock()

    def test_send_read_marker(self) -> None:
        room_id = self.helper.create_room_as(self.owner, tok=self.owner_tok)

        def send_message() -> str:
            res = self.helper.send(room_id=room_id, body="1", tok=self.owner_tok)
            return res["event_id"]

        # Test setting the read marker on the room
        event_id_1 = send_message()

        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/read_markers",
            content={
                "m.fully_read": event_id_1,
            },
            access_token=self.owner_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        # Test moving the read marker to a newer event
        event_id_2 = send_message()
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/read_markers",
            content={
                "m.fully_read": event_id_2,
            },
            access_token=self.owner_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

    def test_send_read_marker_missing_previous_event(self) -> None:
        """
        Test moving a read marker from an event that previously existed but was
        later removed due to retention rules.
        """

        room_id = self.helper.create_room_as(self.owner, tok=self.owner_tok)

        # Set retention rule on the room so we remove old events to test this case
        self.helper.send_state(
            room_id=room_id,
            event_type=EventTypes.Retention,
            body={"max_lifetime": ONE_DAY_MS},
            tok=self.owner_tok,
        )

        def send_message() -> str:
            res = self.helper.send(room_id=room_id, body="1", tok=self.owner_tok)
            return res["event_id"]

        # Test setting the read marker on the room
        event_id_1 = send_message()

        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/read_markers",
            content={
                "m.fully_read": event_id_1,
            },
            access_token=self.owner_tok,
        )

        # Send a second message (retention will not remove the latest event ever)
        send_message()
        # And then advance so retention rules remove the first event (where the marker is)
        self.reactor.advance(ONE_DAY_MS * 2 / 1000)

        event = self.get_success(self.store.get_event(event_id_1, allow_none=True))
        assert event is None

        # Test moving the read marker to a newer event
        event_id_2 = send_message()
        channel = self.make_request(
            "POST",
            f"/rooms/{room_id}/read_markers",
            content={
                "m.fully_read": event_id_2,
            },
            access_token=self.owner_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
