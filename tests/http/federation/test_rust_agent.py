#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Tests for the Rust federation agent, against a real TLS server.

The agent resolves names from fixed tables, and connects to the server below
whatever port resolution picks, so one listener can play every host.
"""

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import attr

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.synapse_rust.federation import RustFederationAgentForTest
from synapse.util.clock import Clock

from tests import unittest
from tests.http import create_test_cert_file, get_test_ca_cert_file, get_test_key_file

# The names the test certificate is valid for. `nocert-server` resolves to the
# test server too, but is not in the certificate.
CERT_NAMES = [b"DNS:testserv", b"DNS:target-server", b"IP:127.0.0.1"]
HOSTS = ["testserv", "target-server", "srv-target", "nocert-server"]


@attr.s(auto_attribs=True, frozen=True)
class SeenRequest:
    connection: int
    sni: str | None
    method: str
    path: str
    host: str | None
    user_agent: str | None


class _TestServer:
    """An HTTPS server on a thread, recording what it is sent."""

    def __init__(self) -> None:
        self.requests: list[SeenRequest] = []
        # Path to (status, body).
        self.routes: dict[str, tuple[int, bytes]] = {}
        self._connections = 0
        self._lock = threading.Lock()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(create_test_cert_file(CERT_NAMES), get_test_key_file())
        sni_by_socket: dict[int, str | None] = {}

        def sni_callback(sock: ssl.SSLObject, name: str | None, _: Any) -> None:
            sni_by_socket[id(sock)] = name

        context.sni_callback = sni_callback

        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                with server._lock:
                    server._connections += 1
                    self.connection_id = server._connections
                self.sni = sni_by_socket.get(id(self.request))

            def do_GET(self) -> None:
                self._respond()

            def do_PUT(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self._respond()

            def _respond(self) -> None:
                with server._lock:
                    server.requests.append(
                        SeenRequest(
                            connection=self.connection_id,
                            sni=self.sni,
                            method=self.command,
                            path=self.path,
                            host=self.headers.get("Host"),
                            user_agent=self.headers.get("User-Agent"),
                        )
                    )
                    status, body = server.routes.get(self.path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class RustFederationAgentTestCase(unittest.HomeserverTestCase):
    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        # `setdefault`, since the base class has already applied any
        # `override_config`.
        config.setdefault("federation_custom_ca_list", [get_test_ca_cert_file()])
        config.setdefault("ip_range_whitelist", ["127.0.0.1"])
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.server = _TestServer()
        self.addCleanup(self.server.stop)
        self.srv_records: dict[str, list[tuple[int, int, int, str]]] = {}

    def _agent(self) -> RustFederationAgentForTest:
        port = self.server.port
        return RustFederationAgentForTest(
            self.hs,
            self.srv_records,
            {host: ["127.0.0.1"] for host in HOSTS},
            {443: port, 8448: port, 8443: port},
        )

    def _seen(self) -> list[SeenRequest]:
        seen = list(self.server.requests)
        self.server.requests.clear()
        return seen

    def test_explicit_port(self) -> None:
        """An explicit port skips `.well-known` and SRV."""
        self.server.routes["/foo"] = (200, b"hello")

        status, _, body = self._agent().request_blocking(
            "GET", "matrix-federation://testserv:8448/foo"
        )

        self.assertEqual((status, body), (200, b"hello"))
        (request,) = self._seen()
        self.assertEqual(request.path, "/foo")
        self.assertEqual(request.host, "testserv:8448")
        self.assertEqual(request.sni, "testserv")
        self.assertEqual(request.user_agent, "Synapse-test")

    def test_no_delegation(self) -> None:
        """With no `.well-known` and no SRV record, we use the name on 8448."""
        self.server.routes["/foo"] = (200, b"")

        status, _, _ = self._agent().request_blocking(
            "GET", "matrix-federation://testserv/foo"
        )

        self.assertEqual(status, 200)
        well_known, request = self._seen()
        self.assertEqual(well_known.path, "/.well-known/matrix/server")
        self.assertEqual(well_known.host, "testserv")
        self.assertEqual(well_known.sni, "testserv")
        self.assertEqual(request.path, "/foo")
        self.assertEqual(request.host, "testserv")
        self.assertEqual(request.sni, "testserv")

    def test_well_known_delegation(self) -> None:
        """A `.well-known` rewrites the host, the Host header and the TLS name,
        and is cached."""
        self.server.routes["/.well-known/matrix/server"] = (
            200,
            json.dumps({"m.server": "target-server"}).encode(),
        )
        self.server.routes["/foo"] = (200, b"")
        agent = self._agent()

        agent.request_blocking("GET", "matrix-federation://testserv/foo")
        well_known, request = self._seen()
        self.assertEqual(well_known.host, "testserv")
        self.assertEqual(request.host, "target-server")
        self.assertEqual(request.sni, "target-server")

        # The second request needs no `.well-known` fetch, and reuses the
        # pooled connection.
        agent.request_blocking("PUT", "matrix-federation://testserv/foo", body=b"{}")
        (second,) = self._seen()
        self.assertEqual(second.method, "PUT")
        self.assertEqual(second.host, "target-server")
        self.assertEqual(second.connection, request.connection)

    def test_well_known_delegation_with_port(self) -> None:
        self.server.routes["/.well-known/matrix/server"] = (
            200,
            json.dumps({"m.server": "target-server:8443"}).encode(),
        )
        self.server.routes["/foo"] = (200, b"")

        self._agent().request_blocking("GET", "matrix-federation://testserv/foo")

        _, request = self._seen()
        self.assertEqual(request.host, "target-server:8443")
        self.assertEqual(request.sni, "target-server")

    def test_srv(self) -> None:
        """An SRV record changes where we connect, but not the Host header or the
        name the certificate must match."""
        self.srv_records["_matrix-fed._tcp.testserv"] = [(0, 0, 8443, "srv-target")]
        self.server.routes["/foo"] = (200, b"")

        self._agent().request_blocking("GET", "matrix-federation://testserv/foo")

        _, request = self._seen()
        self.assertEqual(request.host, "testserv")
        self.assertEqual(request.sni, "testserv")

    def test_srv_service_unavailable(self) -> None:
        self.srv_records["_matrix-fed._tcp.testserv"] = [(0, 0, 0, ".")]

        with self.assertRaisesRegex(RuntimeError, "^service_unavailable: "):
            self._agent().request_blocking("GET", "matrix-federation://testserv/foo")

    def test_ip_literal(self) -> None:
        """An IP literal is verified against the certificate's IP addresses, and
        sends no SNI."""
        self.server.routes["/foo"] = (200, b"")

        self._agent().request_blocking("GET", "matrix-federation://127.0.0.1:8448/foo")

        (request,) = self._seen()
        self.assertEqual(request.host, "127.0.0.1:8448")
        self.assertIsNone(request.sni)

    @unittest.override_config({"ip_range_whitelist": []})
    def test_blocked_address(self) -> None:
        """An address in the blocklist is refused, as a failed DNS lookup."""
        with self.assertRaisesRegex(RuntimeError, "^dns: "):
            self._agent().request_blocking(
                "GET", "matrix-federation://testserv:8448/foo"
            )
        with self.assertRaisesRegex(RuntimeError, "^dns: "):
            self._agent().request_blocking(
                "GET", "matrix-federation://127.0.0.1:8448/foo"
            )
        self.assertEqual(self._seen(), [])

    def test_bad_certificate(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "^tls: "):
            self._agent().request_blocking(
                "GET", "matrix-federation://nocert-server:8448/foo"
            )

    @unittest.override_config({"federation_verify_certificates": False})
    def test_verification_off(self) -> None:
        self.server.routes["/foo"] = (200, b"")
        status, _, _ = self._agent().request_blocking(
            "GET", "matrix-federation://nocert-server:8448/foo"
        )
        self.assertEqual(status, 200)

        # SNI is still sent.
        (request,) = self._seen()
        self.assertEqual(request.sni, "nocert-server")

    @unittest.override_config(
        {"federation_certificate_verification_whitelist": ["nocert-*"]}
    )
    def test_verification_whitelist(self) -> None:
        self.server.routes["/foo"] = (200, b"")
        status, _, _ = self._agent().request_blocking(
            "GET", "matrix-federation://nocert-server:8448/foo"
        )
        self.assertEqual(status, 200)

    def test_status_returned_as_is(self) -> None:
        """The agent returns error responses rather than raising."""
        self.server.routes["/missing"] = (404, b'{"errcode": "M_NOT_FOUND"}')
        status, _, body = self._agent().request_blocking(
            "GET", "matrix-federation://testserv:8448/missing"
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"errcode": "M_NOT_FOUND"})
