#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
import datetime
import logging
from collections import OrderedDict
from types import TracebackType
from typing import TYPE_CHECKING, Dict, Hashable, Iterable, List, Optional, Tuple, Type

import attr
from prometheus_client import Counter

from synapse.api.constants import EduTypes
from synapse.api.errors import (
    FederationDeniedError,
    HttpResponseException,
    RequestSendFailed,
)
from synapse.api.presence import UserPresenceState
from synapse.events import EventBase
from synapse.federation.units import Edu
from synapse.handlers.presence import format_user_presence_state
from synapse.logging import issue9533_logger
from synapse.logging.opentracing import SynapseTags, set_tag
from synapse.metrics import sent_transactions_counter
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.types import JsonDict, ReadReceipt
from synapse.util.retryutils import NotRetryingDestination, get_retry_limiter
from synapse.visibility import filter_events_for_server

if TYPE_CHECKING:
    import synapse.server

# This is defined in the Matrix spec and enforced by the receiver.
MAX_EDUS_PER_TRANSACTION = 100

logger = logging.getLogger(__name__)


sent_edus_counter = Counter(
    "synapse_federation_client_sent_edus", "Total number of EDUs successfully sent"
)

sent_edus_by_type = Counter(
    "synapse_federation_client_sent_edus_by_type",
    "Number of sent EDUs successfully sent, by event type",
    ["type"],
)


# If the retry interval is larger than this then we enter "catchup" mode
CATCHUP_RETRY_INTERVAL = 60 * 60 * 1000

# Limit how many presence states we add to each presence EDU, to ensure that
# they are bounded in size.
MAX_PRESENCE_STATES_PER_EDU = 50


