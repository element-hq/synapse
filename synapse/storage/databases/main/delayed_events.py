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
from typing import TYPE_CHECKING, NewType

import attr

from synapse.api.errors import (
    LimitExceededError,
    NotFoundError,
    SynapseError,
    cs_error,
)
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    StoreError,
)
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict, RoomID
from synapse.util import stringutils
from synapse.util.duration import Duration
from synapse.util.json import json_encoder

if TYPE_CHECKING:
    from synapse.server import HomeServer

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
    state_key: StateKey | None
    origin_server_ts: Timestamp | None
    content: JsonDict
    device_id: DeviceID | None
    sticky_duration_ms: int | None


@attr.s(slots=True, frozen=True, auto_attribs=True)
class DelayedEventDetails(EventDetails):
    delay_id: DelayID
    user_localpart: UserLocalpart


@attr.s(slots=True, frozen=True, auto_attribs=True)
class DelayedEventResponse:
    """The representation of a delayed event in API format."""

    delay_id: str
    room_id: str
    type: str
    state_key: str | None
    delay_ms: int
    delayed_since_ts: int
    content: JsonDict = attr.ib(converter=db_to_json)
    # Present only for finalised delayed events (MSC4140): `finalised_ts`, plus
    # `event_id` if the event was sent, or `error` if sending it failed.
    finalised: JsonDict | None = None

    def asdict(self) -> JsonDict:
        return attr.asdict(self, filter=lambda _attr, v: v is not None)


# TODO: Remove this class once the response format is stable
class DelayedEventResponseLegacyCompat(DelayedEventResponse):
    """For backwards compatibility with field names from earlier revisions of MSC4140."""

    def asdict(self) -> JsonDict:
        return super().asdict() | {
            "delay": self.delay_ms,
            "running_since": self.delayed_since_ts,
        }


class DelayedEventsStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        # Set delayed events to be uniquely identifiable by their delay_id.
        # In practice, delay_ids are already unique because they are generated
        # from cryptographically strong random strings.
        # Therefore, adding this constraint is not expected to ever fail,
        # despite the current pkey technically allowing non-unique delay_ids.
        self.db_pool.updates.register_background_index_update(
            update_name="delayed_events_idx",
            index_name="delayed_events_idx",
            table="delayed_events",
            columns=("delay_id",),
            unique=True,
        )

        self.db_pool.updates.register_background_index_update(
            update_name="delayed_events_finalised_ts",
            index_name="delayed_events_finalised_ts",
            table="delayed_events",
            columns=("finalised_ts",),
        )

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

    async def update_delayed_events_stream_pos(self, stream_id: int | None) -> None:
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
        device_id: str | None,
        creation_ts: Timestamp,
        room_id: str,
        event_type: str,
        state_key: str | None,
        origin_server_ts: int | None,
        content: JsonDict,
        delay: Duration,
        sticky_duration_ms: int | None,
        limit: int,
    ) -> tuple[DelayID, Timestamp]:
        """
        Inserts a new delayed event in the DB.

        Args:
            user_localpart: The localpart of the requester of the delayed event, who will be its owner.
            device_id: The device ID of the requester.
            creation_ts: The timestamp of when the request to add the delayed event was made.
            room_id: The ID of the room where the event should be sent to.
            event_type: The type of event to be sent.
            state_key: The state key of the event to be sent, or None if it is not a state event.
            origin_server_ts: The custom timestamp to send the event with.
                If None, the timestamp will be the actual time when the event is sent.
            content: The content of the event to be sent.
            delay: How long to wait before automatically sending the event.
            sticky_duration_ms: If an MSC4354 sticky event: the sticky duration (in milliseconds).
                The event will be attempted to be reliably delivered to clients and remote servers
                during its sticky period.
            limit: The maximum number of delayed events the DB may store for the given requester.
                Must be greater than 0.
        Returns: The generated ID assigned to the added delayed event,
            and the send time of the next delayed event to be sent,
            which is either the event just added or one added earlier.

        Raises:
            LimitExceededError: if the DB has reached the limit of
                how many delayed events it may store for the given requester.
            AssertionError: if the limit is not greater than 0.
        """
        assert limit > 0, "limit must be greater than 0"

        delay_id = _generate_delay_id()
        delay_ms = delay.as_millis()
        send_ts = creation_ts + delay_ms

        def add_delayed_event_txn(txn: LoggingTransaction) -> Timestamp:
            txn.execute(
                """
                SELECT COUNT(*) FROM delayed_events
                WHERE user_localpart = ?
                    AND finalised_ts IS NULL
                """,
                (user_localpart,),
            )
            num_existing: int = txn.fetchall()[0][0]
            if num_existing >= limit:
                # Find the send_ts threshold that will bring the queue back under the limit.
                # When the amount of existing delayed events has reached the limit,
                # this will be the send time of the next delayed event to be sent.
                # When the amount has exceeded the limit (e.g., due to config changes),
                # this will be the send time of the delayed event that will be sent
                # once all earlier events that exceed the limit have been sent.
                #
                # FIXME: Remove "AS subquery" after dropping support for PostgreSQL <16
                txn.execute(
                    """
                    SELECT MAX(send_ts) FROM (
                        SELECT * FROM delayed_events
                        WHERE user_localpart = ?
                            AND finalised_ts IS NULL
                        ORDER BY send_ts ASC
                        LIMIT ?
                    ) AS subquery
                    """,
                    (
                        user_localpart,
                        num_existing - limit + 1,
                    ),
                )
                row = txn.fetchone()
                assert row
                retry_after_ms = row[0] - self.clock.time_msec()
                err = LimitExceededError(
                    limiter_name="add_delayed_event",
                    retry_after_ms=retry_after_ms if retry_after_ms > 0 else None,
                )
                err.msg = "The maximum number of delayed events has been reached."
                raise err

            self.db_pool.simple_insert_txn(
                txn,
                table="delayed_events",
                values={
                    "delay_id": delay_id,
                    "user_localpart": user_localpart,
                    "device_id": device_id,
                    "delay": delay_ms,
                    "send_ts": send_ts,
                    "room_id": room_id,
                    "event_type": event_type,
                    "state_key": state_key,
                    "origin_server_ts": origin_server_ts,
                    "content": json_encoder.encode(content),
                    "sticky_duration_ms": sticky_duration_ms,
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
        delay_id: str,
        current_ts: Timestamp,
    ) -> Timestamp:
        """
        Restarts the send time of the matching delayed event,
        as long as it hasn't already been marked for processing.

        Args:
            delay_id: The ID of the delayed event to restart.
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
                WHERE delay_id = ?
                    AND NOT is_processed
                    AND finalised_ts IS NULL
                """,
                (
                    current_ts,
                    delay_id,
                ),
            )
            if txn.rowcount == 0:
                txn.execute(
                    """
                    SELECT finalised_event_id IS NOT NULL
                    FROM delayed_events
                    WHERE delay_id = ?
                        AND finalised_ts IS NOT NULL
                    """,
                    (delay_id,),
                )
                row = txn.fetchone()
                if not row:
                    raise NotFoundError("Delayed event not found")
                raise SynapseError(
                    HTTPStatus.CONFLICT,
                    f"Delayed event has already been {'sent' if row[0] else 'cancelled'}",
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
                WHERE finalised_ts IS NULL
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

            txn.execute(
                """
                SELECT DISTINCT(user_localpart)
                FROM delayed_events
                WHERE finalised_ts IS NOT NULL
                """
            )
            for [user_localpart] in txn.fetchall():
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
            WHERE ? - finalised_ts > ?
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
        txn.execute(
            """
            SELECT COUNT(*) FROM delayed_events
            WHERE user_localpart = ?
                AND finalised_ts IS NOT NULL
            """,
            (user_localpart,),
        )
        num_existing: int = txn.fetchall()[0][0]
        if num_existing > retention_limit:
            txn.execute(
                """
                DELETE FROM delayed_events
                WHERE user_localpart = ?
                    AND finalised_ts IS NOT NULL
                ORDER BY finalised_ts
                LIMIT ?
                """,
                (
                    user_localpart,
                    num_existing - retention_limit,
                ),
            )

    async def get_delayed_event_for_user(
        self,
        delay_id: str,
        user_localpart: str,
    ) -> DelayedEventResponse:
        """
        Returns the specified delayed event owned by the given user,
        whether it is still scheduled or has been finalised.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """
        rows = await self.db_pool.execute(
            "get_delayed_event_for_user",
            """
            SELECT
                room_id,
                event_type,
                state_key,
                delay,
                send_ts - delay,
                content,
                finalised_ts,
                finalised_event_id,
                finalised_error
            FROM delayed_events
            WHERE delay_id = ? AND user_localpart = ?
            """,
            delay_id,
            user_localpart,
        )
        if not rows:
            raise NotFoundError("Delayed event not found")
        row = rows[0]
        return DelayedEventResponse(
            delay_id,
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            _make_finalised_dict(row[6], row[7], row[8]),
        )

    async def get_all_delayed_events_for_user(
        self,
        user_localpart: str,
    ) -> list[DelayedEventResponseLegacyCompat]:
        """
        Return all pending delayed events owned by the given user.
        Includes fields from earlier revisions of MSC4140 for
        compatibility with clients that still expect them.
        """
        # TODO: Remove legacy fields once stable
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
                send_ts - delay,
                content
            FROM delayed_events
            WHERE user_localpart = ?
                AND NOT is_processed
                AND finalised_ts IS NULL
            ORDER BY send_ts
            """,
            user_localpart,
        )
        return [DelayedEventResponseLegacyCompat(*row) for row in rows]

    async def process_timeout_delayed_events(
        self, current_ts: Timestamp, reprocess_events: bool = False
    ) -> tuple[
        list[DelayedEventDetails],
        Timestamp | None,
    ]:
        """
        Marks for processing all delayed events that should have been sent prior to the provided time
        that haven't already been marked as such.

        Args:
            current_ts: The current timestamp.
            reprocess_events: Whether to reprocess already-processed delayed
                events. If set to True, events which are marked as processed
                will have their `send_ts` re-checked.

                This is mainly useful for recovering from a server restart;
                which could have occurred between an event being marked as
                processed and the event actually being sent.

        Returns: The details of all newly-processed delayed events,
            and the send time of the next delayed event to be sent, if any.
        """

        def process_timeout_delayed_events_txn(
            txn: LoggingTransaction,
        ) -> tuple[
            list[DelayedEventDetails],
            Timestamp | None,
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
                    "sticky_duration_ms",
                )
            )
            sql_update = "UPDATE delayed_events SET is_processed = TRUE"
            sql_where = "WHERE send_ts <= ? AND finalised_ts IS NULL"

            if not reprocess_events:
                # Skip already-processed events.
                sql_where += " AND NOT is_processed"

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
                    int(row[9]) if row[9] is not None else None,
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
        delay_id: str,
    ) -> tuple[
        DelayedEventDetails | None,
        Timestamp | None,
    ]:
        """
        Marks for processing the matching delayed event, regardless of its timeout time,
        as long as it has not already been marked as such.

        Returns: The details of the matching delayed event,
            and the send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
            SynapseError: if the delayed event has already been cancelled.
        """

        def process_target_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> tuple[
            DelayedEventDetails | None,
            Timestamp | None,
        ]:
            txn.execute(
                """
                UPDATE delayed_events
                SET is_processed = TRUE
                WHERE delay_id = ? AND NOT is_processed
                    AND finalised_ts IS NULL
                RETURNING
                    room_id,
                    event_type,
                    state_key,
                    origin_server_ts,
                    content,
                    device_id,
                    sticky_duration_ms,
                    user_localpart
                """,
                (delay_id,),
            )
            row = txn.fetchone()
            if not row:
                txn.execute(
                    """
                    SELECT finalised_event_id IS NOT NULL
                    FROM delayed_events
                    WHERE delay_id = ?
                        AND finalised_ts IS NOT NULL
                    """,
                    (delay_id,),
                )
                row = txn.fetchone()
                if not row:
                    raise NotFoundError("Delayed event not found")
                elif not row[0]:
                    raise SynapseError(
                        HTTPStatus.CONFLICT,
                        "Delayed event has already been cancelled",
                    )
                return None, None

            event = DelayedEventDetails(
                RoomID.from_string(row[0]),
                EventType(row[1]),
                StateKey(row[2]) if row[2] is not None else None,
                Timestamp(row[3]) if row[3] is not None else None,
                db_to_json(row[4]),
                DeviceID(row[5]) if row[5] is not None else None,
                int(row[6]) if row[6] is not None else None,
                DelayID(delay_id),
                UserLocalpart(row[7]),
            )

            return event, self._get_next_delayed_event_send_ts_txn(txn)

        return await self.db_pool.runInteraction(
            "process_target_delayed_event", process_target_delayed_event_txn
        )

    async def cancel_delayed_event(
        self, delay_id: str, finalised_ts: Timestamp
    ) -> Timestamp | None:
        """
        Cancels the matching delayed event, i.e. remove it as long as it hasn't been processed.

        Returns: The send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
            SynapseError: if the delayed event has already been sent.
        """

        def cancel_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Timestamp | None:
            txn.execute(
                """
                UPDATE delayed_events
                SET finalised_ts = ?
                WHERE delay_id = ?
                    AND NOT is_processed
                    AND finalised_ts IS NULL
                """,
                (
                    finalised_ts,
                    delay_id,
                ),
            )
            if txn.rowcount == 0:
                txn.execute(
                    """
                    SELECT finalised_event_id IS NOT NULL
                    FROM delayed_events
                    WHERE delay_id = ?
                        AND finalised_ts IS NOT NULL
                    """,
                    (delay_id,),
                )
                row = txn.fetchone()
                if not row:
                    raise NotFoundError("Delayed event not found")
                elif row[0]:
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
    ) -> Timestamp | None:
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
        ) -> Timestamp | None:
            txn.execute(
                """
                UPDATE delayed_events
                SET
                    finalised_error = ?,
                    finalised_ts = ?
                WHERE room_id = ? AND event_type = ? AND state_key = ?
                    AND user_localpart <> ?
                    AND NOT is_processed
                    AND finalised_ts IS NULL
                """,
                (
                    _generate_cancelled_by_state_update_json(),
                    finalised_ts,
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

    async def finalise_processed_delayed_event(
        self,
        delay_id: DelayID,
        result_or_error: str | JsonDict,
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

        def finalise_processed_delayed_event_txn(txn: LoggingTransaction) -> None:
            table = "delayed_events"
            txn.execute(
                f"""
                UPDATE {table}
                SET
                    finalised_error = ?,
                    finalised_event_id = ?,
                    finalised_ts = ?
                WHERE delay_id = ?
                    AND is_processed
                    AND finalised_ts IS NULL
                """,
                (
                    json_encoder.encode(send_error) if send_error is not None else None,
                    event_id,
                    finalised_ts,
                    delay_id,
                ),
            )
            rowcount = txn.rowcount
            if rowcount == 0:
                raise StoreError(404, "No row found (%s)" % (table,))
            if rowcount > 1:
                raise StoreError(500, "More than one row matched (%s)" % (table,))

        await self.db_pool.runInteraction(
            "finalise_processed_delayed_event",
            finalise_processed_delayed_event_txn,
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

        def finalise_processed_delayed_state_events(txn: LoggingTransaction) -> None:
            txn.execute(
                """
                UPDATE delayed_events
                SET
                    finalised_error = ?,
                    finalised_ts = ?
                WHERE room_id = ? AND event_type = ? AND state_key = ?
                    AND is_processed
                    AND finalised_ts IS NULL
                """,
                (
                    _generate_cancelled_by_state_update_json(),
                    finalised_ts,
                    room_id,
                    event_type,
                    state_key,
                ),
            )

        await self.db_pool.runInteraction(
            "finalise_processed_delayed_state_events",
            finalise_processed_delayed_state_events,
        )

    async def unprocess_delayed_events(self) -> None:
        """
        Unmark all delayed events for processing.
        """

        def unprocess_delayed_events(txn: LoggingTransaction) -> None:
            txn.execute(
                """
                UPDATE delayed_events SET is_processed = FALSE
                WHERE is_processed
                    AND finalised_ts IS NULL
                """
            )

        await self.db_pool.runInteraction(
            "unprocess_delayed_events",
            unprocess_delayed_events,
        )

    async def get_next_delayed_event_send_ts(self) -> Timestamp | None:
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
    ) -> Timestamp | None:
        txn.execute(
            """
            SELECT MIN(send_ts) FROM delayed_events
            WHERE NOT is_processed
                AND finalised_ts IS NULL
            """
        )
        resp = txn.fetchone()
        return Timestamp(resp[0]) if resp is not None else None


def _generate_delay_id() -> DelayID:
    """Generates an opaque string, for use as a delay ID"""

    # We use the following format for delay IDs:
    #    syd_<random string>
    # They are not scoped to user localparts, but the random string
    # is expected to be sufficiently random to be globally unique.

    return DelayID(f"syd_{stringutils.random_string(20)}")


def _make_finalised_dict(
    finalised_ts: int | None,
    finalised_event_id: str | None,
    finalised_error: str | None,
) -> JsonDict | None:
    """
    Builds the `finalised` object of a delayed event's API representation
    from its stored finalisation columns, or returns None if it is not finalised.
    """
    if finalised_ts is None:
        return None
    finalised: JsonDict = {"finalised_ts": finalised_ts}
    if finalised_event_id is not None:
        finalised["event_id"] = finalised_event_id
    elif finalised_error is not None:
        finalised["error"] = db_to_json(finalised_error)
    return finalised


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
