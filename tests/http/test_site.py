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

from unittest.mock import Mock

from netaddr import IPNetwork
from parameterized import parameterized

from twisted.internet.address import IPv4Address, IPv6Address, UNIXAddress
from twisted.internet.testing import MemoryReactor, StringTransport
from twisted.web.http_headers import Headers

from synapse.app._base import max_request_body_size
from synapse.app.homeserver import SynapseHomeServer
from synapse.http.site import XForwardedForRequest
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, TestCase


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

    @parameterized.expand(
        [
            (b"multipart/form-data",),
            # Also check with a boundary
            (b"multipart/form-data; boundary=abc123",),
            # Headers are case-insensitive, so test that too.
            (b"Multipart/Form-Data",),
        ]
    )
    def test_content_type_multipart(self, content_type: bytes) -> None:
        """HTTP POST requests with `content-type: multipart/form-data` should be rejected"""
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

        # now send request with content-type header
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Type: " + content_type + b"\r\n"
            b"\r\n"
            b"0\r\n"
            b"\r\n"
        )

        while not transport.disconnecting:
            self.reactor.advance(1)

        # we should get a 415
        self.assertRegex(transport.value().decode(), r"^HTTP/1\.1 415 ")

    def test_content_length_too_large(self) -> None:
        """HTTP requests with Content-Length exceeding max size should be rejected with 413"""
        self.hs.start_listening()

        # find the HTTP server which is configured to listen on port 0
        (port, factory, _backlog, interface) = self.reactor.tcpServers[0]
        self.assertEqual(interface, "::")
        self.assertEqual(port, 0)

        # complete the connection and wire it up to a fake transport
        client_address = IPv6Address("TCP", "::1", 2345)
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        # Send a request with Content-Length header that exceeds the limit.
        # Default max is 50MB (from media max_upload_size), so send something larger.
        oversized_length = 1 + max_request_body_size(self.hs.config)
        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Content-Length: " + str(oversized_length).encode() + b"\r\n"
            b"\r\n"
            b"" + b"x" * oversized_length + b"\r\n"
            b"\r\n"
        )

        # Advance the reactor to process the request
        while not transport.disconnecting:
            self.reactor.advance(1)

        # We should get a 413 Content Too Large
        response = transport.value().decode()
        self.assertRegex(response, r"^HTTP/1\.1 413 ")
        self.assertSubstring("M_TOO_LARGE", response)

    def test_too_many_content_length_headers(self) -> None:
        """HTTP requests with multiple Content-Length headers should be rejected with 400"""
        self.hs.start_listening()

        # find the HTTP server which is configured to listen on port 0
        (port, factory, _backlog, interface) = self.reactor.tcpServers[0]
        self.assertEqual(interface, "::")
        self.assertEqual(port, 0)

        # complete the connection and wire it up to a fake transport
        client_address = IPv6Address("TCP", "::1", 2345)
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Content-Length: " + str(5).encode() + b"\r\n"
            b"Content-Length: " + str(5).encode() + b"\r\n"
            b"\r\n"
            b"" + b"xxxxx" + b"\r\n"
            b"\r\n"
        )

        # Advance the reactor to process the request
        while not transport.disconnecting:
            self.reactor.advance(1)

        # We should get a 400
        response = transport.value().decode()
        self.assertRegex(response, r"^HTTP/1\.1 400 ")

    def test_invalid_content_length_headers(self) -> None:
        """HTTP requests with invalid Content-Length header should be rejected with 400"""
        self.hs.start_listening()

        # find the HTTP server which is configured to listen on port 0
        (port, factory, _backlog, interface) = self.reactor.tcpServers[0]
        self.assertEqual(interface, "::")
        self.assertEqual(port, 0)

        # complete the connection and wire it up to a fake transport
        client_address = IPv6Address("TCP", "::1", 2345)
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"POST / HTTP/1.1\r\n"
            b"Connection: close\r\n"
            b"Content-Length: eight\r\n"
            b"\r\n"
            b"" + b"xxxxx" + b"\r\n"
            b"\r\n"
        )

        # Advance the reactor to process the request
        while not transport.disconnecting:
            self.reactor.advance(1)

        # We should get a 400
        response = transport.value().decode()
        self.assertRegex(response, r"^HTTP/1\.1 400 ")


