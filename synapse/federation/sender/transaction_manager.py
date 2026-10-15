#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
import functools
import logging
from typing import TYPE_CHECKING, Mapping

from prometheus_client import Gauge

from synapse.api.constants import EduTypes
from synapse.api.errors import HttpResponseException
from synapse.events import EventBase
from synapse.federation.persistence import TransactionActions
from synapse.federation.units import Edu, Transaction, serialize_and_filter_pdus
from synapse.logging import opentracing
from synapse.logging.opentracing import (
    extract_text_map,
    set_tag,
    start_active_span_follows_from,
    tags,
    whitelisted_homeserver,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.module_api.callbacks.federation import FederatedEventDeliveryMethod
from synapse.synapse_rust.federation import RustTransaction, RustTransactionManager
from synapse.types import JsonDict
from synapse.util.json import json_decoder
from synapse.util.metrics import measure_func

if TYPE_CHECKING:
    import synapse.server

logger = logging.getLogger(__name__)
issue_8631_logger = logging.getLogger("synapse.8631_debug")

last_pdu_ts_metric = Gauge(
    "synapse_federation_last_sent_pdu_time",
    "The timestamp of the last PDU which was successfully sent to the given domain",
    labelnames=("destination_server_name", SERVER_NAME_LABEL),
)


class TransactionManager:
    """Helper class which handles building and sending transactions

    shared between PerDestinationQueue objects

    At `rust_federation_sending: transactions` and above, Rust builds and sends
    the transactions. This class keeps the tracing span and the module
    callback.
    """

    # For tests: check that each transaction Rust sends has the body Python
    # would have sent.
    _rust_strict = False

    def __init__(self, hs: "synapse.server.HomeServer"):
        self.server_name = hs.hostname  # nb must be called this for @measure_func
        self.clock = hs.get_clock()  # nb must be called this for @measure_func
        self._store = hs.get_datastores().main
        self._transaction_actions = TransactionActions(self._store)
        self._transport_layer = hs.get_federation_transport_client()
        self._federation_callbacks = hs.get_module_api_callbacks().federation

        self._federation_metrics_domains = (
            hs.config.federation.federation_metrics_domains
        )

        # HACK to get unique tx id
        self._next_txn_id = int(self.clock.time_msec())

        self._is_shutdown = False

        # The Rust transaction sender. It sends through the Rust request
        # layer, so there is none on an instance whose federation traffic goes
        # through a proxy worker.
        self._rust: RustTransactionManager | None = None
        self._is_mine_server_name = hs.is_mine_server_name
        if hs.config.experimental.rust_federation_sending_at_least("transactions"):
            rust_client = getattr(hs.get_federation_http_client(), "_rust_client", None)
            if rust_client is not None:
                self._rust = RustTransactionManager(rust_client)

    def shutdown(self) -> None:
        self._is_shutdown = True
        self._transport_layer.shutdown()

    @measure_func("_send_new_transaction")
    async def send_new_transaction(
        self,
        destination: str,
        pdus: list[EventBase],
        edus: list[Edu],
    ) -> None:
        """
        Args:
            destination: The destination to send to (e.g. 'example.org')
            pdus: In-order list of PDUs to send
            edus: List of EDUs to send
        """

        if self._is_shutdown:
            logger.warning(
                "TransactionManager has been shutdown, not sending transaction"
            )
            return

        # Make a transaction-sending opentracing span. This span follows on from
        # all the edus in that transaction. This needs to be done since there is
        # no active span here, so if the edus were not received by the remote the
        # span would have no causality and it would be forgotten.

        span_contexts = []
        keep_destination = whitelisted_homeserver(destination)

        for edu in edus:
            context = edu.get_context()
            if context:
                span_contexts.append(extract_text_map(json_decoder.decode(context)))
            if keep_destination:
                edu.strip_context()

        with start_active_span_follows_from("send_transaction", span_contexts):
            logger.debug("TX [%s] _attempt_new_transaction", destination)

            if self._rust is not None:
                await self._send_rust_transaction(self._rust, destination, pdus, edus)
                return

            txn_id = str(self._next_txn_id)

            logger.debug(
                "TX [%s] {%s} Attempting new transaction (pdus: %d, edus: %d)",
                destination,
                txn_id,
                len(pdus),
                len(edus),
            )

            transaction = Transaction(
                origin_server_ts=int(self.clock.time_msec()),
                transaction_id=txn_id,
                origin=self.server_name,
                destination=destination,
                pdus=serialize_and_filter_pdus(pdus),
                edus=[edu.get_dict() for edu in edus],
            )

            self._next_txn_id += 1

            logger.info(
                "TX [%s] {%s} Sending transaction [%s], (PDUs: %d, EDUs: %d)",
                destination,
                txn_id,
                transaction.transaction_id,
                len(pdus),
                len(edus),
            )
            if issue_8631_logger.isEnabledFor(logging.DEBUG):
                DEVICE_UPDATE_EDUS = {
                    EduTypes.DEVICE_LIST_UPDATE,
                    EduTypes.SIGNING_KEY_UPDATE,
                }
                device_list_updates = [
                    edu.content for edu in edus if edu.edu_type in DEVICE_UPDATE_EDUS
                ]
                if device_list_updates:
                    issue_8631_logger.debug(
                        "about to send txn [%s] including device list updates: %s",
                        transaction.transaction_id,
                        device_list_updates,
                    )

            # Actually send the transaction

            # FIXME (erikj): This is a bit of a hack to make the Pdu age
            # keys work
            # FIXME (richardv): I also believe it no longer works. We (now?) store
            #  "age_ts" in "unsigned" rather than at the top level. See
            #  https://github.com/matrix-org/synapse/issues/8429.
            def json_data_cb() -> JsonDict:
                data = transaction.get_dict()
                now = int(self.clock.time_msec())
                if "pdus" in data:
                    for p in data["pdus"]:
                        if "age_ts" in p:
                            unsigned = p.setdefault("unsigned", {})
                            unsigned["age"] = now - int(p["age_ts"])
                            del p["age_ts"]
                return data

            try:
                response = await self._transport_layer.send_transaction(
                    transaction, json_data_cb
                )
            except HttpResponseException as e:
                code = e.code

                set_tag(tags.ERROR, True)

                logger.info("TX [%s] {%s} got %d response", destination, txn_id, code)
                raise

            logger.info("TX [%s] {%s} got 200 response", destination, txn_id)

            pdu_responses = response.get("pdus", {})
            if not isinstance(pdu_responses, Mapping):
                logger.warning(
                    "TX [%s] {%s} Remote returned invalid type for `pdus`",
                    destination,
                    txn_id,
                )
            else:
                for event_id, pdu_response in pdu_responses.items():
                    if not isinstance(pdu_response, Mapping) or "error" in pdu_response:
                        logger.warning(
                            "TX [%s] {%s} Remote returned error for %s: %s",
                            destination,
                            txn_id,
                            event_id,
                            pdu_response,
                        )

                # If modules have requested to be notified about delivered events,
                # build and send that notification.
                if self._federation_callbacks.interested_in_events_delivered_over_federation():
                    # A PDU is considered acknowledged when the remote echoes the event_id back to
                    # us, without an error in the PDU response dict.
                    acknowledged_pdu_ids = {
                        event_id
                        for event_id, pdu_response in response.get("pdus", {}).items()
                        if isinstance(pdu_response, Mapping)
                        and "error" not in pdu_response
                    }
                    await self._federation_callbacks.notify_on_event_delivered_over_federation(
                        destination,
                        [p for p in pdus if p.event_id in acknowledged_pdu_ids],
                        FederatedEventDeliveryMethod.SEND,
                    )

            if pdus and destination in self._federation_metrics_domains:
                last_pdu = pdus[-1]
                last_pdu_ts_metric.labels(
                    destination_server_name=destination,
                    **{SERVER_NAME_LABEL: self.server_name},
                ).set(last_pdu.origin_server_ts / 1000)

    async def _send_rust_transaction(
        self,
        rust: RustTransactionManager,
        destination: str,
        pdus: list[EventBase],
        edus: list[Edu],
    ) -> None:
        """Have Rust build and send the transaction, and handle the response
        as `send_new_transaction` does.
        """
        if self._is_mine_server_name(destination):
            raise RuntimeError("Transport layer cannot send to itself!")

        headers_dict: dict[bytes, list[bytes]] = {}
        opentracing.inject_header_dict(headers_dict, destination)
        extra_headers = [
            (name.decode("ascii"), value.decode("ascii"))
            for name, values in headers_dict.items()
            for value in values
        ]

        want_acknowledged = (
            self._federation_callbacks.interested_in_events_delivered_over_federation()
        )
        check = (
            functools.partial(self._check_rust_transaction, pdus, edus)
            if self._rust_strict
            else None
        )

        try:
            acknowledged = await rust.send_transaction(
                destination,
                pdus,
                [edu.get_dict() for edu in edus],
                extra_headers=extra_headers,
                want_acknowledged=want_acknowledged,
                check=check,
            )
        except HttpResponseException:
            set_tag(tags.ERROR, True)
            raise

        if want_acknowledged and acknowledged is not None:
            acknowledged_ids = set(acknowledged)
            await self._federation_callbacks.notify_on_event_delivered_over_federation(
                destination,
                [p for p in pdus if p.event_id in acknowledged_ids],
                FederatedEventDeliveryMethod.SEND,
            )

        if pdus and destination in self._federation_metrics_domains:
            last_pdu = pdus[-1]
            last_pdu_ts_metric.labels(
                destination_server_name=destination,
                **{SERVER_NAME_LABEL: self.server_name},
            ).set(last_pdu.origin_server_ts / 1000)

    def _check_rust_transaction(
        self, pdus: list[EventBase], edus: list[Edu], transaction: RustTransaction
    ) -> None:
        """Raise if Rust built a different body from the one Python would."""
        expected = Transaction(
            origin_server_ts=transaction.origin_server_ts,
            transaction_id=transaction.transaction_id,
            origin=self.server_name,
            destination=transaction.destination,
            pdus=serialize_and_filter_pdus(pdus),
            edus=[edu.get_dict() for edu in edus],
        ).get_dict()
        actual = transaction.get_dict()
        if actual != expected:
            raise AssertionError(
                f"Rust transaction body differs from Python's: {actual!r} != {expected!r}"
            )
