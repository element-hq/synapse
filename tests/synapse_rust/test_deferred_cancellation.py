#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import LoggingContext, current_context, run_in_background
from synapse.server import HomeServer
from synapse.synapse_rust import reset_logging_config
from synapse.synapse_rust.http_client import HttpClient
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def do_GET(self) -> None:
        self.server.requests += 1
        if self.path == "/stall":
            # The request has no body, so this returns only once the client
            # closes the connection.
            self.connection.settimeout(10)
            if self.connection.recv(1, socket.MSG_PEEK) == b"":
                self.server.client_gone.set()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(HTTPServer):
    """`/stall` never answers; any other path answers at once."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests = 0
        self.client_gone = threading.Event()
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server_port}{path}"


class DeferredCancellationTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.client = HttpClient(runtime=hs.get_rust_runtime(), user_agent="test")
        self.server = _Server()
        self.addCleanup(self.server.shutdown)

    def _wait_until(self, condition: Callable[[], object]) -> None:
        """Let the tokio threads run, without the reactor, until `condition` holds."""
        deadline = time.monotonic() + 10
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.001)

    def test_cancel_aborts_the_request(self) -> None:
        """Cancelling during the request fails the caller at once, in its own
        logcontext, and closes the connection."""
        contexts = []

        async def request() -> None:
            try:
                await self.client.get(
                    url=self.server.url("/stall"), response_limit=1024
                )
            except defer.CancelledError:
                contexts.append(str(current_context()))
                raise

        with LoggingContext(name="caller", server_name="test"):
            d = run_in_background(request)
        self._wait_until(lambda: self.server.requests == 1)
        d.cancel()

        self.assertEqual(contexts, ["caller"])
        self.assertEqual(str(current_context()), "sentinel")
        self.failureResultOf(d, defer.CancelledError)
        self.assertTrue(self.server.client_gone.wait(10))

    def test_late_result_is_dropped(self) -> None:
        """A result already on its way to the reactor when the caller cancels
        is dropped without an error."""
        d = self.client.get(url=self.server.url("/"), response_limit=1024)
        self._wait_until(self.reactor._poll_readers)
        d.cancel()

        with self.assertNoLogs("synapse", level="ERROR"):
            reset_logging_config()
            self.reactor.advance(0)
        self.failureResultOf(d, defer.CancelledError)

    def test_versions_cancelled(self) -> None:
        versions = self.hs.get_rust_handlers().versions
        d = versions.get_versions("@user:test")
        d.cancel()
        self.failureResultOf(d, defer.CancelledError)

        body = self.get_success(versions.get_versions("@user:test"))
        self.assertIn("versions", body)
