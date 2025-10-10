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
from http import HTTPStatus
from typing import List, NewType, Optional, Tuple, Union

import attr

from synapse.api.errors import NotFoundError, SynapseError, cs_error
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import (
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict, RoomID
from synapse.util import stringutils
from synapse.util.json import json_encoder

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
        that target the same piece of state as any scheduled delayed events.
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
        that target the same piece of state as any scheduled delayed events.

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
        limit: int,
    ) -> Tuple[DelayID, Timestamp]:
        """
        Inserts a new delayed event in the DB.

        Returns: The generated ID assigned to the added delayed event,
            and the send time of the next delayed event to be sent,
            which is either the event just added or one added earlier.

        Raises:
            SynapseError: if the user has reached the limit of how many
                delayed events they may have scheduled at a time.
        """
        delay_id = _generate_delay_id()
        send_ts = Timestamp(creation_ts + delay)

        def add_delayed_event_txn(txn: LoggingTransaction) -> Timestamp:
            txn.execute(
                """
                SELECT COUNT(*) FROM delayed_events
                WHERE user_localpart = ?
                    AND (delay_id, user_localpart) NOT IN (
                        SELECT delay_id, user_localpart
                        FROM finalised_delayed_events
                    )
                """,
                (user_localpart,),
            )
            num_existing: int = txn.fetchall()[0][0]
            if num_existing >= limit:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "The maximum number of delayed events has been reached.",
                    additional_fields={
                        "org.matrix.msc4140.errcode": "M_MAX_DELAY_UNSUPPORTED",
                    },
                )

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
            SynapseError: if the delayed event has already been finalised.
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
                    AND (delay_id, user_localpart) NOT IN (
                        SELECT delay_id, user_localpart
                        FROM finalised_delayed_events
                    )
                """,
                (
                    current_ts,
                    delay_id,
                    user_localpart,
                ),
            )
            if txn.rowcount == 0:
                sent = self.db_pool.simple_select_one_onecol_txn(
                    txn,
                    "finalised_delayed_events",
                    keyvalues={"delay_id": delay_id, "user_localpart": user_localpart},
                    retcol="event_id IS NOT NULL",
                    allow_none=True,
                )
                if sent is None:
                    raise NotFoundError("Delayed event not found")
                raise SynapseError(
                    HTTPStatus.CONFLICT,
                    f"Delayed event has already been {'sent' if sent else 'cancelled'}",
                )

            next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            assert next_send_ts is not None
            return next_send_ts

        return await self.db_pool.runInteraction(
            "restart_delayed_event", restart_delayed_event_txn
        )

    async def has_scheduled_delayed_events(self) -> bool:
        """Returns whether there are any scheduled delayed events in the DB."""

        rows = await self.db_pool.execute(
            "has_scheduled_delayed_events",
            """
            SELECT 1 WHERE EXISTS (
                SELECT * FROM delayed_events
                WHERE (delay_id, user_localpart) NOT IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                )
            )
            """,
        )
        return bool(rows)

    async def prune_finalised_delayed_events(
        self,
        current_ts: Timestamp,
        retention_period: int,
        retention_limit: int,
    ) -> None:
        def prune_finalised_delayed_events(txn: LoggingTransaction) -> None:
            self._prune_expired_finalised_delayed_events(
                txn, current_ts, retention_period
            )

            for user_localpart in self.db_pool.simple_select_onecol_txn(
                txn,
                "finalised_delayed_events",
                keyvalues={},
                retcol="DISTINCT(user_localpart)",
            ):
                self._prune_excess_finalised_delayed_events_for_user(
                    txn, user_localpart, retention_limit
                )

        await self.db_pool.runInteraction(
            "prune_finalised_delayed_events", prune_finalised_delayed_events
        )

    def _prune_expired_finalised_delayed_events(
        self, txn: LoggingTransaction, current_ts: Timestamp, retention_period: int
    ) -> None:
        """
        Delete all finalised delayed events that had finalised
        before the end of the given retention period.
        """
        txn.execute(
            """
            DELETE FROM delayed_events
            WHERE (delay_id, user_localpart) IN (
                SELECT delay_id, user_localpart
                FROM finalised_delayed_events
                WHERE ? - finalised_ts > ?
            )
            """,
            (
                current_ts,
                retention_period,
            ),
        )

    def _prune_excess_finalised_delayed_events_for_user(
        self, txn: LoggingTransaction, user_localpart: str, retention_limit: int
    ) -> None:
        """
        Delete the oldest finalised delayed events for the given user,
        such that no more of them remain than the given retention limit.
        """
        num_existing = self.db_pool.simple_select_one_onecol_txn(
            txn,
            "finalised_delayed_events",
            keyvalues={"user_localpart": user_localpart},
            retcol="COUNT(*)",
        )
        if num_existing > retention_limit:
            txn.execute(
                """
                DELETE FROM delayed_events
                WHERE (delay_id, user_localpart) IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                    WHERE user_localpart = ?
                    ORDER BY finalised_ts
                    LIMIT ?
                )
                """,
                (
                    user_localpart,
                    num_existing - retention_limit,
                ),
            )

    async def get_scheduled_delayed_events_for_user(
        self,
        user_localpart: str,
        delay_ids: Optional[List[str]],
    ) -> List[JsonDict]:
        """Returns all scheduled delayed events for the given user."""
        # TODO: Support Pagination stream API ("next_batch" field)
        sql_where = """WHERE user_localpart = ?
            AND (delay_id, user_localpart) NOT IN (
                SELECT delay_id, user_localpart
                FROM finalised_delayed_events
            )"""
        sql_args = [user_localpart]
        if delay_ids:
            delay_id_clause_sql, delay_id_clause_args = make_in_list_sql_clause(
                self.database_engine, "delay_id", delay_ids
            )
            sql_where += f" AND {delay_id_clause_sql}"
            sql_args.extend(delay_id_clause_args)
        rows = await self.db_pool.execute(
            "get_scheduled_delayed_events_for_user",
            f"""
            SELECT
                delay_id,
                room_id,
                event_type,
                state_key,
                delay,
                send_ts,
                content
            FROM delayed_events
            {sql_where}
            ORDER BY send_ts
            """,
            *sql_args,
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

    async def get_finalised_delayed_events_for_user(
        self,
        user_localpart: str,
        delay_ids: Optional[List[str]],
        current_ts: Timestamp,
        retention_period: int,
        retention_limit: int,
    ) -> List[JsonDict]:
        """Returns all finalised delayed events for the given user."""
        # TODO: Support Pagination stream API ("next_batch" field)

        def get_finalised_delayed_events_for_user(
            txn: LoggingTransaction,
        ) -> List[JsonDict]:
            # Clear up some space in the DB before returning any results.
            self._prune_expired_finalised_delayed_events(
                txn, current_ts, retention_period
            )
            self._prune_excess_finalised_delayed_events_for_user(
                txn, user_localpart, retention_limit
            )

            sql_where = "WHERE user_localpart = ?"
            sql_args = [user_localpart]
            if delay_ids:
                delay_id_clause_sql, delay_id_clause_args = make_in_list_sql_clause(
                    self.database_engine, "delay_id", delay_ids
                )
                sql_where += f" AND {delay_id_clause_sql}"
                sql_args.extend(delay_id_clause_args)
            txn.execute(
                f"""
                SELECT
                    delay_id,
                    room_id,
                    event_type,
                    state_key,
                    delay,
                    send_ts,
                    content,
                    error,
                    event_id,
                    finalised_ts
                FROM delayed_events
                JOIN finalised_delayed_events
                USING (delay_id, user_localpart)
                {sql_where}
                ORDER BY finalised_ts
                """,
                sql_args,
            )
            return [
                {
                    "delayed_event": {
                        "delay_id": DelayID(row[0]),
                        "room_id": str(RoomID.from_string(row[1])),
                        "type": EventType(row[2]),
                        **(
                            {"state_key": StateKey(row[3])}
                            if row[3] is not None
                            else {}
                        ),
                        "delay": Delay(row[4]),
                        "running_since": Timestamp(row[5] - row[4]),
                        "content": db_to_json(row[6]),
                    },
                    "outcome": "cancel" if row[8] is None else "send",
                    "reason": (
                        "error"
                        if row[7] is not None
                        else "action"
                        if row[9] < row[5]
                        else "delay"
                    ),
                    **({"error": db_to_json(row[7])} if row[7] is not None else {}),
                    **({"event_id": str(row[8])} if row[8] is not None else {}),
                    "origin_server_ts": Timestamp(row[9]),
                }
                for row in txn
            ]

        return await self.db_pool.runInteraction(
            "get_finalised_delayed_events_for_user",
            get_finalised_delayed_events_for_user,
        )

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
            sql_where = """WHERE send_ts <= ?
                AND NOT is_processed
                AND (delay_id, user_localpart) NOT IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                )"""
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
        Optional[EventDetails],
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
            SynapseError: if the delayed event has already been cancelled.
        """

        def process_target_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Tuple[
            Optional[EventDetails],
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
            sql_where = """WHERE delay_id = ? AND user_localpart = ?
                AND NOT is_processed
                AND (delay_id, user_localpart) NOT IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                )"""
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
                sent = self.db_pool.simple_select_one_onecol_txn(
                    txn,
                    "finalised_delayed_events",
                    keyvalues={"delay_id": delay_id, "user_localpart": user_localpart},
                    retcol="event_id IS NOT NULL",
                    allow_none=True,
                )
                if sent is None:
                    raise NotFoundError("Delayed event not found")
                elif not sent:
                    raise SynapseError(
                        HTTPStatus.CONFLICT,
                        "Delayed event has already been cancelled",
                    )
                return None, None
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
        finalised_ts: Timestamp,
    ) -> Optional[Timestamp]:
        """
        Cancels the matching delayed event, i.e. remove it as long as it hasn't been processed.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.

        Returns: The send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
            SynapseError: if the delayed event has already been sent.
        """

        def cancel_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Optional[Timestamp]:
            txn.execute(
                """
                WITH matching_delayed_events AS (
                    SELECT * FROM delayed_events
                    WHERE delay_id = ? AND user_localpart = ?
                        AND NOT is_processed
                        AND (delay_id, user_localpart) NOT IN (
                            SELECT delay_id, user_localpart
                            FROM finalised_delayed_events
                        )
                )
                INSERT INTO finalised_delayed_events (
                    delay_id,
                    user_localpart,
                    finalised_ts
                )
                SELECT
                    delay_id,
                    user_localpart,
                    ?
                FROM matching_delayed_events
                RETURNING 1
                """,
                (
                    delay_id,
                    user_localpart,
                    finalised_ts,
                ),
            )
            if not txn.fetchall():
                sent = self.db_pool.simple_select_one_onecol_txn(
                    txn,
                    "finalised_delayed_events",
                    keyvalues={"delay_id": delay_id, "user_localpart": user_localpart},
                    retcol="event_id IS NOT NULL",
                )
                if sent is None:
                    raise NotFoundError("Delayed event not found")
                elif sent:
                    raise SynapseError(
                        HTTPStatus.CONFLICT,
                        "Delayed event has already been sent",
                    )
                return None
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
        finalised_ts: Timestamp,
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
                WITH matching_delayed_events AS (
                    SELECT * FROM delayed_events
                    WHERE room_id = ? AND event_type = ? AND state_key = ?
                        AND user_localpart <> ?
                        AND NOT is_processed
                        AND (delay_id, user_localpart) NOT IN (
                            SELECT delay_id, user_localpart
                            FROM finalised_delayed_events
                        )
                )
                INSERT INTO finalised_delayed_events (
                    delay_id,
                    user_localpart,
                    error,
                    finalised_ts
                )
                SELECT
                    delay_id,
                    user_localpart,
                    ?,
                    ?
                FROM matching_delayed_events
                """,
                (
                    room_id,
                    event_type,
                    state_key,
                    not_from_localpart,
                    _generate_cancelled_by_state_update_json(),
                    finalised_ts,
                ),
            )
            return self._get_next_delayed_event_send_ts_txn(txn)

        return await self.db_pool.runInteraction(
            "cancel_delayed_state_events", cancel_delayed_state_events_txn
        )

    async def finalise_processed_delayed_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
        result_or_error: Union[str, JsonDict],
        finalised_ts: Timestamp,
    ) -> None:
        """
        Finalise the matching delayed event, as long as it has been marked as processed.

        Throws:
            StoreError: if there is no matching delayed event, or if it has not yet been processed.
        """
        if isinstance(result_or_error, str):
            event_id = result_or_error
            send_error = None
        else:
            event_id = None
            send_error = result_or_error

        await self.db_pool.execute(
            "finalise_processed_delayed_event",
            """
            WITH matching_delayed_events AS (
                SELECT * FROM delayed_events
                WHERE delay_id = ? AND user_localpart = ?
                    AND is_processed
                    AND (delay_id, user_localpart) NOT IN (
                        SELECT delay_id, user_localpart
                        FROM finalised_delayed_events
                    )
            )
            INSERT INTO finalised_delayed_events (
                delay_id,
                user_localpart,
                error,
                event_id,
                finalised_ts
            )
            SELECT
                delay_id,
                user_localpart,
                ?,
                ?,
                ?
            FROM matching_delayed_events
            """,
            delay_id,
            user_localpart,
            json_encoder.encode(send_error) if send_error else None,
            event_id,
            finalised_ts,
        )

    async def finalise_processed_delayed_state_events(
        self,
        *,
        room_id: str,
        event_type: str,
        state_key: str,
        finalised_ts: Timestamp,
    ) -> None:
        """
        Finalise the matching delayed state events that have been marked as processed.
        """
        await self.db_pool.execute(
            "finalise_processed_delayed_state_events",
            """
            WITH matching_delayed_events AS (
                SELECT * FROM delayed_events
                WHERE room_id = ? AND event_type = ? AND state_key = ?
                    AND user_localpart <> ?
                    AND is_processed
                    AND (delay_id, user_localpart) NOT IN (
                        SELECT delay_id, user_localpart
                        FROM finalised_delayed_events
                    )
            )
            INSERT INTO finalised_delayed_events (
                delay_id,
                user_localpart,
                error,
                finalised_ts
            )
            SELECT
                delay_id,
                user_localpart,
                ?,
                ?
            FROM matching_delayed_events
            """,
            room_id,
            event_type,
            state_key,
            _generate_cancelled_by_state_update_json(),
            finalised_ts,
        )

    async def unprocess_delayed_events(self) -> None:
        """
        Unmark all delayed events for processing.
        """
        await self.db_pool.execute(
            "unprocess_delayed_events",
            """
            UPDATE delayed_events SET is_processed = FALSE
            WHERE is_processed
                AND (delay_id, user_localpart) NOT IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                )
            """,
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
        txn.execute(
            """
            SELECT MIN(send_ts) FROM delayed_events
            WHERE NOT is_processed
                AND (delay_id, user_localpart) NOT IN (
                    SELECT delay_id, user_localpart
                    FROM finalised_delayed_events
                )
            """
        )
        resp = txn.fetchone()
        return Timestamp(resp[0]) if resp is not None else None


def _generate_delay_id() -> DelayID:
    """Generates an opaque string, for use as a delay ID"""

    # We use the following format for delay IDs:
    #    syd_<random string>
    # They are scoped to user localparts, so it is possible for
    # the same ID to exist for multiple users.

    return DelayID(f"syd_{stringutils.random_string(20)}")


def _generate_cancelled_by_state_update_json() -> str:
    return json_encoder.encode(
        cs_error(
            "The delayed event did not get sent because a different user updated the same state event. "
            + "So the scheduled event might change it in an undesired way.",
            **{
                "org.matrix.msc4140.errcode": "M_CANCELLED_BY_STATE_UPDATE",
            },
        )
    )
