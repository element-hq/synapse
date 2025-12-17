#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2023, 2025 New Vector, Ltd
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
from typing import TYPE_CHECKING, AbstractSet, Mapping, cast

import attr

from synapse.api.errors import SlidingSyncUnknownPosition
from synapse.logging.opentracing import log_kv
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.engines import PostgresEngine
from synapse.types import MultiWriterStreamToken, RoomStreamToken
from synapse.types.handlers.sliding_sync import (
    HaveSentRoom,
    HaveSentRoomFlag,
    MutablePerConnectionState,
    PerConnectionState,
    RoomLazyMembershipChanges,
    RoomStatusMap,
    RoomSyncConfig,
)
from synapse.util.caches.descriptors import cached
from synapse.util.duration import Duration
from synapse.util.json import json_encoder

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.storage.databases.main import DataStore

logger = logging.getLogger(__name__)


# How often to update the `last_used_ts` column on
# `sliding_sync_connection_positions` when the client uses a connection
# position. We don't want to update it on every use to avoid excessive
# writes, but we want it to be reasonably up-to-date to help with
# cleaning up old connection positions.
UPDATE_INTERVAL_LAST_USED_TS = Duration(minutes=5)

# Time in milliseconds the connection hasn't been used before we consider it
# expired and delete it.
CONNECTION_EXPIRY = Duration(days=7)

# How often we run the background process to delete old sliding sync connections.
CONNECTION_EXPIRY_FREQUENCY = Duration(hours=1)


class SlidingSyncStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_index_update(
            update_name="sliding_sync_connection_room_configs_required_state_id_idx",
            index_name="sliding_sync_connection_room_configs_required_state_id_idx",
            table="sliding_sync_connection_room_configs",
            columns=("required_state_id",),
        )

        self.db_pool.updates.register_background_index_update(
            update_name="sliding_sync_membership_snapshots_membership_event_id_idx",
            index_name="sliding_sync_membership_snapshots_membership_event_id_idx",
            table="sliding_sync_membership_snapshots",
            columns=("membership_event_id",),
        )

        self.db_pool.updates.register_background_index_update(
            update_name="sliding_sync_membership_snapshots_user_id_stream_ordering",
            index_name="sliding_sync_membership_snapshots_user_id_stream_ordering",
            table="sliding_sync_membership_snapshots",
            columns=("user_id", "event_stream_ordering"),
            replaces_index="sliding_sync_membership_snapshots_user_id",
        )

        if self.hs.config.worker.run_background_tasks:
            self.clock.looping_call(
                self.delete_old_sliding_sync_connections,
                CONNECTION_EXPIRY_FREQUENCY,
            )

    async def get_latest_bump_stamp_for_room(
        self,
        room_id: str,
    ) -> int | None:
        """
        Get the `bump_stamp` for the room.

        The `bump_stamp` is the `stream_ordering` of the last event according to the
        `bump_event_types`. This helps clients sort more readily without them needing to
        pull in a bunch of the timeline to determine the last activity.
        `bump_event_types` is a thing because for example, we don't want display name
        changes to mark the room as unread and bump it to the top. For encrypted rooms,
        we just have to consider any activity as a bump because we can't see the content
        and the client has to figure it out for themselves.

        This should only be called where the server is participating
        in the room (someone local is joined).

        Returns:
            The `bump_stamp` for the room (which can be `None`).
        """

        return cast(
            int | None,
            await self.db_pool.simple_select_one_onecol(
                table="sliding_sync_joined_rooms",
                keyvalues={"room_id": room_id},
                retcol="bump_stamp",
                # FIXME: This should be `False` once we bump `SCHEMA_COMPAT_VERSION` and run the
                # foreground update for
                # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked
                # by https://github.com/element-hq/synapse/issues/17623)
                #
                # The should be `allow_none=False` in the future because event though
                # `bump_stamp` itself can be `None`, we should have a row in the
                # `sliding_sync_joined_rooms` table for any joined room.
                allow_none=True,
            ),
        )

    async def persist_per_connection_state(
        self,
        user_id: str,
        device_id: str,
        conn_id: str,
        previous_connection_position: int | None,
        per_connection_state: "MutablePerConnectionState",
    ) -> int:
        """Persist updates to the per-connection state for a sliding sync
        connection.

        Returns:
            The connection position of the newly persisted state.
        """

        # This cast is safe because the downstream code only cares about
        # `store.get_id_for_instance(...)` and `StreamWorkerStore` is mixed
        # alongside `SlidingSyncStore` wherever we create a store.
        store = cast("DataStore", self)

        return await self.db_pool.runInteraction(
            "persist_per_connection_state",
            self.persist_per_connection_state_txn,
            user_id=user_id,
            device_id=device_id,
            conn_id=conn_id,
            previous_connection_position=previous_connection_position,
            per_connection_state=await PerConnectionStateDB.from_state(
                per_connection_state, store
            ),
        )

    def persist_per_connection_state_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_id: str,
        conn_id: str,
        previous_connection_position: int | None,
        per_connection_state: "PerConnectionStateDB",
    ) -> int:
        # First we fetch (or create) the connection key associated with the
        # previous connection position.
        if previous_connection_position is not None:
            # The `previous_connection_position` is a user-supplied value, so we
            # need to make sure that the one they supplied is actually theirs.
            sql = """
                SELECT connection_key
                FROM sliding_sync_connection_positions
                INNER JOIN sliding_sync_connections USING (connection_key)
                WHERE
                    connection_position = ?
                    AND user_id = ? AND effective_device_id = ? AND conn_id = ?
            """
            txn.execute(
                sql, (previous_connection_position, user_id, device_id, conn_id)
            )
            row = txn.fetchone()
            if row is None:
                raise SlidingSyncUnknownPosition()

            (connection_key,) = row
        else:
            # We're restarting the connection, so we clear the previous existing data we
            # used to track it. We do this here to ensure that if we get lots of
            # one-shot requests we don't stack up lots of entries. We have `ON DELETE
            # CASCADE` setup on the dependent tables so this will clear out all the
            # associated data.
            self.db_pool.simple_delete_txn(
                txn,
                table="sliding_sync_connections",
                keyvalues={
                    "user_id": user_id,
                    "effective_device_id": device_id,
                    "conn_id": conn_id,
                },
            )

            (connection_key,) = self.db_pool.simple_insert_returning_txn(
                txn,
                table="sliding_sync_connections",
                values={
                    "user_id": user_id,
                    "effective_device_id": device_id,
                    "conn_id": conn_id,
                    "created_ts": self.clock.time_msec(),
                    "last_used_ts": self.clock.time_msec(),
                },
                returning=("connection_key",),
            )

        # Define a new connection position for the updates
        (connection_position,) = self.db_pool.simple_insert_returning_txn(
            txn,
            table="sliding_sync_connection_positions",
            values={
                "connection_key": connection_key,
                "created_ts": self.clock.time_msec(),
            },
            returning=("connection_position",),
        )

        # We need to deduplicate the `required_state` JSON. We do this by
        # fetching all JSON associated with the connection and comparing that
        # with the updates to `required_state`

        # Dict from required state json -> required state ID
        required_state_to_id: dict[str, int] = {}
        if previous_connection_position is not None:
            rows = self.db_pool.simple_select_list_txn(
                txn,
                table="sliding_sync_connection_required_state",
                keyvalues={"connection_key": connection_key},
                retcols=("required_state_id", "required_state"),
            )
            for required_state_id, required_state in rows:
                required_state_to_id[required_state] = required_state_id

        room_to_state_ids: dict[str, int] = {}
        unique_required_state: dict[str, list[str]] = {}
        for room_id, room_state in per_connection_state.room_configs.items():
            serialized_state = json_encoder.encode(
                # We store the required state as a sorted list of event type /
                # state key tuples.
                sorted(
                    (event_type, state_key)
                    for event_type, state_keys in room_state.required_state_map.items()
                    for state_key in state_keys
                )
            )

            existing_state_id = required_state_to_id.get(serialized_state)
            if existing_state_id is not None:
                room_to_state_ids[room_id] = existing_state_id
            else:
                unique_required_state.setdefault(serialized_state, []).append(room_id)

        # Insert any new `required_state` json we haven't previously seen.
        for serialized_required_state, room_ids in unique_required_state.items():
            (required_state_id,) = self.db_pool.simple_insert_returning_txn(
                txn,
                table="sliding_sync_connection_required_state",
                values={
                    "connection_key": connection_key,
                    "required_state": serialized_required_state,
                },
                returning=("required_state_id",),
            )
            for room_id in room_ids:
                room_to_state_ids[room_id] = required_state_id

        # Copy over state from the previous connection position (we'll overwrite
        # these rows with any changes).
        if previous_connection_position is not None:
            sql = """
                INSERT INTO sliding_sync_connection_streams
                (connection_position, stream, room_id, room_status, last_token)
                SELECT ?, stream, room_id, room_status, last_token
                FROM sliding_sync_connection_streams
                WHERE connection_position = ?
            """
            txn.execute(sql, (connection_position, previous_connection_position))

            sql = """
                INSERT INTO sliding_sync_connection_room_configs
                (connection_position, room_id, timeline_limit, required_state_id)
                SELECT ?, room_id, timeline_limit, required_state_id
                FROM sliding_sync_connection_room_configs
                WHERE connection_position = ?
            """
            txn.execute(sql, (connection_position, previous_connection_position))

        # We now upsert the changes to the various streams.
        key_values = []
        value_values = []
        for room_id, have_sent_room in per_connection_state.rooms._statuses.items():
            key_values.append((connection_position, "rooms", room_id))
            value_values.append(
                (have_sent_room.status.value, have_sent_room.last_token)
            )

        for room_id, have_sent_room in per_connection_state.receipts._statuses.items():
            key_values.append((connection_position, "receipts", room_id))
            value_values.append(
                (have_sent_room.status.value, have_sent_room.last_token)
            )

        for (
            room_id,
            have_sent_room,
        ) in per_connection_state.account_data._statuses.items():
            key_values.append((connection_position, "account_data", room_id))
            value_values.append(
                (have_sent_room.status.value, have_sent_room.last_token)
            )

        self.db_pool.simple_upsert_many_txn(
            txn,
            table="sliding_sync_connection_streams",
            key_names=(
                "connection_position",
                "stream",
                "room_id",
            ),
            key_values=key_values,
            value_names=(
                "room_status",
                "last_token",
            ),
            value_values=value_values,
        )

        # ... and upsert changes to the room configs.
        keys = []
        values = []
        for room_id, room_config in per_connection_state.room_configs.items():
            keys.append((connection_position, room_id))
            values.append((room_config.timeline_limit, room_to_state_ids[room_id]))

        self.db_pool.simple_upsert_many_txn(
            txn,
            table="sliding_sync_connection_room_configs",
            key_names=(
                "connection_position",
                "room_id",
            ),
            key_values=keys,
            value_names=(
                "timeline_limit",
                "required_state_id",
            ),
            value_values=values,
        )

        self._persist_sliding_sync_connection_lazy_members_txn(
            txn,
            connection_key,
            connection_position,
            per_connection_state.room_lazy_membership,
        )

        return connection_position

    @cached(iterable=True, max_entries=100000)
    async def get_and_clear_connection_positions(
        self, user_id: str, device_id: str, conn_id: str, connection_position: int
    ) -> "PerConnectionState":
        """Get the per-connection state for the given connection position."""

        per_connection_state_db = await self.db_pool.runInteraction(
            "get_and_clear_connection_positions",
            self._get_and_clear_connection_positions_txn,
            user_id=user_id,
            device_id=device_id,
            conn_id=conn_id,
            connection_position=connection_position,
        )

        # This cast is safe because the downstream code only cares about
        # `store.get_id_for_instance(...)` and `StreamWorkerStore` is mixed
        # alongside `SlidingSyncStore` wherever we create a store.
        store = cast("DataStore", self)

        return await per_connection_state_db.to_state(store)

    def _get_and_clear_connection_positions_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_id: str,
        conn_id: str,
        connection_position: int,
    ) -> "PerConnectionStateDB":
        # The `previous_connection_position` is a user-supplied value, so we
        # need to make sure that the one they supplied is actually theirs.
        sql = """
            SELECT connection_key, last_used_ts
            FROM sliding_sync_connection_positions
            INNER JOIN sliding_sync_connections USING (connection_key)
            WHERE
                connection_position = ?
                AND user_id = ? AND effective_device_id = ? AND conn_id = ?
        """
        txn.execute(sql, (connection_position, user_id, device_id, conn_id))
        row = txn.fetchone()
        if row is None:
            raise SlidingSyncUnknownPosition()

        (connection_key, last_used_ts) = row

        # Update the `last_used_ts` if it's due to be updated. We don't update
        # every time to avoid excessive writes.
        now = self.clock.time_msec()
        if (
            last_used_ts is None
            or now - last_used_ts > UPDATE_INTERVAL_LAST_USED_TS.as_millis()
        ):
            self.db_pool.simple_update_txn(
                txn,
                table="sliding_sync_connections",
                keyvalues={
                    "connection_key": connection_key,
                },
                updatevalues={"last_used_ts": now},
            )

        # Now that we have seen the client has received and used the connection
        # position, we can delete all the other connection positions.
        sql = """
            DELETE FROM sliding_sync_connection_positions
            WHERE connection_key = ? AND connection_position != ?
        """
        txn.execute(sql, (connection_key, connection_position))

        # Move any lazy membership entries for this connection position to have
        # `NULL` connection position, indicating that it applies to all future
        # positions on this connection. This is safe because we have deleted all
        # other (potentially forked) connection positions, and so all future
        # positions in this connection will be a continuation of the current
        # position. Thus any lazy membership entries we have sent down will still
        # be valid.
        self.db_pool.simple_update_txn(
            txn,
            table="sliding_sync_connection_lazy_members",
            keyvalues={
                "connection_key": connection_key,
                "connection_position": connection_position,
            },
            updatevalues={"connection_position": None},
        )

        # Fetch and create a mapping from required state ID to the actual
        # required state for the connection.
        rows = self.db_pool.simple_select_list_txn(
            txn,
            table="sliding_sync_connection_required_state",
            keyvalues={"connection_key": connection_key},
            retcols=(
                "required_state_id",
                "required_state",
            ),
        )

        required_state_map: dict[int, dict[str, set[str]]] = {}
        for row in rows:
            state = required_state_map[row[0]] = {}
            for event_type, state_key in db_to_json(row[1]):
                state.setdefault(event_type, set()).add(state_key)

        # Get all the room configs, looking up the required state from the map
        # above.
        room_config_rows = self.db_pool.simple_select_list_txn(
            txn,
            table="sliding_sync_connection_room_configs",
            keyvalues={"connection_position": connection_position},
            retcols=(
                "room_id",
                "timeline_limit",
                "required_state_id",
            ),
        )

        room_configs: dict[str, RoomSyncConfig] = {}
        for (
            room_id,
            timeline_limit,
            required_state_id,
        ) in room_config_rows:
            room_configs[room_id] = RoomSyncConfig(
                timeline_limit=timeline_limit,
                required_state_map=required_state_map[required_state_id],
            )

        # Now look up the per-room stream data.
        rooms: dict[str, HaveSentRoom[str]] = {}
        receipts: dict[str, HaveSentRoom[str]] = {}
        account_data: dict[str, HaveSentRoom[str]] = {}

        receipt_rows = self.db_pool.simple_select_list_txn(
            txn,
            table="sliding_sync_connection_streams",
            keyvalues={"connection_position": connection_position},
            retcols=(
                "stream",
                "room_id",
                "room_status",
                "last_token",
            ),
        )
        for stream, room_id, room_status, last_token in receipt_rows:
            have_sent_room: HaveSentRoom[str] = HaveSentRoom(
                status=HaveSentRoomFlag(room_status), last_token=last_token
            )
            if stream == "rooms":
                rooms[room_id] = have_sent_room
            elif stream == "receipts":
                receipts[room_id] = have_sent_room
            elif stream == "account_data":
                account_data[room_id] = have_sent_room
            else:
                # For forwards compatibility we ignore unknown streams, as in
                # future we want to be able to easily add more stream types.
                logger.warning("Unrecognized sliding sync stream in DB %r", stream)

        return PerConnectionStateDB(
            last_used_ts=last_used_ts,
            rooms=RoomStatusMap(rooms),
            receipts=RoomStatusMap(receipts),
            account_data=RoomStatusMap(account_data),
            room_configs=room_configs,
            room_lazy_membership={},
        )

    async def get_sliding_sync_connection_lazy_members(
        self,
        connection_position: int,
        room_id: str,
        user_ids: AbstractSet[str],
    ) -> Mapping[str, int]:
        """Get which user IDs in the room we have previously sent lazy
        membership for.

        Args:
            connection_position: The sliding sync connection position.
            room_id: The room ID to get lazy members for.
            user_ids: The user IDs to check whether we've previously sent
                because of lazy membership.

        Returns:
            The mapping of user IDs to the last seen timestamp for those user
            IDs. Only includes user IDs that we have previously sent lazy
            membership for, and so may be a subset of the `user_ids` passed in.
        """

        def get_sliding_sync_connection_lazy_members_txn(
            txn: LoggingTransaction,
        ) -> Mapping[str, int]:
            user_clause, user_args = make_in_list_sql_clause(
                txn.database_engine, "user_id", user_ids
            )

            # Fetch all the lazy membership entries for the given connection,
            # room and user IDs. We don't have the `connection_key` here, so we
            # join against `sliding_sync_connection_positions` to get it.
            #
            # Beware that there are two `connection_position` columns in the
            # query which are different, the one in
            # `sliding_sync_connection_positions` is the one we match to get the
            # connection_key, whereas the one in
            # `sliding_sync_connection_lazy_members` is what we filter against
            # (it may be null or the same as the one passed in).
            #
            # FIXME: We should pass in `connection_key` here to avoid the join.
            # We don't do this currently as the caller doesn't have it handy.
            sql = f"""
                SELECT user_id, members.connection_position, last_seen_ts
                FROM sliding_sync_connection_lazy_members AS members
                INNER JOIN sliding_sync_connection_positions AS pos USING (connection_key)
                WHERE pos.connection_position = ? AND room_id = ? AND {user_clause}
            """

            txn.execute(sql, (connection_position, room_id, *user_args))

            # Filter out any cache entries that only apply to forked connection
            # positions. Entries with `NULL` `connection_position` apply to all
            # positions on the connection.
            return {
                user_id: last_seen_ts
                for user_id, db_connection_position, last_seen_ts in txn
                if db_connection_position == connection_position
                or db_connection_position is None
            }

        return await self.db_pool.runInteraction(
            "get_sliding_sync_connection_lazy_members",
            get_sliding_sync_connection_lazy_members_txn,
            db_autocommit=True,  # Avoid transaction for single read
        )

    def _persist_sliding_sync_connection_lazy_members_txn(
        self,
        txn: LoggingTransaction,
        connection_key: int,
        new_connection_position: int,
        all_changes: dict[str, RoomLazyMembershipChanges],
    ) -> None:
        """Persist that we have sent lazy membership for the given user IDs."""

        now = self.clock.time_msec()

        # Figure out which cache entries to add or update.
        #
        # These are either a) new entries we've never sent before (i.e. with a
        # None last_seen_ts), or b) where the `last_seen_ts` is old enough that
        # we want to update it.
        #
        # We don't update the timestamp every time to avoid hammering the DB
        # with writes, and we don't need the timestamp to be precise. It is used
        # to evict old entries that haven't been used in a while.
        to_update: list[tuple[str, str]] = []
        for room_id, room_changes in all_changes.items():
            user_ids_to_update = room_changes.get_returned_user_ids_to_update(
                self.clock
            )
            to_update.extend((room_id, user_id) for user_id in user_ids_to_update)

        if to_update:
            # Upsert the new/updated entries.
            #
            # Ignore conflicts where the existing entry has a different
            # connection position (i.e. from a forked connection position). This
            # may mean that we lose some updates, but that's acceptable as this
            # is a cache and its fine for it to *not* include rows. (Downstream
            # this will cause us to maybe send a few extra lazy members down
            # sync, but we're allowed to send extra members).
            sql = """
                INSERT INTO sliding_sync_connection_lazy_members
                (connection_key, connection_position, room_id, user_id, last_seen_ts)
                VALUES {value_placeholder}
                ON CONFLICT (connection_key, room_id, user_id)
                DO UPDATE SET last_seen_ts = EXCLUDED.last_seen_ts
                WHERE sliding_sync_connection_lazy_members.connection_position IS NULL
                    OR sliding_sync_connection_lazy_members.connection_position = EXCLUDED.connection_position
            """

            args = [
                (connection_key, new_connection_position, room_id, user_id, now)
                for room_id, user_id in to_update
            ]

            if isinstance(self.database_engine, PostgresEngine):
                sql = sql.format(value_placeholder="?")
                txn.execute_values(sql, args, fetch=False)
            else:
                sql = sql.format(value_placeholder="(?, ?, ?, ?, ?)")
                txn.execute_batch(sql, args)

        # Remove any invalidated entries.
        to_remove: list[tuple[str, str]] = []
        for room_id, room_changes in all_changes.items():
            for user_id in room_changes.invalidated_user_ids:
                to_remove.append((room_id, user_id))

        if to_remove:
            # We don't try and match on connection position here: it's fine to
            # remove it from all forks. This is a cache so it's fine to expire
            # arbitrary entries, the worst that happens is we send a few extra
            # lazy members down sync.
            self.db_pool.simple_delete_many_batch_txn(
                txn,
                table="sliding_sync_connection_lazy_members",
                keys=("connection_key", "room_id", "user_id"),
                values=[
                    (connection_key, room_id, user_id) for room_id, user_id in to_remove
                ],
            )

    @wrap_as_background_process("delete_old_sliding_sync_connections")
    async def delete_old_sliding_sync_connections(self) -> None:
        """Delete sliding sync connections that have not been used for a long time."""
        cutoff_ts = self.clock.time_msec() - CONNECTION_EXPIRY.as_millis()

        def delete_old_sliding_sync_connections_txn(txn: LoggingTransaction) -> None:
            sql = """
                DELETE FROM sliding_sync_connections
                WHERE last_used_ts IS NOT NULL AND last_used_ts < ?
            """
            txn.execute(sql, (cutoff_ts,))

        await self.db_pool.runInteraction(
            "delete_old_sliding_sync_connections",
            delete_old_sliding_sync_connections_txn,
        )


