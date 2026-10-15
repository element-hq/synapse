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

from typing import Any, Callable, Mapping, Sequence

from signedjson.types import SigningKey

from twisted.internet.defer import Deferred

from synapse.server import HomeServer
from synapse.synapse_rust.events import Event
from synapse.types import JsonDict

def build_auth_header_for_test(
    signing_key: SigningKey,
    origin: str,
    method: str,
    uri: str,
    destination: str | None,
    destination_is: str | None = None,
    content: JsonDict | None = None,
) -> str:
    """Build the `X-Matrix` `Authorization` header the Rust request layer
    sends. Tests compare it with `MatrixFederationHttpClient.build_auth_headers`.
    """

class RustFederationAgentForTest:
    """The Rust federation agent, built from the homeserver's config, resolving
    names from fixed tables. For tests only.

    `request_blocking` blocks the calling thread until the response arrives, so
    the server it talks to must run on another thread.
    """

    def __init__(
        self,
        hs: HomeServer,
        srv_records: Mapping[str, Sequence[tuple[int, int, int, str]]],
        host_records: Mapping[str, Sequence[str]],
        port_overrides: Mapping[int, int],
        user_agent: str = "Synapse-test",
    ) -> None:
        """
        Args:
            hs: The homeserver whose config and Rust runtime to use.
            srv_records: SRV name to `(priority, weight, port, target)` tuples.
            host_records: Host name to IP addresses.
            port_overrides: Ports that resolution picks, mapped to the ports to
                really connect to.
            user_agent: The `User-Agent` header to send.
        """

    def request_blocking(
        self,
        method: str,
        uri: str,
        headers: Sequence[tuple[str, str]] = (),
        body: bytes = b"",
    ) -> tuple[int, list[tuple[str, bytes]], bytes]:
        """Send a request and return `(status, headers, body)`.

        Raises:
            RuntimeError: `"<kind>: <message>"` if no response arrived, where
                `kind` is e.g. `dns`, `connect`, `tls` or `service_unavailable`.
        """

def federation_gil_stats() -> dict[str, tuple[int, float]]:
    """How often the Rust federation code has taken the GIL: call site to
    `(calls, seconds held)`."""

class RustFederationClient:
    """The Rust federation request layer: signing, retries, the backoff and the
    connection pool. `MatrixFederationHttpClient` sends its JSON requests
    through it at `rust_federation_sending: client`.
    """

    def __init__(
        self,
        hs: HomeServer,
        user_agent: str,
        agent_user_agent: str,
        write_retry_timings: Callable[[str, int | None, int, int, bool], None],
        record_metrics: Callable[[str, int, list[int]], None],
        transport: Callable[
            [str, str, str, list[tuple[bytes, bytes]], bytes],
            tuple[int, list[tuple[bytes, bytes]], bytes],
        ]
        | None = None,
    ) -> None:
        """
        Args:
            hs: The homeserver whose config, signing key and Rust runtime to use.
            user_agent: The `User-Agent` header of federation requests.
            agent_user_agent: The `User-Agent` header of `.well-known` fetches.
            write_retry_timings: Called on the reactor as
                `(destination, failure_ts, retry_last_ts, retry_interval,
                server_up)` when a destination's backoff changes.
            record_metrics: Called once per request as `(method, attempts,
                response_codes)`.
            transport: For tests: send through this callable, as
                `(destination, method, path, headers, body) -> (status,
                headers, body)`, instead of the network.
        """

    def request(
        self,
        method: str,
        destination: str,
        path: str,
        query: str,
        body: JsonDict | None,
        parser_kind: str,
        parser: Any = None,
        *,
        rewrite_age_ts: bool = False,
        extra_headers: Sequence[tuple[str, str]] = (),
        retry_on_dns_fail: bool = True,
        timeout_ms: int | None = None,
        long_retries: bool = False,
        ignore_backoff: bool = False,
        backoff_on_404: bool = False,
        backoff_on_all_error_codes: bool = False,
        try_trailing_slash_on_400: bool = False,
        want_headers: bool = False,
    ) -> Deferred[tuple[Any, dict[bytes, list[bytes]] | None]]:
        """Send a request and parse the response, raising what
        `MatrixFederationHttpClient` raises.

        `parser_kind` is `"json"`, `"legacy_json_send"` or `"custom"`, for
        which `parser` is the `ByteParser`.
        """

    def shutdown(self) -> None:
        """Stop retrying requests."""

    def wake_destination(self, destination: str) -> None:
        """End the sleep before any retry to `destination`."""

class RustTransaction:
    """A transaction built by `RustTransactionManager`, with the attributes of
    `synapse.federation.units.Transaction`."""

    @property
    def transaction_id(self) -> str: ...
    @property
    def origin(self) -> str: ...
    @property
    def destination(self) -> str: ...
    @property
    def origin_server_ts(self) -> int: ...
    @property
    def pdus(self) -> list[JsonDict]: ...
    @property
    def edus(self) -> list[JsonDict]: ...
    def get_dict(self) -> JsonDict:
        """The request body, as `Transaction.get_dict` gives it."""

class RustTransactionManager:
    """Builds and sends `/send` transactions through a `RustFederationClient`,
    at `rust_federation_sending: transactions`."""

    def __init__(self, client: RustFederationClient) -> None: ...
    def send_transaction(
        self,
        destination: str,
        pdus: Sequence[Event],
        edus: Sequence[JsonDict],
        *,
        extra_headers: Sequence[tuple[str, str]] = (),
        want_acknowledged: bool = False,
        check: Callable[[RustTransaction], None] | None = None,
    ) -> Deferred[list[str] | None]:
        """Send `pdus` and `edus` to `destination` in one transaction.

        Resolves to the ids of the PDUs the remote accepted if
        `want_acknowledged`, else `None`. Raises what `put_json` raises.

        `check`, for tests, is called with the transaction before it is sent,
        and may raise to stop the send.
        """

    def build_transaction(
        self, destination: str, pdus: Sequence[Event], edus: Sequence[JsonDict]
    ) -> RustTransaction:
        """Build the transaction `send_transaction` would send, without sending
        it. For tests."""

    def acknowledged_pdus(
        self, transaction: RustTransaction, response: JsonDict
    ) -> list[str]:
        """The ids of the PDUs `response` accepts, logging the ones it rejects.
        For tests."""
