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
"""The per-destination queue at `rust_federation_sending: queues`.

The queue and its transmission loop are Rust's. `RustPerDestinationQueue` has
the methods of `PerDestinationQueue` that `FederationSender` calls, and
forwards them. The loop calls back into it for the parts still in Python:
catch-up, the sticky event backlog, and the database work around each
transaction. Those are the `_rust_*` coroutines.
"""

import logging
from typing import TYPE_CHECKING, Hashable, Iterable

from synapse.api.presence import UserPresenceState
from synapse.events import EventBase
from synapse.federation.sender.per_destination_queue import (
    StickyEventBacklogTracker,
    _CatchUpLoop,
    _StickyEventsTransactionInfo,
    sent_edus_by_type,
    sent_edus_counter,
)
from synapse.federation.sender.transaction_manager import last_pdu_ts_metric
from synapse.federation.units import Edu
from synapse.logging import issue9533_logger
from synapse.logging.opentracing import whitelisted_homeserver
from synapse.metrics import SERVER_NAME_LABEL, sent_transactions_counter
from synapse.metrics.background_process_metrics import BackgroundProcessLoggingContext
from synapse.module_api.callbacks.federation import FederatedEventDeliveryMethod
from synapse.synapse_rust.federation import (
    RustDestinationQueue,
    RustDestinationQueues,
)
from synapse.types import JsonDict, ReadReceipt

if TYPE_CHECKING:
    import synapse.server
    from synapse.federation.sender import TransactionManager

logger = logging.getLogger(__name__)


class _RustStickyBacklog:
    """`StickyEventBacklogTracker`'s flag, which the Rust queue holds."""

    def __init__(self, rust: RustDestinationQueue) -> None:
        self._rust = rust

    @property
    def is_backlogged(self) -> bool:
        return self._rust.sticky_backlogged

    @property
    def _backlogged(self) -> bool:
        return self._rust.sticky_backlogged

    @_backlogged.setter
    def _backlogged(self, backlogged: bool) -> None:
        self._rust.sticky_backlogged = backlogged

    def notify_potential_new_backlog(self) -> None:
        self._rust.sticky_backlogged = True


