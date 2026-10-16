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
from unittest import SkipTest
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

from synapse.api.presence import UserPresenceState
from synapse.events import EventBase
from synapse.federation.sender import FederationSender
from synapse.federation.sender.per_destination_queue import PerDestinationQueue
from synapse.federation.sender.rust_queue import (
    RustPerDestinationQueue,
    use_native_store,
)
from synapse.federation.units import Edu
from synapse.metrics import REGISTRY, SERVER_NAME_LABEL, sent_transactions_counter
from synapse.module_api.callbacks.federation import (
    FederatedEventDeliveryMethod,
    FederationEventDeliveryEvent,
)
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.storage.rust_pool import RustConnectionPool
from synapse.synapse_rust.federation import (
    RustDestinationQueues,
    RustFederationClient,
    RustTransactionManager,
    federation_gil_stats,
)
from synapse.util.clock import Clock
from synapse.util.duration import Duration

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

        # Modules register their callbacks before the queues are made, which
        # read whether there are any.
        self.delivered = AsyncMock(return_value=None)
        hs.get_module_api_callbacks().federation.register_callbacks(
            on_event_delivered_over_federation=self.delivered
        )

        sender = hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        self.sender = sender
        self.queues = self._make_queues(RustTransactionManager(rust_client))
        sender._rust_queues = self.queues

        user = self.register_user("user", "pass")
        tok = self.login("user", "pass")
        room_id = self.helper.create_room_as(user, tok=tok)
        self.room_id = room_id
        self.tok = tok

        self.store = hs.get_datastores().main
        self.event = self._send_event("hello")

        queue = sender._get_per_destination_queue(DEST)
        assert isinstance(queue, RustPerDestinationQueue)
        self.queue = queue

    def _make_queues(
        self, transactions: RustTransactionManager
    ) -> RustDestinationQueues:
        return RustDestinationQueues(
            transactions, self.hs.config.experimental.msc4354_enabled, {DEST}
        )

    def _send_event(self, body: str) -> EventBase:
        event_id = self.helper.send(self.room_id, body, tok=self.tok)["event_id"]
        return self.get_success(self.store.get_event(event_id))

    def _settle(self) -> None:
        wait_for_rust_queues(self.reactor, self.queues)

    def _queue_calls(self, before: dict[str, tuple[int, float]]) -> dict[str, int]:
        """The GIL crossings of the queues since `before`."""
        after = federation_gil_stats()
        return {
            site: after[site][0] - before.get(site, (0, 0.0))[0]
            for site in after
            if site.startswith("queue_")
            and after[site][0] != before.get(site, (0, 0.0))[0]
        }

    def _sent_transactions(self) -> float:
        self.sender._flush_rust_queue_metrics()
        return sent_transactions_counter.labels(
            **{SERVER_NAME_LABEL: self.hs.hostname}
        )._value.get()

    def test_send_pdu(self) -> None:
        """The queue sends the PDU through the Rust client and records it."""
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
        self.delivered.assert_awaited_once_with(
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

        self.assertEqual(
            self._queue_calls(before), {"queue_device_edus": 2, "queue_complete": 1}
        )

    def test_shutdown(self) -> None:
        self.sender.shutdown()
        self.queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.queue.transmission_loop_running)


