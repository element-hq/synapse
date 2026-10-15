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
"""Tests for `TransactionManager` at `rust_federation_sending: transactions`,
where Rust builds and sends the transactions.

As in `test_rust_federation_client`, the Rust client sends through a Python
callable in place of the network. `TransactionManager._rust_strict` is set
for every test, so each transaction body is also checked against the one
Python builds.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import HttpResponseException, RequestSendFailed
from synapse.events import EventBase
from synapse.federation.sender.transaction_manager import (
    TransactionManager,
    last_pdu_ts_metric,
)
from synapse.federation.units import Edu
from synapse.metrics import SERVER_NAME_LABEL
from synapse.module_api.callbacks.federation import (
    FederatedEventDeliveryMethod,
    FederationEventDeliveryEvent,
)
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.synapse_rust.federation import (
    RustFederationClient,
    RustTransactionManager,
    federation_gil_stats,
)
from synapse.util.clock import Clock

from tests import unittest
from tests.http.test_rust_federation_client import (
    DEST,
    FakeTransport,
    json_response,
)


class RustTransactionsTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_federation_sending"] = (
            "transactions"
        )
        config["federation_metrics_domains"] = [DEST]
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        cl = hs.get_federation_http_client()
        self.transport = FakeTransport()
        rust_client = RustFederationClient(
            hs,
            hs.version_string,
            hs.version_string,
            cl._write_retry_timings,
            cl._record_request_metrics,
            self.transport,
        )

        self.manager = TransactionManager(hs)
        assert self.manager._rust is not None
        self.manager._rust = RustTransactionManager(rust_client)

        user = self.register_user("user", "pass")
        tok = self.login("user", "pass")
        room_id = self.helper.create_room_as(user, tok=tok)
        self.helper.send(room_id, "hello", tok=tok)

        store = hs.get_datastores().main
        event_ids = self.get_success(store.get_latest_event_ids_in_room(room_id))
        self.event = self.get_success(store.get_event(next(iter(event_ids))))

    def _send(self, pdus: list[EventBase], edus: list[Edu] | None = None) -> None:
        self.get_success(self.manager.send_new_transaction(DEST, pdus, edus or []))

    def test_send(self) -> None:
        """The transaction is PUT to `/send/<txn_id>` with the body Python
        builds."""
        self.transport.responses.append(json_response(200, {"pdus": {}}))
        edu = Edu(
            edu_type="m.typing",
            content={"room_id": "!r:test", "user_id": "@u:test", "typing": True},
            origin=self.hs.hostname,
            destination=DEST,
        )

        self._send([self.event], [edu])

        [request] = self.transport.requests
        self.assertEqual(request.method, "PUT")
        self.assertRegex(request.path, r"^/_matrix/federation/v1/send/\d+$")
        self.assertIsNotNone(request.header(b"Authorization"))

        body = json.loads(request.body)
        self.assertEqual(body["origin"], self.hs.hostname)
        self.assertEqual(body["edus"], [edu.get_dict()])
        self.assertEqual(body["pdus"], [self.event.get_pdu_json()])

    def test_txn_ids_increase(self) -> None:
        self.transport.responses.extend([json_response(200, {})] * 2)
        self._send([self.event])
        self._send([self.event])

        first, second = (
            int(request.path.rsplit("/", 1)[1]) for request in self.transport.requests
        )
        self.assertEqual(second, first + 1)

    def test_http_error(self) -> None:
        """A 4xx is raised as Python raises it, and backs the destination
        off."""
        self.transport.responses.append(
            json_response(403, {"errcode": "M_FORBIDDEN", "error": "no"})
        )

        f = self.get_failure(
            self.manager.send_new_transaction(DEST, [self.event], []),
            HttpResponseException,
        )
        self.assertEqual(f.value.code, 403)

        # Let the dispatched write of the backoff run.
        self.reactor.advance(0)
        timings = self.get_success(
            self.hs.get_datastores().main.get_destination_retry_timings(DEST)
        )
        self.assertIsNotNone(timings)

    def test_invalid_response(self) -> None:
        self.transport.responses.append(json_response(200, ["not", "a", "dict"]))
        self.get_failure(
            self.manager.send_new_transaction(DEST, [self.event], []),
            RequestSendFailed,
        )

    def test_module_callback(self) -> None:
        """Modules hear of the PDUs the remote accepted, and of no others."""
        callback = AsyncMock(return_value=None)
        self.hs.get_module_api_callbacks().federation.register_callbacks(
            on_event_delivered_over_federation=callback
        )
        self.transport.responses.append(
            json_response(
                200,
                {"pdus": {self.event.event_id: {}, "$other": {"error": "bad"}}},
            )
        )

        self._send([self.event])

        callback.assert_awaited_once_with(
            FederationEventDeliveryEvent(
                server_name=DEST,
                events=[self.event],
                method=FederatedEventDeliveryMethod.SEND,
            )
        )

    def test_module_callback_rejected(self) -> None:
        callback = AsyncMock(return_value=None)
        self.hs.get_module_api_callbacks().federation.register_callbacks(
            on_event_delivered_over_federation=callback
        )
        self.transport.responses.append(
            json_response(200, {"pdus": {self.event.event_id: {"error": "bad"}}})
        )

        self._send([self.event])

        # No PDU was accepted, so there is nothing to tell the module.
        callback.assert_not_awaited()

    def test_python_decoder_fallback(self) -> None:
        """A body serde_json refuses, but Python's decoder accepts, still
        reaches the module callback."""
        callback = AsyncMock(return_value=None)
        self.hs.get_module_api_callbacks().federation.register_callbacks(
            on_event_delivered_over_federation=callback
        )
        body = (
            '{"pdus": {"%s": {}, "$x": {"error": "\\ud800"}}}' % self.event.event_id
        ).encode()
        self.transport.responses.append(
            (200, [(b"content-type", b"application/json")], body)
        )

        self._send([self.event])

        callback.assert_awaited_once_with(
            FederationEventDeliveryEvent(
                server_name=DEST,
                events=[self.event],
                method=FederatedEventDeliveryMethod.SEND,
            )
        )

    def test_last_sent_pdu_time(self) -> None:
        self.transport.responses.append(json_response(200, {}))
        self._send([self.event])

        gauge = last_pdu_ts_metric.labels(
            destination_server_name=DEST, **{SERVER_NAME_LABEL: self.hs.hostname}
        )
        self.assertEqual(
            gauge._value.get(),
            self.event.origin_server_ts / 1000,
        )

    def test_to_self(self) -> None:
        self.get_failure(
            self.manager.send_new_transaction(self.hs.hostname, [self.event], []),
            RuntimeError,
        )
        self.assertEqual(self.transport.requests, [])

    def test_gil_crossings(self) -> None:
        """Below the facade, a transaction takes the GIL once to hand back the
        result, plus once for the test-only body check."""
        self.transport.responses.append(json_response(200, {}))
        before = federation_gil_stats()

        self._send([self.event])

        after = federation_gil_stats()
        calls = {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if after[site][0] != before.get(site, (0, 0.0))[0]
        }
        self.assertEqual(calls, {"transaction_check": 1, "transaction_response": 1})


class RustTransactionsLevelTestCase(unittest.HomeserverTestCase):
    """Which transaction sender each level gets."""

    def _manager(self) -> TransactionManager:
        return TransactionManager(self.hs)

    @unittest.override_config(
        {"experimental_features": {"rust_federation_sending": "client"}}
    )
    def test_client_level(self) -> None:
        self.assertIsNone(self._manager()._rust)

    @unittest.override_config(
        {"experimental_features": {"rust_federation_sending": "transactions"}}
    )
    def test_transactions_level(self) -> None:
        self.assertIsNotNone(self._manager()._rust)

    @unittest.override_config(
        {
            "experimental_features": {"rust_federation_sending": "transactions"},
            "instance_map": {
                "main": {"host": "testserv", "port": 8765},
                "federation_sender": {"host": "testserv", "port": 1001},
            },
            "outbound_federation_restricted_to": ["federation_sender"],
            "worker_replication_secret": "secret",
        }
    )
    def test_proxy_worker(self) -> None:
        """An instance that sends through a proxy worker has no Rust client,
        so it builds transactions in Python."""
        self.assertIsNone(self._manager()._rust)
