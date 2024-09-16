#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2016 OpenMarket Ltd
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

import logging
from typing import (
    TYPE_CHECKING,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    cast,
)

from synapse.api.constants import EventContentFields
from synapse.logging import issue9533_logger
from synapse.logging.opentracing import (
    SynapseTags,
    log_kv,
    set_tag,
    start_active_span,
    trace,
)
from synapse.replication.tcp.streams import ToDeviceStream
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import JsonDict
from synapse.util import json_encoder
from synapse.util.caches.expiringcache import ExpiringCache
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DeviceInboxWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._instance_name = hs.get_instance_name()

        # Map of (user_id, device_id) to the last stream_id that has been
        # deleted up to. This is so that we can no op deletions.
        self._last_device_delete_cache: ExpiringCache[
            Tuple[str, Optional[str]], int
        ] = ExpiringCache(
            cache_name="last_device_delete_cache",
            clock=self._clock,
            max_len=10000,
            expiry_ms=30 * 60 * 1000,
        )

        self._can_write_to_device = (
            self._instance_name in hs.config.worker.writers.to_device
        )

        self._to_device_msg_id_gen: MultiWriterIdGenerator = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="to_device",
            instance_name=self._instance_name,
            tables=[
                ("device_inbox", "instance_name", "stream_id"),
                ("device_federation_outbox", "instance_name", "stream_id"),
            ],
            sequence_name="device_inbox_sequence",
            writers=hs.config.worker.writers.to_device,
        )

        max_device_inbox_id = self._to_device_msg_id_gen.get_current_token()
        device_inbox_prefill, min_device_inbox_id = self.db_pool.get_cache_dict(
            db_conn,
            "device_inbox",
            entity_column="user_id",
            stream_column="stream_id",
            max_value=max_device_inbox_id,
            limit=1000,
        )
        self._device_inbox_stream_cache = StreamChangeCache(
            "DeviceInboxStreamChangeCache",
            min_device_inbox_id,
            prefilled_cache=device_inbox_prefill,
        )

        # The federation outbox and the local device inbox uses the same
        # stream_id generator.
        device_outbox_prefill, min_device_outbox_id = self.db_pool.get_cache_dict(
            db_conn,
            "device_federation_outbox",
            entity_column="destination",
            stream_column="stream_id",
            max_value=max_device_inbox_id,
            limit=1000,
        )
        self._device_federation_outbox_stream_cache = StreamChangeCache(
            "DeviceFederationOutboxStreamChangeCache",
            min_device_outbox_id,
            prefilled_cache=device_outbox_prefill,
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[ToDeviceStream.ToDeviceStreamRow],
    ) -> None:
        if stream_name == ToDeviceStream.NAME:
            # If replication is happening than postgres must be being used.
            assert isinstance(self._to_device_msg_id_gen, MultiWriterIdGenerator)
            self._to_device_msg_id_gen.advance(instance_name, token)
            for row in rows:
                if row.entity.startswith("@"):
                    self._device_inbox_stream_cache.entity_has_changed(
                        row.entity, token
                    )
                else:
                    self._device_federation_outbox_stream_cache.entity_has_changed(
                        row.entity, token
                    )
        return super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == ToDeviceStream.NAME:
            self._to_device_msg_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def get_to_device_stream_token(self) -> int:
        return self._to_device_msg_id_gen.get_current_token()

    def get_to_device_id_generator(self) -> MultiWriterIdGenerator:
        return self._to_device_msg_id_gen

    async def get_messages_for_user_devices(
        self,
        user_ids: Collection[str],
        from_stream_id: int,
        to_stream_id: int,
    ) -> Dict[Tuple[str, str], List[JsonDict]]:
        """
        Retrieve to-device messages for a given set of users.

        Only to-device messages with stream ids between the given boundaries
        (from < X <= to) are returned.

        Args:
            user_ids: The users to retrieve to-device messages for.
            from_stream_id: The lower boundary of stream id to filter with (exclusive).
            to_stream_id: The upper boundary of stream id to filter with (inclusive).

        Returns:
            A dictionary of (user id, device id) -> list of to-device messages.
        """
        # We expect the stream ID returned by _get_device_messages to always
        # be to_stream_id. So, no need to return it from this function.
        (
            user_id_device_id_to_messages,
            last_processed_stream_id,
        ) = await self._get_device_messages(
            user_ids=user_ids,
            from_stream_id=from_stream_id,
            to_stream_id=to_stream_id,
        )

        assert (
            last_processed_stream_id == to_stream_id
        ), "Expected _get_device_messages to process all to-device messages up to `to_stream_id`"

        return user_id_device_id_to_messages

    async def get_messages_for_device(
        self,
        user_id: str,
        device_id: str,
        from_stream_id: int,
        to_stream_id: int,
        limit: int = 100,
    ) -> Tuple[List[JsonDict], int]:
        """
        Retrieve to-device messages for a single user device.

        Only to-device messages with stream ids between the given boundaries
        (from < X <= to) are returned.

        Args:
            user_id: The ID of the user to retrieve messages for.
            device_id: The ID of the device to retrieve to-device messages for.
            from_stream_id: The lower boundary of stream id to filter with (exclusive).
            to_stream_id: The upper boundary of stream id to filter with (inclusive).
            limit: A limit on the number of to-device messages returned.

        Returns:
            A tuple containing:
                * A list of to-device messages within the given stream id range intended for
                  the given user / device combo.
                * The last-processed stream ID. Subsequent calls of this function with the
                  same device should pass this value as 'from_stream_id'.
        """
        if not self._device_inbox_stream_cache.has_entity_changed(
            user_id, from_stream_id
        ):
            # There were no messages!
            return [], to_stream_id

        def get_device_messages_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[JsonDict], int]:
            sql = """
                SELECT stream_id, message_json FROM device_inbox
                WHERE user_id = ? AND device_id = ?
                AND ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """
            txn.execute(sql, (user_id, device_id, from_stream_id, to_stream_id, limit))

            # Create and fill a dictionary of (user ID, device ID) -> list of messages
            # intended for each device.
            last_processed_stream_pos = to_stream_id
            to_device_messages: List[JsonDict] = []
            rowcount = 0
            for row in txn:
                rowcount += 1

                last_processed_stream_pos = row[0]
                message_dict = db_to_json(row[1])

                # Store the device details
                to_device_messages.append(message_dict)

                # start a new span for each message, so that we can tag each separately
                with start_active_span("get_to_device_message"):
                    set_tag(SynapseTags.TO_DEVICE_TYPE, message_dict["type"])
                    set_tag(SynapseTags.TO_DEVICE_SENDER, message_dict["sender"])
                    set_tag(SynapseTags.TO_DEVICE_RECIPIENT, user_id)
                    set_tag(SynapseTags.TO_DEVICE_RECIPIENT_DEVICE, device_id)
                    set_tag(
                        SynapseTags.TO_DEVICE_MSGID,
                        message_dict["content"].get(EventContentFields.TO_DEVICE_MSGID),
                    )

            if rowcount == limit:
                # We ended up bumping up against the message limit. There may be more messages
                # to retrieve. Return what we have, as well as the last stream position that
                # was processed.
                #
                # The caller is expected to set this as the lower (exclusive) bound
                # for the next query of this device.
                return to_device_messages, last_processed_stream_pos

            # The limit was not reached, thus we know that recipient_device_to_messages
            # contains all to-device messages for the given device and stream id range.
            #
            # We return to_stream_id, which the caller should then provide as the lower
            # (exclusive) bound on the next query of this device.
            return to_device_messages, to_stream_id

        return await self.db_pool.runInteraction(
            "get_messages_for_device", get_device_messages_txn
        )

    async def _get_device_messages(
        self,
        user_ids: Collection[str],
        from_stream_id: int,
        to_stream_id: int,
    ) -> Tuple[Dict[Tuple[str, str], List[JsonDict]], int]:
        """
        Retrieve pending to-device messages for a collection of user devices.

        Only to-device messages with stream ids between the given boundaries
        (from < X <= to) are returned.

        Note that a stream ID can be shared by multiple copies of the same message with
        different recipient devices. Stream IDs are only unique in the context of a single
        user ID / device ID pair. Thus, applying a limit (of messages to return) when working
        with a sliding window of stream IDs is only possible when querying messages of a
        single user device.

        Finally, note that device IDs are not unique across users.

        Args:
            user_ids: The user IDs to filter device messages by.
            from_stream_id: The lower boundary of stream id to filter with (exclusive).
            to_stream_id: The upper boundary of stream id to filter with (inclusive).


        Returns:
            A tuple containing:
                * A dict of (user_id, device_id) -> list of to-device messages
                * The last-processed stream ID. If this is less than `to_stream_id`, then
                    there may be more messages to retrieve. If `limit` is not set, then this
                    is always equal to 'to_stream_id'.
        """
        if not user_ids:
            logger.warning("No users provided upon querying for device IDs")
            return {}, to_stream_id

        user_ids_to_query: Set[str] = set()

        # Determine which users have devices with pending messages
        for user_id in user_ids:
            if self._device_inbox_stream_cache.has_entity_changed(
                user_id, from_stream_id
            ):
                # This user has new messages sent to them. Query messages for them
                user_ids_to_query.add(user_id)

        if not user_ids_to_query:
            return {}, to_stream_id

        def get_device_messages_txn(
            txn: LoggingTransaction,
        ) -> Tuple[Dict[Tuple[str, str], List[JsonDict]], int]:
            # Build a query to select messages from any of the given devices that
            # are between the given stream id bounds.

            # If a list of device IDs was not provided, retrieve all devices IDs
            # for the given users. We explicitly do not query hidden devices, as
            # hidden devices should not receive to-device messages.
            # Note that this is more efficient than just dropping `device_id` from the query,
            # since device_inbox has an index on `(user_id, device_id, stream_id)`

            user_device_dicts = cast(
                List[Tuple[str]],
                self.db_pool.simple_select_many_txn(
                    txn,
                    table="devices",
                    column="user_id",
                    iterable=user_ids_to_query,
                    keyvalues={"hidden": False},
                    retcols=("device_id",),
                ),
            )

            device_ids_to_query = {row[0] for row in user_device_dicts}

            if not device_ids_to_query:
                # We've ended up with no devices to query.
                return {}, to_stream_id

            # We include both user IDs and device IDs in this query, as we have an index
            # (device_inbox_user_stream_id) for them.
            user_id_many_clause_sql, user_id_many_clause_args = make_in_list_sql_clause(
                self.database_engine, "user_id", user_ids_to_query
            )
            (
                device_id_many_clause_sql,
                device_id_many_clause_args,
            ) = make_in_list_sql_clause(
                self.database_engine, "device_id", device_ids_to_query
            )

            sql = f"""
                SELECT stream_id, user_id, device_id, message_json FROM device_inbox
                WHERE {user_id_many_clause_sql}
                AND {device_id_many_clause_sql}
                AND ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
            """
            sql_args = (
                *user_id_many_clause_args,
                *device_id_many_clause_args,
                from_stream_id,
                to_stream_id,
            )

            txn.execute(sql, sql_args)

            # Create and fill a dictionary of (user ID, device ID) -> list of messages
            # intended for each device.
            recipient_device_to_messages: Dict[Tuple[str, str], List[JsonDict]] = {}
            rowcount = 0
            for row in txn:
                rowcount += 1

                recipient_user_id = row[1]
                recipient_device_id = row[2]
                message_dict = db_to_json(row[3])

                # Store the device details
                recipient_device_to_messages.setdefault(
                    (recipient_user_id, recipient_device_id), []
                ).append(message_dict)

                # start a new span for each message, so that we can tag each separately
                with start_active_span("get_to_device_message"):
                    set_tag(SynapseTags.TO_DEVICE_TYPE, message_dict["type"])
                    set_tag(SynapseTags.TO_DEVICE_SENDER, message_dict["sender"])
                    set_tag(SynapseTags.TO_DEVICE_RECIPIENT, recipient_user_id)
                    set_tag(SynapseTags.TO_DEVICE_RECIPIENT_DEVICE, recipient_device_id)
                    set_tag(
                        SynapseTags.TO_DEVICE_MSGID,
                        message_dict["content"].get(EventContentFields.TO_DEVICE_MSGID),
                    )

            # We return to_stream_id, which the caller should then provide as the lower
            # (exclusive) bound on the next query of this device.
            return recipient_device_to_messages, to_stream_id

        return await self.db_pool.runInteraction(
            "get_device_messages", get_device_messages_txn
        )

    @trace
    async def delete_messages_for_device(
        self,
        user_id: str,
        device_id: Optional[str],
        up_to_stream_id: int,
    ) -> int:
        """
        Args:
            user_id: The recipient user_id.
            device_id: The recipient device_id.
            up_to_stream_id: Where to delete messages up to.

        Returns:
            The number of messages deleted.
        """
        # If we have cached the last stream id we've deleted up to, we can
        # check if there is likely to be anything that needs deleting
        last_deleted_stream_id = self._last_device_delete_cache.get(
            (user_id, device_id), None
        )

        set_tag("last_deleted_stream_id", str(last_deleted_stream_id))

        if last_deleted_stream_id:
            has_changed = self._device_inbox_stream_cache.has_entity_changed(
                user_id, last_deleted_stream_id
            )
            if not has_changed:
                log_kv({"message": "No changes in cache since last check"})
                return 0

        from_stream_id = None
        count = 0
        while True:
            from_stream_id, loop_count = await self.delete_messages_for_device_between(
                user_id,
                device_id,
                from_stream_id=from_stream_id,
                to_stream_id=up_to_stream_id,
                limit=1000,
            )
            count += loop_count
            if from_stream_id is None:
                break

        log_kv({"message": f"deleted {count} messages for device", "count": count})

        # Update the cache, ensuring that we only ever increase the value
        updated_last_deleted_stream_id = self._last_device_delete_cache.get(
            (user_id, device_id), 0
        )
        self._last_device_delete_cache[(user_id, device_id)] = max(
            updated_last_deleted_stream_id, up_to_stream_id
        )

        return count

    @trace
    async def delete_messages_for_device_between(
        self,
        user_id: str,
        device_id: Optional[str],
        from_stream_id: Optional[int],
        to_stream_id: int,
        limit: int,
    ) -> Tuple[Optional[int], int]:
        """Delete N device messages between the stream IDs, returning the
        highest stream ID deleted (or None if all messages in the range have
        been deleted) and the number of messages deleted.

        This is more efficient than `delete_messages_for_device` when calling in
        a loop to batch delete messages.
        """

        # Keeping track of a lower bound of stream ID where we've deleted
        # everything below makes the queries much faster. Otherwise, every time
        # we scan for rows to delete we'd re-scan across all the rows that have
        # previously deleted (until the next table VACUUM).

        if from_stream_id is None:
            # Minimum device stream ID is 1.
            from_stream_id = 0

        def delete_messages_for_device_between_txn(
            txn: LoggingTransaction,
        ) -> Tuple[Optional[int], int]:
            txn.execute(
                """
                SELECT MAX(stream_id) FROM (
                    SELECT stream_id FROM device_inbox
                    WHERE user_id = ? AND device_id = ?
                        AND ? < stream_id AND stream_id <= ?
                    ORDER BY stream_id
                    LIMIT ?
                ) AS d
                """,
                (user_id, device_id, from_stream_id, to_stream_id, limit),
            )
            row = txn.fetchone()
            if row is None or row[0] is None:
                return None, 0

            (max_stream_id,) = row

            txn.execute(
                """
                DELETE FROM device_inbox
                WHERE user_id = ? AND device_id = ?
                AND ? < stream_id AND stream_id <= ?
                """,
                (user_id, device_id, from_stream_id, max_stream_id),
            )

            num_deleted = txn.rowcount
            if num_deleted < limit:
                return None, num_deleted

            return max_stream_id, num_deleted

        return await self.db_pool.runInteraction(
            "delete_messages_for_device_between",
            delete_messages_for_device_between_txn,
            db_autocommit=True,  # We don't need to run in a transaction
        )

    @trace
    async def get_new_device_msgs_for_remote(
        self, destination: str, last_stream_id: int, current_stream_id: int, limit: int
    ) -> Tuple[List[JsonDict], int]:
        """
        Args:
            destination: The name of the remote server.
            last_stream_id: The last position of the device message stream
                that the server sent up to.
            current_stream_id: The current position of the device message stream.
        Returns:
            A list of messages for the device and where in the stream the messages got to.
        """

        set_tag("destination", destination)
        set_tag("last_stream_id", last_stream_id)
        set_tag("current_stream_id", current_stream_id)
        set_tag("limit", limit)

        has_changed = self._device_federation_outbox_stream_cache.has_entity_changed(
            destination, last_stream_id
        )
        if not has_changed or last_stream_id == current_stream_id:
            log_kv({"message": "No new messages in stream"})
            return [], current_stream_id

        if limit <= 0:
            # This can happen if we run out of room for EDUs in the transaction.
            return [], last_stream_id

        @trace
        def get_new_messages_for_remote_destination_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[JsonDict], int]:
            sql = (
                "SELECT stream_id, messages_json FROM device_federation_outbox"
                " WHERE destination = ?"
                " AND ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC"
                " LIMIT ?"
            )
            txn.execute(sql, (destination, last_stream_id, current_stream_id, limit))

            messages = []
            stream_pos = current_stream_id

            for row in txn:
                stream_pos = row[0]
                messages.append(db_to_json(row[1]))

            # If the limit was not reached we know that there's no more data for this
            # user/device pair up to current_stream_id.
            if len(messages) < limit:
                log_kv({"message": "Set stream position to current position"})
                stream_pos = current_stream_id

            return messages, stream_pos

        return await self.db_pool.runInteraction(
            "get_new_device_msgs_for_remote",
            get_new_messages_for_remote_destination_txn,
        )

    @trace
    async def delete_device_msgs_for_remote(
        self, destination: str, up_to_stream_id: int
    ) -> None:
        """Used to delete messages when the remote destination acknowledges
        their receipt.

        Args:
            destination: The destination server_name
            up_to_stream_id: Where to delete messages up to.
        """

        def delete_messages_for_remote_destination_txn(txn: LoggingTransaction) -> None:
            sql = (
                "DELETE FROM device_federation_outbox"
                " WHERE destination = ?"
                " AND stream_id <= ?"
            )
            txn.execute(sql, (destination, up_to_stream_id))

        await self.db_pool.runInteraction(
            "delete_device_msgs_for_remote", delete_messages_for_remote_destination_txn
        )

    async def get_all_new_device_messages(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
        """Get updates for to device replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def get_all_new_device_messages_txn(
            txn: LoggingTransaction,
        ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
            # We limit like this as we might have multiple rows per stream_id, and
            # we want to make sure we always get all entries for any stream_id
            # we return.
            upto_token = min(current_id, last_id + limit)
            sql = (
                "SELECT max(stream_id), user_id"
                " FROM device_inbox"
                " WHERE ? < stream_id AND stream_id <= ?"
                " GROUP BY user_id"
            )
            txn.execute(sql, (last_id, upto_token))
            updates = [(row[0], row[1:]) for row in txn]

            sql = (
                "SELECT max(stream_id), destination"
                " FROM device_federation_outbox"
                " WHERE ? < stream_id AND stream_id <= ?"
                " GROUP BY destination"
            )
            txn.execute(sql, (last_id, upto_token))
            updates.extend((row[0], row[1:]) for row in txn)

            # Order by ascending stream ordering
            updates.sort()

            return updates, upto_token, upto_token < current_id

        return await self.db_pool.runInteraction(
            "get_all_new_device_messages", get_all_new_device_messages_txn
        )

    @trace
    async def add_messages_to_device_inbox(
        self,
        local_messages_by_user_then_device: Dict[str, Dict[str, JsonDict]],
        remote_messages_by_destination: Dict[str, JsonDict],
    ) -> int:
        """Used to send messages from this server.

        Args:
            local_messages_by_user_then_device:
                Dictionary of recipient user_id to recipient device_id to message.
            remote_messages_by_destination:
                Dictionary of destination server_name to the EDU JSON to send.

        Returns:
            The new stream_id.
        """

        assert self._can_write_to_device

        def add_messages_txn(
            txn: LoggingTransaction, now_ms: int, stream_id: int
        ) -> None:
            # Add the local messages directly to the local inbox.
            self._add_messages_to_local_device_inbox_txn(
                txn, stream_id, local_messages_by_user_then_device
            )

            # Add the remote messages to the federation outbox.
            # We'll send them to a remote server when we next send a
            # federation transaction to that destination.
            self.db_pool.simple_insert_many_txn(
                txn,
                table="device_federation_outbox",
                keys=(
                    "destination",
                    "stream_id",
                    "queued_ts",
                    "messages_json",
                    "instance_name",
                ),
                values=[
                    (
                        destination,
                        stream_id,
                        now_ms,
                        json_encoder.encode(edu),
                        self._instance_name,
                    )
                    for destination, edu in remote_messages_by_destination.items()
                ],
            )

            for destination, edu in remote_messages_by_destination.items():
                if issue9533_logger.isEnabledFor(logging.DEBUG):
                    issue9533_logger.debug(
                        "Queued outgoing to-device messages with "
                        "stream_id %i, EDU message_id %s, type %s for %s: %s",
                        stream_id,
                        edu["message_id"],
                        edu["type"],
                        destination,
                        [
                            f"{user_id}/{device_id} (msgid "
                            f"{msg.get(EventContentFields.TO_DEVICE_MSGID)})"
                            for (user_id, messages_by_device) in edu["messages"].items()
                            for (device_id, msg) in messages_by_device.items()
                        ],
                    )

                for user_id, messages_by_device in edu["messages"].items():
                    for device_id, msg in messages_by_device.items():
                        with start_active_span("store_outgoing_to_device_message"):
                            set_tag(SynapseTags.TO_DEVICE_EDU_ID, edu["sender"])
                            set_tag(SynapseTags.TO_DEVICE_EDU_ID, edu["message_id"])
                            set_tag(SynapseTags.TO_DEVICE_TYPE, edu["type"])
                            set_tag(SynapseTags.TO_DEVICE_RECIPIENT, user_id)
                            set_tag(SynapseTags.TO_DEVICE_RECIPIENT_DEVICE, device_id)
                            set_tag(
                                SynapseTags.TO_DEVICE_MSGID,
                                msg.get(EventContentFields.TO_DEVICE_MSGID),
                            )

        async with self._to_device_msg_id_gen.get_next() as stream_id:
            now_ms = self._clock.time_msec()
            await self.db_pool.runInteraction(
                "add_messages_to_device_inbox", add_messages_txn, now_ms, stream_id
            )
            for user_id in local_messages_by_user_then_device.keys():
                self._device_inbox_stream_cache.entity_has_changed(user_id, stream_id)
            for destination in remote_messages_by_destination.keys():
                self._device_federation_outbox_stream_cache.entity_has_changed(
                    destination, stream_id
                )

        return self._to_device_msg_id_gen.get_current_token()

    async def add_messages_from_remote_to_device_inbox(
        self,
        origin: str,
        message_id: str,
        local_messages_by_user_then_device: Dict[str, Dict[str, JsonDict]],
    ) -> int:
        assert self._can_write_to_device

        def add_messages_txn(
            txn: LoggingTransaction, now_ms: int, stream_id: int
        ) -> None:
            # Check if we've already inserted a matching message_id for that
            # origin. This can happen if the origin doesn't receive our
            # acknowledgement from the first time we received the message.
            already_inserted = self.db_pool.simple_select_list_txn(
                txn,
                table="device_federation_inbox",
                keyvalues={"origin": origin, "message_id": message_id},
                retcols=("message_id",),
            )
            if already_inserted:
                return

            # Add an entry for this message_id so that we know we've processed
            # it.
            self.db_pool.simple_insert_txn(
                txn,
                table="device_federation_inbox",
                values={
                    "origin": origin,
                    "message_id": message_id,
                    "received_ts": now_ms,
                },
            )

            # Add the messages to the appropriate local device inboxes so that
            # they'll be sent to the devices when they next sync.
            self._add_messages_to_local_device_inbox_txn(
                txn, stream_id, local_messages_by_user_then_device
            )

        async with self._to_device_msg_id_gen.get_next() as stream_id:
            now_ms = self._clock.time_msec()
            await self.db_pool.runInteraction(
                "add_messages_from_remote_to_device_inbox",
                add_messages_txn,
                now_ms,
                stream_id,
            )
            for user_id in local_messages_by_user_then_device.keys():
                self._device_inbox_stream_cache.entity_has_changed(user_id, stream_id)

        return stream_id

    def _add_messages_to_local_device_inbox_txn(
        self,
        txn: LoggingTransaction,
        stream_id: int,
        messages_by_user_then_device: Dict[str, Dict[str, JsonDict]],
    ) -> None:
        assert self._can_write_to_device

        local_by_user_then_device = {}
        for user_id, messages_by_device in messages_by_user_then_device.items():
            messages_json_for_user = {}
            devices = list(messages_by_device.keys())
            if len(devices) == 1 and devices[0] == "*":
                # Handle wildcard device_ids.
                # We exclude hidden devices (such as cross-signing keys) here as they are
                # not expected to receive to-device messages.
                devices = self.db_pool.simple_select_onecol_txn(
                    txn,
                    table="devices",
                    keyvalues={"user_id": user_id, "hidden": False},
                    retcol="device_id",
                )

                message_json = json_encoder.encode(messages_by_device["*"])
                for device_id in devices:
                    # Add the message for all devices for this user on this
                    # server.
                    messages_json_for_user[device_id] = message_json
            else:
                if not devices:
                    continue

                # We exclude hidden devices (such as cross-signing keys) here as they are
                # not expected to receive to-device messages.
                rows = cast(
                    List[Tuple[str]],
                    self.db_pool.simple_select_many_txn(
                        txn,
                        table="devices",
                        keyvalues={"user_id": user_id, "hidden": False},
                        column="device_id",
                        iterable=devices,
                        retcols=("device_id",),
                    ),
                )

                for (device_id,) in rows:
                    # Only insert into the local inbox if the device exists on
                    # this server
                    with start_active_span("serialise_to_device_message"):
                        msg = messages_by_device[device_id]
                        set_tag(SynapseTags.TO_DEVICE_TYPE, msg["type"])
                        set_tag(SynapseTags.TO_DEVICE_SENDER, msg["sender"])
                        set_tag(SynapseTags.TO_DEVICE_RECIPIENT, user_id)
                        set_tag(SynapseTags.TO_DEVICE_RECIPIENT_DEVICE, device_id)
                        set_tag(
                            SynapseTags.TO_DEVICE_MSGID,
                            msg["content"].get(EventContentFields.TO_DEVICE_MSGID),
                        )
                        message_json = json_encoder.encode(msg)

                    messages_json_for_user[device_id] = message_json

            if messages_json_for_user:
                local_by_user_then_device[user_id] = messages_json_for_user

        if not local_by_user_then_device:
            return

        self.db_pool.simple_insert_many_txn(
            txn,
            table="device_inbox",
            keys=("user_id", "device_id", "stream_id", "message_json", "instance_name"),
            values=[
                (user_id, device_id, stream_id, message_json, self._instance_name)
                for user_id, messages_by_device in local_by_user_then_device.items()
                for device_id, message_json in messages_by_device.items()
            ],
        )

        if issue9533_logger.isEnabledFor(logging.DEBUG):
            issue9533_logger.debug(
                "Stored to-device messages with stream_id %i: %s",
                stream_id,
                [
                    f"{user_id}/{device_id} (msgid "
                    f"{msg['content'].get(EventContentFields.TO_DEVICE_MSGID)})"
                    for (
                        user_id,
                        messages_by_device,
                    ) in messages_by_user_then_device.items()
                    for (device_id, msg) in messages_by_device.items()
                ],
            )


class DeviceInboxBackgroundUpdateStore(SQLBaseStore):
    DEVICE_INBOX_STREAM_ID = "device_inbox_stream_drop"
    REMOVE_DEAD_DEVICES_FROM_INBOX = "remove_dead_devices_from_device_inbox"
    CLEANUP_DEVICE_FEDERATION_OUTBOX = "cleanup_device_federation_outbox"

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_index_update(
            "device_inbox_stream_index",
            index_name="device_inbox_stream_id_user_id",
            table="device_inbox",
            columns=["stream_id", "user_id"],
        )

        self.db_pool.updates.register_background_update_handler(
            self.DEVICE_INBOX_STREAM_ID, self._background_drop_index_device_inbox
        )

        self.db_pool.updates.register_background_update_handler(
            self.REMOVE_DEAD_DEVICES_FROM_INBOX,
            self._remove_dead_devices_from_device_inbox,
        )

        self.db_pool.updates.register_background_update_handler(
            self.CLEANUP_DEVICE_FEDERATION_OUTBOX,
            self._cleanup_device_federation_outbox,
        )

    async def _background_drop_index_device_inbox(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        def reindex_txn(conn: LoggingDatabaseConnection) -> None:
            txn = conn.cursor()
            txn.execute("DROP INDEX IF EXISTS device_inbox_stream_id")
            txn.close()

        await self.db_pool.runWithConnection(reindex_txn)

        await self.db_pool.updates._end_background_update(self.DEVICE_INBOX_STREAM_ID)

        return 1

    async def _remove_dead_devices_from_device_inbox(
        self,
        progress: JsonDict,
        batch_size: int,
    ) -> int:
        """A background update to remove devices that were either deleted or hidden from
        the device_inbox table.

        Args:
            progress: The update's progress dict.
            batch_size: The batch size for this update.

        Returns:
            The number of rows deleted.
        """

        def _remove_dead_devices_from_device_inbox_txn(
            txn: LoggingTransaction,
        ) -> Tuple[int, bool]:
            if "max_stream_id" in progress:
                max_stream_id = progress["max_stream_id"]
            else:
                txn.execute("SELECT max(stream_id) FROM device_inbox")
                # There's a type mismatch here between how we want to type the row and
                # what fetchone says it returns, but we silence it because we know that
                # res can't be None.
                res = cast(Tuple[Optional[int]], txn.fetchone())
                if res[0] is None:
                    # this can only happen if the `device_inbox` table is empty, in which
                    # case we have no work to do.
                    return 0, True
                else:
                    max_stream_id = res[0]

            start = progress.get("stream_id", 0)
            stop = start + batch_size

            # delete rows in `device_inbox` which do *not* correspond to a known,
            # unhidden device.
            sql = """
                DELETE FROM device_inbox
                WHERE
                    stream_id >= ? AND stream_id < ?
                    AND NOT EXISTS (
                        SELECT * FROM devices d
                        WHERE
                            d.device_id=device_inbox.device_id
                            AND d.user_id=device_inbox.user_id
                            AND NOT hidden
                    )
                """

            txn.execute(sql, (start, stop))

            self.db_pool.updates._background_update_progress_txn(
                txn,
                self.REMOVE_DEAD_DEVICES_FROM_INBOX,
                {
                    "stream_id": stop,
                    "max_stream_id": max_stream_id,
                },
            )

            return stop > max_stream_id

        finished = await self.db_pool.runInteraction(
            "_remove_devices_from_device_inbox_txn",
            _remove_dead_devices_from_device_inbox_txn,
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                self.REMOVE_DEAD_DEVICES_FROM_INBOX,
            )

        return batch_size

    async def _cleanup_device_federation_outbox(
        self,
        progress: JsonDict,
        batch_size: int,
    ) -> int:
        def _cleanup_device_federation_outbox_txn(
            txn: LoggingTransaction,
        ) -> bool:
            if "max_stream_id" in progress:
                max_stream_id = progress["max_stream_id"]
            else:
                txn.execute("SELECT max(stream_id) FROM device_federation_outbox")
                res = cast(Tuple[Optional[int]], txn.fetchone())
                if res[0] is None:
                    # this can only happen if the `device_inbox` table is empty, in which
                    # case we have no work to do.
                    return True
                else:
                    max_stream_id = res[0]

            start = progress.get("stream_id", 0)
            stop = start + batch_size

            sql = """
                SELECT destination FROM device_federation_outbox
                WHERE ? < stream_id AND stream_id <= ?
            """

            txn.execute(sql, (start, stop))

            destinations = {d for (d,) in txn}
            to_remove = set()
            for d in destinations:
                try:
                    parse_and_validate_server_name(d)
                except ValueError:
                    to_remove.add(d)

            self.db_pool.simple_delete_many_txn(
                txn,
                table="device_federation_outbox",
                column="destination",
                values=to_remove,
                keyvalues={},
            )

            self.db_pool.updates._background_update_progress_txn(
                txn,
                self.CLEANUP_DEVICE_FEDERATION_OUTBOX,
                {
                    "stream_id": stop,
                    "max_stream_id": max_stream_id,
                },
            )

            return stop >= max_stream_id

        finished = await self.db_pool.runInteraction(
            "_cleanup_device_federation_outbox",
            _cleanup_device_federation_outbox_txn,
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                self.CLEANUP_DEVICE_FEDERATION_OUTBOX,
            )

        return batch_size


class DeviceInboxStore(DeviceInboxWorkerStore, DeviceInboxBackgroundUpdateStore):
    pass
