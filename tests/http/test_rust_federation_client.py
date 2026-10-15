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
"""Tests for `MatrixFederationHttpClient` at `rust_federation_sending: client`,
where its JSON requests go through the Rust request layer.

The Rust client sends through a Python callable here, in place of the network.
It runs on a tokio thread, so the tests wait for it with `get_success`, and
advance the reactor to get past the sleeps between retries.
"""

import json
import re
import time
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import attr
from signedjson.key import get_verify_key
from signedjson.sign import verify_signed_json

from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.api.errors import (
    FederationDeniedError,
    HttpResponseException,
    RequestSendFailed,
)
from synapse.http.matrixfederationclient import (
    ByteParser,
    LegacyJsonSendParser,
    incoming_responses_counter,
    outgoing_requests_counter,
)
from synapse.server import HomeServer
from synapse.synapse_rust.federation import (
    RustFederationClient,
    federation_gil_stats,
)
from synapse.types import JsonDict
from synapse.util.clock import Clock
from synapse.util.retryutils import NotRetryingDestination

from tests import unittest

DEST = "remote.example"


@attr.s(auto_attribs=True, frozen=True)
class SentRequest:
    destination: str
    method: str
    path: str
    headers: list[tuple[bytes, bytes]]
    body: bytes

    def header(self, name: bytes) -> bytes | None:
        for key, value in self.headers:
            if key == name.lower():
                return value
        return None


Response = tuple[int, list[tuple[bytes, bytes]], bytes]


def json_response(status: int, body: Any) -> Response:
    return status, [(b"content-type", b"application/json")], json.dumps(body).encode()


