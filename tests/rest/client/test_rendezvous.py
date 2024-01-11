#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from twisted.test.proto_helpers import MemoryReactor

from synapse.rest.client import rendezvous
from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest
from tests.unittest import override_config

endpoint = "/_matrix/client/unstable/org.matrix.msc3886/rendezvous"


class RendezvousServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        rendezvous.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()
        return self.hs

    def test_disabled(self) -> None:
        channel = self.make_request("POST", endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)

    @override_config({"experimental_features": {"msc3886_endpoint": "/asd"}})
    def test_redirect(self) -> None:
        channel = self.make_request("POST", endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 307)
        self.assertEqual(channel.headers.getRawHeaders("Location"), ["/asd"])
