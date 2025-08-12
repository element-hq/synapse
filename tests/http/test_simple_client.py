#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from unittest.mock import Mock

from netaddr import IPSet

from twisted.internet import defer
from twisted.internet.error import DNSLookupError
from twisted.test.proto_helpers import MemoryReactor

from synapse.http import RequestTimedOutError
from synapse.http.client import SimpleHttpClient
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class SimpleHttpClientTests(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: "HomeServer") -> None:
        # Add a DNS entry for a test server
        self.reactor.lookups["testserv"] = "1.2.3.4"

        self.cl = hs.get_simple_http_client()

    def test_dns_error(self) -> None:
        """
        If the DNS lookup returns an error, it will bubble up.
        """
        d = defer.ensureDeferred(self.cl.get_json("http://testserv2:8008/foo/bar"))
        self.pump()

        f = self.failureResultOf(d)
        self.assertIsInstance(f.value, DNSLookupError)

    def test_client_connection_refused(self) -> None:
        d = defer.ensureDeferred(self.cl.get_json("http://testserv:8008/foo/bar"))

        self.pump()

        # Nothing happened yet
        self.assertNoResult(d)

        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8008)
        e = Exception("go away")
        factory.clientConnectionFailed(None, e)
        self.pump(0.5)

        f = self.failureResultOf(d)

        self.assertIs(f.value, e)

    def test_client_never_connect(self) -> None:
        """
        If the HTTP request is not connected and is timed out, it'll give a
        ConnectingCancelledError or TimeoutError.
        """
        d = defer.ensureDeferred(self.cl.get_json("http://testserv:8008/foo/bar"))

        self.pump()

        # Nothing happened yet
        self.assertNoResult(d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0][0], "1.2.3.4")
        self.assertEqual(clients[0][1], 8008)

        # Deferred is still without a result
        self.assertNoResult(d)

        # Push by enough to time it out
        self.reactor.advance(120)
        f = self.failureResultOf(d)

        self.assertIsInstance(f.value, RequestTimedOutError)

    def test_client_connect_no_response(self) -> None:
        """
        If the HTTP request is connected, but gets no response before being
        timed out, it'll give a ResponseNeverReceived.
        """
        d = defer.ensureDeferred(self.cl.get_json("http://testserv:8008/foo/bar"))

        self.pump()

        # Nothing happened yet
        self.assertNoResult(d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0][0], "1.2.3.4")
        self.assertEqual(clients[0][1], 8008)

        conn = Mock()
        client = clients[0][2].buildProtocol(None)
        client.makeConnection(conn)

        # Deferred is still without a result
        self.assertNoResult(d)

        # Push by enough to time it out
        self.reactor.advance(120)
        f = self.failureResultOf(d)

        self.assertIsInstance(f.value, RequestTimedOutError)

    def test_client_ip_range_blocklist(self) -> None:
        """Ensure that Synapse does not try to connect to blocked IPs"""

        # Add some DNS entries we'll block
        self.reactor.lookups["internal"] = "127.0.0.1"
        self.reactor.lookups["internalv6"] = "fe80:0:0:0:0:8a2e:370:7337"
        ip_blocklist = IPSet(["127.0.0.0/8", "fe80::/64"])

        cl = SimpleHttpClient(self.hs, ip_blocklist=ip_blocklist)

        # Try making a GET request to a blocked IPv4 address
        # ------------------------------------------------------
        # Make the request
        d = defer.ensureDeferred(cl.get_json("http://internal:8008/foo/bar"))
        self.pump(1)

        # Check that it was unable to resolve the address
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 0)

        self.failureResultOf(d, DNSLookupError)

        # Try making a POST request to a blocked IPv6 address
        # -------------------------------------------------------
        # Make the request
        d = defer.ensureDeferred(
            cl.post_json_get_json("http://internalv6:8008/foo/bar", {})
        )

        # Move the reactor forwards
        self.pump(1)

        # Check that it was unable to resolve the address
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 0)

        # Check that it was due to a blocked DNS lookup
        self.failureResultOf(d, DNSLookupError)

        # Try making a GET request to a non-blocked IPv4 address
        # ----------------------------------------------------------
        # Make the request
        d = defer.ensureDeferred(cl.get_json("http://testserv:8008/foo/bar"))

        # Nothing has happened yet
        self.assertNoResult(d)

        # Move the reactor forwards
        self.pump(1)

        # Check that it was able to resolve the address
        clients = self.reactor.tcpClients
        self.assertNotEqual(len(clients), 0)

        # Connection will still fail as this IP address does not resolve to anything
        self.failureResultOf(d, RequestTimedOutError)
