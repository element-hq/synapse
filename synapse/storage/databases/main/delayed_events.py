import logging
from typing import List, NewType, Optional, Tuple

from synapse.api.errors import NotFoundError
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import LoggingTransaction, StoreError
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

# TODO: Maybe use attr class
DelayedEventDetails = Tuple[
    DelayID,
    UserLocalpart,
    RoomID,
    EventType,
    Optional[StateKey],
    Timestamp,
    JsonDict,
    Optional[DeviceID],
]


class DelayedEventsStore(SQLBaseStore):
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
    ) -> Tuple[DelayID, bool]:
        """
        Inserts a new delayed event in the DB.

        Returns: The generated ID assigned to the added delayed event,
            and whether the added delayed event is the next to be sent.
        """
        delay_id = _generate_delay_id()
        send_ts = creation_ts + delay

        def _add_delayed_event_txn(txn: LoggingTransaction) -> bool:
            old_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)

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

            return old_next_send_ts is None or send_ts < old_next_send_ts

        changed = await self.db_pool.runInteraction(
            "add_delayed_event", _add_delayed_event_txn
        )

        return delay_id, changed

    async def restart_delayed_event(
        self,
        *,
        delay_id: str,
        user_localpart: str,
        current_ts: Timestamp,
    ) -> Tuple[bool, Timestamp]:
        """
        Restarts the send time of the matching delayed event,
        as long as it hasn't already been marked for processing.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.
            current_ts: The current time, which will be used to calculate the new send time.

        Returns: Whether the matching delayed event would have been the next to be sent,
            and if so, what the next soonest send time is, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def restart_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Tuple[bool, Timestamp]:
            old_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            if old_next_send_ts is None:
                raise NotFoundError("Delayed event not found")

            if self.database_engine.supports_returning:
                txn.execute(
                    """
                    UPDATE delayed_events
                    SET send_ts = ? + delay
                    WHERE delay_id = ? AND user_localpart = ?
                        AND NOT is_processed
                    RETURNING send_ts
                    """,
                    (
                        current_ts,
                        delay_id,
                        user_localpart,
                    ),
                )
                row = txn.fetchone()
                if row is None:
                    raise NotFoundError("Delayed event not found")

                restarted_send_ts = row[0]
            else:
                keyvalues = {
                    "delay_id": delay_id,
                    "user_localpart": user_localpart,
                    "is_processed": False,
                }
                delay = self.db_pool.simple_select_one_onecol_txn(
                    txn,
                    table="delayed_events",
                    keyvalues=keyvalues,
                    retcol="delay",
                    allow_none=True,
                )
                if delay is None:
                    raise NotFoundError("Delayed event not found")

                restarted_send_ts = current_ts + delay
                self.db_pool.simple_update_one_txn(
                    txn,
                    table="delayed_events",
                    keyvalues=keyvalues,
                    updatevalues={"send_ts": restarted_send_ts},
                )

            new_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            assert new_next_send_ts is not None
            return new_next_send_ts < old_next_send_ts, new_next_send_ts

        return await self.db_pool.runInteraction(
            "restart_delayed_event", restart_delayed_event_txn
        )

    async def get_all_delayed_events_for_user(
        self,
        user_localpart: str,
    ) -> List[JsonDict]:
        """Returns all pending delayed events owned by the given user."""
        # TODO: Store and return "transaction_id"
        # TODO: Support Pagination stream API ("next_batch" field)
        rows = await self.db_pool.simple_select_list(
            table="delayed_events",
            keyvalues={
                "user_localpart": user_localpart,
                "is_processed": False,
            },
            retcols=(
                "delay_id",
                "room_id",
                "event_type",
                "state_key",
                "delay",
                "send_ts",
                "content",
            ),
            desc="get_all_delayed_events_for_user",
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

    async def process_timeout_delayed_events(self, current_ts: Timestamp) -> Tuple[
        List[DelayedEventDetails],
        Optional[Timestamp],
    ]:
        """
        Marks for processing all delayed events that should have been sent prior to the provided time
        that haven't already been marked as such.

        Returns: The details of all newly-processed delayed events,
            and the send time of the next delayed event to be sent, if any.
        """

        def process_timeout_delayed_events_txn(txn: LoggingTransaction) -> Tuple[
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
            if self.database_engine.supports_returning:
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
                (
                    DelayID(row[0]),
                    UserLocalpart(row[1]),
                    RoomID.from_string(row[2]),
                    EventType(row[3]),
                    StateKey(row[4]) if row[4] is not None else None,
                    Timestamp(row[5] if row[5] is not None else row[6]),
                    db_to_json(row[7]),
                    DeviceID(row[8]) if row[8] is not None else None,
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
        Tuple[
            RoomID,
            EventType,
            Optional[StateKey],
            Timestamp,
            JsonDict,
            Optional[DeviceID],
        ],
        bool,
        Optional[Timestamp],
    ]:
        """
        Marks for processing the matching delayed event, regardless of its timeout time,
        as long as it has not already been marked as such.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.

        Returns: The details of the matching delayed event,
            whether the matching delayed event would have been the next to be sent,
            and the send time of the next delayed event to be sent, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def process_target_delayed_event_txn(txn: LoggingTransaction) -> Tuple[
            Tuple[
                RoomID,
                EventType,
                Optional[StateKey],
                Timestamp,
                JsonDict,
                Optional[DeviceID],
            ],
            bool,
            Optional[Timestamp],
        ]:
            sql_cols = ", ".join(
                (
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
            sql_where = "WHERE delay_id = ? AND user_localpart = ? AND NOT is_processed"
            sql_args = (delay_id, user_localpart)
            txn.execute(
                (
                    f"{sql_update} RETURNING {sql_cols}"
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

            send_ts = Timestamp(row[4])
            event = (
                RoomID.from_string(row[0]),
                EventType(row[1]),
                StateKey(row[2]) if row[2] is not None else None,
                Timestamp(row[3]) if row[3] is not None else send_ts,
                db_to_json(row[5]),
                DeviceID(row[6]) if row[6] is not None else None,
            )

            next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            return event, next_send_ts != send_ts, next_send_ts

        return await self.db_pool.runInteraction(
            "process_target_delayed_event", process_target_delayed_event_txn
        )

    async def cancel_delayed_event(
        self,
        *,
        delay_id: str,
        user_localpart: str,
    ) -> Tuple[bool, Optional[Timestamp]]:
        """
        Cancels the matching delayed event, i.e. remove it as long as it hasn't been processed.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.

        Returns: Whether the matching delayed event would have been the next to be sent,
            and if so, what the next soonest send time is, if any.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def cancel_delayed_event_txn(
            txn: LoggingTransaction,
        ) -> Tuple[bool, Optional[Timestamp]]:
            old_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            if old_next_send_ts is None:
                raise NotFoundError("Delayed event not found")

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

            new_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
            return new_next_send_ts != old_next_send_ts, new_next_send_ts

        return await self.db_pool.runInteraction(
            "cancel_delayed_event", cancel_delayed_event_txn
        )

    async def cancel_delayed_state_events(
        self,
        *,
        room_id: str,
        event_type: str,
        state_key: str,
    ) -> Tuple[bool, Optional[Timestamp]]:
        """
        Cancels all matching delayed state events, i.e. remove them as long as they haven't been processed.

        Returns: Whether any of the matching delayed events would have been the next to be sent,
            and if so, what the next soonest send time is, if any.
        """

        def cancel_delayed_state_events_txn(
            txn: LoggingTransaction,
        ) -> Tuple[bool, Optional[Timestamp]]:
            old_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)

            if (
                self.db_pool.simple_delete_txn(
                    txn,
                    table="delayed_events",
                    keyvalues={
                        "room_id": room_id,
                        "event_type": event_type,
                        "state_key": state_key,
                        "is_processed": False,
                    },
                )
                > 0
            ):
                assert old_next_send_ts is not None
                new_next_send_ts = self._get_next_delayed_event_send_ts_txn(txn)
                return new_next_send_ts != old_next_send_ts, new_next_send_ts
            else:
                return False, None

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
