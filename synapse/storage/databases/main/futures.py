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

from binascii import crc32
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    NewType,
    Optional,
    Tuple,
    TypeVar,
)

from synapse.api.errors import NotFoundError, StoreError, SynapseError
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.types import JsonDict, RoomID, UserID
from synapse.util import json_encoder, stringutils as stringutils
from synapse.util.stringutils import base62_encode

if TYPE_CHECKING:
    from synapse.server import HomeServer


class FutureTokenType(Enum):
    SEND = 1
    CANCEL = 2
    REFRESH = 4

    def __str__(self) -> str:
        if self == FutureTokenType.SEND:
            return "is_send"
        elif self == FutureTokenType.CANCEL:
            return "is_cancel"
        else:
            return "is_refresh"


FutureToken = NewType("FutureToken", str)
GroupID = NewType("GroupID", str)
EventType = NewType("EventType", str)
StateKey = NewType("StateKey", str)

FutureID = NewType("FutureID", int)
Timeout = NewType("Timeout", int)
Timestamp = NewType("Timestamp", int)

AddFutureReturn = Tuple[
    FutureID,
    GroupID,
    FutureToken,
    FutureToken,
    Optional[FutureToken],
]


# TODO: Try to support workers
class FuturesStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

    async def add_future(
        self,
        *,
        user_id: UserID,
        group_id: Optional[str],
        room_id: RoomID,
        event_type: str,
        state_key: Optional[str],
        origin_server_ts: Optional[int],
        content: JsonDict,
        timeout: Optional[int],
        timestamp: Optional[int],
    ) -> AddFutureReturn:
        """Inserts a new future in the DB."""
        user_localpart = user_id.localpart

        def add_future_txn(txn: LoggingTransaction) -> AddFutureReturn:
            T = TypeVar("T", bound=str)

            def insert_and_get_unique_colval(
                table: str,
                values: Dict[str, Any],
                column: str,
                colval_generator: Callable[[], T],
            ) -> T:
                attempts_remaining = 10
                while True:
                    colval = colval_generator()
                    try:
                        self.db_pool.simple_insert_txn(
                            txn,
                            table,
                            values={
                                **values,
                                column: colval,
                            },
                        )
                        return colval
                    # TODO: Handle only the error type for DB key collisions
                    except Exception:
                        if attempts_remaining > 0:
                            attempts_remaining -= 1
                        else:
                            raise SynapseError(
                                500,
                                f"Couldn't generate a unique value for column {column} in table {table}",
                            )

            if group_id is None:
                group_id_final = insert_and_get_unique_colval(
                    "future_groups",
                    {
                        "user_localpart": user_localpart,
                    },
                    "group_id",
                    _generate_group_id,
                )
            else:
                txn.execute(
                    """
                    INSERT INTO future_groups (user_localpart, group_id)
                    VALUES (?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (user_localpart, group_id),
                )
                group_id_final = GroupID(group_id)

            txn.execute(
                """
                INSERT INTO futures (
                    user_localpart, group_id,
                    room_id, event_type, state_key, origin_server_ts,
                    content
                ) VALUES (
                    ?, ?,
                    ?, ?, ?, ?,
                    ?
                )
                RETURNING future_id
                """,
                (
                    user_localpart,
                    group_id_final,
                    room_id.to_string(),
                    event_type,
                    state_key,
                    origin_server_ts,
                    json_encoder.encode(content),
                ),
            )
            row = txn.fetchone()
            assert row is not None
            future_id = FutureID(row[0])

            def insert_and_get_future_token(token_type: FutureTokenType) -> FutureToken:
                return insert_and_get_unique_colval(
                    "future_tokens",
                    {
                        "future_id": future_id,
                        str(token_type): True,
                    },
                    "token",
                    _generate_future_token,
                )

            send_token = insert_and_get_future_token(FutureTokenType.SEND)
            cancel_token = insert_and_get_future_token(FutureTokenType.CANCEL)
            if timeout is not None:
                self.db_pool.simple_insert_txn(
                    txn,
                    table="future_timeouts",
                    values={
                        "future_id": future_id,
                        "timeout": timeout,
                        "timestamp": timestamp,
                    },
                )
                refresh_token = insert_and_get_future_token(FutureTokenType.REFRESH)
            else:
                refresh_token = None

            return (
                future_id,
                group_id_final,
                send_token,
                cancel_token,
                refresh_token,
            )

        return await self.db_pool.runInteraction("add_future", add_future_txn)

    async def update_future_timestamp(
        self,
        future_id: FutureID,
        current_ts: Timestamp,
    ) -> Timeout:
        """Updates the timestamp of the timeout future for the given future_id.

        Params:
            future_id: The ID of the future timeout to update.
            current_ts: The current time to which the future's timestamp will be set relative to.

        Returns: The timeout value for the future.

        Raises:
            NotFoundError if there is no timeout future for the given future_id.
        """
        rows = await self.db_pool.execute(
            "update_future_timestamp",
            """
            UPDATE future_timeouts
            SET timestamp = ? + timeout
            WHERE future_id = ?
            RETURNING timeout
            """,
            current_ts,
            future_id,
        )
        if len(rows) == 0 or len(rows[0]) == 0:
            raise NotFoundError
        return Timeout(rows[0][0])

    async def has_group_id(
        self,
        user_id: UserID,
        group_id: str,
    ) -> bool:
        """Returns whether a future group exists for the given group_id."""
        count: int = await self.db_pool.simple_select_one_onecol(
            table="future_groups",
            keyvalues={"user_localpart": user_id.localpart, "group_id": group_id},
            retcol="COUNT(1)",
            desc="has_group_id",
        )
        return count > 0

    async def get_future_by_token(
        self,
        token: str,
    ) -> Tuple[FutureID, FutureTokenType]:
        """Returns the future ID for the given token, and what type of token it is.

        Raises:
            NotFoundError if there is no future for the given token.
        """
        row = await self.db_pool.simple_select_one(
            table="future_tokens",
            keyvalues={"token": token},
            retcols=("is_send", "is_cancel", "is_refresh", "future_id"),
            allow_none=True,
            desc="get_future_by_token",
        )
        if row is None:
            raise NotFoundError

        return FutureID(row[3]), FutureTokenType(sum(row[i] * 2**i for i in range(3)))

    async def get_all_futures_for_user(
        self,
        user_id: UserID,
    ) -> List[JsonDict]:
        """Returns all pending futures that were requested by the given user."""
        rows = await self.db_pool.execute(
            "_get_all_futures_for_user_txn",
            """
            SELECT
                group_id, timeout,
                room_id, event_type, state_key, content,
                send_token, cancel_token, refresh_token
            FROM futures
            LEFT JOIN (
                SELECT future_id, token AS send_token
                FROM future_tokens
                WHERE is_send
            ) USING (future_id)
            LEFT JOIN (
                SELECT future_id, token AS cancel_token
                FROM future_tokens
                WHERE is_cancel
            ) USING (future_id)
            LEFT JOIN (
                SELECT future_id, token AS refresh_token
                FROM future_tokens
                WHERE is_refresh
            ) USING (future_id)
            LEFT JOIN future_timeouts USING (future_id)
            WHERE user_localpart = ?
            """,
            user_id.localpart,
        )
        return [
            {
                "group_id": str(row[0]),
                **({"timeout": int(row[1])} if row[1] is not None else {}),
                "room_id": str(row[2]),
                "type": str(row[3]),
                **({"state_key": str(row[4])} if row[4] is not None else {}),
                # TODO: Verify contents?
                "content": db_to_json(row[5]),
                # TODO: If suppressing send/cancel tokens is allowed, omit them if None
                "send_token": str(row[6]),
                "cancel_token": str(row[7]),
                **({"refresh_token": str(row[8])} if row[8] is not None else {}),
            }
            for row in rows
        ]

    async def get_all_future_timestamps(
        self,
    ) -> List[Tuple[FutureID, Timestamp]]:
        """Returns all timeout futures' IDs and when they will be sent if not refreshed."""
        return await self.db_pool.simple_select_list(
            table="future_timeouts",
            keyvalues=None,
            retcols=("future_id", "timestamp"),
            desc="get_all_future_timestamps",
        )

    async def pop_future_event(
        self,
        future_id: FutureID,
    ) -> Tuple[
        str, RoomID, EventType, Optional[StateKey], Optional[Timestamp], JsonDict
    ]:
        """Get the partial event of the future with the specified future_id,
        and remove all futures in its group from the DB.
        """

        def pop_future_event_txn(txn: LoggingTransaction) -> Tuple[Any, ...]:
            try:
                row = self.db_pool.simple_select_one_txn(
                    txn,
                    table="futures",
                    keyvalues={"future_id": future_id},
                    retcols=(
                        "user_localpart",
                        "group_id",
                        "room_id",
                        "event_type",
                        "state_key",
                        "origin_server_ts",
                        "content",
                    ),
                )
                assert row is not None
            except StoreError:
                raise NotFoundError

            self.db_pool.simple_delete_txn(
                txn,
                table="future_groups",
                keyvalues={
                    "user_localpart": str(row[0]),
                    "group_id": GroupID(row[1]),
                },
            )
            return (row[0], *row[2:])

        row = await self.db_pool.runInteraction(
            "pop_future_event", pop_future_event_txn
        )
        room_id = RoomID.from_string(row[1])
        # TODO: verify contents?
        content: JsonDict = db_to_json(row[5])
        return (row[0], room_id, *row[2:5], content)

    async def remove_future(
        self,
        future_id: FutureID,
    ) -> None:
        """Removes the future for the given future_id from the DB.
        If the future is the only timeout future in its group, removes the whole group.
        """

        def remove_future_txn(txn: LoggingTransaction) -> None:
            txn.execute(
                """
                WITH futures_with_timeout AS (
                    SELECT future_id, user_localpart, group_id
                    FROM futures
                    JOIN future_timeouts USING (future_id)
                ), timeout_group AS (
                    SELECT user_localpart, group_id
                    FROM futures_with_timeout
                    WHERE future_id = ?
                )
                SELECT DISTINCT COUNT(1), user_localpart, group_id
                FROM futures_with_timeout
                JOIN timeout_group USING (user_localpart, group_id)
                GROUP BY user_localpart, group_id
                """,
                (future_id,),
            )
            row = txn.fetchone()

            if row is not None and row[0] == 1:
                self.db_pool.simple_delete_one_txn(
                    txn,
                    "future_groups",
                    keyvalues={
                        "user_localpart": row[1],
                        "group_id": row[2],
                    },
                )
            else:
                self.db_pool.simple_delete_one_txn(
                    txn,
                    "futures",
                    keyvalues={
                        "future_id": future_id,
                    },
                )

        await self.db_pool.runInteraction("remove_future", remove_future_txn)


def _generate_future_token() -> FutureToken:
    """Generates an opaque string, for use as a future token"""

    # We use the following format for future tokens:
    #    syf_<random string>_<base62 crc check>
    # They are NOT scoped to user localparts so that any delegate given the token
    # won't necessarily know which user created the future.

    random_string = stringutils.random_string(20)
    base = f"syf_{random_string}"

    crc = base62_encode(crc32(base.encode("ascii")), minwidth=6)
    return FutureToken(f"{base}_{crc}")


def _generate_group_id() -> GroupID:
    """Generates an opaque string, for use as a future group ID"""

    # We use the following format for future tokens:
    #    syf_<random string>_<base62 crc check>
    # They are scoped to user localparts, but that CANNOT be relied on
    # to keep them globally unique, as users may set their own group_id.

    random_string = stringutils.random_string(20)
    base = f"syg_{random_string}"

    crc = base62_encode(crc32(base.encode("ascii")), minwidth=6)
    return GroupID(f"{base}_{crc}")
