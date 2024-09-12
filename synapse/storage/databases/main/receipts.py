#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

import attr
from immutabledict import immutabledict

from synapse.api.constants import EduTypes
from synapse.replication.tcp.streams import ReceiptsStream
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_in_list_sql_clause,
)
from synapse.storage.engines._base import IsolationLevel
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import (
    JsonDict,
    JsonMapping,
    MultiWriterStreamToken,
    PersistedPosition,
    StrCollection,
)
from synapse.util import json_encoder
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.iterutils import batch_iter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class ReceiptInRoom:
    receipt_type: str
    user_id: str
    event_id: str
    thread_id: Optional[str]
    data: JsonMapping

    @staticmethod
    def merge_to_content(receipts: Collection["ReceiptInRoom"]) -> JsonMapping:
        """Merge the given set of receipts (in a room) into the receipt
        content format.

        Returns:
            A mapping of the combined receipts: event ID -> receipt type -> user
            ID -> receipt data.
        """
        # MSC4102: always replace threaded receipts with unthreaded ones if
        # there is a clash. This means we will drop some receipts, but MSC4102
        # is designed to drop semantically meaningless receipts, so this is
        # okay. Previously, we would drop meaningful data!
        #
        # We do this by finding the unthreaded receipts, and then filtering out
        # matching threaded receipts.

        # Set of (user_id, event_id)
        unthreaded_receipts: Set[Tuple[str, str]] = {
            (receipt.user_id, receipt.event_id)
            for receipt in receipts
            if receipt.thread_id is None
        }

        # event_id -> receipt_type -> user_id -> receipt data
        content: Dict[str, Dict[str, Dict[str, JsonMapping]]] = {}
        for receipt in receipts:
            data = receipt.data
            if receipt.thread_id is not None:
                if (receipt.user_id, receipt.event_id) in unthreaded_receipts:
                    # Ignore threaded receipts if we have an unthreaded one.
                    continue

                data = dict(data)
                data["thread_id"] = receipt.thread_id

            content.setdefault(receipt.event_id, {}).setdefault(
                receipt.receipt_type, {}
            )[receipt.user_id] = data

        return content


class ReceiptsWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self._instance_name = hs.get_instance_name()

        # In the worker store this is an ID tracker which we overwrite in the non-worker
        # class below that is used on the main process.
        self._receipts_id_gen: MultiWriterIdGenerator

        self._can_write_to_receipts = (
            self._instance_name in hs.config.worker.writers.receipts
        )

        self._receipts_id_gen = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="receipts",
            instance_name=self._instance_name,
            tables=[("receipts_linearized", "instance_name", "stream_id")],
            sequence_name="receipts_sequence",
            writers=hs.config.worker.writers.receipts,
        )

        super().__init__(database, db_conn, hs)

        max_receipts_stream_id = self.get_max_receipt_stream_id()
        receipts_stream_prefill, min_receipts_stream_id = self.db_pool.get_cache_dict(
            db_conn,
            "receipts_linearized",
            entity_column="room_id",
            stream_column="stream_id",
            max_value=max_receipts_stream_id.stream,
            limit=10000,
        )
        self._receipts_stream_cache = StreamChangeCache(
            "ReceiptsRoomChangeCache",
            min_receipts_stream_id,
            prefilled_cache=receipts_stream_prefill,
        )

    def get_max_receipt_stream_id(self) -> MultiWriterStreamToken:
        """Get the current max stream ID for receipts stream"""

        min_pos = self._receipts_id_gen.get_current_token()

        positions = {}
        if isinstance(self._receipts_id_gen, MultiWriterIdGenerator):
            # The `min_pos` is the minimum position that we know all instances
            # have finished persisting to, so we only care about instances whose
            # positions are ahead of that. (Instance positions can be behind the
            # min position as there are times we can work out that the minimum
            # position is ahead of the naive minimum across all current
            # positions. See MultiWriterIdGenerator for details)
            positions = {
                i: p
                for i, p in self._receipts_id_gen.get_positions().items()
                if p > min_pos
            }

        return MultiWriterStreamToken(
            stream=min_pos, instance_map=immutabledict(positions)
        )

    def get_receipt_stream_id_for_instance(self, instance_name: str) -> int:
        return self._receipts_id_gen.get_current_token_for_writer(instance_name)

    def get_receipts_stream_id_gen(self) -> MultiWriterIdGenerator:
        return self._receipts_id_gen

    def get_last_unthreaded_receipt_for_user_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        room_id: str,
        receipt_types: Collection[str],
    ) -> Optional[Tuple[str, int]]:
        """
        Fetch the event ID and stream_ordering for the latest unthreaded receipt
        in a room with one of the given receipt types.

        Args:
            user_id: The user to fetch receipts for.
            room_id: The room ID to fetch the receipt for.
            receipt_types: The receipt types to fetch.

        Returns:
            The event ID and stream ordering of the latest receipt, if one exists.
        """

        clause, args = make_in_list_sql_clause(
            self.database_engine, "receipt_type", receipt_types
        )

        sql = f"""
            SELECT event_id, event_stream_ordering
            FROM receipts_linearized
            WHERE {clause}
            AND user_id = ?
            AND room_id = ?
            AND thread_id IS NULL
            ORDER BY event_stream_ordering DESC
            LIMIT 1
        """

        args.extend((user_id, room_id))
        txn.execute(sql, args)

        return cast(Optional[Tuple[str, int]], txn.fetchone())

    async def get_receipts_for_user(
        self, user_id: str, receipt_types: Iterable[str]
    ) -> Dict[str, str]:
        """
        Fetch the event IDs for the latest receipts sent by the given user.

        Args:
            user_id: The user to fetch receipts for.
            receipt_types: The receipt types to check.

        Returns:
            A map of room ID to the event ID of the latest receipt for that room.

            If the user has not sent a receipt to a room then it will not appear
            in the returned dictionary.
        """
        results = await self.get_receipts_for_user_with_orderings(
            user_id, receipt_types
        )

        # Reduce the result to room ID -> event ID.
        return {
            room_id: room_result["event_id"] for room_id, room_result in results.items()
        }

    async def get_receipts_for_user_with_orderings(
        self, user_id: str, receipt_types: Iterable[str]
    ) -> JsonDict:
        """
        Fetch receipts for all rooms that the given user is joined to.

        Args:
            user_id: The user to fetch receipts for.
            receipt_types: The receipt types to fetch. Earlier receipt types
                are given priority if multiple receipts point to the same event.

        Returns:
            A map of room ID to the latest receipt (for the given types).
        """
        results: JsonDict = {}
        for receipt_type in receipt_types:
            partial_result = await self._get_receipts_for_user_with_orderings(
                user_id, receipt_type
            )
            for room_id, room_result in partial_result.items():
                # If the room has not yet been seen, or the receipt is newer,
                # use it.
                if (
                    room_id not in results
                    or results[room_id]["stream_ordering"]
                    < room_result["stream_ordering"]
                ):
                    results[room_id] = room_result

        return results

    @cached()
    async def _get_receipts_for_user_with_orderings(
        self, user_id: str, receipt_type: str
    ) -> JsonMapping:
        """
        Fetch receipts for all rooms that the given user is joined to.

        Args:
            user_id: The user to fetch receipts for.
            receipt_type: The receipt type to fetch.

        Returns:
            A map of room ID to the latest receipt information.
        """

        def f(txn: LoggingTransaction) -> List[Tuple[str, str, int, int]]:
            sql = (
                "SELECT rl.room_id, rl.event_id,"
                " e.topological_ordering, e.stream_ordering"
                " FROM receipts_linearized AS rl"
                " INNER JOIN events AS e USING (room_id, event_id)"
                " WHERE rl.room_id = e.room_id"
                " AND rl.event_id = e.event_id"
                " AND user_id = ?"
                " AND receipt_type = ?"
            )
            txn.execute(sql, (user_id, receipt_type))
            return cast(List[Tuple[str, str, int, int]], txn.fetchall())

        rows = await self.db_pool.runInteraction(
            "get_receipts_for_user_with_orderings", f
        )
        return {
            row[0]: {
                "event_id": row[1],
                "topological_ordering": row[2],
                "stream_ordering": row[3],
            }
            for row in rows
        }

    async def get_linearized_receipts_for_rooms(
        self,
        room_ids: Iterable[str],
        to_key: MultiWriterStreamToken,
        from_key: Optional[MultiWriterStreamToken] = None,
    ) -> List[JsonMapping]:
        """Get receipts for multiple rooms for sending to clients.

        Args:
            room_id: The room IDs to fetch receipts of.
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A list of receipts.
        """
        room_ids = set(room_ids)

        if from_key is not None:
            # Only ask the database about rooms where there have been new
            # receipts added since `from_key`
            room_ids = self._receipts_stream_cache.get_entities_changed(
                room_ids, from_key.stream
            )

        results = await self._get_linearized_receipts_for_rooms(
            room_ids, to_key, from_key=from_key
        )

        return [ev for res in results.values() for ev in res]

    async def get_linearized_receipts_for_room(
        self,
        room_id: str,
        to_key: MultiWriterStreamToken,
        from_key: Optional[MultiWriterStreamToken] = None,
    ) -> Sequence[JsonMapping]:
        """Get receipts for a single room for sending to clients.

        Args:
            room_ids: The room id.
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A list of receipts.
        """
        if from_key is not None:
            # Check the cache first to see if any new receipts have been added
            # since`from_key`. If not we can no-op.
            if not self._receipts_stream_cache.has_entity_changed(
                room_id, from_key.stream
            ):
                return []

        return await self._get_linearized_receipts_for_room(room_id, to_key, from_key)

    @cached(tree=True)
    async def _get_linearized_receipts_for_room(
        self,
        room_id: str,
        to_key: MultiWriterStreamToken,
        from_key: Optional[MultiWriterStreamToken] = None,
    ) -> Sequence[JsonMapping]:
        """See get_linearized_receipts_for_room"""

        def f(txn: LoggingTransaction) -> List[Tuple[str, str, str, str]]:
            if from_key:
                sql = """
                    SELECT stream_id, instance_name, receipt_type, user_id, event_id, data
                    FROM receipts_linearized
                    WHERE room_id = ? AND stream_id > ? AND stream_id <= ?
                """

                txn.execute(
                    sql, (room_id, from_key.stream, to_key.get_max_stream_pos())
                )
            else:
                sql = """
                    SELECT stream_id, instance_name, receipt_type, user_id, event_id, data
                    FROM receipts_linearized WHERE
                    room_id = ? AND stream_id <= ?
                """

                txn.execute(sql, (room_id, to_key.get_max_stream_pos()))

            return [
                (receipt_type, user_id, event_id, data)
                for stream_id, instance_name, receipt_type, user_id, event_id, data in txn
                if MultiWriterStreamToken.is_stream_position_in_range(
                    from_key, to_key, instance_name, stream_id
                )
            ]

        rows = await self.db_pool.runInteraction("get_linearized_receipts_for_room", f)

        if not rows:
            return []

        content: JsonDict = {}
        for receipt_type, user_id, event_id, data in rows:
            content.setdefault(event_id, {}).setdefault(receipt_type, {})[user_id] = (
                db_to_json(data)
            )

        return [{"type": EduTypes.RECEIPT, "room_id": room_id, "content": content}]

    @cachedList(
        cached_method_name="_get_linearized_receipts_for_room",
        list_name="room_ids",
        num_args=3,
    )
    async def _get_linearized_receipts_for_rooms(
        self,
        room_ids: Collection[str],
        to_key: MultiWriterStreamToken,
        from_key: Optional[MultiWriterStreamToken] = None,
    ) -> Mapping[str, Sequence[JsonMapping]]:
        if not room_ids:
            return {}

        def f(
            txn: LoggingTransaction,
        ) -> Mapping[str, Sequence[ReceiptInRoom]]:
            if from_key:
                sql = """
                    SELECT stream_id, instance_name, room_id, receipt_type,
                        user_id, event_id, thread_id, data
                    FROM receipts_linearized WHERE
                    stream_id > ? AND stream_id <= ? AND
                """
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "room_id", room_ids
                )

                txn.execute(
                    sql + clause,
                    [from_key.stream, to_key.get_max_stream_pos()] + list(args),
                )
            else:
                sql = """
                    SELECT stream_id, instance_name, room_id, receipt_type,
                        user_id, event_id, thread_id, data
                    FROM receipts_linearized WHERE
                    stream_id <= ? AND
                """

                clause, args = make_in_list_sql_clause(
                    self.database_engine, "room_id", room_ids
                )

                txn.execute(sql + clause, [to_key.get_max_stream_pos()] + list(args))

            results: Dict[str, List[ReceiptInRoom]] = {}
            for (
                stream_id,
                instance_name,
                room_id,
                receipt_type,
                user_id,
                event_id,
                thread_id,
                data,
            ) in txn:
                if not MultiWriterStreamToken.is_stream_position_in_range(
                    from_key, to_key, instance_name, stream_id
                ):
                    continue

                results.setdefault(room_id, []).append(
                    ReceiptInRoom(
                        receipt_type=receipt_type,
                        user_id=user_id,
                        event_id=event_id,
                        thread_id=thread_id,
                        data=db_to_json(data),
                    )
                )

            return results

        txn_results = await self.db_pool.runInteraction(
            "_get_linearized_receipts_for_rooms", f
        )

        results: JsonDict = {
            room_id: {
                "room_id": room_id,
                "type": EduTypes.RECEIPT,
                "content": ReceiptInRoom.merge_to_content(receipts),
            }
            for room_id, receipts in txn_results.items()
        }

        results = {
            room_id: [results[room_id]] if room_id in results else []
            for room_id in room_ids
        }
        return results

    async def get_linearized_receipts_for_events(
        self,
        room_and_event_ids: Collection[Tuple[str, str]],
    ) -> Mapping[str, Sequence[ReceiptInRoom]]:
        """Get all receipts for the given set of events.

        Arguments:
            room_and_event_ids: A collection of 2-tuples of room ID and
                event IDs to fetch receipts for

        Returns:
            A list of receipts, one per room.
        """
        if not room_and_event_ids:
            return {}

        def get_linearized_receipts_for_events_txn(
            txn: LoggingTransaction,
            room_id_event_id_tuples: Collection[Tuple[str, str]],
        ) -> List[Tuple[str, str, str, str, Optional[str], str]]:
            clause, args = make_tuple_in_list_sql_clause(
                self.database_engine, ("room_id", "event_id"), room_id_event_id_tuples
            )

            sql = f"""
                SELECT room_id, receipt_type, user_id, event_id, thread_id, data
                FROM receipts_linearized
                WHERE {clause}
            """

            txn.execute(sql, args)

            return txn.fetchall()

        # room_id -> receipts
        room_to_receipts: Dict[str, List[ReceiptInRoom]] = {}
        for batch in batch_iter(room_and_event_ids, 1000):
            batch_results = await self.db_pool.runInteraction(
                "get_linearized_receipts_for_events",
                get_linearized_receipts_for_events_txn,
                batch,
            )

            for (
                room_id,
                receipt_type,
                user_id,
                event_id,
                thread_id,
                data,
            ) in batch_results:
                room_to_receipts.setdefault(room_id, []).append(
                    ReceiptInRoom(
                        receipt_type=receipt_type,
                        user_id=user_id,
                        event_id=event_id,
                        thread_id=thread_id,
                        data=db_to_json(data),
                    )
                )

        return room_to_receipts

    @cached(
        num_args=2,
    )
    async def get_linearized_receipts_for_all_rooms(
        self,
        to_key: MultiWriterStreamToken,
        from_key: Optional[MultiWriterStreamToken] = None,
    ) -> Mapping[str, JsonMapping]:
        """Get receipts for all rooms between two stream_ids, up
        to a limit of the latest 100 read receipts.

        Args:
            to_key: Max stream id to fetch receipts up to.
            from_key: Min stream id to fetch receipts from. None fetches
                from the start.

        Returns:
            A dictionary of roomids to a list of receipts.
        """

        def f(txn: LoggingTransaction) -> List[Tuple[str, str, str, str, str]]:
            if from_key:
                sql = """
                    SELECT stream_id, instance_name, room_id, receipt_type, user_id, event_id, data
                    FROM receipts_linearized WHERE
                    stream_id > ? AND stream_id <= ?
                    ORDER BY stream_id DESC
                    LIMIT 100
                """
                txn.execute(sql, [from_key.stream, to_key.get_max_stream_pos()])
            else:
                sql = """
                    SELECT stream_id, instance_name, room_id, receipt_type, user_id, event_id, data
                    FROM receipts_linearized WHERE
                    stream_id <= ?
                    ORDER BY stream_id DESC
                    LIMIT 100
                """

                txn.execute(sql, [to_key.get_max_stream_pos()])

            return [
                (room_id, receipt_type, user_id, event_id, data)
                for stream_id, instance_name, room_id, receipt_type, user_id, event_id, data in txn
                if MultiWriterStreamToken.is_stream_position_in_range(
                    from_key, to_key, instance_name, stream_id
                )
            ]

        txn_results = await self.db_pool.runInteraction(
            "get_linearized_receipts_for_all_rooms", f
        )

        results: JsonDict = {}
        for room_id, receipt_type, user_id, event_id, data in txn_results:
            # We want a single event per room, since we want to batch the
            # receipts by room, event and type.
            room_event = results.setdefault(
                room_id,
                {"type": EduTypes.RECEIPT, "room_id": room_id, "content": {}},
            )

            # The content is of the form:
            # {"$foo:bar": { "read": { "@user:host": <receipt> }, .. }, .. }
            event_entry = room_event["content"].setdefault(event_id, {})
            receipt_type_dict = event_entry.setdefault(receipt_type, {})

            receipt_type_dict[user_id] = db_to_json(data)

        return results

    async def get_linearized_receipts_for_user_in_rooms(
        self, user_id: str, room_ids: StrCollection, to_key: MultiWriterStreamToken
    ) -> Mapping[str, Sequence[ReceiptInRoom]]:
        """Fetch all receipts for the user in the given room.

        Returns:
            A dict from room ID to receipts in the room.
        """

        def get_linearized_receipts_for_user_in_rooms_txn(
            txn: LoggingTransaction,
            batch_room_ids: StrCollection,
        ) -> List[Tuple[str, str, str, str, Optional[str], str]]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", batch_room_ids
            )

            sql = f"""
                SELECT instance_name, stream_id, room_id, receipt_type, user_id, event_id, thread_id, data
                FROM receipts_linearized
                WHERE {clause} AND user_id = ? AND stream_id <= ?
            """

            args.append(user_id)
            args.append(to_key.get_max_stream_pos())

            txn.execute(sql, args)

            return [
                (room_id, receipt_type, user_id, event_id, thread_id, data)
                for instance_name, stream_id, room_id, receipt_type, user_id, event_id, thread_id, data in txn
                if MultiWriterStreamToken.is_stream_position_in_range(
                    low=None,
                    high=to_key,
                    instance_name=instance_name,
                    pos=stream_id,
                )
            ]

        # room_id -> receipts
        room_to_receipts: Dict[str, List[ReceiptInRoom]] = {}
        for batch in batch_iter(room_ids, 1000):
            batch_results = await self.db_pool.runInteraction(
                "get_linearized_receipts_for_events",
                get_linearized_receipts_for_user_in_rooms_txn,
                batch,
            )

            for (
                room_id,
                receipt_type,
                user_id,
                event_id,
                thread_id,
                data,
            ) in batch_results:
                room_to_receipts.setdefault(room_id, []).append(
                    ReceiptInRoom(
                        receipt_type=receipt_type,
                        user_id=user_id,
                        event_id=event_id,
                        thread_id=thread_id,
                        data=db_to_json(data),
                    )
                )

        return room_to_receipts

    async def get_rooms_with_receipts_between(
        self,
        room_ids: StrCollection,
        from_key: MultiWriterStreamToken,
        to_key: MultiWriterStreamToken,
    ) -> StrCollection:
        """Given a set of room_ids, find out which ones (may) have receipts
        between the two tokens (> `from_token` and <= `to_token`)."""

        room_ids = self._receipts_stream_cache.get_entities_changed(
            room_ids, from_key.stream
        )
        if not room_ids:
            return []

        def f(txn: LoggingTransaction, room_ids: StrCollection) -> StrCollection:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", room_ids
            )

            sql = f"""
                SELECT DISTINCT room_id FROM receipts_linearized
                WHERE {clause} AND ? < stream_id AND stream_id <= ?
            """
            args.append(from_key.stream)
            args.append(to_key.get_max_stream_pos())

            txn.execute(sql, args)

            return [room_id for (room_id,) in txn]

        results: List[str] = []
        for batch in batch_iter(room_ids, 1000):
            batch_result = await self.db_pool.runInteraction(
                "get_rooms_with_receipts_between", f, batch
            )
            results.extend(batch_result)

        return results

    async def get_users_sent_receipts_between(
        self, last_id: int, current_id: int
    ) -> List[str]:
        """Get all users who sent receipts between `last_id` exclusive and
        `current_id` inclusive.

        Returns:
            The list of users.
        """

        if last_id == current_id:
            return []

        def _get_users_sent_receipts_between_txn(txn: LoggingTransaction) -> List[str]:
            sql = """
                SELECT DISTINCT user_id FROM receipts_linearized
                WHERE ? < stream_id AND stream_id <= ?
            """
            txn.execute(sql, (last_id, current_id))

            return [r[0] for r in txn]

        return await self.db_pool.runInteraction(
            "get_users_sent_receipts_between", _get_users_sent_receipts_between_txn
        )

    async def get_all_updated_receipts(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[
        List[Tuple[int, Tuple[str, str, str, str, Optional[str], JsonDict]]], int, bool
    ]:
        """Get updates for receipts replication stream.

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

        def get_all_updated_receipts_txn(
            txn: LoggingTransaction,
        ) -> Tuple[
            List[Tuple[int, Tuple[str, str, str, str, Optional[str], JsonDict]]],
            int,
            bool,
        ]:
            sql = """
                SELECT stream_id, room_id, receipt_type, user_id, event_id, thread_id, data
                FROM receipts_linearized
                WHERE ? < stream_id AND stream_id <= ?
                AND instance_name = ?
                ORDER BY stream_id ASC
                LIMIT ?
            """
            txn.execute(sql, (last_id, current_id, instance_name, limit))

            updates = cast(
                List[Tuple[int, Tuple[str, str, str, str, Optional[str], JsonDict]]],
                [(r[0], r[1:6] + (db_to_json(r[6]),)) for r in txn],
            )

            limited = False
            upper_bound = current_id

            if len(updates) == limit:
                limited = True
                upper_bound = updates[-1][0]

            return updates, upper_bound, limited

        return await self.db_pool.runInteraction(
            "get_all_updated_receipts", get_all_updated_receipts_txn
        )

    def invalidate_caches_for_receipt(
        self, room_id: str, receipt_type: str, user_id: str
    ) -> None:
        self._get_receipts_for_user_with_orderings.invalidate((user_id, receipt_type))
        self._get_linearized_receipts_for_room.invalidate((room_id,))

        # We use this method to invalidate so that we don't end up with circular
        # dependencies between the receipts and push action stores.
        self._attempt_to_invalidate_cache(
            "get_unread_event_push_actions_by_room_for_user", (room_id,)
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == ReceiptsStream.NAME:
            self._receipts_id_gen.advance(instance_name, token)
            for row in rows:
                self.invalidate_caches_for_receipt(
                    row.room_id, row.receipt_type, row.user_id
                )
                self._receipts_stream_cache.entity_has_changed(row.room_id, token)

        return super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == ReceiptsStream.NAME:
            self._receipts_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def _insert_linearized_receipt_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_id: str,
        thread_id: Optional[str],
        data: JsonDict,
        stream_id: int,
    ) -> Optional[int]:
        """Inserts a receipt into the database if it's newer than the current one.

        Returns:
            None if the receipt is older than the current receipt
            otherwise, the rx timestamp of the event that the receipt corresponds to
                (or 0 if the event is unknown)
        """
        assert self._can_write_to_receipts

        res = self.db_pool.simple_select_one_txn(
            txn,
            table="events",
            retcols=["stream_ordering", "received_ts"],
            keyvalues={"event_id": event_id},
            allow_none=True,
        )

        stream_ordering = int(res[0]) if res else None
        rx_ts = res[1] if res else 0

        # We don't want to clobber receipts for more recent events, so we
        # have to compare orderings of existing receipts
        if stream_ordering is not None:
            if thread_id is None:
                thread_clause = "r.thread_id IS NULL"
                thread_args: Tuple[str, ...] = ()
            else:
                thread_clause = "r.thread_id = ?"
                thread_args = (thread_id,)

            # If the receipt doesn't have a stream ordering it is because we
            # don't have the associated event, and so must be a remote receipt.
            # Hence it's safe to just allow new receipts to clobber it.
            sql = f"""
            SELECT r.event_stream_ordering, r.event_id FROM receipts_linearized AS r
            WHERE r.room_id = ? AND r.receipt_type = ? AND r.user_id = ?
            AND r.event_stream_ordering IS NOT NULL AND {thread_clause}
            """
            txn.execute(
                sql,
                (
                    room_id,
                    receipt_type,
                    user_id,
                )
                + thread_args,
            )

            for so, eid in txn:
                if int(so) >= stream_ordering:
                    logger.debug(
                        "Ignoring new receipt for %s in favour of existing "
                        "one for later event %s",
                        event_id,
                        eid,
                    )
                    return None

        txn.call_after(
            self.invalidate_caches_for_receipt, room_id, receipt_type, user_id
        )

        txn.call_after(
            self._receipts_stream_cache.entity_has_changed, room_id, stream_id
        )

        keyvalues = {
            "room_id": room_id,
            "receipt_type": receipt_type,
            "user_id": user_id,
        }
        where_clause = ""
        if thread_id is None:
            where_clause = "thread_id IS NULL"
        else:
            keyvalues["thread_id"] = thread_id

        self.db_pool.simple_upsert_txn(
            txn,
            table="receipts_linearized",
            keyvalues=keyvalues,
            values={
                "stream_id": stream_id,
                "instance_name": self._instance_name,
                "event_id": event_id,
                "event_stream_ordering": stream_ordering,
                "data": json_encoder.encode(data),
            },
            where_clause=where_clause,
        )

        return rx_ts

    def _graph_to_linear(
        self, txn: LoggingTransaction, room_id: str, event_ids: List[str]
    ) -> str:
        """
        Generate a linearized event from a list of events (i.e. a list of forward
        extremities in the room).

        This should allow for calculation of the correct read receipt even if
        servers have different event ordering.

        Args:
            txn: The transaction
            room_id: The room ID the events are in.
            event_ids: The list of event IDs to linearize.

        Returns:
            The linearized event ID.
        """
        # TODO: Make this better.
        clause, args = make_in_list_sql_clause(
            self.database_engine, "event_id", event_ids
        )

        sql = """
            SELECT event_id WHERE room_id = ? AND stream_ordering IN (
                SELECT max(stream_ordering) WHERE %s
            )
        """ % (clause,)

        txn.execute(sql, [room_id] + list(args))
        rows = txn.fetchall()
        if rows:
            return rows[0][0]
        else:
            raise RuntimeError("Unrecognized event_ids: %r" % (event_ids,))

    async def insert_receipt(
        self,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_ids: List[str],
        thread_id: Optional[str],
        data: dict,
    ) -> Optional[PersistedPosition]:
        """Insert a receipt, either from local client or remote server.

        Automatically does conversion between linearized and graph
        representations.

        Returns:
            The new receipts stream ID and token, if the receipt is newer than
            what was previously persisted. None, otherwise.
        """
        assert self._can_write_to_receipts

        if not event_ids:
            return None

        if len(event_ids) == 1:
            linearized_event_id = event_ids[0]
        else:
            # we need to points in graph -> linearized form.
            linearized_event_id = await self.db_pool.runInteraction(
                "insert_receipt_conv", self._graph_to_linear, room_id, event_ids
            )

        async with self._receipts_id_gen.get_next() as stream_id:
            event_ts = await self.db_pool.runInteraction(
                "insert_linearized_receipt",
                self._insert_linearized_receipt_txn,
                room_id,
                receipt_type,
                user_id,
                linearized_event_id,
                thread_id,
                data,
                stream_id=stream_id,
                # Read committed is actually beneficial here because we check for a receipt with
                # greater stream order, and checking the very latest data at select time is better
                # than the data at transaction start time.
                isolation_level=IsolationLevel.READ_COMMITTED,
            )

        # If the receipt was older than the currently persisted one, nothing to do.
        if event_ts is None:
            return None

        now = self._clock.time_msec()
        logger.debug(
            "Receipt %s for event %s in %s (%i ms old)",
            receipt_type,
            linearized_event_id,
            room_id,
            now - event_ts,
        )

        await self._insert_graph_receipt(
            room_id,
            receipt_type,
            user_id,
            event_ids,
            thread_id,
            data,
        )

        return PersistedPosition(self._instance_name, stream_id)

    async def _insert_graph_receipt(
        self,
        room_id: str,
        receipt_type: str,
        user_id: str,
        event_ids: List[str],
        thread_id: Optional[str],
        data: JsonDict,
    ) -> None:
        assert self._can_write_to_receipts

        keyvalues = {
            "room_id": room_id,
            "receipt_type": receipt_type,
            "user_id": user_id,
        }
        where_clause = ""
        if thread_id is None:
            where_clause = "thread_id IS NULL"
        else:
            keyvalues["thread_id"] = thread_id

        await self.db_pool.simple_upsert(
            desc="insert_graph_receipt",
            table="receipts_graph",
            keyvalues=keyvalues,
            values={
                "event_ids": json_encoder.encode(event_ids),
                "data": json_encoder.encode(data),
            },
            where_clause=where_clause,
        )

        self._get_receipts_for_user_with_orderings.invalidate((user_id, receipt_type))

        # FIXME: This shouldn't invalidate the whole cache
        self._get_linearized_receipts_for_room.invalidate((room_id,))


class ReceiptsBackgroundUpdateStore(SQLBaseStore):
    POPULATE_RECEIPT_EVENT_STREAM_ORDERING = "populate_event_stream_ordering"
    RECEIPTS_LINEARIZED_UNIQUE_INDEX_UPDATE_NAME = "receipts_linearized_unique_index"
    RECEIPTS_GRAPH_UNIQUE_INDEX_UPDATE_NAME = "receipts_graph_unique_index"

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_update_handler(
            self.POPULATE_RECEIPT_EVENT_STREAM_ORDERING,
            self._populate_receipt_event_stream_ordering,
        )
        self.db_pool.updates.register_background_update_handler(
            self.RECEIPTS_LINEARIZED_UNIQUE_INDEX_UPDATE_NAME,
            self._background_receipts_linearized_unique_index,
        )
        self.db_pool.updates.register_background_update_handler(
            self.RECEIPTS_GRAPH_UNIQUE_INDEX_UPDATE_NAME,
            self._background_receipts_graph_unique_index,
        )
        self.db_pool.updates.register_background_index_update(
            update_name="receipts_room_id_event_id_index",
            index_name="receipts_linearized_event_id",
            table="receipts_linearized",
            columns=("room_id", "event_id"),
        )

    async def _populate_receipt_event_stream_ordering(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        def _populate_receipt_event_stream_ordering_txn(
            txn: LoggingTransaction,
        ) -> bool:
            if "max_stream_id" in progress:
                max_stream_id = progress["max_stream_id"]
            else:
                txn.execute("SELECT max(stream_id) FROM receipts_linearized")
                res = txn.fetchone()
                if res is None or res[0] is None:
                    return True
                else:
                    max_stream_id = res[0]

            start = progress.get("stream_id", 0)
            stop = start + batch_size

            sql = """
                UPDATE receipts_linearized
                SET event_stream_ordering = (
                    SELECT stream_ordering
                    FROM events
                    WHERE event_id = receipts_linearized.event_id
                )
                WHERE stream_id >= ? AND stream_id < ?
            """
            txn.execute(sql, (start, stop))

            self.db_pool.updates._background_update_progress_txn(
                txn,
                self.POPULATE_RECEIPT_EVENT_STREAM_ORDERING,
                {
                    "stream_id": stop,
                    "max_stream_id": max_stream_id,
                },
            )

            return stop > max_stream_id

        finished = await self.db_pool.runInteraction(
            "_remove_devices_from_device_inbox_txn",
            _populate_receipt_event_stream_ordering_txn,
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                self.POPULATE_RECEIPT_EVENT_STREAM_ORDERING
            )

        return batch_size

    async def _background_receipts_linearized_unique_index(
        self, progress: dict, batch_size: int
    ) -> int:
        """Removes duplicate receipts and adds a unique index on
        `(room_id, receipt_type, user_id)` to `receipts_linearized`, for non-thread
        receipts."""

        def _remote_duplicate_receipts_txn(txn: LoggingTransaction) -> None:
            ROW_ID_NAME = self.database_engine.row_id_name
            # Identify any duplicate receipts arising from
            # https://github.com/matrix-org/synapse/issues/14406.
            # The following query takes less than a minute on matrix.org.
            sql = """
                SELECT MAX(stream_id), room_id, receipt_type, user_id
                FROM receipts_linearized
                WHERE thread_id IS NULL
                GROUP BY room_id, receipt_type, user_id
                HAVING COUNT(*) > 1
            """
            txn.execute(sql)
            duplicate_keys = cast(List[Tuple[int, str, str, str]], list(txn))

            # Then remove duplicate receipts, keeping the one with the highest
            # `stream_id`. Since there might be duplicate rows with the same
            # `stream_id`, we delete by the ctid instead.
            for stream_id, room_id, receipt_type, user_id in duplicate_keys:
                sql = f"""
                SELECT {ROW_ID_NAME}
                FROM receipts_linearized
                WHERE
                    room_id = ? AND
                    receipt_type = ? AND
                    user_id = ? AND
                    thread_id IS NULL AND
                    stream_id = ?
                LIMIT 1
                """
                txn.execute(sql, (room_id, receipt_type, user_id, stream_id))
                row_id = cast(Tuple[str], txn.fetchone())[0]

                sql = f"""
                    DELETE FROM receipts_linearized
                    WHERE
                        room_id = ? AND
                        receipt_type = ? AND
                        user_id = ? AND
                        thread_id IS NULL AND
                        {ROW_ID_NAME} != ?
                """
                txn.execute(sql, (room_id, receipt_type, user_id, row_id))

        await self.db_pool.runInteraction(
            self.RECEIPTS_LINEARIZED_UNIQUE_INDEX_UPDATE_NAME,
            _remote_duplicate_receipts_txn,
        )

        await self.db_pool.updates.create_index_in_background(
            index_name="receipts_linearized_unique_index",
            table="receipts_linearized",
            columns=["room_id", "receipt_type", "user_id"],
            where_clause="thread_id IS NULL",
            unique=True,
        )

        await self.db_pool.updates._end_background_update(
            self.RECEIPTS_LINEARIZED_UNIQUE_INDEX_UPDATE_NAME
        )

        return 1

    async def _background_receipts_graph_unique_index(
        self, progress: dict, batch_size: int
    ) -> int:
        """Removes duplicate receipts and adds a unique index on
        `(room_id, receipt_type, user_id)` to `receipts_graph`, for non-thread
        receipts."""

        def _remote_duplicate_receipts_txn(txn: LoggingTransaction) -> None:
            # Identify any duplicate receipts arising from
            # https://github.com/matrix-org/synapse/issues/14406.
            # We expect the following query to use the per-thread receipt index and take
            # less than a minute.
            sql = """
                SELECT room_id, receipt_type, user_id FROM receipts_graph
                WHERE thread_id IS NULL
                GROUP BY room_id, receipt_type, user_id
                HAVING COUNT(*) > 1
            """
            txn.execute(sql)
            duplicate_keys = cast(List[Tuple[str, str, str]], list(txn))

            # Then remove all duplicate receipts.
            # We could be clever and try to keep the latest receipt out of every set of
            # duplicates, but it's far simpler to remove them all.
            for room_id, receipt_type, user_id in duplicate_keys:
                sql = """
                    DELETE FROM receipts_graph
                    WHERE
                        room_id = ? AND
                        receipt_type = ? AND
                        user_id = ? AND
                        thread_id IS NULL
                """
                txn.execute(sql, (room_id, receipt_type, user_id))

        await self.db_pool.runInteraction(
            self.RECEIPTS_GRAPH_UNIQUE_INDEX_UPDATE_NAME,
            _remote_duplicate_receipts_txn,
        )

        await self.db_pool.updates.create_index_in_background(
            index_name="receipts_graph_unique_index",
            table="receipts_graph",
            columns=["room_id", "receipt_type", "user_id"],
            where_clause="thread_id IS NULL",
            unique=True,
        )

        await self.db_pool.updates._end_background_update(
            self.RECEIPTS_GRAPH_UNIQUE_INDEX_UPDATE_NAME
        )

        return 1


class ReceiptsStore(ReceiptsWorkerStore, ReceiptsBackgroundUpdateStore):
    pass
