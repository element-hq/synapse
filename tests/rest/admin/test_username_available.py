#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from synapse.api.errors import Codes, SynapseError
from synapse.rest.client import login
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest


class UsernameAvailableTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
    ]
    url = "/_synapse/admin/v1/username_available"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

        async def check_username(
            localpart: str,
            guest_access_token: str | None = None,
            assigned_user_id: str | None = None,
            inhibit_user_in_use_error: bool = False,
        ) -> None:
            if localpart == "allowed":
                return
            raise SynapseError(
                400,
                "User ID already taken.",
                errcode=Codes.USER_IN_USE,
            )

        handler = self.hs.get_registration_handler()
        handler.check_username = check_username  # type: ignore[method-assign]

    def test_username_available(self) -> None:
        """
        The endpoint should return a 200 response if the username does not exist
        """

        url = "%s?username=%s" % (self.url, "allowed")
        channel = self.make_request("GET", url, access_token=self.admin_user_tok)

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertTrue(channel.json_body["available"])

    def test_username_unavailable(self) -> None:
        """
        The endpoint should return a 200 response if the username does not exist
        """

        url = "%s?username=%s" % (self.url, "disallowed")
        channel = self.make_request("GET", url, access_token=self.admin_user_tok)

        self.assertEqual(400, channel.code, msg=channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_USER_IN_USE")
        self.assertEqual(channel.json_body["error"], "User ID already taken.")
