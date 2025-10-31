# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Coroutine, Generator, TypeVar, Union

from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import (
    LoggingContext,
    PreserveLoggingContext,
    _Sentinel,
    current_context,
    run_in_background,
)
from synapse.server import HomeServer
from synapse.synapse_rust.http_client import HttpClient
from synapse.util.clock import Clock
from synapse.util.json import json_decoder

from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StubRequestHandler(BaseHTTPRequestHandler):
    server: "StubServer"

    def do_GET(self) -> None:
        self.server.calls += 1

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        # Don't log anything; by default, the server logs to stderr
        pass


class StubServer(HTTPServer):
    """A stub HTTP server that we can send requests to for testing.

    This opens a real HTTP server on a random port, on a separate thread.
    """

    calls: int = 0
    """How many times has the endpoint been requested."""

    _thread: threading.Thread

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), StubRequestHandler)

        self._thread = threading.Thread(
            target=self.serve_forever,
            name="StubServer",
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        super().shutdown()
        self._thread.join()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"


class HttpClientTestCase(HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        # XXX: We must create the Rust HTTP client before we call `reactor.run()` below.
        # Twisted's `MemoryReactor` doesn't invoke `callWhenRunning` callbacks if it's
        # already running and we rely on that to start the Tokio thread pool in Rust. In
        # the future, this may not matter, see https://github.com/twisted/twisted/pull/12514
        self._http_client = hs.get_proxied_http_client()
        self._rust_http_client = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.server = StubServer()

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def till_deferred_has_result(
        self,
        awaitable: Union[
            "Coroutine[Deferred[Any], Any, T]",
            "Generator[Deferred[Any], Any, T]",
            "Deferred[T]",
        ],
    ) -> "Deferred[T]":
        """Wait until a deferred has a result.

        This is useful because the Rust HTTP client will resolve the deferred
        using reactor.callFromThread, which are only run when we call
        reactor.advance.
        """
        deferred = ensureDeferred(awaitable)
        tries = 0
        while not deferred.called:
            time.sleep(0.1)
            self.reactor.advance(0)
            tries += 1
            if tries > 100:
                raise Exception("Timed out waiting for deferred to resolve")

        return deferred

    def _check_current_logcontext(self, expected_logcontext_string: str) -> None:
        context = current_context()
        assert isinstance(context, LoggingContext) or isinstance(context, _Sentinel), (
            f"Expected LoggingContext({expected_logcontext_string}) but saw {context}"
        )
        self.assertEqual(
            str(context),
            expected_logcontext_string,
            f"Expected LoggingContext({expected_logcontext_string}) but saw {context}",
        )

    def test_request_response(self) -> None:
        """
        Test to make sure we can make a basic request and get the expected
        response.
        """

        async def do_request() -> None:
            resp_body = await self._rust_http_client.get(
                url=self.server.endpoint,
                response_limit=1 * 1024 * 1024,
            )
            raw_response = json_decoder.decode(resp_body.decode("utf-8"))
            self.assertEqual(raw_response, {"ok": True})

        self.get_success(self.till_deferred_has_result(do_request()))
        self.assertEqual(self.server.calls, 1)

    async def test_logging_context(self) -> None:
        """
        Test to make sure the `LoggingContext` (logcontext) is handled correctly
        when making requests.
        """
        # Sanity check that we start in the sentinel context
        self._check_current_logcontext("sentinel")

        callback_finished = False

        async def do_request() -> None:
            nonlocal callback_finished
            try:
                # Should have the same logcontext as the caller
                self._check_current_logcontext("foo")

                with LoggingContext(name="competing", server_name="test_server"):
                    # Make the actual request
                    await self._rust_http_client.get(
                        url=self.server.endpoint,
                        response_limit=1 * 1024 * 1024,
                    )
                    self._check_current_logcontext("competing")

                # Back to the caller's context outside of the `LoggingContext` block
                self._check_current_logcontext("foo")
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="foo", server_name="test_server"):
            # Fire off the function, but don't wait on it.
            run_in_background(do_request)

            # Now wait for the function under test to have run
            with PreserveLoggingContext():
                while not callback_finished:
                    # await self.hs.get_clock().sleep(0)
                    time.sleep(0.1)
                    self.reactor.advance(0)

            # check that the logcontext is left in a sane state.
            self._check_current_logcontext("foo")

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # Back to the sentinel context
        self._check_current_logcontext("sentinel")
