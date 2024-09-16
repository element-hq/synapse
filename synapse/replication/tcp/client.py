#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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
"""A replication client for use by synapse workers."""

import logging
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Set, Tuple

from sortedcontainers import SortedList

from twisted.internet import defer
from twisted.internet.defer import Deferred

from synapse.api.constants import EventTypes, Membership, ReceiptTypes
from synapse.federation import send_queue
from synapse.federation.sender import FederationSender
from synapse.logging.context import PreserveLoggingContext, make_deferred_yieldable
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.replication.tcp.streams import (
    AccountDataStream,
    DeviceListsStream,
    PushersStream,
    PushRulesStream,
    ReceiptsStream,
    ToDeviceStream,
    TypingStream,
    UnPartialStatedEventStream,
    UnPartialStatedRoomStream,
)
from synapse.replication.tcp.streams.events import (
    EventsStream,
    EventsStreamEventRow,
    EventsStreamRow,
)
from synapse.replication.tcp.streams.partial_state import (
    UnPartialStatedEventStreamRow,
    UnPartialStatedRoomStreamRow,
)
from synapse.types import PersistedEventPosition, ReadReceipt, StreamKeyType, UserID
from synapse.util.async_helpers import Linearizer, timeout_deferred
from synapse.util.iterutils import batch_iter
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# How long we allow callers to wait for replication updates before timing out.
_WAIT_FOR_REPLICATION_TIMEOUT_SECONDS = 5