class FakeTransport:
    """Answers each request with the next queued response."""

    def __init__(self) -> None:
        self.responses: deque[Response] = deque()
        self.requests: list[SentRequest] = []

    def __call__(
        self,
        destination: str,
        method: str,
        path: str,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> Response:
        self.requests.append(SentRequest(destination, method, path, headers, body))
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {path}")
        return self.responses.popleft()


class RustFederationClientTestCase(unittest.HomeserverTestCase):
    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_federation_sending"] = (
            "client"
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.cl = hs.get_federation_http_client()
        assert self.cl._rust_client is not None

        self.transport = FakeTransport()
        self.cl._rust_client = RustFederationClient(
            hs,
            hs.version_string,
            hs.version_string,
            self.cl._write_retry_timings,
            self.cl._record_request_metrics,
            self.transport,
        )

    def _wait(self, d: "Deferred[Any]", step: float = 0.5, limit: int = 4000) -> None:
        """Advance the reactor in `step` second steps, giving tokio a moment
        after each, until `d` fires."""
        for _ in range(limit):
            if d.called and not d.paused:
                return
            self.reactor.advance(step)
            time.sleep(0.001)
        raise AssertionError("request did not finish")

    def test_get_json(self) -> None:
        self.transport.responses.append(json_response(200, {"foo": "bar"}))

        result = self.get_success(
            self.cl.get_json(DEST, "/_matrix/federation/v1/x", args={"a": ["1", "2"]})
        )

        self.assertEqual(result, {"foo": "bar"})
        (request,) = self.transport.requests
        self.assertEqual(request.destination, DEST)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/_matrix/federation/v1/x?a=1&a=2")
        self.assertEqual(request.body, b"")
        self.assertIsNone(request.header(b"Content-Type"))
        self.assertEqual(request.header(b"User-Agent"), self.hs.version_string.encode())
        self._assert_signed(request, None)

    def test_put_json_body(self) -> None:
        self.transport.responses.append(json_response(200, {}))

        self.get_success(
            self.cl.put_json(DEST, "/_matrix/federation/v1/y", data={"b": 2, "a": "é"})
        )

        (request,) = self.transport.requests
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.header(b"Content-Type"), b"application/json")
        self.assertEqual(request.body, '{"a":"é","b":2}'.encode())
        self._assert_signed(request, {"b": 2, "a": "é"})

    def _assert_signed(self, request: SentRequest, content: JsonDict | None) -> None:
        """Check the `X-Matrix` header, as the receiving server would."""
        header = request.header(b"Authorization")
        assert header is not None
        params = dict(re.findall(r'(\w+)="([^"]*)"', header.decode()))
        self.assertEqual(params["origin"], self.hs.hostname)
        self.assertEqual(params["destination"], DEST)

        signed: JsonDict = {
            "method": request.method,
            "uri": request.path,
            "origin": params["origin"],
            "destination": params["destination"],
            "signatures": {params["origin"]: {params["key"]: params["sig"]}},
        }
        if content is not None:
            signed["content"] = content
        verify_key = get_verify_key(self.hs.signing_key)
        verify_signed_json(signed, params["origin"], verify_key)

    def test_retries_server_errors(self) -> None:
        self.transport.responses.extend(
            [json_response(502, {}), json_response(500, {}), json_response(200, {})]
        )

        d = ensureDeferred(self.cl.get_json(DEST, "/x"))
        self._wait(d)

        self.assertEqual(self.successResultOf(d), {})
        self.assertEqual(len(self.transport.requests), 3)

    def test_client_error_raises(self) -> None:
        self.transport.responses.append(
            json_response(404, {"errcode": "M_NOT_FOUND", "error": "nope"})
        )

        failure = self.get_failure(self.cl.get_json(DEST, "/x"), HttpResponseException)

        self.assertEqual(failure.value.code, 404)
        self.assertEqual(failure.value.to_synapse_error().errcode, "M_NOT_FOUND")
        self.assertEqual(len(self.transport.requests), 1)

    def test_failures_start_a_backoff(self) -> None:
        """Running out of retries records a backoff, which the next request
        honours. This goes through the Python write and its cache
        invalidation, which reaches the Rust client's copy of the timings."""
        # The first attempt plus the three short retries.
        self.transport.responses.extend([json_response(500, {})] * 4)

        d = ensureDeferred(self.cl.get_json(DEST, "/x"))
        self._wait(d)
        failure = self.failureResultOf(d, RequestSendFailed)
        assert isinstance(failure.value, RequestSendFailed)
        self.assertTrue(failure.value.can_retry)
        self.assertIsInstance(failure.value.inner_exception, HttpResponseException)

        timings = self.get_success(
            self.hs.get_datastores().main.get_destination_retry_timings(DEST)
        )
        assert timings is not None
        self.assertEqual(
            timings.retry_interval,
            self.hs.config.federation.destination_min_retry_interval_ms,
        )

        self.get_failure(self.cl.get_json(DEST, "/x"), NotRetryingDestination)
        self.assertEqual(len(self.transport.requests), 4)

        # Ignoring the backoff sends, and success clears it.
        self.transport.responses.append(json_response(200, {}))
        self.get_success(self.cl.get_json(DEST, "/x", ignore_backoff=True))
        self.reactor.advance(0)
        timings = self.get_success(
            self.hs.get_datastores().main.get_destination_retry_timings(DEST)
        )
        self.assertIsNone(timings)

    def test_send_direct_request_raises_the_http_error(self) -> None:
        self.transport.responses.extend([json_response(503, {})] * 4)

        d = ensureDeferred(self.cl.send_direct_request("GET", DEST, "/x"))
        self._wait(d)

        failure = self.failureResultOf(d, HttpResponseException)
        self.assertEqual(failure.value.code, 503)

    def _send_failing(self, timeout: int | None) -> int:
        """Send a request that fails to connect on every attempt, and return
        the number of attempts."""
        attempts = 0

        def fail(*args: Any) -> Response:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("boom")

        self.cl._rust_client = RustFederationClient(
            self.hs,
            self.hs.version_string,
            self.hs.version_string,
            self.cl._write_retry_timings,
            self.cl._record_request_metrics,
            fail,
        )
        d = ensureDeferred(self.cl.get_json(DEST, "/x", timeout=timeout))
        self._wait(d)
        failure = self.failureResultOf(d, RequestSendFailed)
        assert isinstance(failure.value, RequestSendFailed)
        self.assertIsInstance(failure.value.inner_exception, ConnectionError)
        self.assertTrue(failure.value.can_retry)
        return attempts

    def test_transport_error_with_a_timeout_is_not_retried(self) -> None:
        self.assertEqual(self._send_failing(timeout=10_000), 1)

    def test_transport_error_without_a_timeout_is_retried(self) -> None:
        retries = self.hs.config.federation.max_short_retries
        self.assertEqual(self._send_failing(timeout=None), retries + 1)

    @unittest.override_config({"federation_domain_whitelist": ["other.example"]})
    def test_whitelist(self) -> None:
        self.get_failure(self.cl.get_json(DEST, "/x"), FederationDeniedError)
        self.assertEqual(self.transport.requests, [])

    def test_invalid_json(self) -> None:
        self.transport.responses.append(
            (200, [(b"content-type", b"application/json")], b"{not json")
        )
        failure = self.get_failure(self.cl.get_json(DEST, "/x"), RequestSendFailed)
        self.assertFalse(failure.value.can_retry)
        self.assertIsInstance(failure.value.inner_exception, ValueError)

    def test_json_that_is_not_an_object(self) -> None:
        self.transport.responses.append(json_response(200, [1, 2]))
        failure = self.get_failure(self.cl.get_json(DEST, "/x"), RequestSendFailed)
        self.assertFalse(failure.value.can_retry)
        self.assertIn("list", str(failure.value.inner_exception))

    def test_json_python_accepts_and_serde_does_not(self) -> None:
        """A lone surrogate is valid JSON to Python."""
        self.transport.responses.append(
            (200, [(b"content-type", b"application/json")], b'{"a": "\\ud800"}')
        )
        result = self.get_success(self.cl.get_json(DEST, "/x"))
        self.assertEqual(result, {"a": "\ud800"})

    def test_wrong_content_type(self) -> None:
        self.transport.responses.append((200, [(b"content-type", b"text/html")], b"{}"))
        failure = self.get_failure(self.cl.get_json(DEST, "/x"), RequestSendFailed)
        self.assertFalse(failure.value.can_retry)
        self.assertIn("text/html", str(failure.value.inner_exception))

    def test_legacy_json_send_parser(self) -> None:
        self.transport.responses.append(json_response(200, [200, {"ok": True}]))
        result = self.get_success(
            self.cl.put_json(
                DEST, "/send_join", data={"a": 1}, parser=LegacyJsonSendParser()
            )
        )
        self.assertEqual(result, [200, {"ok": True}])

        self.transport.responses.append(json_response(200, {"ok": True}))
        self.get_failure(
            self.cl.put_json(
                DEST, "/send_join", data={"a": 1}, parser=LegacyJsonSendParser()
            ),
            RequestSendFailed,
        )

    def test_custom_parser(self) -> None:
        class Collect(ByteParser[bytes]):
            CONTENT_TYPE = "application/json"

            def __init__(self) -> None:
                self.data = b""

            def write(self, data: bytes) -> int:
                self.data += data
                return len(data)

            def finish(self) -> bytes:
                return self.data

        body = json.dumps({"big": "x" * 200_000}).encode()
        self.transport.responses.append(
            (200, [(b"content-type", b"application/json")], body)
        )
        result = self.get_success(self.cl.get_json(DEST, "/x", parser=Collect()))
        self.assertEqual(result, body)

    def test_custom_parser_is_finished_when_write_fails(self) -> None:
        class Broken(ByteParser[None]):
            CONTENT_TYPE = "application/json"
            finished = False

            def write(self, data: bytes) -> int:
                raise ValueError("bad body")

            def finish(self) -> None:
                self.finished = True

        self.transport.responses.append(json_response(200, {}))
        parser = Broken()
        failure = self.get_failure(
            self.cl.get_json(DEST, "/x", parser=parser), RequestSendFailed
        )
        self.assertIsInstance(failure.value.inner_exception, ValueError)
        self.assertTrue(parser.finished)

    def test_get_json_with_headers(self) -> None:
        self.transport.responses.append(
            (
                200,
                [(b"content-type", b"application/json"), (b"x-thing", b"1")],
                b"{}",
            )
        )
        _, headers = self.get_success(self.cl.get_json_with_headers(DEST, "/x"))
        self.assertEqual(headers[b"X-Thing"], [b"1"])
        self.assertEqual(headers[b"Content-Type"], [b"application/json"])

    def test_transaction_age_rewrite(self) -> None:
        """`send_transaction`'s `json_data_callback` is replaced by the Rust
        client rewriting `age_ts` on each attempt."""
        self.transport.responses.append(json_response(200, {}))
        now = self.clock.time_msec()
        data = {"pdus": [{"event_id": "$a", "age_ts": now - 1000}]}

        def callback() -> JsonDict:
            raise AssertionError("the callback is not called")

        self.get_success(
            self.cl.put_json(
                DEST,
                "/send/1",
                data=data,
                json_data_callback=callback,
                rewrite_age_ts=True,
            )
        )
        (request,) = self.transport.requests
        self.assertEqual(
            json.loads(request.body)["pdus"],
            [{"event_id": "$a", "unsigned": {"age": 1000}}],
        )

    def test_other_json_data_callback_uses_python(self) -> None:
        """Only the age rewrite is done in Rust. Any other callback sends the
        request through the Python client."""
        send = AsyncMock(return_value=Mock())
        self.cl._send_request_with_optional_trailing_slash = send  # type: ignore[method-assign]

        with patch(
            "synapse.http.matrixfederationclient._handle_response",
            AsyncMock(return_value={}),
        ):
            self.get_success(
                self.cl.put_json(DEST, "/x", data={"a": 1}, json_data_callback=dict)
            )
            self.get_success(self.cl.put_json(DEST, "/x", json_data_callback=dict))

        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.transport.requests, [])

    def test_request_metrics(self) -> None:
        self.transport.responses.extend(
            [json_response(500, {}), json_response(200, {})]
        )
        name = self.hs.hostname
        requests = outgoing_requests_counter.labels(method="GET", server_name=name)
        ok = incoming_responses_counter.labels(method="GET", code=200, server_name=name)
        errors = incoming_responses_counter.labels(
            method="GET", code=500, server_name=name
        )
        before = (requests._value.get(), ok._value.get(), errors._value.get())

        d = ensureDeferred(self.cl.get_json(DEST, "/x"))
        self._wait(d)
        self.successResultOf(d)

        after = (requests._value.get(), ok._value.get(), errors._value.get())
        self.assertEqual(
            [b - a for a, b in zip(before, after, strict=True)], [2.0, 1.0, 1.0]
        )

    def test_server_up_wakes_the_retry_sleep(self) -> None:
        """A destination coming back, here through another request clearing
        its backoff, ends the sleep before a retry in the Rust client."""
        store = self.hs.get_datastores().main
        # A zero `failure_ts` does not count as failing.
        self.reactor.advance(10)
        self.get_success(
            store.set_destination_retry_timings(
                DEST, self.clock.time_msec(), self.clock.time_msec(), 1000
            )
        )
        # The first request fails, and sleeps for a long retry. The second
        # ignores the backoff and succeeds, which clears it and calls
        # `notify_remote_server_up`.
        self.transport.responses.extend(
            [json_response(500, {}), json_response(200, {}), json_response(200, {})]
        )
        first = ensureDeferred(
            self.cl.put_json(DEST, "/first", ignore_backoff=True, long_retries=True)
        )
        self._wait_for_requests(1)
        # Let the first request reach its sleep.
        time.sleep(0.05)
        self.get_success(self.cl.get_json(DEST, "/second", ignore_backoff=True))

        # Without the wake, the first long retry sleeps for at least 3.2
        # seconds.
        start = self.clock.time()
        self._wait(first, step=0.1)
        self.assertLess(self.clock.time() - start, 3)
        self.assertEqual(
            [r.path for r in self.transport.requests], ["/first", "/second", "/first"]
        )

    def test_shutdown_stops_retries(self) -> None:
        self.transport.responses.extend([json_response(500, {})] * 4)
        d = ensureDeferred(self.cl.get_json(DEST, "/x"))
        self._wait_for_requests(1)

        self.cl.shutdown()
        self._wait(d)

        self.failureResultOf(d)
        self.assertEqual(len(self.transport.requests), 1)

    def _wait_for_requests(self, count: int) -> None:
        for _ in range(4000):
            if len(self.transport.requests) >= count:
                return
            # The transport runs on the reactor.
            self.reactor.advance(0)
            time.sleep(0.001)
        raise AssertionError("the request was not sent")

    def test_gil_crossings(self) -> None:
        """A successful request takes the GIL once below the facade, to hand
        back the result. The transport here is Python and is not counted."""
        self.transport.responses.append(json_response(200, {}))
        before = federation_gil_stats()

        self.get_success(self.cl.get_json(DEST, "/x"))

        after = federation_gil_stats()
        calls = {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if after[site][0] != before.get(site, (0, 0.0))[0]
        }
        self.assertEqual(calls, {"response": 1})


class ProxyWorkerTestCase(unittest.HomeserverTestCase):
    """An instance that sends through a proxy worker stays on the Python
    client."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["experimental_features"] = {"rust_federation_sending": "client"}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "federation_sender": {"host": "testserv", "port": 1001},
        }
        config["outbound_federation_restricted_to"] = ["federation_sender"]
        config["worker_replication_secret"] = "secret"
        return config

    def test_no_rust_client(self) -> None:
        self.assertIsNone(self.hs.get_federation_http_client()._rust_client)