class PerDestinationQueue:
    """
    Manages the per-destination transmission queues.

    Args:
        hs
        transaction_sender
        destination: the server_name of the destination that we are managing
            transmission for.
    """

    def __init__(
        self,
        hs: "synapse.server.HomeServer",
        transaction_manager: "synapse.federation.sender.TransactionManager",
        destination: str,
    ):
        self._server_name = hs.hostname
        self._clock = hs.get_clock()
        self._storage_controllers = hs.get_storage_controllers()
        self._store = hs.get_datastores().main
        self._transaction_manager = transaction_manager
        self._instance_name = hs.get_instance_name()
        self._federation_shard_config = hs.config.worker.federation_shard_config
        self._state = hs.get_state_handler()

        self._should_send_on_this_instance = True
        if not self._federation_shard_config.should_handle(
            self._instance_name, destination
        ):
            # We don't raise an exception here to avoid taking out any other
            # processing. We have a guard in `attempt_new_transaction` that
            # ensure we don't start sending stuff.
            logger.error(
                "Create a per destination queue for %s on wrong worker",
                destination,
            )
            self._should_send_on_this_instance = False

        self._destination = destination
        self.transmission_loop_running = False

        # Flag to signal to any running transmission loop that there is new data
        # queued up to be sent.
        self._new_data_to_send = False

        # True whilst we are sending events that the remote homeserver missed
        # because it was unreachable. We start in this state so we can perform
        # catch-up at startup.
        # New events will only be sent once this is finished, at which point
        # _catching_up is flipped to False.
        self._catching_up: bool = True

        # The stream_ordering of the most recent PDU that was discarded due to
        # being in catch-up mode.
        self._catchup_last_skipped: int = 0

        # Cache of the last successfully-transmitted stream ordering for this
        # destination (we are the only updater so this is safe)
        self._last_successful_stream_ordering: Optional[int] = None

        # a queue of pending PDUs
        self._pending_pdus: List[EventBase] = []

        # XXX this is never actually used: see
        # https://github.com/matrix-org/synapse/issues/7549
        self._pending_edus: List[Edu] = []

        # Pending EDUs by their "key". Keyed EDUs are EDUs that get clobbered
        # based on their key (e.g. typing events by room_id)
        # Map of (edu_type, key) -> Edu
        self._pending_edus_keyed: Dict[Tuple[str, Hashable], Edu] = {}

        # Map of user_id -> UserPresenceState of pending presence to be sent to this
        # destination
        self._pending_presence: OrderedDict[str, UserPresenceState] = OrderedDict()

        # List of room_id -> receipt_type -> user_id -> receipt_dict,
        #
        # Each receipt can only have a single receipt per
        # (room ID, receipt type, user ID, thread ID) tuple.
        self._pending_receipt_edus: List[Dict[str, Dict[str, Dict[str, dict]]]] = []
        self._rrs_pending_flush = False

        # stream_id of last successfully sent to-device message.
        # NB: may be a long or an int.
        self._last_device_stream_id = 0

        # stream_id of last successfully sent device list update.
        self._last_device_list_stream_id = 0

    def __str__(self) -> str:
        return "PerDestinationQueue[%s]" % self._destination

    def pending_pdu_count(self) -> int:
        return len(self._pending_pdus)

    def pending_edu_count(self) -> int:
        return (
            len(self._pending_edus)
            + len(self._pending_presence)
            + len(self._pending_edus_keyed)
        )

    def send_pdu(self, pdu: EventBase) -> None:
        """Add a PDU to the queue, and start the transmission loop if necessary

        Args:
            pdu: pdu to send
        """
        if not self._catching_up or self._last_successful_stream_ordering is None:
            # only enqueue the PDU if we are not catching up (False) or do not
            # yet know if we have anything to catch up (None)
            self._pending_pdus.append(pdu)
        else:
            assert pdu.internal_metadata.stream_ordering
            self._catchup_last_skipped = pdu.internal_metadata.stream_ordering

        self.attempt_new_transaction()

    def send_presence(
        self, states: Iterable[UserPresenceState], start_loop: bool = True
    ) -> None:
        """Add presence updates to the queue.

        Args:
            states: Presence updates to send
            start_loop: Whether to start the transmission loop if not already
                running.

        Args:
            states: presence to send
        """
        self._pending_presence.update({state.user_id: state for state in states})
        self._new_data_to_send = True

        if start_loop:
            self.attempt_new_transaction()

    def queue_read_receipt(self, receipt: ReadReceipt) -> None:
        """Add a RR to the list to be sent. Doesn't start the transmission loop yet
        (see flush_read_receipts_for_room)

        Args:
            receipt: receipt to be queued
        """
        serialized_receipt: JsonDict = {
            "event_ids": receipt.event_ids,
            "data": receipt.data,
        }
        if receipt.thread_id is not None:
            serialized_receipt["data"]["thread_id"] = receipt.thread_id

        # Find which EDU to add this receipt to. There's three situations depending
        # on the (room ID, receipt type, user, thread ID) tuple:
        #
        # 1. If it fully matches, clobber the information.
        # 2. If it is missing, add the information.
        # 3. If the subset tuple of (room ID, receipt type, user) matches, check
        #    the next EDU (or add a new EDU).
        for edu in self._pending_receipt_edus:
            receipt_content = edu.setdefault(receipt.room_id, {}).setdefault(
                receipt.receipt_type, {}
            )
            # If this room ID, receipt type, user ID is not in this EDU, OR if
            # the full tuple matches, use the current EDU.
            if (
                receipt.user_id not in receipt_content
                or receipt_content[receipt.user_id].get("thread_id")
                == receipt.thread_id
            ):
                receipt_content[receipt.user_id] = serialized_receipt
                break

        # If no matching EDU was found, create a new one.
        else:
            self._pending_receipt_edus.append(
                {
                    receipt.room_id: {
                        receipt.receipt_type: {receipt.user_id: serialized_receipt}
                    }
                }
            )

    def flush_read_receipts_for_room(self, room_id: str) -> None:
        # If there are any pending receipts for this room then force-flush them
        # in a new transaction.
        for edu in self._pending_receipt_edus:
            if room_id in edu:
                self._rrs_pending_flush = True
                self.attempt_new_transaction()
                # No use in checking remaining EDUs if the room was found.
                break

    def send_keyed_edu(self, edu: Edu, key: Hashable) -> None:
        self._pending_edus_keyed[(edu.edu_type, key)] = edu
        self.attempt_new_transaction()

    def send_edu(self, edu: Edu) -> None:
        self._pending_edus.append(edu)
        self.attempt_new_transaction()

    def mark_new_data(self) -> None:
        """Marks that the destination has new data to send, without starting a
        new transaction.

        If a transaction loop is already in progress then a new transaction will
        be attempted when the current one finishes.
        """

        self._new_data_to_send = True

    def attempt_new_transaction(self) -> None:
        """Try to start a new transaction to this destination

        If there is already a transaction in progress to this destination,
        returns immediately. Otherwise kicks off the process of sending a
        transaction in the background.
        """

        # Mark that we (may) have new things to send, so that any running
        # transmission loop will recheck whether there is stuff to send.
        self._new_data_to_send = True

        if self.transmission_loop_running:
            # XXX: this can get stuck on by a never-ending
            # request at which point pending_pdus just keeps growing.
            # we need application-layer timeouts of some flavour of these
            # requests
            logger.debug("TX [%s] Transaction already in progress", self._destination)
            return

        if not self._should_send_on_this_instance:
            # We don't raise an exception here to avoid taking out any other
            # processing.
            logger.error(
                "Trying to start a transaction to %s on wrong worker", self._destination
            )
            return

        logger.debug("TX [%s] Starting transaction loop", self._destination)

        run_as_background_process(
            "federation_transaction_transmission_loop",
            self._transaction_transmission_loop,
        )

    async def _transaction_transmission_loop(self) -> None:
        pending_pdus: List[EventBase] = []
        try:
            self.transmission_loop_running = True

            # This will throw if we wouldn't retry. We do this here so we fail
            # quickly, but we will later check this again in the http client,
            # hence why we throw the result away.
            await get_retry_limiter(self._destination, self._clock, self._store)

            if self._catching_up:
                # we potentially need to catch-up first
                await self._catch_up_transmission_loop()
                if self._catching_up:
                    # not caught up yet
                    return

            while True:
                self._new_data_to_send = False

                async with _TransactionQueueManager(self) as (
                    pending_pdus,  # noqa: F811
                    pending_edus,
                ):
                    if not pending_pdus and not pending_edus:
                        logger.debug("TX [%s] Nothing to send", self._destination)

                        # If we've gotten told about new things to send during
                        # checking for things to send, we try looking again.
                        # Otherwise new PDUs or EDUs might arrive in the meantime,
                        # but not get sent because we hold the
                        # `transmission_loop_running` flag.
                        if self._new_data_to_send:
                            continue
                        else:
                            return

                    if pending_pdus:
                        logger.debug(
                            "TX [%s] len(pending_pdus_by_dest[dest]) = %d",
                            self._destination,
                            len(pending_pdus),
                        )

                    await self._transaction_manager.send_new_transaction(
                        self._destination, pending_pdus, pending_edus
                    )

                    sent_transactions_counter.inc()
                    sent_edus_counter.inc(len(pending_edus))
                    for edu in pending_edus:
                        sent_edus_by_type.labels(edu.edu_type).inc()

        except NotRetryingDestination as e:
            logger.debug(
                "TX [%s] not ready for retry yet (next retry at %s) - "
                "dropping transaction for now",
                self._destination,
                datetime.datetime.fromtimestamp(
                    (e.retry_last_ts + e.retry_interval) / 1000.0
                ),
            )

            if e.retry_interval > CATCHUP_RETRY_INTERVAL:
                # we won't retry for another hour!
                # (this suggests a significant outage)
                # We drop pending EDUs because otherwise they will
                # rack up indefinitely.
                # (Dropping PDUs is already performed by `_start_catching_up`.)
                # Note that:
                # - the EDUs that are being dropped here are those that we can
                #   afford to drop (specifically, only typing notifications,
                #   read receipts and presence updates are being dropped here)
                # - Other EDUs such as to_device messages are queued with a
                #   different mechanism
                # - this is all volatile state that would be lost if the
                #   federation sender restarted anyway

                # dropping read receipts is a bit sad but should be solved
                # through another mechanism, because this is all volatile!
                self._pending_edus = []
                self._pending_edus_keyed = {}
                self._pending_presence.clear()
                self._pending_receipt_edus = []

                self._start_catching_up()
        except FederationDeniedError as e:
            logger.info(e)
        except HttpResponseException as e:
            logger.warning(
                "TX [%s] Received %d response to transaction: %s",
                self._destination,
                e.code,
                e,
            )

        except RequestSendFailed as e:
            logger.warning(
                "TX [%s] Failed to send transaction: %s", self._destination, e
            )

            for p in pending_pdus:
                logger.info(
                    "Failed to send event %s to %s", p.event_id, self._destination
                )
        except Exception:
            logger.exception("TX [%s] Failed to send transaction", self._destination)
            for p in pending_pdus:
                logger.info(
                    "Failed to send event %s to %s", p.event_id, self._destination
                )
        finally:
            # We want to be *very* sure we clear this after we stop processing
            self.transmission_loop_running = False

    async def _catch_up_transmission_loop(self) -> None:
        first_catch_up_check = self._last_successful_stream_ordering is None

        if first_catch_up_check:
            # first catchup so get last_successful_stream_ordering from database
            self._last_successful_stream_ordering = (
                await self._store.get_destination_last_successful_stream_ordering(
                    self._destination
                )
            )

        _tmp_last_successful_stream_ordering = self._last_successful_stream_ordering
        if _tmp_last_successful_stream_ordering is None:
            # if it's still None, then this means we don't have the information
            # in our database ­ we haven't successfully sent a PDU to this server
            # (at least since the introduction of the feature tracking
            # last_successful_stream_ordering).
            # Sadly, this means we can't do anything here as we don't know what
            # needs catching up — so catching up is futile; let's stop.
            self._catching_up = False
            return

        last_successful_stream_ordering: int = _tmp_last_successful_stream_ordering

        # get at most 50 catchup room/PDUs
        while True:
            event_ids = await self._store.get_catch_up_room_event_ids(
                self._destination, last_successful_stream_ordering
            )

            if not event_ids:
                # No more events to catch up on, but we can't ignore the chance
                # of a race condition, so we check that no new events have been
                # skipped due to us being in catch-up mode

                if self._catchup_last_skipped > last_successful_stream_ordering:
                    # another event has been skipped because we were in catch-up mode
                    continue

                # we are done catching up!
                self._catching_up = False
                break

            if first_catch_up_check:
                # as this is our check for needing catch-up, we may have PDUs in
                # the queue from before we *knew* we had to do catch-up, so
                # clear those out now.
                self._start_catching_up()

            # fetch the relevant events from the event store
            # - redacted behaviour of REDACT is fine, since we only send metadata
            #   of redacted events to the destination.
            # - don't need to worry about rejected events as we do not actively
            #   forward received events over federation.
            catchup_pdus = await self._store.get_events_as_list(event_ids)
            if not catchup_pdus:
                raise AssertionError(
                    "No events retrieved when we asked for %r. "
                    "This should not happen." % event_ids
                )

            logger.info(
                "Catching up destination %s with %d PDUs",
                self._destination,
                len(catchup_pdus),
            )

            # We send transactions with events from one room only, as its likely
            # that the remote will have to do additional processing, which may
            # take some time. It's better to give it small amounts of work
            # rather than risk the request timing out and repeatedly being
            # retried, and not making any progress.
            #
            # Note: `catchup_pdus` will have exactly one PDU per room.
            for pdu in catchup_pdus:
                # The PDU from the DB will be the newest PDU in the room from
                # *this server* that we tried---but were unable---to send to the remote.
                # servers may have sent lots of events since then, and we want
                # to try and tell the remote only about the *latest* events in
                # the room. This is so that it doesn't get inundated by events
                # from various parts of the DAG, which all need to be processed.
                #
                # Note: this does mean that in large rooms a server coming back
                # online will get sent the same events from all the different
                # servers, but the remote will correctly deduplicate them and
                # handle it only once.

                # Step 1, fetch the current extremities
                extrems = await self._store.get_prev_events_for_room(pdu.room_id)

                if pdu.event_id in extrems:
                    # If the event is in the extremities, then great! We can just
                    # use that without having to do further checks.
                    room_catchup_pdus = [pdu]
                elif await self._store.is_partial_state_room(pdu.room_id):
                    # We can't be sure which events the destination should
                    # see using only partial state. Avoid doing so, and just retry
                    # sending our the newest PDU the remote is missing from us.
                    room_catchup_pdus = [pdu]
                else:
                    # If not, fetch the extremities and figure out which we can
                    # send.
                    extrem_events = await self._store.get_events_as_list(extrems)

                    new_pdus = []
                    for p in extrem_events:
                        # We pulled this from the DB, so it'll be non-null
                        assert p.internal_metadata.stream_ordering

                        # Filter out events that happened before the remote went
                        # offline
                        if (
                            p.internal_metadata.stream_ordering
                            < last_successful_stream_ordering
                        ):
                            continue

                        new_pdus.append(p)

                    # Filter out events where the server is not in the room,
                    # e.g. it may have left/been kicked. *Ideally* we'd pull
                    # out the kick and send that, but it's a rare edge case
                    # so we don't bother for now (the server that sent the
                    # kick should send it out if its online).
                    new_pdus = await filter_events_for_server(
                        self._storage_controllers,
                        self._destination,
                        self._server_name,
                        new_pdus,
                        redact=False,
                        filter_out_erased_senders=True,
                        filter_out_remote_partial_state_events=True,
                    )

                    # If we've filtered out all the extremities, fall back to
                    # sending the original event. This should ensure that the
                    # server gets at least some of missed events (especially if
                    # the other sending servers are up).
                    if new_pdus:
                        room_catchup_pdus = new_pdus
                    else:
                        room_catchup_pdus = [pdu]

                logger.info(
                    "Catching up rooms to %s: %r", self._destination, pdu.room_id
                )

                await self._transaction_manager.send_new_transaction(
                    self._destination, room_catchup_pdus, []
                )

                sent_transactions_counter.inc()

                # We pulled this from the DB, so it'll be non-null
                assert pdu.internal_metadata.stream_ordering

                # Note that we mark the last successful stream ordering as that
                # from the *original* PDU, rather than the PDU(s) we actually
                # send. This is because we use it to mark our position in the
                # queue of missed PDUs to process.
                last_successful_stream_ordering = pdu.internal_metadata.stream_ordering

                self._last_successful_stream_ordering = last_successful_stream_ordering
                await self._store.set_destination_last_successful_stream_ordering(
                    self._destination, last_successful_stream_ordering
                )

    def _get_receipt_edus(self, force_flush: bool, limit: int) -> Iterable[Edu]:
        if not self._pending_receipt_edus:
            return
        if not force_flush and not self._rrs_pending_flush:
            # not yet time for this lot
            return

        # Send at most limit EDUs for receipts.
        for content in self._pending_receipt_edus[:limit]:
            yield Edu(
                origin=self._server_name,
                destination=self._destination,
                edu_type=EduTypes.RECEIPT,
                content=content,
            )
        self._pending_receipt_edus = self._pending_receipt_edus[limit:]

        # If there are still pending read-receipts, don't reset the pending flush
        # flag.
        if not self._pending_receipt_edus:
            self._rrs_pending_flush = False

    def _pop_pending_edus(self, limit: int) -> List[Edu]:
        pending_edus = self._pending_edus
        pending_edus, self._pending_edus = pending_edus[:limit], pending_edus[limit:]
        return pending_edus

    async def _get_device_update_edus(self, limit: int) -> Tuple[List[Edu], int]:
        last_device_list = self._last_device_list_stream_id

        # Retrieve list of new device updates to send to the destination
        now_stream_id, results = await self._store.get_device_updates_by_remote(
            self._destination, last_device_list, limit=limit
        )
        edus = [
            Edu(
                origin=self._server_name,
                destination=self._destination,
                edu_type=edu_type,
                content=content,
            )
            for (edu_type, content) in results
        ]

        assert len(edus) <= limit, "get_device_updates_by_remote returned too many EDUs"

        return edus, now_stream_id

    async def _get_to_device_message_edus(self, limit: int) -> Tuple[List[Edu], int]:
        last_device_stream_id = self._last_device_stream_id
        to_device_stream_id = self._store.get_to_device_stream_token()
        contents, stream_id = await self._store.get_new_device_msgs_for_remote(
            self._destination, last_device_stream_id, to_device_stream_id, limit
        )
        for content in contents:
            message_id = content.get("message_id")
            if not message_id:
                continue

            set_tag(SynapseTags.TO_DEVICE_EDU_ID, message_id)

        edus = [
            Edu(
                origin=self._server_name,
                destination=self._destination,
                edu_type=EduTypes.DIRECT_TO_DEVICE,
                content=content,
            )
            for content in contents
        ]

        if edus:
            issue9533_logger.debug(
                "Sending %i to-device messages to %s, up to stream id %i",
                len(edus),
                self._destination,
                stream_id,
            )

        return edus, stream_id

    def _start_catching_up(self) -> None:
        """
        Marks this destination as being in catch-up mode.

        This throws away the PDU queue.
        """
        self._catching_up = True
        self._pending_pdus = []


