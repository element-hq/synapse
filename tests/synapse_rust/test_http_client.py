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

import time
from typing import Any, Coroutine, Generator, TypeVar, Union

from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import LoggingContext
from synapse.server import HomeServer
from synapse.synapse_rust.http_client import HttpClient
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

T = TypeVar("T")


class HttpClientTestCase(HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()
        return hs

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self._http_client = hs.get_proxied_http_client()
        self._rust_http_client = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

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

    def test_logging_context(self) -> None:
        async def asdf() -> None:
            with LoggingContext("test"):
                # TODO: Test logging context before/after this call
                await self._rust_http_client.get(
                    url="http://localhost",
                    response_limit=1 * 1024 * 1024,
                )

        self.get_success(self.till_deferred_has_result(asdf()))
