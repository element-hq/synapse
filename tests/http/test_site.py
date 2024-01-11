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

from twisted.internet.address import IPv6Address
from twisted.test.proto_helpers import MemoryReactor, StringTransport

from synapse.app.homeserver import SynapseHomeServer
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class SynapseRequestTestCase(HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver(homeserver_to_use=SynapseHomeServer)

    def test_large_request(self) -> None:
        """overlarge HTTP requests should be rejected"""
        self.hs.start_listening()

        # find the HTTP server which is configured to listen on port 0
        (port, factory, _backlog, interface) = self.reactor.tcpServers[0]
        self.assertEqual(interface, "::")
        self.assertEqual(port, 0)

        # as a control case, first send a regular request.

        # complete the connection and wire it up to a fake transport
        client_address = IPv6Address("TCP", "::1", 2345)
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n"
            b"\r\n"
        )

        while not transport.disconnecting:
            self.reactor.advance(1)

        # we should get a 404
        self.assertRegex(transport.value().decode(), r"^HTTP/1\.1 404 ")

        # now send an oversized request
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
        )

        # we deliberately send all the data in one big chunk, to ensure that
        # twisted isn't buffering the data in the chunked transfer decoder.
        # we start with the chunk size, in hex. (We won't actually send this much)
        protocol.dataReceived(b"10000000\r\n")
        sent = 0
        while not transport.disconnected:
            self.assertLess(sent, 0x10000000, "connection did not drop")
            protocol.dataReceived(b"\0" * 1024)
            sent += 1024

        # default max upload size is 50M, so it should drop on the next buffer after
        # that.
        self.assertEqual(sent, 50 * 1024 * 1024 + 1024)
