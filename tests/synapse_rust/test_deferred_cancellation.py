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
from typing import Callable

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import (
    LoggingContext,
    PreserveLoggingContext,
    current_context,
    run_in_background,
)
from synapse.server import HomeServer
from synapse.synapse_rust.http_client import HttpClient
from synapse.util.clock import Clock

from tests.synapse_rust.test_http_client import StubRequestHandler, StubServer
from tests.unittest import HomeserverTestCase, logcontext_clean

# Reached only when a test fails; long enough for a loaded CI runner.
_TIMEOUT = 10


class _StallingHandler(StubRequestHandler):
    server: "_Server"

    def do_GET(self) -> None:
        if self.path != "/stall":
            return super().do_GET()
        self.server.calls += 1
        # The request has no body, so this returns only once the client
        # closes the connection.
        self.connection.settimeout(_TIMEOUT)
        if self.connection.recv(1, socket.MSG_PEEK) == b"":
            self.server.client_gone.set()


class _Server(StubServer):
    """`/stall` never answers; any other path answers at once."""

    def __init__(self) -> None:
        self.client_gone = threading.Event()
        super().__init__(_StallingHandler)


class DeferredCancellationTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.client = HttpClient(runtime=hs.get_rust_runtime(), user_agent="test")
        self.server = _Server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _wait_until(self, condition: Callable[[], object]) -> None:
        """Let the tokio threads run, without the reactor, until `condition` holds."""
        deadline = time.monotonic() + _TIMEOUT
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.001)

    @logcontext_clean
    def test_cancel_aborts_the_request(self) -> None:
        """Cancelling during the request fails the caller at once, in its own
        logcontext, and closes the connection."""
        contexts = []

        async def request() -> None:
            try:
                await self.client.get(
                    url=self.server.endpoint + "stall", response_limit=1024
                )
            except defer.CancelledError:
                contexts.append(str(current_context()))
                raise

        with LoggingContext(name="caller", server_name="test"):
            d = run_in_background(request)
            with PreserveLoggingContext():
                self._wait_until(lambda: self.server.calls == 1)
                d.cancel()
            self.assertEqual(str(current_context()), "caller")

        self.assertEqual(contexts, ["caller"])
        self.failureResultOf(d, defer.CancelledError)
        self.assertTrue(self.server.client_gone.wait(_TIMEOUT))

    def test_late_result_is_dropped(self) -> None:
        """A result already on its way to the reactor when the caller cancels
        is dropped without an error."""
        d = self.client.get(url=self.server.endpoint, response_limit=1024)
        # Wait until the task has queued its result for the reactor, which
        # hasn't run it yet.
        self._wait_until(self.reactor._poll_readers)
        d.cancel()

        with self.assertNoLogs("synapse", level="ERROR"):
            self.reactor.advance(0)
        self.failureResultOf(d, defer.CancelledError)