@attr.s(slots=True, auto_attribs=True)
class _TransactionQueueManager:
    """A helper async context manager for pulling stuff off the queues and
    tracking what was last successfully sent, etc.
    """

    queue: PerDestinationQueue

    _device_stream_id: Optional[int] = None
    _device_list_id: Optional[int] = None
    _last_stream_ordering: Optional[int] = None
    _pdus: List[EventBase] = attr.Factory(list)

    async def __aenter__(self) -> Tuple[List[EventBase], List[Edu]]:
        # First we calculate the EDUs we want to send, if any.

        # There's a maximum number of EDUs that can be sent with a transaction,
        # generally device updates and to-device messages get priority, but we
        # want to ensure that there's room for some other EDUs as well.
        #
        # This is done by:
        #
        # * Add a presence EDU, if one exists.
        # * Add up-to a small limit of read receipt EDUs.
        # * Add to-device EDUs, but leave some space for device list updates.
        # * Add device list updates EDUs.
        # * If there's any remaining room, add other EDUs.
        pending_edus = []

        # Add presence EDU.
        if self.queue._pending_presence:
            # Only send max 50 presence entries in the EDU, to bound the amount
            # of data we're sending.
            presence_to_add: List[JsonDict] = []
            while (
                self.queue._pending_presence
                and len(presence_to_add) < MAX_PRESENCE_STATES_PER_EDU
            ):
                _, presence = self.queue._pending_presence.popitem(last=False)
                presence_to_add.append(
                    format_user_presence_state(presence, self.queue._clock.time_msec())
                )

            pending_edus.append(
                Edu(
                    origin=self.queue._server_name,
                    destination=self.queue._destination,
                    edu_type=EduTypes.PRESENCE,
                    content={"push": presence_to_add},
                )
            )

        # Add read receipt EDUs.
        pending_edus.extend(self.queue._get_receipt_edus(force_flush=False, limit=5))
        edu_limit = MAX_EDUS_PER_TRANSACTION - len(pending_edus)

        # Next, prioritize to-device messages so that existing encryption channels
        # work. We also keep a few slots spare (by reducing the limit) so that
        # we can still trickle out some device list updates.
        (
            to_device_edus,
            device_stream_id,
        ) = await self.queue._get_to_device_message_edus(edu_limit - 10)

        if to_device_edus:
            self._device_stream_id = device_stream_id
        else:
            self.queue._last_device_stream_id = device_stream_id

        pending_edus.extend(to_device_edus)
        edu_limit -= len(to_device_edus)

        # Add device list update EDUs.
        device_update_edus, dev_list_id = await self.queue._get_device_update_edus(
            edu_limit
        )

        if device_update_edus:
            self._device_list_id = dev_list_id
        else:
            self.queue._last_device_list_stream_id = dev_list_id

        pending_edus.extend(device_update_edus)
        edu_limit -= len(device_update_edus)

        # Finally add any other types of EDUs if there is room.
        other_edus = self.queue._pop_pending_edus(edu_limit)
        pending_edus.extend(other_edus)
        edu_limit -= len(other_edus)
        while edu_limit > 0 and self.queue._pending_edus_keyed:
            _, val = self.queue._pending_edus_keyed.popitem()
            pending_edus.append(val)
            edu_limit -= 1

        # Now we look for any PDUs to send, by getting up to 50 PDUs from the
        # queue
        self._pdus = self.queue._pending_pdus[:50]

        if not self._pdus and not pending_edus:
            return [], []

        # if we've decided to send a transaction anyway, and we have room, we
        # may as well send any pending RRs
        if edu_limit:
            pending_edus.extend(
                self.queue._get_receipt_edus(force_flush=True, limit=edu_limit)
            )

        if self._pdus:
            self._last_stream_ordering = self._pdus[
                -1
            ].internal_metadata.stream_ordering
            assert self._last_stream_ordering

        return self._pdus, pending_edus

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        if exc_type is not None:
            # Failed to send transaction, so we bail out.
            return

        # Successfully sent transactions, so we remove pending PDUs from the queue
        if self._pdus:
            self.queue._pending_pdus = self.queue._pending_pdus[len(self._pdus) :]

        # Succeeded to send the transaction so we record where we have sent up
        # to in the various streams

        if self._device_stream_id:
            await self.queue._store.delete_device_msgs_for_remote(
                self.queue._destination, self._device_stream_id
            )
            self.queue._last_device_stream_id = self._device_stream_id

        # also mark the device updates as sent
        if self._device_list_id:
            logger.info(
                "Marking as sent %r %r", self.queue._destination, self._device_list_id
            )
            await self.queue._store.mark_as_sent_devices_by_remote(
                self.queue._destination, self._device_list_id
            )
            self.queue._last_device_list_stream_id = self._device_list_id

        if self._last_stream_ordering:
            # we sent some PDUs and it was successful, so update our
            # last_successful_stream_ordering in the destinations table.
            await self.queue._store.set_destination_last_successful_stream_ordering(
                self.queue._destination, self._last_stream_ordering
            )
