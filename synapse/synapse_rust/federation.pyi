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

from typing import Any, Awaitable, Callable, Mapping, Sequence

from signedjson.types import SigningKey

from twisted.internet.defer import Deferred

from synapse.api.presence import UserPresenceState
from synapse.federation.units import Edu
from synapse.server import HomeServer
from synapse.synapse_rust.events import Event
from synapse.types import JsonDict, ReadReceipt

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

class RustDestinationQueues:
    """Makes the Rust per-destination queues, at
    `rust_federation_sending: queues`."""

    def __init__(
        self,
        transactions: RustTransactionManager,
        msc4354_enabled: bool,
        metrics_domains: set[str],
    ) -> None:
        """`metrics_domains` is `federation_metrics_domains`."""

    def use_native_store(
        self,
        db_pool: Any,
        stream_tokens: Callable[[], tuple[int, int]],
        allow_device_name_lookup_over_federation: bool,
        to_device_since: int,
        device_lists_since: int,
    ) -> None:
        """Do the loops' database work through the native driver. `db_pool`
        is the `RustConnectionPool`, and `stream_tokens()` returns the current
        to-device and device list stream positions. The change trackers know
        about the changes after the two positions given, and must be told of
        every later one."""

    def to_device_changed(self, destination: str | None, stream_pos: int) -> None:
        """A `StreamChangeCache` listener for to-device messages."""

    def device_list_changed(self, destination: str | None, stream_pos: int) -> None:
        """A `StreamChangeCache` listener for device list updates."""

    def take_metrics(
        self,
    ) -> tuple[int, dict[str, int], int, dict[int, int], dict[str, int]]:
        """The counts since the last call: `(sent_transactions,
        sent_edus_by_type, put_requests, put_responses_by_code,
        last_sent_pdu_ts_by_destination)`."""

    def queue(
        self,
        destination: str,
        facade: Any,
        should_send_on_this_instance: bool,
        notify_delivered: bool,
    ) -> "RustDestinationQueue":
        """The queue for `destination`. `facade` provides the `_rust_*`
        hooks, and is held weakly. `notify_delivered` is whether a module
        wants to hear about delivered events."""

    def idle(self) -> bool:
        """Whether every transmission loop has finished or is waiting on the
        reactor. For tests."""

    def set_send_for_test(
        self, send: Callable[[RustTransaction], Awaitable[Any]] | None
    ) -> None:
        """Send transactions through `send` in place of the network. For
        tests."""

class RustDestinationQueue:
    """One destination's queue. Each method is the `PerDestinationQueue`
    method of the same name."""

    transmission_loop_running: bool
    new_data_to_send: bool
    catching_up: bool
    catchup_last_skipped: int
    last_successful_stream_ordering: int | None
    transmission_loop_enabled: bool
    sticky_backlogged: bool

    def shutdown(self) -> None: ...
    def pending_pdu_count(self) -> int: ...
    def pending_edu_count(self) -> int: ...
    def send_pdu(self, pdu: Event) -> None: ...
    def send_presence(
        self, states: Sequence[UserPresenceState], start_loop: bool = True
    ) -> None: ...
    def queue_read_receipt(self, receipt: ReadReceipt) -> None: ...
    def send_keyed_edu(self, edu: Edu, key: str) -> None: ...
    def send_edu(self, edu: Edu) -> None: ...
    def mark_new_data(self) -> None: ...
    def attempt_new_transaction(self) -> None: ...
    def start_catching_up(self) -> None: ...
    def pending_pdu_ids(self) -> list[str]:
        """The event IDs of the queued PDUs, oldest first. For tests."""

class FederationStoreForTest:
    """The Rust federation store, for tests that compare it with Python's.
    Each method blocks until its query is done."""

    def __init__(
        self, pool: Any, allow_device_name_lookup_over_federation: bool
    ) -> None: ...
    def get_new_device_msgs_for_remote(
        self, destination: str, last_stream_id: int, current_stream_id: int, limit: int
    ) -> tuple[list[JsonDict], int]: ...
    def delete_device_msgs_for_remote(
        self, destination: str, up_to_stream_id: int
    ) -> None: ...
    def get_device_updates_by_remote(
        self, destination: str, from_stream_id: int, now_stream_id: int, limit: int
    ) -> tuple[int, list[tuple[str, JsonDict]]]: ...
    def mark_as_sent_devices_by_remote(
        self, destination: str, stream_id: int
    ) -> None: ...
    def set_destination_last_successful_stream_ordering(
        self, destination: str, stream_ordering: int
    ) -> None: ...
