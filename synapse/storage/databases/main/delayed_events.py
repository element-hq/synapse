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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
#

import logging
from typing import List, NewType, Optional, Tuple

from synapse.api.errors import NotFoundError
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import LoggingTransaction, make_tuple_in_list_sql_clause
from synapse.types import JsonDict, RoomID
from synapse.util import json_encoder, stringutils as stringutils

logger = logging.getLogger(__name__)


DelayID = NewType("DelayID", str)
UserLocalpart = NewType("UserLocalpart", str)
EventType = NewType("EventType", str)
StateKey = NewType("StateKey", str)

Delay = NewType("Delay", int)
Timestamp = NewType("Timestamp", int)

DelayedPartialEvent = Tuple[
    RoomID,
    EventType,
    Optional[StateKey],
    Optional[Timestamp],
    JsonDict,
]

# TODO: If a Tuple type hint can be extended, extend the above one
DelayedPartialEventWithUser = Tuple[
    UserLocalpart,
    RoomID,
    EventType,
    Optional[StateKey],
    Optional[Timestamp],
    JsonDict,
]


# TODO: Try to support workers
class DelayedEventsStore(SQLBaseStore):
    async def add_delayed_event(
        self,
        *,
        user_localpart: UserLocalpart,
        current_ts: Timestamp,
        room_id: RoomID,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        delay: int,
    ) -> DelayID:
        """
        Inserts a new delayed event in the DB.

        Returns: The generated ID assigned to the added delayed event.
        """
        delay_id = _generate_delay_id()
        await self.db_pool.simple_insert(
            table="delayed_events",
            values={
                "delay_id": delay_id,
                "user_localpart": user_localpart,
                "delay": delay,
                "running_since": current_ts,
                "room_id": room_id.to_string(),
                "event_type": event_type,
                "state_key": state_key,
                "origin_server_ts": origin_server_ts,
                "content": json_encoder.encode(content),
            },
            desc="add_delayed_event",
        )
        return delay_id

    async def restart_delayed_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
        current_ts: Timestamp,
    ) -> Delay:
        """
        Restarts the send time of the matching delayed event.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.
            current_ts: The current time, to which the delayed event's "running_since" will be set to.

        Returns: The delay at which the delayed event will be sent (unless it is reset again).

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def restart_txn(txn: LoggingTransaction) -> Delay:
            if self.database_engine.supports_returning:
                txn.execute(
                    """
                    UPDATE delayed_events
                    SET running_since = ?
                    WHERE delay_id = ? AND user_localpart = ?
                    RETURNING delay
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
                return Delay(row[0])
            else:
                keyvalues = {
                    "delay_id": delay_id,
                    "user_localpart": user_localpart,
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
                self.db_pool.simple_update_one_txn(
                    txn,
                    table="delayed_events",
                    keyvalues=keyvalues,
                    updatevalues={"running_since": current_ts},
                )
                return Delay(delay)

        return await self.db_pool.runInteraction("restart_delayed_event", restart_txn)

    async def get_all_delayed_events_for_user(
        self,
        user_localpart: UserLocalpart,
    ) -> List[JsonDict]:
        """Returns all pending delayed events owned by the given user."""
        # TODO: Store and return "transaction_id"
        # TODO: Support Pagination stream API ("next_batch" field)
        rows = await self.db_pool.simple_select_list(
            table="delayed_events",
            keyvalues={"user_localpart": user_localpart},
            retcols=(
                "delay_id",
                "room_id",
                "event_type",
                "state_key",
                "delay",
                "running_since",
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
                **({"delay": Delay(row[4])} if row[4] is not None else {}),
                "running_since": Timestamp(row[5]),
                "content": db_to_json(row[6]),
            }
            for row in rows
        ]

    async def process_all_delayed_events(self, current_ts: Timestamp) -> Tuple[
        List[DelayedPartialEventWithUser],
        List[Tuple[DelayID, UserLocalpart, Delay]],
    ]:
        """
        Pops all delayed events that should have timed out prior to the provided time,
        and returns all remaining timeout delayed events along with
        how much later from the provided time they should time out at.

        Does not return any delayed events that got removed but not sent, as this is
        meant to be called on startup before any delayed events have been scheduled.
        """

        def process_all_delays_txn(txn: LoggingTransaction) -> Tuple[
            List[DelayedPartialEventWithUser],
            List[Tuple[DelayID, UserLocalpart, Delay]],
        ]:
            sql_cols = ", ".join(
                (
                    "user_localpart",
                    "room_id",
                    "event_type",
                    "state_key",
                    "origin_server_ts",
                    "content",
                )
            )
            sql_from = "FROM delayed_events WHERE running_since + delay < ?"
            sql_order = "ORDER BY running_since + delay"
            if self.database_engine.supports_returning:
                txn.execute(
                    f"""
                    WITH timed_out_events AS (
                        DELETE {sql_from} RETURNING *
                    ) SELECT {sql_cols} FROM timed_out_events {sql_order}
                    """,
                    (current_ts,),
                )
                rows = txn.fetchall()
            else:
                txn.execute(
                    f"SELECT {sql_cols}, delay_id {sql_from} {sql_order}", (current_ts,)
                )
                rows = txn.fetchall()
                sql_key_clause, sql_key_args = make_tuple_in_list_sql_clause(
                    self.database_engine,
                    ("delay_id", "user_localpart"),
                    tuple((row[-1], row[0]) for row in rows),
                )
                txn.execute(
                    f"DELETE from delayed_events WHERE {sql_key_clause}", sql_key_args
                )
            events = [
                (
                    UserLocalpart(row[0]),
                    RoomID.from_string(row[1]),
                    EventType(row[2]),
                    StateKey(row[3]) if row[3] is not None else None,
                    Timestamp(row[4]) if row[4] is not None else None,
                    db_to_json(row[5]),
                )
                for row in rows
            ]

            txn.execute(
                """
                SELECT
                    delay_id,
                    user_localpart,
                    running_since + delay - ? AS relative_delay
                FROM delayed_events
                """,
                (current_ts,),
            )
            remaining_timeout_delays = [
                (
                    DelayID(row[0]),
                    UserLocalpart(row[1]),
                    Delay(row[2]),
                )
                for row in txn
            ]
            return events, remaining_timeout_delays

        return await self.db_pool.runInteraction(
            "process_all_delayed_events", process_all_delays_txn
        )

    async def pop_delayed_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
    ) -> DelayedPartialEvent:
        """
        Gets the partial event of the matching delayed event, and remove it from the DB.

        Returns:
            The partial event to send for the matching delayed event.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        def pop_event_txn(txn: LoggingTransaction) -> DelayedPartialEvent:
            sql_cols = ", ".join(
                (
                    "room_id",
                    "event_type",
                    "state_key",
                    "origin_server_ts",
                    "content",
                )
            )
            sql_from = "FROM delayed_events WHERE delay_id = ? AND user_localpart = ?"
            txn.execute(
                (
                    f"DELETE {sql_from} RETURNING {sql_cols}"
                    if self.database_engine.supports_returning
                    else f"SELECT {sql_cols} {sql_from}"
                ),
                (delay_id, user_localpart),
            )
            row = txn.fetchone()
            if row is None:
                raise NotFoundError("Delayed event not found")
            elif not self.database_engine.supports_returning:
                txn.execute(f"DELETE {sql_from}")
                assert txn.rowcount == 1

            return (
                RoomID.from_string(row[0]),
                EventType(row[1]),
                StateKey(row[2]) if row[2] is not None else None,
                Timestamp(row[3]) if row[3] is not None else None,
                db_to_json(row[4]),
            )

        return await self.db_pool.runInteraction("pop_delayed_event", pop_event_txn)

    async def remove_delayed_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
    ) -> None:
        """
        Removes the matching delayed event.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """
        await self.db_pool.simple_delete(
            table="delayed_events",
            keyvalues={
                "delay_id": delay_id,
                "user_localpart": user_localpart,
            },
            desc="remove_delayed_event",
        )

    async def remove_delayed_state_events(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
    ) -> List[Tuple[DelayID, UserLocalpart]]:
        """
        Removes all matching delayed state events from the DB.

        Returns:
            The ID & owner of every removed delayed event.
        """

        def remove_state_events_txn(txn: LoggingTransaction) -> List[Tuple]:
            sql_cols = ", ".join(
                (
                    "delay_id",
                    "user_localpart",
                )
            )
            sql_from = (
                "FROM delayed_events "
                "WHERE room_id = ? AND event_type = ? AND state_key = ?"
            )
            sql_args = (room_id, event_type, state_key)
            if self.database_engine.supports_returning:
                txn.execute(f"DELETE {sql_from} RETURNING {sql_cols}", sql_args)
                rows = txn.fetchall()
            else:
                txn.execute(f"SELECT {sql_cols} {sql_from}", sql_args)
                rows = txn.fetchall()
                txn.execute(f"DELETE {sql_from}")
            return [
                (
                    DelayID(row[0]),
                    UserLocalpart(row[1]),
                )
                for row in rows
            ]

        return await self.db_pool.runInteraction(
            "remove_delayed_state_events", remove_state_events_txn
        )


def _generate_delay_id() -> DelayID:
    """Generates an opaque string, for use as a delay ID"""

    # We use the following format for delay IDs:
    #    syd_<random string>
    # They are scoped to user localparts, so it is possible for
    # the same ID to exist for multiple users.

    return DelayID(f"syd_{stringutils.random_string(20)}")
