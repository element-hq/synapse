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
from http import HTTPStatus
from typing import Any, Dict, List, NewType, Optional, Set, Tuple

from synapse.api.errors import (
    Codes,
    InvalidAPICallError,
    NotFoundError,
    StoreError,
    SynapseError,
)
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import LoggingTransaction
from synapse.types import JsonDict, RoomID, StrCollection
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
    async def add(
        self,
        *,
        user_localpart: UserLocalpart,
        current_ts: Timestamp,
        room_id: RoomID,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        delay: Optional[int],
        parent_id: Optional[str],
    ) -> DelayID:
        """
        Inserts a new delayed event in the DB.

        Returns: The generated ID assigned to the added delayed event.

        Raises:
            SynapseError: if the delayed event failed to be added.
        """

        def add_txn(txn: LoggingTransaction) -> DelayID:
            delay_id = _generate_delay_id()
            try:
                sql = """
                    INSERT INTO delayed_events (
                        delay_id, user_localpart, running_since,
                        room_id, event_type, state_key, origin_server_ts,
                        content
                    ) VALUES (
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?
                    )
                    """
                if self.database_engine.supports_returning:
                    sql += "RETURNING delay_rowid"
                txn.execute(
                    sql,
                    (
                        delay_id,
                        user_localpart,
                        current_ts,
                        room_id.to_string(),
                        event_type,
                        state_key,
                        origin_server_ts,
                        json_encoder.encode(content),
                    ),
                )
            # TODO: Handle only the error for DB key collisions
            except Exception as e:
                logger.debug(
                    "Error inserting into delayed_events",
                    str(e),
                )
                raise SynapseError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Couldn't generate a unique delay_id for user_localpart {user_localpart}",
                )

            if not self.database_engine.supports_returning:
                txn.execute(
                    """
                    SELECT delay_rowid
                    FROM delayed_events
                    WHERE delay_id = ? AND user_localpart = ?
                    """,
                    (
                        delay_id,
                        user_localpart,
                    ),
                )
            row = txn.fetchone()
            assert row is not None
            delay_rowid = row[0]

            if delay is not None:
                self.db_pool.simple_insert_txn(
                    txn,
                    table="delayed_event_timeouts",
                    values={
                        "delay_rowid": delay_rowid,
                        "delay": delay,
                    },
                )

            if parent_id is None:
                self.db_pool.simple_insert_txn(
                    txn,
                    table="delayed_event_parents",
                    values={"delay_rowid": delay_rowid},
                )
            else:
                try:
                    txn.execute(
                        """
                        INSERT INTO delayed_event_children (child_rowid, parent_rowid)
                        SELECT ?, delay_rowid
                        FROM delayed_events
                        WHERE delay_id = ? AND user_localpart = ?
                        """,
                        (
                            delay_rowid,
                            parent_id,
                            user_localpart,
                        ),
                    )
                # TODO: Handle only the error for the relevant foreign key / check violation
                except Exception as e:
                    logger.debug(
                        "Error inserting into delayed_event_children",
                        str(e),
                    )
                    raise SynapseError(
                        HTTPStatus.BAD_REQUEST,
                        # TODO: Improve the wording for this
                        "Invalid parent delayed event",
                        Codes.INVALID_PARAM,
                    )
                if txn.rowcount != 1:
                    raise NotFoundError("Parent delayed event not found")

            return delay_id

        attempts_remaining = 10
        while True:
            try:
                return await self.db_pool.runInteraction("add", add_txn)
            except SynapseError as e:
                if (
                    e.code == HTTPStatus.INTERNAL_SERVER_ERROR
                    and attempts_remaining > 0
                ):
                    attempts_remaining -= 1
                else:
                    raise e

    async def restart(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
        current_ts: Timestamp,
    ) -> Delay:
        """
        Resets the matching delayed event, as long as it has a timeout.

        Args:
            delay_id: The ID of the delayed event to restart.
            user_localpart: The localpart of the delayed event's owner.
            current_ts: The current time, to which the delayed event's "running_since" will be set to.

        Returns: The delay at which the delayed event will be sent (unless it is reset again).

        Raises:
            NotFoundError: if there is no matching delayed event.
            SynapseError: if the matching delayed event has no timeout.
        """

        def restart_txn(txn: LoggingTransaction) -> Delay:
            keyvalues = {
                "delay_id": delay_id,
                "user_localpart": user_localpart,
            }
            row = self.db_pool.simple_select_one_txn(
                txn,
                table="delayed_events JOIN delayed_event_timeouts USING (delay_rowid)",
                keyvalues=keyvalues,
                retcols=("delay_rowid", "delay"),
                allow_none=True,
            )
            if row is None:
                try:
                    self.db_pool.simple_select_one_onecol_txn(
                        txn,
                        table="delayed_events",
                        keyvalues=keyvalues,
                        retcol="1",
                    )
                except StoreError:
                    raise NotFoundError("Delayed event not found")
                else:
                    raise InvalidAPICallError("Delayed event has no timeout")

            self.db_pool.simple_update_txn(
                txn,
                table="delayed_events",
                keyvalues={"delay_rowid": row[0]},
                updatevalues={"running_since": current_ts},
            )
            return Delay(row[1])

        return await self.db_pool.runInteraction("restart", restart_txn)

    async def get_all_for_user(
        self,
        user_localpart: UserLocalpart,
    ) -> List[JsonDict]:
        """Returns all pending delayed events owned by the given user."""
        # TODO: Store and return "transaction_id"
        # TODO: Support Pagination stream API ("next_batch" field)
        rows = await self.db_pool.execute(
            "get_all_for_user",
            """
            SELECT
                delay_id,
                room_id, event_type, state_key,
                delay, parent_id,
                running_since,
                content
            FROM delayed_events
            LEFT JOIN delayed_event_timeouts USING (delay_rowid)
            LEFT JOIN (
                SELECT delay_id AS parent_id, child_rowid
                FROM delayed_event_children
                JOIN delayed_events ON parent_rowid = delay_rowid
            ) ON delay_rowid = child_rowid
            WHERE user_localpart = ?
            """,
            user_localpart,
        )
        return [
            {
                "delay_id": DelayID(row[0]),
                "room_id": str(RoomID.from_string(row[1])),
                "type": EventType(row[2]),
                **({"state_key": StateKey(row[3])} if row[3] is not None else {}),
                **({"delay": Delay(row[4])} if row[4] is not None else {}),
                **({"parent_delay_id": DelayID(row[5])} if row[5] is not None else {}),
                "running_since": Timestamp(row[6]),
                "content": db_to_json(row[7]),
            }
            for row in rows
        ]

    async def process_all_delays(self, current_ts: Timestamp) -> Tuple[
        List[DelayedPartialEventWithUser],
        List[Tuple[DelayID, UserLocalpart, Delay]],
    ]:
        """
        Pops all delayed events that should have timed out prior to the provided time,
        and returns all remaining timeout delayed events along with
        how much later from the provided time they should time out at.
        """

        def process_all_delays_txn(txn: LoggingTransaction) -> Tuple[
            List[DelayedPartialEventWithUser],
            List[Tuple[DelayID, UserLocalpart, Delay]],
        ]:
            events: List[DelayedPartialEventWithUser] = []
            removed_timeout_delay_ids: Set[DelayID] = set()

            txn.execute(
                """
                WITH delay_send_times AS (
                    SELECT *, running_since + delay AS send_ts
                    FROM delayed_events
                    JOIN delayed_event_timeouts USING (delay_rowid)
                )
                SELECT delay_rowid, user_localpart
                FROM delay_send_times
                WHERE send_ts < ?
                ORDER BY send_ts
                """,
                (current_ts,),
            )
            for row in txn.fetchall():
                try:
                    event, removed_timeout_delay_id = self._pop_event_txn(
                        txn,
                        keyvalues={"delay_rowid": row[0]},
                    )
                except NotFoundError:
                    pass
                events.append((UserLocalpart(row[1]), *event))
                removed_timeout_delay_ids |= removed_timeout_delay_id

            txn.execute(
                """
                SELECT
                    delay_id,
                    user_localpart,
                    running_since + delay - ? AS relative_delay
                FROM delayed_events
                JOIN delayed_event_timeouts USING (delay_rowid)
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
            "process_all_delays", process_all_delays_txn
        )

    async def pop_event(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
    ) -> Tuple[
        DelayedPartialEvent,
        Set[DelayID],
    ]:
        """
        Get the partial event of the matching delayed event,
        and remove it and all of its parent/child/sibling events from the DB.

        Returns:
            A tuple of:
                - The partial event to send for the matching delayed event.
                - The IDs of all removed delayed events with a timeout that must be unscheduled.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """
        return await self.db_pool.runInteraction(
            "pop_event",
            self._pop_event_txn,
            keyvalues={
                "delay_id": delay_id,
                "user_localpart": user_localpart,
            },
        )

    def _pop_event_txn(
        self,
        txn: LoggingTransaction,
        keyvalues: Dict[str, Any],
    ) -> Tuple[
        DelayedPartialEvent,
        Set[DelayID],
    ]:
        row = self.db_pool.simple_select_one_txn(
            txn,
            table="delayed_events",
            keyvalues=keyvalues,
            retcols=(
                "delay_rowid",
                "room_id",
                "event_type",
                "state_key",
                "origin_server_ts",
                "content",
            ),
            allow_none=True,
        )
        if row is None:
            raise NotFoundError("Delayed event not found")
        target_delay_rowid = row[0]
        event_row = row[1:]

        parent_rowid = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="delayed_event_children JOIN delayed_events ON child_rowid = delay_rowid",
            keyvalues={"delay_rowid": target_delay_rowid},
            retcol="parent_rowid",
            allow_none=True,
        )

        removed_timeout_delay_ids = self._remove_txn(
            txn,
            keyvalues={
                "delay_rowid": (
                    parent_rowid if parent_rowid is not None else target_delay_rowid
                ),
            },
            retcols=("delay_id",),
        )

        contents: JsonDict = db_to_json(event_row[4])
        return (
            (
                RoomID.from_string(event_row[0]),
                EventType(event_row[1]),
                StateKey(event_row[2]) if event_row[2] is not None else None,
                Timestamp(event_row[3]) if event_row[3] is not None else None,
                contents,
            ),
            {DelayID(r[0]) for r in removed_timeout_delay_ids},
        )

    async def remove(
        self,
        delay_id: DelayID,
        user_localpart: UserLocalpart,
    ) -> Set[DelayID]:
        """
        Removes the matching delayed event, as well as all of its child events if it is a parent.

        Returns:
            The IDs of all removed delayed events with a timeout that must be unscheduled.

        Raises:
            NotFoundError: if there is no matching delayed event.
        """

        removed_timeout_delay_ids = await self.db_pool.runInteraction(
            "remove",
            self._remove_txn,
            keyvalues={
                "delay_id": delay_id,
                "user_localpart": user_localpart,
            },
            retcols=("delay_id",),
        )
        return {DelayID(r[0]) for r in removed_timeout_delay_ids}

    async def remove_state_events(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
    ) -> List[Tuple[DelayID, UserLocalpart]]:
        """
        Removes all matching delayed state events from the DB, as well as their children.

        Returns:
            The ID & owner of every removed delayed event with a timeout that must be unscheduled.
        """
        return await self.db_pool.runInteraction(
            "remove_state_events",
            self._remove_txn,
            keyvalues={
                "room_id": room_id,
                "event_type": event_type,
                "state_key": state_key,
            },
            retcols=("delay_id", "user_localpart"),
            allow_none=True,
        )

    def _remove_txn(
        self,
        txn: LoggingTransaction,
        keyvalues: Dict[str, Any],
        retcols: StrCollection,
        allow_none: bool = False,
    ) -> List[Tuple]:
        """
        Removes delayed events matching the keyvalues, and any children they may have.

        Returns:
            The specified columns for each delayed event with a timeout that was removed.

        Raises:
            NotFoundError: if allow_none is False and no delayed events match the keyvalues.
        """
        sql_with = f"""
            WITH target_rowids AS (
                SELECT delay_rowid
                FROM delayed_events
                WHERE {" AND ".join("%s = ?" % k for k in keyvalues)}
            )
        """
        sql_where = """
            WHERE delay_rowid IN (SELECT * FROM target_rowids)
            OR delay_rowid IN (
                SELECT child_rowid
                FROM delayed_event_children
                JOIN target_rowids ON parent_rowid = delay_rowid
            )
        """
        args = list(keyvalues.values())
        txn.execute(
            f"""
            {sql_with}
            SELECT {", ".join(retcols)}
            FROM delayed_events
            JOIN delayed_event_timeouts USING (delay_rowid)
            {sql_where}
            """,
            args,
        )
        rows = txn.fetchall()
        txn.execute(
            f"""
            {sql_with}
            DELETE FROM delayed_events
            {sql_where}
            """,
            args,
        )
        if not allow_none and txn.rowcount == 0:
            raise NotFoundError("No delayed event found")
        return rows


def _generate_delay_id() -> DelayID:
    """Generates an opaque string, for use as a delay ID"""

    # We use the following format for delay IDs:
    #    syf_<random string>
    # They are scoped to user localparts, so it is possible for
    # the same ID to exist for multiple users.

    return DelayID(f"syd_{stringutils.random_string(20)}")