class XForwardedForRequestTestCase(TestCase):
    """Tests for resolving the client IP from the X-Forwarded-For header."""

    def _make_request(
        self,
        peer: object,
        forwarded_for: list[bytes],
        trusted_proxies: tuple[IPNetwork, ...] = (),
    ) -> XForwardedForRequest:
        site = Mock()
        site.reactor = Mock()
        site.trusted_proxies = trusted_proxies

        channel = Mock()
        channel.getPeer.return_value = peer

        request = XForwardedForRequest(channel, site, our_server_name="test.server")
        request.requestHeaders = Headers({b"X-Forwarded-For": forwarded_for})
        request._process_forwarded_headers()
        return request

    def test_no_trusted_proxies_keeps_first_entry(self) -> None:
        """Without trusted_proxies, the first entry is used as before."""
        request = self._make_request(
            peer=IPv4Address("TCP", "192.168.0.1", 45000),
            forwarded_for=[b"9.9.9.9, 1.2.3.4"],
        )
        self.assertEqual(request.getClientIP(), "9.9.9.9")

    def test_trusted_proxy_single_hop(self) -> None:
        """The address reported by a trusted proxy is used."""
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"1.2.3.4"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "1.2.3.4")

    def test_spoofed_entries_are_ignored(self) -> None:
        """Entries injected by the client cannot override the proxy's report.

        The reverse proxy appends the real client IP to the (spoofed) chain it
        received, giving `9.9.9.9, 1.2.3.4`. Only `1.2.3.4` is one hop away
        from the trusted proxy, so `9.9.9.9` must be ignored.
        """
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"9.9.9.9, 1.2.3.4"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "1.2.3.4")

    def test_untrusted_peer_is_not_spoofable(self) -> None:
        """The header is ignored entirely if the peer is not a trusted proxy."""
        request = self._make_request(
            peer=IPv4Address("TCP", "192.168.0.1", 45000),
            forwarded_for=[b"1.2.3.4"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "192.168.0.1")

    def test_multiple_trusted_proxies(self) -> None:
        """The chain is followed through multiple trusted proxies."""
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"10.1.0.9, 10.0.0.5, 1.2.3.4"],
            trusted_proxies=(
                IPNetwork("10.0.0.0/8"),
                IPNetwork("10.1.0.0/16"),
            ),
        )
        self.assertEqual(request.getClientIP(), "1.2.3.4")

    def test_all_trusted_chain_uses_leftmost_entry(self) -> None:
        """If the whole chain is trusted, the leftmost entry is the client."""
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"10.1.0.9, 10.0.0.5"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "10.1.0.9")

    def test_unix_socket_peer_is_trusted(self) -> None:
        """A unix socket peer is local, so the chain is honoured."""
        request = self._make_request(
            peer=UNIXAddress(b"/run/synapse.sock"),
            forwarded_for=[b"10.0.0.5, 1.2.3.4"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "1.2.3.4")

    def test_invalid_entry_stops_the_chain(self) -> None:
        """The chain is not followed past an entry which is not an IP address."""
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"1.2.3.4, not-an-ip, 10.0.0.5"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "10.0.0.5")

    def test_multiple_headers_are_concatenated(self) -> None:
        """Multiple X-Forwarded-For headers behave as one comma-separated list."""
        request = self._make_request(
            peer=IPv4Address("TCP", "10.0.0.9", 45000),
            forwarded_for=[b"9.9.9.9", b"1.2.3.4"],
            trusted_proxies=(IPNetwork("10.0.0.0/8"),),
        )
        self.assertEqual(request.getClientIP(), "1.2.3.4")
