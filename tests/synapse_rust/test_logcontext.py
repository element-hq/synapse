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

"""Cross-language logcontext attribution for Rust.

The current logcontext lives in the Rust slot (`synapse.synapse_rust.logcontext`
/ `rust/src/logging/context.rs`), visible from both Python (reactor/threadpool
threads) and Rust (tokio tasks). These tests exercise the two guarantees that
gives us, through real production code paths:

1. Log records emitted from Rust while a task is being polled (e.g. reqwest
   connecting) are attributed to the logcontext that was current when Python
   called into Rust — not the sentinel.
2. When Rust calls back into Python (`run_python_awaitable`, as the Rust
   `/versions` handler does for its per-user feature DB lookup), the Python code
   runs in that same logcontext, so its DB-transaction accounting lands on the
   right request.
"""

import logging
import time
from typing import Callable

from twisted.internet.testing import MemoryReactor

from synapse.logging.context import (
    LoggingContext,
    LoggingContextFilter,
    PreserveLoggingContext,
    _Sentinel,
    current_context,
    run_in_background,
)
from synapse.rest import admin
from synapse.rest.client import login
from synapse.server import HomeServer
from synapse.synapse_rust import reset_logging_config
from synapse.synapse_rust.http_client import HttpClient
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)

# Log-target roots that Rust code emits under while running on tokio worker
# threads: the reqwest dependency stack, plus "synapse"/"synapse_rust" because
# the Rust crate is itself named `synapse` (see rust/Cargo.toml). Anything
# emitted under these while a task is being polled should be attributed to the
# caller's logcontext, never the sentinel.
#
# NB: bare "synapse" also matches every *Python* `synapse.*` record, so the
# attribution assertion below implicitly relies on nothing else logging during
# the pump (MemoryReactor with `advance(0)`, so no timed background work fires).
# If this test starts flaking on unrelated records, tighten this filter rather
# than weakening the assertion.
_RUST_LOGGER_ROOTS = frozenset(
    {"reqwest", "hyper", "hyper_util", "h2", "rustls", "synapse_rust", "synapse"}
)


class RustLogContextTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        # XXX: We must create the Rust HTTP client before we call `reactor.run()`
        # below. Twisted's `MemoryReactor` doesn't invoke `callWhenRunning`
        # callbacks if it's already running and we rely on that to start the Tokio
        # thread pool in Rust.
        self._http_client = hs.get_proxied_http_client()
        self._rust_http_client = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()

        return hs

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the Tokio
        # thread pool to be stopped.
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.user_id = self.register_user("user1", "pass")

    def _check_current_logcontext(self, expected: str) -> None:
        context = current_context()
        assert isinstance(context, (LoggingContext, _Sentinel)), context
        self.assertEqual(str(context), expected, f"expected {expected}, saw {context}")

    def _run_in_logcontext_and_pump(
        self, name: str, body: Callable[[dict[str, object]], None]
    ) -> dict[str, object]:
        """Run `body` fired off inside a fresh `LoggingContext(name)`, pumping the
        reactor (and yielding to the Tokio pool) until it sets `result["done"]`.

        Returns the `result` dict `body` populated. Asserts the caller logcontext
        is intact afterwards and that we end back in the sentinel.
        """
        self._check_current_logcontext("sentinel")
        result: dict[str, object] = {}

        with LoggingContext(name=name, server_name="test_server"):
            body(result)

            with PreserveLoggingContext():
                # Generous upper bound (the work is a real HTTP round-trip or DB
                # hop on a possibly-loaded CI box); the loop exits early via
                # `result["done"]`, and we fail below if it never gets set.
                for _ in range(50000):
                    if result.get("done"):
                        break
                    # Let the Tokio worker threads make progress...
                    time.sleep(0)
                    # ...and run anything they scheduled back on the reactor.
                    self.reactor.advance(0)

            # The caller's logcontext must be intact after firing off the work.
            self._check_current_logcontext(name)

        # ...and we must not have leaked it into the reactor.
        self._check_current_logcontext("sentinel")

        self.assertTrue(
            result.get("done"),
            "work never finished; the test probably didn't pump long enough",
        )
        return result

    def test_rust_log_records_attributed_to_caller_logcontext(self) -> None:
        """A log record emitted from Rust on a tokio thread (reqwest connecting)
        is attributed to the caller's logcontext via `LoggingContextFilter`, not
        the sentinel."""
        records: list[tuple[str, object]] = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append((record.name, getattr(record, "request", "<unset>")))

        handler = CapturingHandler()
        # The global filter is what copies `str(current_context())` onto the
        # record as `record.request`; attach it so we observe what Synapse would.
        handler.addFilter(LoggingContextFilter())

        root = logging.getLogger()
        root.addHandler(handler)

        # Turn up the Rust-side loggers so reqwest actually emits, and refresh
        # pyo3-log's cached levels so it forwards them.
        saved_levels = {
            name: logging.getLogger(name).level for name in _RUST_LOGGER_ROOTS
        }
        for name in _RUST_LOGGER_ROOTS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        reset_logging_config()

        try:
            server = _StubServer()
            self.addCleanup(server.shutdown)

            def body(result: dict[str, object]) -> None:
                async def do() -> None:
                    try:
                        await self._rust_http_client.get(
                            url=server.endpoint,
                            response_limit=1 * 1024 * 1024,
                        )
                    finally:
                        result["done"] = True

                run_in_background(do)

            self._run_in_logcontext_and_pump("http-caller", body)
        finally:
            root.removeHandler(handler)
            for name, level in saved_levels.items():
                logging.getLogger(name).setLevel(level)
            reset_logging_config()

        rust_records = [
            (name, req)
            for (name, req) in records
            if name.split(".", 1)[0] in _RUST_LOGGER_ROOTS
        ]
        self.assertTrue(
            rust_records,
            "expected at least one Rust-origin log record (e.g. reqwest connecting); "
            f"captured loggers: {sorted({name for name, _ in records})}",
        )
        for name, req in rust_records:
            self.assertEqual(
                req,
                "http-caller",
                f"Rust log record from {name!r} was attributed to {req!r}, "
                "not the caller's logcontext",
            )

    def test_db_callback_runs_in_caller_logcontext(self) -> None:
        """The Rust `/versions` handler's per-user feature lookup calls back into
        Python via `run_python_awaitable`; the DB transaction it runs must be
        accounted against the caller's logcontext. (Before the Rust storage
        port, a FIXME in `run_python_awaitable` meant this ran in the
        sentinel and the accounting was lost.)"""
        versions_handler = self.hs.get_rust_handlers().versions

        def body(result: dict[str, object]) -> None:
            async def do() -> None:
                try:
                    context = current_context()
                    assert isinstance(context, LoggingContext)
                    before = context.get_resource_usage().db_txn_count

                    # Passing a user id makes the Rust handler do a per-user
                    # feature DB lookup (msc3881/msc3575 default to off), which
                    # goes Rust -> run_python_awaitable -> runInteraction.
                    await versions_handler.get_versions(self.user_id)

                    after = context.get_resource_usage().db_txn_count
                    result["db_txn_delta"] = after - before
                finally:
                    result["done"] = True

            run_in_background(do)

        result = self._run_in_logcontext_and_pump("db-caller", body)

        db_txn_delta = result.get("db_txn_delta", 0)
        assert isinstance(db_txn_delta, int)
        self.assertGreaterEqual(
            db_txn_delta,
            1,
            "the Rust handler's DB work was not accounted against the caller's "
            "logcontext — run_python_awaitable is not restoring it (ran in the "
            "sentinel instead)",
        )


class _StubServer:
    """A real HTTP server on a random port, served from a background thread."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="StubServer",
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join()
