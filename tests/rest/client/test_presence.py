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
from unittest.mock import AsyncMock, Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.handlers.presence import PresenceHandler
from synapse.rest.client import presence
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest


class PresenceTestCase(unittest.HomeserverTestCase):
    """Tests presence REST API."""

    user_id = "@sid:red"

    user = UserID.from_string(user_id)
    servlets = [presence.register_servlets]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.presence_handler = Mock(spec=PresenceHandler)
        self.presence_handler.set_state = AsyncMock(return_value=None)

        hs = self.setup_test_homeserver(
            "red",
            federation_client=Mock(),
            presence_handler=self.presence_handler,
        )

        return hs

    def test_put_presence(self) -> None:
        """
        PUT to the status endpoint with use_presence enabled will call
        set_state on the presence handler.
        """
        self.hs.config.server.presence_enabled = True

        body = {"presence": "here", "status_msg": "beep boop"}
        channel = self.make_request(
            "PUT", "/presence/%s/status" % (self.user_id,), body
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(self.presence_handler.set_state.call_count, 1)

    @unittest.override_config({"use_presence": False})
    def test_put_presence_disabled(self) -> None:
        """
        PUT to the status endpoint with presence disabled will NOT call
        set_state on the presence handler.
        """

        body = {"presence": "here", "status_msg": "beep boop"}
        channel = self.make_request(
            "PUT", "/presence/%s/status" % (self.user_id,), body
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(self.presence_handler.set_state.call_count, 0)

    @unittest.override_config({"presence": {"enabled": "untracked"}})
    def test_put_presence_untracked(self) -> None:
        """
        PUT to the status endpoint with presence untracked will NOT call
        set_state on the presence handler.
        """

        body = {"presence": "here", "status_msg": "beep boop"}
        channel = self.make_request(
            "PUT", "/presence/%s/status" % (self.user_id,), body
        )

        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(self.presence_handler.set_state.call_count, 0)