class RustQueuesNativeTestCase(RustQueuesTestCase):
    """The same, with the database work done through the native driver, and
    sticky events on, so the loops look at the sticky event backlog."""

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["experimental_features"]["msc4354_enabled"] = True
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        return hs

    def _make_queues(
        self, transactions: RustTransactionManager
    ) -> RustDestinationQueues:
        queues = super()._make_queues(transactions)
        self.assertTrue(use_native_store(self.hs, queues))
        return queues

    def _catch_up(self) -> None:
        """Send a first transaction, which leaves catch-up and reads the
        device streams for the first time."""
        self.transport.responses.append(json_response(200, {}))
        self.queue.send_pdu(self.event)
        self._settle()
        self.transport.requests.clear()

    def test_gil_crossings(self) -> None:
        """Once caught up, a transaction with no device updates takes the GIL
        only to tell the module about the delivered event."""
        self._catch_up()
        self.transport.responses.append(
            json_response(200, {"pdus": {self.event.event_id: {}}})
        )

        before = federation_gil_stats()
        self.queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self._queue_calls(before), {"queue_complete": 1})

    def test_gil_crossings_without_modules(self) -> None:
        """With no module to tell, it takes the GIL nowhere in the loop."""
        self.hs.get_module_api_callbacks().federation._on_event_delivered_over_federation_callbacks.clear()
        queue = self.sender._get_per_destination_queue("other.example.com")
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.extend([json_response(200, {})] * 2)
        queue.send_pdu(self.event)
        self._settle()
        self.transport.requests.clear()

        before = federation_gil_stats()
        queue.send_pdu(self.event)
        self._settle()

        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self._queue_calls(before), {})

    def _catch_up_destination(self, destination: str, missed: EventBase) -> None:
        """Make `destination` one we sent nothing to since before `missed`."""
        stream_ordering = missed.internal_metadata.stream_ordering
        assert stream_ordering is not None
        self.get_success(
            self.store.set_destination_last_successful_stream_ordering(
                destination, stream_ordering - 1
            )
        )
        self.get_success(
            self.store.store_destination_rooms_entries(
                [destination], missed.room_id, stream_ordering
            )
        )

    def test_catch_up(self) -> None:
        """Catch-up runs in Rust: it sends the room's latest event in place of
        the one the destination missed, and records how far it got, without
        taking the GIL."""
        self.hs.get_module_api_callbacks().federation._on_event_delivered_over_federation_callbacks.clear()
        destination = "catchup.example.com"
        self._catch_up_destination(destination, self.event)
        latest = self._send_event("newer")
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.append(json_response(200, {}))

        before = federation_gil_stats()
        queue.attempt_new_transaction()
        self._settle()

        [request] = self.transport.requests
        body = json.loads(request.body)
        self.assertEqual(
            [pdu["content"].get("body") for pdu in body["pdus"]],
            [latest.content["body"]],
        )
        self.assertEqual(
            self.get_success(
                self.store.get_destination_last_successful_stream_ordering(destination)
            ),
            self.event.internal_metadata.stream_ordering,
        )
        self.assertFalse(queue._catching_up)
        self.assertNotIn("queue_catch_up", self._queue_calls(before))

    def test_catch_up_falls_back(self) -> None:
        """With history visible to members only, the destination, which has
        no members, may not see the room's latest events, so catch-up sends
        the event it missed."""
        destination = "catchup.example.com"
        self._catch_up_destination(destination, self.event)
        self.helper.send_state(
            self.room_id,
            "m.room.history_visibility",
            {"history_visibility": "joined"},
            tok=self.tok,
        )
        self._send_event("newer")
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.append(json_response(200, {}))

        queue.attempt_new_transaction()
        self._settle()

        [request] = self.transport.requests
        body = json.loads(request.body)
        self.assertEqual(
            [pdu["content"].get("body") for pdu in body["pdus"]],
            [self.event.content["body"]],
        )

    def test_catch_up_notifies_module(self) -> None:
        """The module hears of the events catch-up sent."""
        destination = "catchup.example.com"
        self._catch_up_destination(destination, self.event)
        latest = self._send_event("newer")
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.append(
            json_response(200, {"pdus": {latest.event_id: {}}})
        )

        queue.attempt_new_transaction()
        self._settle()

        # The module gets a new copy of the event, so compare by ID.
        self.delivered.assert_awaited_once()
        assert self.delivered.await_args is not None
        [delivery] = self.delivered.await_args.args
        self.assertEqual(delivery.server_name, destination)
        self.assertEqual([e.event_id for e in delivery.events], [latest.event_id])
        self.assertEqual(delivery.method, FederatedEventDeliveryMethod.SEND)

    def test_catch_up_send_fails(self) -> None:
        """A failed catch-up send records nothing, and leaves the destination
        catching up."""
        destination = "catchup.example.com"
        self._catch_up_destination(destination, self.event)
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.append(
            json_response(403, {"errcode": "M_FORBIDDEN", "error": "no"})
        )

        queue.attempt_new_transaction()
        self._settle()

        self.assertEqual(len(self.transport.requests), 1)
        stream_ordering = self.event.internal_metadata.stream_ordering
        assert stream_ordering is not None
        self.assertEqual(
            self.get_success(
                self.store.get_destination_last_successful_stream_ordering(destination)
            ),
            stream_ordering - 1,
        )
        self.assertTrue(queue._catching_up)
        self.delivered.assert_not_awaited()

    def _backlog_sticky(self, destination: str, body: str) -> EventBase:
        """Leave `destination` to catch up past a sticky event, which is
        returned, and a later plain one."""
        self._catch_up_destination(destination, self.event)
        event_id = self.helper.send_sticky_event(
            self.room_id,
            "m.room.message",
            duration=Duration(minutes=10),
            content={"body": body, "msgtype": "m.text"},
            tok=self.tok,
        )["event_id"]
        latest = self._send_event("latest")
        stream_ordering = latest.internal_metadata.stream_ordering
        assert stream_ordering is not None
        self.get_success(
            self.store.store_destination_rooms_entries(
                [destination], self.room_id, stream_ordering
            )
        )
        return self.get_success(self.store.get_event(event_id))

    def _backlog_rows(self, destination: str) -> list[tuple[str]]:
        return self.get_success(
            self.store.db_pool.simple_select_list(
                "destination_rooms_sticky_events_backlog",
                {"destination": destination},
                ["room_id"],
            )
        )

    def _sent_bodies(self) -> list[list[str]]:
        return [
            [pdu["content"].get("body") for pdu in json.loads(request.body)["pdus"]]
            for request in self.transport.requests
        ]

    def test_sticky_backlog_filtered_out(self) -> None:
        """A sticky batch the destination may not see is not sent, and the
        backlog still moves on, in Rust."""
        self.hs.get_module_api_callbacks().federation._on_event_delivered_over_federation_callbacks.clear()
        destination = "sticky.example.com"
        self._backlog_sticky(destination, "sticky")
        # The sender's events are hidden once they are erased.
        self.get_success(self.store.mark_user_erased(self.event.sender))
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.extend([json_response(200, {})] * 2)

        before = federation_gil_stats()
        queue.attempt_new_transaction()
        self._settle()

        # Catch-up sends the event the destination missed, which is the
        # room's latest, unfiltered. The sticky event is not sent.
        self.assertEqual(self._sent_bodies(), [["latest"]])
        self.assertEqual(self._backlog_rows(destination), [])
        self.assertFalse(queue._rust.sticky_backlogged)
        calls = self._queue_calls(before)
        self.assertNotIn("queue_sticky_backlog", calls)
        self.assertNotIn("queue_complete", calls)

    def _set_partial_state(self, event: EventBase) -> None:
        """Mark `event`, and so its room, as having partial state."""
        self.get_success(
            self.store.db_pool.simple_insert(
                "partial_state_rooms", {"room_id": self.room_id}
            )
        )
        self.store.is_partial_state_room.invalidate((self.room_id,))
        self.get_success(
            self.store.db_pool.simple_insert(
                "partial_state_events",
                {"room_id": self.room_id, "event_id": event.event_id},
            )
        )
        self.store.is_partial_state_event.invalidate((event.event_id,))

    def test_sticky_backlog_needs_full_state(self) -> None:
        """A sticky batch that needs the full state to filter goes to the
        Python hook, which waits for it."""
        self.hs.get_module_api_callbacks().federation._on_event_delivered_over_federation_callbacks.clear()
        destination = "sticky.example.com"
        self.helper.send_state(
            self.room_id,
            "m.room.history_visibility",
            {"history_visibility": "joined"},
            tok=self.tok,
        )
        sticky = self._backlog_sticky(destination, "sticky")
        self._set_partial_state(sticky)
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.extend([json_response(200, {})] * 2)

        before = federation_gil_stats()
        queue.attempt_new_transaction()
        self._settle()
        # Catch-up sent the event the destination missed. The sticky batch
        # waits in Python for the full state.
        self.assertEqual(self._sent_bodies(), [["latest"]])
        self.assertIn("queue_sticky_backlog", self._queue_calls(before))
        self.assertNotEqual(self._backlog_rows(destination), [])

        self.get_success(
            self.store.db_pool.simple_delete_one(
                "partial_state_events", {"event_id": sticky.event_id}
            )
        )
        self.get_success(
            self.store.db_pool.simple_delete_one(
                "partial_state_rooms", {"room_id": self.room_id}
            )
        )
        self.store.is_partial_state_room.invalidate((self.room_id,))
        self.store.is_partial_state_event.invalidate((sticky.event_id,))
        self.hs.get_storage_controllers().state.notify_event_un_partial_stated(
            sticky.event_id
        )
        self._settle()

        # The destination has no members, so may not see the sticky event.
        # Python's batch is still recorded as sent.
        self.assertEqual(self._sent_bodies(), [["latest"]])
        self.assertEqual(self._backlog_rows(destination), [])
        self.assertIn("queue_complete", self._queue_calls(before))

    def test_sticky_backlog(self) -> None:
        """Catching up past a sticky event backlogs it, and the loop then
        sends it, all without taking the GIL."""
        self.hs.get_module_api_callbacks().federation._on_event_delivered_over_federation_callbacks.clear()
        destination = "sticky.example.com"
        self._catch_up_destination(destination, self.event)
        self.helper.send_sticky_event(
            self.room_id,
            "m.room.message",
            duration=Duration(minutes=10),
            content={"body": "sticky", "msgtype": "m.text"},
            tok=self.tok,
        )
        latest = self._send_event("latest")
        stream_ordering = latest.internal_metadata.stream_ordering
        assert stream_ordering is not None
        self.get_success(
            self.store.store_destination_rooms_entries(
                [destination], self.room_id, stream_ordering
            )
        )
        queue = self.sender._get_per_destination_queue(destination)
        assert isinstance(queue, RustPerDestinationQueue)
        self.transport.responses.extend([json_response(200, {})] * 2)

        before = federation_gil_stats()
        queue.attempt_new_transaction()
        self._settle()

        bodies = [
            [pdu["content"].get("body") for pdu in json.loads(request.body)["pdus"]]
            for request in self.transport.requests
        ]
        self.assertEqual(bodies, [["latest"], ["sticky"]])
        # The loop looked again, found the backlog sent, and cleared it.
        self.assertEqual(
            self.get_success(
                self.store.db_pool.simple_select_list(
                    "destination_rooms_sticky_events_backlog",
                    {"destination": destination},
                    ["room_id"],
                )
            ),
            [],
        )
        self.assertFalse(queue._rust.sticky_backlogged)
        calls = self._queue_calls(before)
        self.assertNotIn("queue_catch_up", calls)
        self.assertNotIn("queue_sticky_backlog", calls)
        self.assertNotIn("queue_complete", calls)

    def test_to_device(self) -> None:
        """A to-device message is sent, and deleted once sent. Reading the
        stream position is the one GIL crossing."""
        self._catch_up()
        self.transport.responses.append(json_response(200, {}))
        message = {
            "sender": "@user:test",
            "type": "m.test",
            "message_id": "m1",
            "messages": {"@u:remote": {"D": {"body": "hi"}}},
        }

        before = federation_gil_stats()
        self.get_success(
            self.store.add_remote_messages_from_client_to_device_inbox({DEST: message})
        )
        self.queue.attempt_new_transaction()
        self._settle()

        [request] = self.transport.requests
        body = json.loads(request.body)
        self.assertEqual(
            body["edus"], [{"edu_type": "m.direct_to_device", "content": message}]
        )
        self.assertEqual(self._queue_calls(before), {"queue_stream_tokens": 1})
        current = self.store.get_to_device_stream_token()
        self.assertEqual(
            self.get_success(
                self.store.get_new_device_msgs_for_remote(DEST, 0, current, 10)
            ),
            ([], current),
        )

    def test_device_list_update(self) -> None:
        """A device list update is sent, and marked as sent."""
        self._catch_up()
        self.transport.responses.append(json_response(200, {}))
        user = "@user:test"
        self.get_success(self.store.store_device(user, "NEW", None))
        self.get_success(self.store.add_device_change_to_streams(user, ["NEW"], []))
        self.get_success(
            self.store.add_device_list_outbound_pokes(
                user_id=user, device_id="NEW", room_id="!r", hosts=[DEST], context={}
            )
        )

        self.queue.attempt_new_transaction()
        self._settle()

        [request] = self.transport.requests
        [edu] = json.loads(request.body)["edus"]
        self.assertEqual(edu["edu_type"], "m.device_list_update")
        self.assertEqual(edu["content"]["device_id"], "NEW")
        pokes = self.get_success(
            self.store.db_pool.simple_select_list(
                "device_lists_outbound_pokes", {"destination": DEST}, ["stream_id"]
            )
        )
        self.assertEqual(pokes, [])

    def test_metrics(self) -> None:
        """The loops' counts reach the Python metrics in a scrape."""
        self._catch_up()

        def scrape(name: str, **labels: str) -> float | None:
            # Collecting the registry runs the pre-collect hooks first.
            return REGISTRY.get_sample_value(
                name, {SERVER_NAME_LABEL: self.hs.hostname, **labels}
            )

        sent_before = scrape("synapse_federation_client_sent_transactions_total")
        assert sent_before is not None

        # A new event, so the gauge can only hold its time if this send set it.
        self.reactor.advance(10)
        event = self._send_event("again")
        self.assertNotEqual(event.origin_server_ts, self.event.origin_server_ts)
        self.transport.responses.append(json_response(200, {}))
        self.queue.send_pdu(event)
        self._settle()

        self.assertEqual(
            scrape("synapse_federation_client_sent_transactions_total"),
            sent_before + 1,
        )
        self.assertEqual(
            scrape(
                "synapse_federation_last_sent_pdu_time", destination_server_name=DEST
            ),
            event.origin_server_ts / 1000,
        )


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
