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

from parameterized import parameterized

from twisted.internet.address import IPv6Address
from twisted.internet.testing import MemoryReactor, StringTransport

from synapse.app._base import max_request_body_size
from synapse.app.homeserver import SynapseHomeServer
from synapse.server import HomeServer
from synapse.util.clock import Clock

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

    def _send_raw_request(self, target: bytes) -> str:
        """Send a bare GET with the given request-target and return the raw response."""
        self.hs.start_listening()

        (port, factory, _backlog, interface) = self.reactor.tcpServers[0]
        self.assertEqual(interface, "::")
        self.assertEqual(port, 0)

        client_address = IPv6Address("TCP", "::1", 2345)
        protocol = factory.buildProtocol(client_address)
        transport = StringTransport()
        protocol.makeConnection(transport)

        protocol.dataReceived(
            b"GET " + target + b" HTTP/1.1\r\nConnection: close\r\n\r\n"
        )

        while not transport.disconnecting:
            self.reactor.advance(1)

        return transport.value().decode()

    @parameterized.expand(
        [
            (b"/_matrix/client/versions/..",),
            (b"/_matrix/client/../../etc/passwd",),
            (b"/_matrix/client/versions/.",),
            (b"/..",),
            (b"/_matrix/client/versions/%2e%2e",),
            (b"/_matrix/client/versions/%2E%2E",),
            # An encoded separator hides a dot segment from a naive split.
            (b"/_matrix/client/versions/a%2f..%2fb",),
            # Dot segments anywhere in the path, not just at the end.
            (b"/_matrix/../client/versions",),
            # The query string must not save an otherwise-bad path.
            (b"/_matrix/client/versions/..?foo=bar",),
        ]
    )
    def test_dot_segments_rejected(self, target: bytes) -> None:
        """Request paths containing "." or ".." segments should be rejected with 400"""
        response = self._send_raw_request(target)

        self.assertRegex(response, r"^HTTP/1\.1 400 ")
        self.assertSubstring("M_INVALID_PARAM", response)

    @parameterized.expand(
        [
            # Dots that are not a whole segment are fine.
            (b"/_matrix/client/versions",),
            (b"/_matrix/client/v3/rooms/%21a%3Ab/state/m.room.name/..suffix",),
            (b"/_matrix/client/v3/rooms/%21a%3Ab/state/m.room.name/a..b",),
            (b"/_matrix/client/v3/rooms/%21a%3Ab/state/m.room.name/...",),
            # Only one round of decoding, so this is a literal "%2e%2e" segment.
            (b"/_matrix/client/v3/rooms/%21a%3Ab/state/m.room.name/%252e%252e",),
            # Dot segments in the query string are none of our business.
            (b"/_matrix/client/versions?redirectUrl=../../foo",),
        ]
    )
    def test_paths_without_dot_segments_allowed(self, target: bytes) -> None:
        """Paths that merely contain dots should be routed as normal"""
        response = self._send_raw_request(target)

        self.assertNotRegex(response, r"^HTTP/1\.1 400 ")
        self.assertNotIn("M_INVALID_PARAM", response)

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