@attr.s(auto_attribs=True, frozen=True)
class PerConnectionStateDB:
    """An equivalent to `PerConnectionState` that holds data in a format stored
    in the DB.

    The principal difference is that the tokens for the different streams are
    serialized to strings.

    When persisting this *only* contains updates to the state.
    """

    last_used_ts: int | None

    rooms: "RoomStatusMap[str]"
    receipts: "RoomStatusMap[str]"
    account_data: "RoomStatusMap[str]"

    room_configs: Mapping[str, "RoomSyncConfig"]

    room_lazy_membership: dict[str, RoomLazyMembershipChanges]
    """Lazy membership changes to persist alongside this state. Only used
    when persisting."""

    @staticmethod
    async def from_state(
        per_connection_state: "MutablePerConnectionState", store: "DataStore"
    ) -> "PerConnectionStateDB":
        """Convert from a standard `PerConnectionState`"""
        rooms = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    await status.last_token.to_string(store)
                    if status.last_token is not None
                    else None
                ),
            )
            for room_id, status in per_connection_state.rooms.get_updates().items()
        }

        receipts = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    await status.last_token.to_string(store)
                    if status.last_token is not None
                    else None
                ),
            )
            for room_id, status in per_connection_state.receipts.get_updates().items()
        }

        account_data = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    str(status.last_token) if status.last_token is not None else None
                ),
            )
            for room_id, status in per_connection_state.account_data.get_updates().items()
        }

        log_kv(
            {
                "rooms": rooms,
                "receipts": receipts,
                "account_data": account_data,
                "room_configs": per_connection_state.room_configs.maps[0],
            }
        )

        return PerConnectionStateDB(
            last_used_ts=per_connection_state.last_used_ts,
            rooms=RoomStatusMap(rooms),
            receipts=RoomStatusMap(receipts),
            account_data=RoomStatusMap(account_data),
            room_configs=per_connection_state.room_configs.maps[0],
            room_lazy_membership=per_connection_state.room_lazy_membership,
        )

    async def to_state(self, store: "DataStore") -> "PerConnectionState":
        """Convert into a standard `PerConnectionState`"""
        rooms = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    await RoomStreamToken.parse(store, status.last_token)
                    if status.last_token is not None
                    else None
                ),
            )
            for room_id, status in self.rooms._statuses.items()
        }

        receipts = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    await MultiWriterStreamToken.parse(store, status.last_token)
                    if status.last_token is not None
                    else None
                ),
            )
            for room_id, status in self.receipts._statuses.items()
        }

        account_data = {
            room_id: HaveSentRoom(
                status=status.status,
                last_token=(
                    int(status.last_token) if status.last_token is not None else None
                ),
            )
            for room_id, status in self.account_data._statuses.items()
        }

        return PerConnectionState(
            last_used_ts=self.last_used_ts,
            rooms=RoomStatusMap(rooms),
            receipts=RoomStatusMap(receipts),
            account_data=RoomStatusMap(account_data),
            room_configs=self.room_configs,
        )