class ReplicationDataHandler:
    """Handles incoming stream updates from replication.

    This instance notifies the data store about updates. Can be subclassed
    to handle updates in additional ways.
    """

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.notifier = hs.get_notifier()
        self._reactor = hs.get_reactor()
        self._clock = hs.get_clock()
        self._streams = hs.get_replication_streams()
        self._instance_name = hs.get_instance_name()
        self._typing_handler = hs.get_typing_handler()
        self._state_storage_controller = hs.get_storage_controllers().state

        self._notify_pushers = hs.config.worker.start_pushers
        self._pusher_pool = hs.get_pusherpool()
        self._presence_handler = hs.get_presence_handler()

        self.send_handler: Optional[FederationSenderHandler] = None
        if hs.should_send_federation():
            self.send_handler = FederationSenderHandler(hs)

        # Map from stream and instance to list of deferreds waiting for the stream to
        # arrive at a particular position. The lists are sorted by stream position.
        self._streams_to_waiters: Dict[
            Tuple[str, str], SortedList[Tuple[int, Deferred]]
        ] = {}

    async def on_rdata(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ) -> None:
        """Called to handle a batch of replication data with a given stream token.

        By default, this just pokes the data store. Can be overridden in subclasses to
        handle more.

        Args:
            stream_name: name of the replication stream for this batch of rows
            instance_name: the instance that wrote the rows.
            token: stream token for this batch of rows
            rows: a list of Stream.ROW_TYPE objects as returned by Stream.parse_row.
        """
        all_room_ids: Set[str] = set()
        if stream_name == DeviceListsStream.NAME:
            if any(not row.is_signature and not row.hosts_calculated for row in rows):
                prev_token = self.store.get_device_stream_token()
                all_room_ids = await self.store.get_all_device_list_changes(
                    prev_token, token
                )
                self.store.device_lists_in_rooms_have_changed(all_room_ids, token)

            # If we're sending federation we need to update the device lists
            # outbound pokes stream change cache with updated hosts.
            if self.send_handler and any(row.hosts_calculated for row in rows):
                hosts = await self.store.get_destinations_for_device(token)
                self.store.device_lists_outbound_pokes_have_changed(hosts, token)

        self.store.process_replication_rows(stream_name, instance_name, token, rows)
        # NOTE: this must be called after process_replication_rows to ensure any
        # cache invalidations are first handled before any stream ID advances.
        self.store.process_replication_position(stream_name, instance_name, token)

        if self.send_handler:
            await self.send_handler.process_replication_rows(stream_name, token, rows)

        if stream_name == TypingStream.NAME:
            self._typing_handler.process_replication_rows(token, rows)
            self.notifier.on_new_event(
                StreamKeyType.TYPING, token, rooms=[row.room_id for row in rows]
            )
        elif stream_name == PushRulesStream.NAME:
            self.notifier.on_new_event(
                StreamKeyType.PUSH_RULES, token, users=[row.user_id for row in rows]
            )
        elif stream_name in AccountDataStream.NAME:
            self.notifier.on_new_event(
                StreamKeyType.ACCOUNT_DATA, token, users=[row.user_id for row in rows]
            )
        elif stream_name == ReceiptsStream.NAME:
            new_token = self.store.get_max_receipt_stream_id()
            self.notifier.on_new_event(
                StreamKeyType.RECEIPT, new_token, rooms=[row.room_id for row in rows]
            )
            await self._pusher_pool.on_new_receipts({row.user_id for row in rows})
        elif stream_name == ToDeviceStream.NAME:
            entities = [row.entity for row in rows if row.entity.startswith("@")]
            if entities:
                self.notifier.on_new_event(
                    StreamKeyType.TO_DEVICE, token, users=entities
                )
        elif stream_name == DeviceListsStream.NAME:
            # `all_room_ids` can be large, so let's wake up those streams in batches
            for batched_room_ids in batch_iter(all_room_ids, 100):
                self.notifier.on_new_event(
                    StreamKeyType.DEVICE_LIST, token, rooms=batched_room_ids
                )

                # Yield to reactor so that we don't block.
                await self._clock.sleep(0)
        elif stream_name == PushersStream.NAME:
            for row in rows:
                if row.deleted:
                    self.stop_pusher(row.user_id, row.app_id, row.pushkey)
                else:
                    await self.process_pusher_change(
                        row.user_id, row.app_id, row.pushkey
                    )
        elif stream_name == EventsStream.NAME:
            # We shouldn't get multiple rows per token for events stream, so
            # we don't need to optimise this for multiple rows.
            for row in rows:
                if row.type != EventsStreamEventRow.TypeId:
                    # The row's data is an `EventsStreamCurrentStateRow`.
                    # When we recompute the current state of a room based on forward
                    # extremities (see `update_current_state`), no new events are
                    # persisted, so we must poke the replication callbacks ourselves.
                    # This functionality is used when finishing up a partial state join.
                    self.notifier.notify_replication()
                    continue
                assert isinstance(row, EventsStreamRow)
                assert isinstance(row.data, EventsStreamEventRow)

                if row.data.rejected:
                    continue

                extra_users: Tuple[UserID, ...] = ()
                if row.data.type == EventTypes.Member and row.data.state_key:
                    extra_users = (UserID.from_string(row.data.state_key),)

                max_token = self.store.get_room_max_token()
                event_pos = PersistedEventPosition(instance_name, token)
                event_entry = self.notifier.create_pending_room_event_entry(
                    event_pos,
                    extra_users,
                    row.data.room_id,
                    row.data.type,
                    row.data.state_key,
                    row.data.membership,
                )
                await self.notifier.notify_new_room_events(
                    [(event_entry, row.data.event_id)], max_token
                )

                # If this event is a join, make a note of it so we have an accurate
                # cross-worker room rate limit.
                # TODO: Erik said we should exclude rows that came from ex_outliers
                #  here, but I don't see how we can determine that. I guess we could
                #  add a flag to row.data?
                if (
                    row.data.type == EventTypes.Member
                    and row.data.membership == Membership.JOIN
                    and not row.data.outlier
                ):
                    # TODO retrieve the previous state, and exclude join -> join transitions
                    self.notifier.notify_user_joined_room(
                        row.data.event_id, row.data.room_id
                    )

                # If this is a server ACL event, clear the cache in the storage controller.
                if row.data.type == EventTypes.ServerACL:
                    self._state_storage_controller.get_server_acl_for_room.invalidate(
                        (row.data.room_id,)
                    )
        elif stream_name == UnPartialStatedRoomStream.NAME:
            for row in rows:
                assert isinstance(row, UnPartialStatedRoomStreamRow)

                # Wake up any tasks waiting for the room to be un-partial-stated.
                self._state_storage_controller.notify_room_un_partial_stated(
                    row.room_id
                )
                await self.notifier.on_un_partial_stated_room(row.room_id, token)
        elif stream_name == UnPartialStatedEventStream.NAME:
            for row in rows:
                assert isinstance(row, UnPartialStatedEventStreamRow)

                # Wake up any tasks waiting for the event to be un-partial-stated.
                self._state_storage_controller.notify_event_un_partial_stated(
                    row.event_id
                )

        await self._presence_handler.process_replication_rows(
            stream_name, instance_name, token, rows
        )

        # Notify any waiting deferreds. The list is ordered by position so we
        # just iterate through the list until we reach a position that is
        # greater than the received row position.
        waiting_list = self._streams_to_waiters.get((stream_name, instance_name))
        if not waiting_list:
            return

        # Index of first item with a position after the current token, i.e we
        # have called all deferreds before this index. If not overwritten by
        # loop below means either a) no items in list so no-op or b) all items
        # in list were called and so the list should be cleared. Setting it to
        # `len(list)` works for both cases.
        index_of_first_deferred_not_called = len(waiting_list)

        # We don't fire the deferreds until after we finish iterating over the
        # list, to avoid the list changing when we fire the deferreds.
        deferreds_to_callback = []

        for idx, (position, deferred) in enumerate(waiting_list):
            if position <= token:
                deferreds_to_callback.append(deferred)
            else:
                # The list is sorted by position so we don't need to continue
                # checking any further entries in the list.
                index_of_first_deferred_not_called = idx
                break

        # Drop all entries in the waiting list that were called in the above
        # loop. (This maintains the order so no need to resort)
        del waiting_list[:index_of_first_deferred_not_called]

        for deferred in deferreds_to_callback:
            try:
                with PreserveLoggingContext():
                    deferred.callback(None)
            except Exception:
                # The deferred has been cancelled or timed out.
                pass

    async def on_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        await self.on_rdata(stream_name, instance_name, token, [])

        # We poke the generic "replication" notifier to wake anything up that
        # may be streaming.
        self.notifier.notify_replication()

    async def wait_for_stream_position(
        self,
        instance_name: str,
        stream_name: str,
        position: int,
    ) -> None:
        """Wait until this instance has received updates up to and including
        the given stream position.

        Args:
            instance_name
            stream_name
            position
        """

        if instance_name == self._instance_name:
            # We don't get told about updates written by this process, and
            # anyway in that case we don't need to wait.
            return

        current_position = self._streams[stream_name].current_token(instance_name)
        if position <= current_position:
            # We're already past the position
            return

        # Create a new deferred that times out after N seconds, as we don't want
        # to wedge here forever.
        deferred: "Deferred[None]" = Deferred()
        deferred = timeout_deferred(
            deferred, _WAIT_FOR_REPLICATION_TIMEOUT_SECONDS, self._reactor
        )

        waiting_list = self._streams_to_waiters.setdefault(
            (stream_name, instance_name), SortedList(key=lambda t: t[0])
        )

        waiting_list.add((position, deferred))

        # We measure here to get in flight counts and average waiting time.
        with Measure(self._clock, "repl.wait_for_stream_position"):
            logger.info(
                "Waiting for repl stream %r to reach %s (%s); currently at: %s",
                stream_name,
                position,
                instance_name,
                current_position,
            )
            try:
                await make_deferred_yieldable(deferred)
            except defer.TimeoutError:
                logger.warning(
                    "Timed out waiting for repl stream %r to reach %s (%s)"
                    "; currently at: %s",
                    stream_name,
                    position,
                    instance_name,
                    self._streams[stream_name].current_token(instance_name),
                )
                return

            logger.info(
                "Finished waiting for repl stream %r to reach %s (%s)",
                stream_name,
                position,
                instance_name,
            )

    def stop_pusher(self, user_id: str, app_id: str, pushkey: str) -> None:
        if not self._notify_pushers:
            return

        key = "%s:%s" % (app_id, pushkey)
        pushers_for_user = self._pusher_pool.pushers.get(user_id, {})
        pusher = pushers_for_user.pop(key, None)
        if pusher is None:
            return
        logger.info("Stopping pusher %r / %r", user_id, key)
        pusher.on_stop()

    async def process_pusher_change(
        self, user_id: str, app_id: str, pushkey: str
    ) -> None:
        if not self._notify_pushers:
            return

        key = "%s:%s" % (app_id, pushkey)
        logger.info("Starting pusher %r / %r", user_id, key)
        await self._pusher_pool.process_pusher_change_by_id(app_id, pushkey, user_id)


