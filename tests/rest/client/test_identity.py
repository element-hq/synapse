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

import synapse.rest.admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest


class IdentityTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["enable_3pid_lookup"] = False
        self.hs = self.setup_test_homeserver(config=config)

        return self.hs

    def test_3pid_lookup_disabled(self) -> None:
        self.hs.config.registration.enable_3pid_lookup = False

        self.register_user("kermit", "monkey")
        tok = self.login("kermit", "monkey")

        channel = self.make_request(b"POST", "/createRoom", b"{}", access_token=tok)
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        room_id = channel.json_body["room_id"]

        request_data = {
            "id_server": "testis",
            "medium": "email",
            "address": "test@example.com",
            "id_access_token": tok,
        }
        request_url = ("/rooms/%s/invite" % (room_id)).encode("ascii")
        channel = self.make_request(
            b"POST", request_url, request_data, access_token=tok
        )
        self.assertEqual(channel.code, HTTPStatus.FORBIDDEN, channel.result)