class RustPerDestinationQueue(_CatchUpLoop):
    """`PerDestinationQueue`, with the queue and the loop in Rust."""

    def __init__(
        self,
        hs: "synapse.server.HomeServer",
        transaction_manager: "TransactionManager",
        destination: str,
        rust_queues: RustDestinationQueues,
    ):
        self.server_name = hs.hostname
        self._hs = hs
        self._storage_controllers = hs.get_storage_controllers()
        self._store = hs.get_datastores().main
        self._transaction_manager = transaction_manager
        self._destination = destination
        self._federation_callbacks = hs.get_module_api_callbacks().federation
        self._federation_metrics_domains = (
            hs.config.federation.federation_metrics_domains
        )
        self._record_request_metrics = (
            hs.get_federation_http_client()._record_request_metrics
        )
        # Its database half. The Rust queue holds its flag.
        self._sticky_backlog_store = StickyEventBacklogTracker(
            destination, hs, hs.config.experimental.msc4354_enabled
        )

        should_send_on_this_instance = (
            hs.config.worker.federation_shard_config.should_handle(
                hs.get_instance_name(), destination
            )
        )
        self._rust = rust_queues.queue(destination, self, should_send_on_this_instance)
        self._sticky_event_backlog_tracker = _RustStickyBacklog(self._rust)

    def __str__(self) -> str:
        return "RustPerDestinationQueue[%s]" % self._destination

    def shutdown(self) -> None:
        """Instruct the queue to stop processing any further requests"""
        self._transaction_manager.shutdown()
        self._rust.shutdown()

    def pending_pdu_count(self) -> int:
        return self._rust.pending_pdu_count()

    def pending_edu_count(self) -> int:
        return self._rust.pending_edu_count()

    @property
    def transmission_loop_running(self) -> bool:
        return self._rust.transmission_loop_running

    @property
    def _new_data_to_send(self) -> bool:
        return self._rust.new_data_to_send

    def send_pdu(self, pdu: EventBase) -> None:
        self._rust.send_pdu(pdu)

    def send_presence(
        self, states: Iterable[UserPresenceState], start_loop: bool = True
    ) -> None:
        self._rust.send_presence(list(states), start_loop)

    def queue_read_receipt(self, receipt: ReadReceipt) -> None:
        self._rust.queue_read_receipt(receipt)

    def send_keyed_edu(self, edu: Edu, key: Hashable) -> None:
        self._strip_context(edu)
        self._rust.send_keyed_edu(edu, repr(key))

    def send_edu(self, edu: Edu) -> None:
        self._strip_context(edu)
        self._rust.send_edu(edu)

    def mark_new_data(self) -> None:
        self._rust.mark_new_data()

    def attempt_new_transaction(self) -> None:
        self._rust.attempt_new_transaction()

    def _strip_context(self, edu: Edu) -> None:
        # As `send_new_transaction` does. The `send_transaction` span the
        # contexts would have followed from is not made at this level.
        if whitelisted_homeserver(self._destination):
            edu.strip_context()

    # The catch-up state, which `_catch_up_transmission_loop` reads and
    # writes.

    @property
    def _catching_up(self) -> bool:
        return self._rust.catching_up

    @_catching_up.setter
    def _catching_up(self, catching_up: bool) -> None:
        self._rust.catching_up = catching_up

    @property
    def _catchup_last_skipped(self) -> int:
        return self._rust.catchup_last_skipped

    @_catchup_last_skipped.setter
    def _catchup_last_skipped(self, stream_ordering: int) -> None:
        self._rust.catchup_last_skipped = stream_ordering

    @property
    def _last_successful_stream_ordering(self) -> int | None:
        return self._rust.last_successful_stream_ordering

    @_last_successful_stream_ordering.setter
    def _last_successful_stream_ordering(self, stream_ordering: int | None) -> None:
        self._rust.last_successful_stream_ordering = stream_ordering

    @property
    def _transmission_loop_enabled(self) -> bool:  # type: ignore[override]
        return self._rust.transmission_loop_enabled

    def _start_catching_up(self) -> None:
        self._rust.start_catching_up()

    # The hooks the Rust loop calls. Each runs in a logcontext of its own, as
    # the Python loop runs as a background process.

    def _logcontext(self, name: str) -> BackgroundProcessLoggingContext:
        return BackgroundProcessLoggingContext(name=name, server_name=self.server_name)

    async def _rust_catch_up(self) -> None:
        with self._logcontext("federation_catch_up_transmission_loop"):
            await self._catch_up_transmission_loop()

    async def _rust_device_edus(
        self,
        last_device_stream_id: int,
        last_device_list_stream_id: int,
        to_device_limit: int,
        edu_limit: int,
    ) -> tuple[list[JsonDict], int, list[tuple[str, JsonDict]], int]:
        """The to-device messages and device list updates for the next
        transaction, and how far through each stream they go."""
        with self._logcontext("federation_queue_device_edus"):
            to_device_stream_id = self._store.get_to_device_stream_token()
            contents, stream_id = await self._store.get_new_device_msgs_for_remote(
                self._destination,
                last_device_stream_id,
                to_device_stream_id,
                to_device_limit,
            )
            if contents:
                issue9533_logger.debug(
                    "Sending %i to-device messages to %s, up to stream id %i",
                    len(contents),
                    self._destination,
                    stream_id,
                )

            device_list_limit = edu_limit - len(contents)
            (
                device_list_stream_id,
                results,
            ) = await self._store.get_device_updates_by_remote(
                self._destination, last_device_list_stream_id, limit=device_list_limit
            )
            assert len(results) <= device_list_limit, (
                "get_device_updates_by_remote returned too many EDUs"
            )
            return contents, stream_id, list(results), device_list_stream_id

    async def _rust_sticky_backlog(
        self,
    ) -> tuple[str, _StickyEventsTransactionInfo, list[EventBase]] | None:
        with self._logcontext("federation_queue_sticky_backlog"):
            loaded = await self._sticky_backlog_store.load_backlog()
        if loaded is None:
            return None
        info, events = loaded
        return info.room_id.to_string(), info, events

    async def _rust_complete(
        self,
        to_device_stream_id: int | None,
        device_list_stream_id: int | None,
        last_stream_ordering: int | None,
        sticky: tuple[str, _StickyEventsTransactionInfo] | None,
        sent: tuple[list[EventBase], list[str], list[str], int, list[int]] | None,
    ) -> None:
        """Record a finished transaction: what `send_new_transaction` does
        after the response, the counters, and `_complete_transaction`."""
        with self._logcontext("federation_queue_complete"):
            if sent is not None:
                await self._record_sent(*sent)

            if to_device_stream_id:
                await self._store.delete_device_msgs_for_remote(
                    self._destination, to_device_stream_id
                )
            if device_list_stream_id:
                await self._store.mark_as_sent_devices_by_remote(
                    self._destination, device_list_stream_id
                )
            if last_stream_ordering:
                await self._store.set_destination_last_successful_stream_ordering(
                    self._destination, last_stream_ordering
                )
            if sticky is not None:
                await self._sticky_backlog_store.complete_transaction(sticky[1])

    async def _record_sent(
        self,
        pdus: list[EventBase],
        edu_types: list[str],
        acknowledged: list[str],
        attempts: int,
        responses: list[int],
    ) -> None:
        self._record_request_metrics("PUT", attempts, responses)

        if self._federation_callbacks.interested_in_events_delivered_over_federation():
            acknowledged_ids = set(acknowledged)
            await self._federation_callbacks.notify_on_event_delivered_over_federation(
                self._destination,
                [p for p in pdus if p.event_id in acknowledged_ids],
                FederatedEventDeliveryMethod.SEND,
            )

        if pdus and self._destination in self._federation_metrics_domains:
            last_pdu_ts_metric.labels(
                destination_server_name=self._destination,
                **{SERVER_NAME_LABEL: self.server_name},
            ).set(pdus[-1].origin_server_ts / 1000)

        sent_transactions_counter.labels(**{SERVER_NAME_LABEL: self.server_name}).inc()
        sent_edus_counter.labels(**{SERVER_NAME_LABEL: self.server_name}).inc(
            len(edu_types)
        )
        for edu_type in edu_types:
            sent_edus_by_type.labels(
                type=edu_type, **{SERVER_NAME_LABEL: self.server_name}
            ).inc()