class FederationSenderHandler:
    """Processes the fedration replication stream

    This class is only instantiate on the worker responsible for sending outbound
    federation transactions. It receives rows from the replication stream and forwards
    the appropriate entries to the FederationSender class.
    """

    def __init__(self, hs: "HomeServer"):
        assert hs.should_send_federation()

        self.store = hs.get_datastores().main
        self._is_mine_id = hs.is_mine_id
        self._hs = hs

        # We need to make a temporary value to ensure that mypy picks up the
        # right type. We know we should have a federation sender instance since
        # `should_send_federation` is True.
        sender = hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        self.federation_sender = sender

        # Stores the latest position in the federation stream we've gotten up
        # to. This is always set before we use it.
        self.federation_position: Optional[int] = None

        self._fed_position_linearizer = Linearizer(name="_fed_position_linearizer")

    async def process_replication_rows(
        self, stream_name: str, token: int, rows: list
    ) -> None:
        # The federation stream contains things that we want to send out, e.g.
        # presence, typing, etc.
        if stream_name == "federation":
            await send_queue.process_rows_for_federation(self.federation_sender, rows)
            await self.update_token(token)

        # ... and when new receipts happen
        elif stream_name == ReceiptsStream.NAME:
            await self._on_new_receipts(rows)

        # ... as well as device updates and messages
        elif stream_name == DeviceListsStream.NAME:
            # The entities are either user IDs (starting with '@') whose devices
            # have changed, or remote servers that we need to tell about
            # changes.
            if any(row.hosts_calculated for row in rows):
                hosts = await self.store.get_destinations_for_device(token)
                await self.federation_sender.send_device_messages(
                    hosts, immediate=False
                )

        elif stream_name == ToDeviceStream.NAME:
            # The to_device stream includes stuff to be pushed to both local
            # clients and remote servers, so we ignore entities that start with
            # '@' (since they'll be local users rather than destinations).
            hosts = {row.entity for row in rows if not row.entity.startswith("@")}
            await self.federation_sender.send_device_messages(hosts)

    async def _on_new_receipts(
        self, rows: Iterable[ReceiptsStream.ReceiptsStreamRow]
    ) -> None:
        """
        Args:
            rows: new receipts to be processed
        """
        for receipt in rows:
            # we only want to send on receipts for our own users
            if not self._is_mine_id(receipt.user_id):
                continue
            # Private read receipts never get sent over federation.
            if receipt.receipt_type == ReceiptTypes.READ_PRIVATE:
                continue
            receipt_info = ReadReceipt(
                receipt.room_id,
                receipt.receipt_type,
                receipt.user_id,
                [receipt.event_id],
                thread_id=receipt.thread_id,
                data=receipt.data,
            )
            await self.federation_sender.send_read_receipt(receipt_info)

    async def update_token(self, token: int) -> None:
        """Update the record of where we have processed to in the federation stream.

        Called after we have processed a an update received over replication. Sends
        a FEDERATION_ACK back to the master, and stores the token that we have processed
         in `federation_stream_position` so that we can restart where we left off.
        """
        self.federation_position = token

        # We save and send the ACK to master asynchronously, so we don't block
        # processing on persistence. We don't need to do this operation for
        # every single RDATA we receive, we just need to do it periodically.

        if self._fed_position_linearizer.is_queued(None):
            # There is already a task queued up to save and send the token, so
            # no need to queue up another task.
            return

        run_as_background_process("_save_and_send_ack", self._save_and_send_ack)

    async def _save_and_send_ack(self) -> None:
        """Save the current federation position in the database and send an ACK
        to master with where we're up to.
        """
        # We should only be calling this once we've got a token.
        assert self.federation_position is not None

        try:
            # We linearize here to ensure we don't have races updating the token
            #
            # XXX this appears to be redundant, since the ReplicationCommandHandler
            # has a linearizer which ensures that we only process one line of
            # replication data at a time. Should we remove it, or is it doing useful
            # service for robustness? Or could we replace it with an assertion that
            # we're not being re-entered?

            async with self._fed_position_linearizer.queue(None):
                # We persist and ack the same position, so we take a copy of it
                # here as otherwise it can get modified from underneath us.
                current_position = self.federation_position

                await self.store.update_federation_out_pos(
                    "federation", current_position
                )

                # We ACK this token over replication so that the master can drop
                # its in memory queues
                self._hs.get_replication_command_handler().send_federation_ack(
                    current_position
                )
        except Exception:
            logger.exception("Error updating federation stream position")
