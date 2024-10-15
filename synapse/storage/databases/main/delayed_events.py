#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging
from typing import List, NewType, Optional, Tuple

import attr

from synapse.api.errors import NotFoundError
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import LoggingTransaction, StoreError
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict, RoomID
from synapse.util import json_encoder, stringutils as stringutils

logger = logging.getLogger(__name__)


DelayID = NewType("DelayID", str)
UserLocalpart = NewType("UserLocalpart", str)
DeviceID = NewType("DeviceID", str)
EventType = NewType("EventType", str)
StateKey = NewType("StateKey", str)

Delay = NewType("Delay", int)
Timestamp = NewType("Timestamp", int)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventDetails:
    room_id: RoomID
    type: EventType
    state_key: Optional[StateKey]
    origin_server_ts: Optional[Timestamp]
    content: JsonDict
    device_id: Optional[DeviceID]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class DelayedEventDetails(EventDetails):
    delay_id: DelayID
    user_localpart: UserLocalpart


class DelayedEventsStore(SQLBaseStore):
    async def get_delayed_events_stream_pos(self) -> int:
        """
        Gets the stream position of the background process to watch for state events
        that target the same piece of state as any pending delayed events.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="delayed_events_stream_pos",
            keyvalues={},
            retcol="stream_id",
            desc="get_delayed_events_stream_pos",
        )

    async def update_delayed_events_stream_pos(self, stream_id: Optional[int]) -> None:
        """
        Updates the stream position of the background process to watch for state events
        that target the same piece of state as any pending delayed events.

        Must only be used by the worker running the background process.
        """
        await self.db_pool.simple_update_one(
            table="delayed_events_stream_pos",
            keyvalues={},
            updatevalues={"stream_id": stream_id},
            desc="update_delayed_events_stream_pos",
        )

    async def add_delayed_event(
        self,
        *,
        user_localpart: str,
        device_id: Optional[str],
        creation_ts: Timestamp,
        room_id: str,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        delay: int,
    ) -> Tuple[DelayID, Timestamp]:
        """
        Inserts a new delayed event in the DB.

        Returns: The generated ID assigned to the added delayed event,
            and the send time of the next delayed event to be sent,
            which is either the event just added or one added earlier.
        """
        delay_id = _generate_delay_id()
        send_ts = Timestamp(creation_ts + delay)

        def add_delayed_event_txn(txn: LoggingTransaction) -> Timestamp:
            self.db_pool.simple_insert_txn(
                txn,
                table="delayed_events",
                values={
                    "delay_id": delay_id,
                    "user_localpart": user_localpart,
                    "device_id": device_id,
                    "delay": delay,
                    "send_ts": send_ts,
                    "room_id": room_id,
                    "event_type": event_type,
                    "state_key": state_key,
                    "origin_server_ts": origin_server_ts,
                    "content": json_encoder.encode(content),
                },
            )

            next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            assert next_send_ts is not None
            return next_send_ts

        next_send_ts = await self.db_pool.runInteraction(
            "add_delayed_event", add_delayed_event_txn
        )

        return delay_id, next_send_ts

    async def restart_delayed_event(
        self,
        *,
        delay_id: str,
        user_localpart: str,
        current_ts: Timestamp,
    ) -> Timestamp:
        """
        Restarts the send time of the matching delayed event,
        as long as it hasn't already been marked for processing.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.
            current_ts: The current time, which will be used to calculate the new send time.

        Returns: The send time of the next delayed event to be sent,
            which is either the event just restarted, or another one
            with an earlier send time than the restarted one's new send time.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def restart_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Timestamp:
            txn.execute(
                """
                UPDATE delayed_events
                SET send_ts = ? + delay
                WHERE delay_id = ? AND user_localpart = ?
                    AND NOT is_processed
                """,
                (
                    current_ts,
                    delay_id,
                    user_localpart,
                ),
            )
            if txn.rowcount == 0:
                raise NotFoundError("Delayed event not found")

            next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            assert next_send_ts is not None
            return next_send_ts

        return await self.db_pool.runInteraction(
            "restart_delayed_event", restart_delayed_event_txn
        )

    async def get_all_delayed_events_for_user(
        self,
        user_localpart: str,
    ) -> List[JsonDict]:
        """Returns all pending delayed events owned by the given user."""
        # TODO: Support Pagination stream API ("next_batch" field)
        rows = await self.db_pool.execute(
            "get_all_delayed_events_for_user",
            """
            SELECT
                delay_id,
                room_id,
                event_type,
                state_key,
                delay,
                send_ts,
                content
            FROM delayed_events
            WHERE user_localpart = ? AND NOT is_processed
            ORDER BY send_ts
            """,
            user_localpart,
        )
        return [
            {
                "delay_id": DelayID(row[0]),
                "room_id": str(RoomID.from_string(row[1])),
                "type": EventType(row[2]),
                **({"state_key": StateKey(row[3])} if row[3] is not None else {}),
                "delay": Delay(row[4]),
                "running_since": Timestamp(row[5] - row[4]),
                "content": db_to_json(row[6]),
            }
            for row in rows
        ]

    async def process_timeout_delayed_events(
        self, current_ts: Timestamp
    ) -> Tuple[
        List[DelayedEventDetails],
        Optional[Timestamp],
    ]:
        """
        Marks for processing all delayed events that should have been sent prior to the provided time
        that haven't already been marked as such.

        Returns: The details of all newly-processed delayed events,
            and the send time of the next delayed event to be sent, if any.
        """

        def process_timeout_delayed_events_txn(
            txn: LoggingTransaction,
        ) -> Tuple[
            List[DelayedEventDetails],
            Optional[Timestamp],
        ]:
            sql_cols = ", ".join(
                (
                    "delay_id",
                    "user_localpart",
                    "room_id",
                    "event_type",
                    "state_key",
                    "origin_server_ts",
                    "send_ts",
                    "content",
                    "device_id",
                )
            )
            sql_update = "UPDATE delayed_events SET is_processed = TRUE"
            sql_where = "WHERE send_ts <= ? AND NOT is_processed"
            sql_args = (current_ts,)
            sql_order = "ORDER BY send_ts"
            if isinstance(self.database_engine, PostgresEngine):
                # Do this only in Postgres because:
                # - SQLite's RETURNING emits rows in an arbitrary order
                #   - https://www.sqlite.org/lang_returning.html#limitations_and_caveats
                # - SQLite does not support data-modifying statements in a WITH clause
                #   - https://www.sqlite.org/lang_with.html
                #   - https://www.postgresql.org/docs/current/queries-with.html#QUERIES-WITH-MODIFYING
                txn.execute(
                    f"""
                    WITH events_to_send AS (
                        {sql_update} {sql_where} RETURNING *
                    ) SELECT {sql_cols} FROM events_to_send {sql_order}
                    """,
                    sql_args,
                )
                rows = txn.fetchall()
            else:
                txn.execute(
                    f"SELECT {sql_cols} FROM delayed_events {sql_where} {sql_order}",
                    sql_args,
                )
                rows = txn.fetchall()
                txn.execute(f"{sql_update} {sql_where}", sql_args)
                assert txn.rowcount == len(rows)

            events = [
                DelayedEventDetails(
                    RoomID.from_string(row[2]),
                    EventType(row[3]),
                    StateKey(row[4]) if row[4] is not None else None,
                    # If no custom_origin_ts is set, use send_ts as the event's timestamp
                    Timestamp(row[5] if row[5] is not None else row[6]),
                    db_to_json(row[7]),
                    DeviceID(row[8]) if row[8] is not None else None,
                    DelayID(row[0]),
                    UserLocalpart(row[1]),
                )
                for row in rows
            ]
            next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            return events, next_send_ts

        return await self.db_pool.runInteraction(
            "process_timeout_delayed_events", process_timeout_delayed_events_txn
        )

    async def process_target_delayed_event(
        self,
        *,
        delay_id: str,
        user_localpart: str,
    ) -> Tuple[
        EventDetails,
        Optional[Timestamp],
    ]:
        """
        Marks for processing the matching delayed event, regardless of its timeout time,
        as long as it has not already been marked as such.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.

        Returns: The details of the matching delayed event,
            and the send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def process_target_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Tuple[
            EventDetails,
            Optional[Timestamp],
        ]:
            sql_cols = ", ".join(
                (
                    "room_id",
                    "event_type",
                    "state_key",
                    "origin_server_ts",
                    "content",
                    "device_id",
                )
            )
            sql_update = "UPDATE delayed_events SET is_processed = TRUE"
            sql_where = "WHERE delay_id = ? AND user_localpart = ? AND NOT is_processed"
            sql_args = (delay_id, user_localpart)
            txn.execute(
                (
                    f"{sql_update} {sql_where} RETURNING {sql_cols}"
                    if self.database_engine.supports_returning
                    else f"SELECT {sql_cols} FROM delayed_events {sql_where}"
                ),
                sql_args,
            )
            row = txn.fetchone()
            if row is None:
                raise NotFoundError("Delayed event not found")
            elif not self.database_engine.supports_returning:
                txn.execute(f"{sql_update} {sql_where}", sql_args)
                assert txn.rowcount == 1

            event = EventDetails(
                RoomID.from_string(row[0]),
                EventType(row[1]),
                StateKey(row[2]) if row[2] is not None else None,
                Timestamp(row[3]) if row[3] is not None else None,
                db_to_json(row[4]),
                DeviceID(row[5]) if row[5] is not None else None,
            )

            return event, self._get_next_delayed_event_send_ts_txn(txn)

        return await self.db_pool.runInteraction(
            "process_target_delayed_event", process_target_delayed_event_txn
        )

    async def cancel_delayed_event(
        self,
        *,
        delay_id: str,
        user_localpart: str,
    ) -> Optional[Timestamp]:
        """
        Cancels the matching delayed event, i.e. remove it as long as it hasn't been processed.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.

        Returns: The send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def cancel_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Optional[Timestamp]:
            try:
                self.db_pool.simple_delete_one_txn(
                    txn,
                    table="delayed_events",
                    keyvalues={
                        "delay_id": delay_id,
                        "user_localpart": user_localpart,
                        "is_processed": False,
                    },
                )
            except StoreError:
                if txn.rowcount == 0:
                    raise NotFoundError("Delayed event not found")
                else:
                    raise

            return self._get_next_delayed_event_send_ts_txn(txn)

        return await self.db_pool.runInteraction(
            "cancel_delayed_event", cancel_delayed_event_txn
        )

    async def cancel_delayed_state_events(
        self,
        *,
        room_id: str,
        event_type: str,
        state_key: str,
        not_from_localpart: str,
    ) -> Optional[Timestamp]:
        """
        Cancels all matching delayed state events, i.e. remove them as long as they haven't been processed.

        Args:
            room_id: The room ID to match against.
            event_type: The event type to match against.
            state_key: The state key to match against.
            not_from_localpart: The localpart of a user whose delayed events to not cancel.
                If set to the empty string, any users' delayed events may be cancelled.

        Returns: The send time of the next delayed event to be sent, if any.
        """

        def cancel_delayed_state_events_txn(
            txn: LoggingTransaction,
        ) -> Optional[Timestamp]:
            txn.execute(
                """
                DELETE FROM delayed_events
                WHERE room_id = ? AND event_type = ? AND state_key = ?
                    AND user_localpart <> ?
                    AND NOT is_processed
                """,
                (
                    room_id,
                    event_type,
                    state_key,
                    not_from_localpart,
                ),
            )
            return self._get_next_delayed_event_send_ts_txn(txn)

        return await self.db_pool.runInteraction(
            "cancel_delayed_state_events", cancel_delayed_state_events_txn
        )

    async def delete_processed_delayed_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
    ) -> None:
        """
        Delete the matching delayed event, as long as it has been marked as processed.

        Throws:
            StoreError: if there is no matching delayed event, or if it has not yet been processed.
        """
        return await self.db_pool.simple_delete_one(
            table="delayed_events",
            keyvalues={
                "delay_id": delay_id,
                "user_localpart": user_localpart,
                "is_processed": True,
            },
            desc="delete_processed_delayed_event",
        )

    async def delete_processed_delayed_state_events(
        self,
        *,
        room_id: str,
        event_type: str,
        state_key: str,
    ) -> None:
        """
        Delete the matching delayed state events that have been marked as processed.
        """
        await self.db_pool.simple_delete(
            table="delayed_events",
            keyvalues={
                "room_id": room_id,
                "event_type": event_type,
                "state_key": state_key,
                "is_processed": True,
            },
            desc="delete_processed_delayed_state_events",
        )

    async def unprocess_delayed_events(self) -> None:
        """
        Unmark all delayed events for processing.
        """
        await self.db_pool.simple_update(
            table="delayed_events",
            keyvalues={"is_processed": True},
            updatevalues={"is_processed": False},
            desc="unprocess_delayed_events",
        )

    async def get_next_delayed_event_send_ts(self) -> Optional[Timestamp]:
        """
        Returns the send time of the next delayed event to be sent, if any.
        """
        return await self.db_pool.runInteraction(
            "get_next_delayed_event_send_ts",
            self._get_next_delayed_event_send_ts_txn,
            db_autocommit=True,
        )

    def _get_next_delayed_event_send_ts_txn(
        self, txn: LoggingTransaction
    ) -> Optional[Timestamp]:
        result = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="delayed_events",
            keyvalues={"is_processed": False},
            retcol="MIN(send_ts)",
            allow_none=True,
        )
        return Timestamp(result) if result is not None else None


def _generate_delay_id() -> DelayID:
    """Generates an opaque string, for use as a delay ID"""

    # We use the following format for delay IDs:
    #    syd_<random string>
    # They are scoped to user localparts, so it is possible for
    # the same ID to exist for multiple users.

    return DelayID(f"syd_{stringutils.random_string(20)}")
