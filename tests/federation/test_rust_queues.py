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
"""Tests for the per-destination queues at `rust_federation_sending: queues`.

The sender, catch-up and sticky event tests also run at this level, through
`RustQueuesMixin`, with their mocked `send_transaction`. These tests send
through the Rust request layer instead, with a Python callable in place of the
network, as `tests.http.test_rust_federation_client` does.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

from synapse.api.presence import UserPresenceState
from synapse.federation.sender import FederationSender
from synapse.federation.sender.per_destination_queue import PerDestinationQueue
from synapse.federation.sender.rust_queue import RustPerDestinationQueue
from synapse.federation.units import Edu
from synapse.metrics import SERVER_NAME_LABEL, sent_transactions_counter
from synapse.module_api.callbacks.federation import (
    FederatedEventDeliveryMethod,
    FederationEventDeliveryEvent,
)
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.synapse_rust.federation import (
    RustDestinationQueues,
    RustFederationClient,
    RustTransactionManager,
    federation_gil_stats,
)
from synapse.util.clock import Clock

from tests import unittest
from tests.federation._rust_transactions import wait_for_rust_queues
from tests.http.test_rust_federation_client import (
    DEST,
    FakeTransport,
    json_response,
)


class RustQueuesTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_federation_sending"] = (
            "queues"
        )
        config["federation_sender_instances"] = None
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

        sender = hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        self.sender = sender
        self.queues = RustDestinationQueues(RustTransactionManager(rust_client), False)
        sender._rust_queues = self.queues

        user = self.register_user("user", "pass")
        tok = self.login("user", "pass")
        room_id = self.helper.create_room_as(user, tok=tok)
        self.helper.send(room_id, "hello", tok=tok)

        self.store = hs.get_datastores().main
        event_ids = self.get_success(self.store.get_latest_event_ids_in_room(room_id))
        self.event = self.get_success(self.store.get_event(next(iter(event_ids))))

        queue = sender._get_per_destination_queue(DEST)
        assert isinstance(queue, RustPerDestinationQueue)
        self.queue = queue

    def _settle(self) -> None:
        wait_for_rust_queues(self.reactor, self.queues)

    def _sent_transactions(self) -> float:
        return sent_transactions_counter.labels(
            **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def test_send_pdu(self) -> None:
        """The queue sends the PDU through the Rust client and records it."""
        callback = AsyncMock(return_value=None)
        self.hs.get_module_api_callbacks().federation.register_callbacks(
            on_event_delivered_over_federation=callback
        )
        self.transport.responses.append(
            json_response(200, {"pdus": {self.event.event_id: {}}})
        )
        sent_before = self._sent_transactions()

        self.queue.send_pdu(self.event)
        self._settle()

        [request] = self.transport.requests
        self.assertEqual(request.method, "PUT")
        self.assertRegex(request.path, r"^/_matrix/federation/v1/send/\d+$")
        body = json.loads(request.body)
        self.assertEqual(body["pdus"], [self.event.get_pdu_json()])
        self.assertNotIn("edus", body)

        self.assertEqual(self.queue.pending_pdu_count(), 0)
        self.assertFalse(self.queue.transmission_loop_running)
        self.assertEqual(
            self.get_success(
                self.store.get_destination_last_successful_stream_ordering(DEST)
            ),
            self.event.internal_metadata.stream_ordering,
        )
        callback.assert_awaited_once_with(
            FederationEventDeliveryEvent(
                server_name=DEST,
                events=[self.event],
                method=FederatedEventDeliveryMethod.SEND,
            )
        )
        self.assertEqual(self._sent_transactions(), sent_before + 1)

    def test_edus(self) -> None:
        """Keyed EDUs replace each other, and go with plain ones."""
        self.transport.responses.append(json_response(200, {}))

        def typing(room_id: str, typing: bool) -> Edu:
            return Edu(
                edu_type="m.typing",
                content={"room_id": room_id, "user_id": "@u:test", "typing": typing},
                origin=self.hs.hostname,
                destination=DEST,
            )

        other = Edu(
            edu_type="m.other", content={}, origin=self.hs.hostname, destination=DEST
        )
        # The loop cannot get past its backoff check until the reactor runs,
        # so these all go in the first transaction.
        self.queue.send_edu(other)
        self.queue.send_keyed_edu(typing("!a", True), ("!a",))
        self.queue.send_keyed_edu(typing("!a", False), ("!a",))
        self._settle()

        [request] = self.transport.requests
        body = json.loads(request.body)
        self.assertEqual(
            body["edus"], [other.get_dict(), typing("!a", False).get_dict()]
        )

    def test_failed_send_keeps_pdu(self) -> None:
        """An error response leaves the PDU queued and backs off."""
        self.transport.responses.append(
            json_response(403, {"errcode": "M_FORBIDDEN", "error": "no"})
        )

        self.queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.queue._rust.pending_pdu_ids(), [self.event.event_id])
        timings = self.get_success(self.store.get_destination_retry_timings(DEST))
        self.assertIsNotNone(timings)

    def test_long_backoff_catches_up(self) -> None:
        """Backing off for over an hour drops the volatile EDUs and the PDUs,
        and leaves catch-up to send the PDUs later."""
        now = self.clock.time_msec()
        self.get_success(
            self.store.set_destination_retry_timings(DEST, now, now, 2 * 60 * 60 * 1000)
        )
        self.queue.send_presence(
            [UserPresenceState.default("@u:test")], start_loop=False
        )
        self.assertEqual(self.queue.pending_edu_count(), 1)
        self.queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(self.transport.requests, [])
        self.assertTrue(self.queue._catching_up)
        self.assertEqual(self.queue.pending_pdu_count(), 0)
        self.assertEqual(self.queue.pending_edu_count(), 0)

    def test_gil_crossings(self) -> None:
        """Once caught up, a transaction takes the GIL twice in the loop: for
        its device EDUs and to record it. The loop then looks for more to
        send, which asks for device EDUs again, as Python's does."""
        self.transport.responses.extend([json_response(200, {})] * 2)
        self.queue.send_pdu(self.event)
        self._settle()

        before = federation_gil_stats()
        self.queue.send_pdu(self.event)
        self._settle()
        after = federation_gil_stats()

        calls = {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if after[site][0] != before.get(site, (0, 0.0))[0]
        }
        queue_calls = {
            site: count for site, count in calls.items() if site.startswith("queue_")
        }
        self.assertEqual(queue_calls, {"queue_device_edus": 2, "queue_complete": 1})

    def test_shutdown(self) -> None:
        self.sender.shutdown()
        self.queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.queue.transmission_loop_running)


class RustQueuesLevelTestCase(unittest.HomeserverTestCase):
    """Which queue each level gets."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["federation_sender_instances"] = None
        return config

    def _queue(self) -> object:
        sender = self.hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        return sender._get_per_destination_queue(DEST)

    @unittest.override_config(
        {"experimental_features": {"rust_federation_sending": "transactions"}}
    )
    def test_transactions_level(self) -> None:
        self.assertIsInstance(self._queue(), PerDestinationQueue)

    @unittest.override_config(
        {"experimental_features": {"rust_federation_sending": "queues"}}
    )
    def test_queues_level(self) -> None:
        self.assertIsInstance(self._queue(), RustPerDestinationQueue)

    @unittest.override_config(
        {
            "experimental_features": {"rust_federation_sending": "queues"},
            "instance_map": {
                "main": {"host": "testserv", "port": 8765},
                "federation_sender": {"host": "testserv", "port": 1001},
            },
            "outbound_federation_restricted_to": ["federation_sender"],
            "worker_replication_secret": "secret",
        }
    )
    def test_proxy_worker(self) -> None:
        """Without the Rust transaction sender, the queues are Python's."""
        self.assertIsInstance(self._queue(), PerDestinationQueue)
