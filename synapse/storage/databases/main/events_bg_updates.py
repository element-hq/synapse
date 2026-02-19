#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2022 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, cast

import attr

from synapse.api.constants import (
    MAX_DEPTH,
    EventContentFields,
    Membership,
    RelationTypes,
)
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase, make_event_from_dict
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_comparison_clause,
)
from synapse.storage.databases.main.events import (
    SLIDING_SYNC_RELEVANT_STATE_SET,
    PersistEventsStore,
    SlidingSyncMembershipInfoWithEventPos,
    SlidingSyncMembershipSnapshotSharedInsertValues,
    SlidingSyncStateInsertValues,
)
from synapse.storage.databases.main.events_worker import (
    DatabaseCorruptionError,
    InvalidEventError,
)
from synapse.storage.databases.main.state_deltas import StateDeltasStore
from synapse.storage.databases.main.stream import StreamWorkerStore
from synapse.storage.engines import PostgresEngine
from synapse.storage.types import Cursor
from synapse.types import JsonDict, RoomStreamToken, StateMap, StrCollection
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.types.state import StateFilter
from synapse.types.storage import _BackgroundUpdates
from synapse.util.iterutils import batch_iter
from synapse.util.json import json_encoder

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


_REPLACE_STREAM_ORDERING_SQL_COMMANDS = (
    # there should be no leftover rows without a stream_ordering2, but just in case...
    "UPDATE events SET stream_ordering2 = stream_ordering WHERE stream_ordering2 IS NULL",
    # now we can drop the rule and switch the columns
    "DROP RULE populate_stream_ordering2 ON events",
    "ALTER TABLE events DROP COLUMN stream_ordering",
    "ALTER TABLE events RENAME COLUMN stream_ordering2 TO stream_ordering",
    # ... and finally, rename the indexes into place for consistency with sqlite
    "ALTER INDEX event_contains_url_index2 RENAME TO event_contains_url_index",
    "ALTER INDEX events_order_room2 RENAME TO events_order_room",
    "ALTER INDEX events_room_stream2 RENAME TO events_room_stream",
    "ALTER INDEX events_ts2 RENAME TO events_ts",
)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _CalculateChainCover:
    """Return value for _calculate_chain_cover_txn."""

    # The last room_id/depth/stream processed.
    room_id: str
    depth: int
    stream: int

    # Number of rows processed
    processed_count: int

    # Map from room_id to last depth/stream processed for each room that we have
    # processed all events for (i.e. the rooms we can flip the
    # `has_auth_chain_index` for)
    finished_room_map: dict[str, tuple[int, int]]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _JoinedRoomStreamOrderingUpdate:
    """
    Intermediate container class used in `SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE`
    """

    # The most recent event stream_ordering for the room
    most_recent_event_stream_ordering: int
    # The most recent event `bump_stamp` for the room
    most_recent_bump_stamp: int | None


class EventsBackgroundUpdatesStore(StreamWorkerStore, StateDeltasStore, SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME,
            self._background_reindex_origin_server_ts,
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME,
            self._background_reindex_fields_sender,
        )

        self.db_pool.updates.register_background_index_update(
            "event_contains_url_index",
            index_name="event_contains_url_index",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering"],
            where_clause="contains_url = true AND outlier = false",
        )

        # an event_id index on event_search is useful for the purge_history
        # api. Plus it means we get to enforce some integrity with a UNIQUE
        # clause
        self.db_pool.updates.register_background_index_update(
            "event_search_event_id_idx",
            index_name="event_search_event_id_idx",
            table="event_search",
            columns=["event_id"],
            unique=True,
            psql_only=True,
        )

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.DELETE_SOFT_FAILED_EXTREMITIES,
            self._cleanup_extremities_bg_update,
        )

        self.db_pool.updates.register_background_update_handler(
            "redactions_received_ts", self._redactions_received_ts
        )

        # This index gets deleted in `event_fix_redactions_bytes` update
        self.db_pool.updates.register_background_index_update(
            "event_fix_redactions_bytes_create_index",
            index_name="redactions_censored_redacts",
            table="redactions",
            columns=["redacts"],
            where_clause="have_censored",
        )

        self.db_pool.updates.register_background_update_handler(
            "event_fix_redactions_bytes", self._event_fix_redactions_bytes
        )

        self.db_pool.updates.register_background_update_handler(
            "event_store_labels", self._event_store_labels
        )

        self.db_pool.updates.register_background_index_update(
            "redactions_have_censored_ts_idx",
            index_name="redactions_have_censored_ts",
            table="redactions",
            columns=["received_ts"],
            where_clause="NOT have_censored",
        )

        self.db_pool.updates.register_background_index_update(
            "users_have_local_media",
            index_name="users_have_local_media",
            table="local_media_repository",
            columns=["user_id", "created_ts"],
        )

        self.db_pool.updates.register_background_update_handler(
            "rejected_events_metadata",
            self._rejected_events_metadata,
        )

        self.db_pool.updates.register_background_update_handler(
            "chain_cover",
            self._chain_cover_index,
        )

        self.db_pool.updates.register_background_update_handler(
            "purged_chain_cover",
            self._purged_chain_cover_index,
        )

        self.db_pool.updates.register_background_update_handler(
            "event_arbitrary_relations",
            self._event_arbitrary_relations,
        )

        ################################################################################

        # bg updates for replacing stream_ordering with a BIGINT
        # (these only run on postgres.)

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.POPULATE_STREAM_ORDERING2,
            self._background_populate_stream_ordering2,
        )
        # CREATE UNIQUE INDEX events_stream_ordering ON events(stream_ordering2);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2,
            index_name="events_stream_ordering",
            table="events",
            columns=["stream_ordering2"],
            unique=True,
        )
        # CREATE INDEX event_contains_url_index ON events(room_id, topological_ordering, stream_ordering) WHERE contains_url = true AND outlier = false;
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_CONTAINS_URL,
            index_name="event_contains_url_index2",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering2"],
            where_clause="contains_url = true AND outlier = false",
        )
        # CREATE INDEX events_order_room ON events(room_id, topological_ordering, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_ROOM_ORDER,
            index_name="events_order_room2",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering2"],
        )
        # CREATE INDEX events_room_stream ON events(room_id, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_ROOM_STREAM,
            index_name="events_room_stream2",
            table="events",
            columns=["room_id", "stream_ordering2"],
        )
        # CREATE INDEX events_ts ON events(origin_server_ts, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_TS,
            index_name="events_ts2",
            table="events",
            columns=["origin_server_ts", "stream_ordering2"],
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.REPLACE_STREAM_ORDERING_COLUMN,
            self._background_replace_stream_ordering_column,
        )

        ################################################################################

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS,
            self._background_drop_invalid_event_edges_rows,
        )

        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.EVENT_EDGES_REPLACE_INDEX,
            index_name="event_edges_event_id_prev_event_id_idx",
            table="event_edges",
            columns=["event_id", "prev_event_id"],
            unique=True,
            # the old index which just covered event_id is now redundant.
            replaces_index="ev_edges_id",
        )

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS,
            self._background_events_populate_state_key_rejections,
        )

        # Add an index that would be useful for jumping to date using
        # get_event_id_for_timestamp.
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.EVENTS_JUMP_TO_DATE_INDEX,
            index_name="events_jump_to_date_idx",
            table="events",
            columns=["room_id", "origin_server_ts"],
            where_clause="NOT outlier",
        )

        # These indices are needed to validate the foreign key constraint
        # when events are deleted.
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.CURRENT_STATE_EVENTS_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="current_state_events_stream_ordering_idx",
            table="current_state_events",
            columns=["event_stream_ordering"],
        )
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.ROOM_MEMBERSHIPS_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="room_memberships_stream_ordering_idx",
            table="room_memberships",
            columns=["event_stream_ordering"],
        )
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.LOCAL_CURRENT_MEMBERSHIP_STREAM_ORDERING_INDEX_UPDATE_NAME,
            index_name="local_current_membership_stream_ordering_idx",
            table="local_current_membership",
            columns=["event_stream_ordering"],
        )

        # Handle background updates for Sliding Sync tables
        #
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE,
            self._sliding_sync_prefill_joined_rooms_to_recalculate_table_bg_update,
        )
        # Add some background updates to populate the sliding sync tables
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE,
            self._sliding_sync_joined_rooms_bg_update,
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
            self._sliding_sync_membership_snapshots_bg_update,
        )
        # Add a background update to fix data integrity issue in the
        # `sliding_sync_membership_snapshots` -> `forgotten` column
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_FIX_FORGOTTEN_COLUMN_BG_UPDATE,
            self._sliding_sync_membership_snapshots_fix_forgotten_column_bg_update,
        )

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.FIXUP_MAX_DEPTH_CAP, self.fixup_max_depth_cap_bg_update
        )

        # We want this to run on the main database at startup before we start processing
        # events.
        #
        # FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
        # foreground update for
        # `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
        # https://github.com/element-hq/synapse/issues/17623)
        with db_conn.cursor(txn_name="resolve_sliding_sync") as txn:
            _resolve_stale_data_in_sliding_sync_tables(
                txn=txn,
            )

    async def _background_reindex_fields_sender(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        def reindex_txn(txn: LoggingTransaction) -> int:
            sql = (
                "SELECT stream_ordering, event_id, json FROM events"
                " INNER JOIN event_json USING (event_id)"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]

            update_rows = []
            for row in rows:
                try:
                    event_id = row[1]
                    event_json = db_to_json(row[2])
                    sender = event_json["sender"]
                    content = event_json["content"]

                    contains_url = "url" in content
                    if contains_url:
                        contains_url &= isinstance(content["url"], str)
                except (KeyError, AttributeError):
                    # If the event is missing a necessary field then
                    # skip over it.
                    continue

                update_rows.append((sender, contains_url, event_id))

            sql = "UPDATE events SET sender = ?, contains_url = ? WHERE event_id = ?"

            txn.execute_batch(sql, update_rows)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows),
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, progress
            )

            return len(rows)

        result = await self.db_pool.runInteraction(
            _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, reindex_txn
        )

        if not result:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME
            )

        return result

    async def _background_reindex_origin_server_ts(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        def reindex_search_txn(txn: LoggingTransaction) -> int:
            sql = (
                "SELECT stream_ordering, event_id FROM events"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]
            event_ids = [row[1] for row in rows]

            rows_to_update = []

            chunks = [event_ids[i : i + 100] for i in range(0, len(event_ids), 100)]
            for chunk in chunks:
                ev_rows = cast(
                    list[tuple[str, str]],
                    self.db_pool.simple_select_many_txn(
                        txn,
                        table="event_json",
                        column="event_id",
                        iterable=chunk,
                        retcols=["event_id", "json"],
                        keyvalues={},
                    ),
                )

                for event_id, json in ev_rows:
                    event_json = db_to_json(json)
                    try:
                        origin_server_ts = event_json["origin_server_ts"]
                    except (KeyError, AttributeError):
                        # If the event is missing a necessary field then
                        # skip over it.
                        continue

                    rows_to_update.append((origin_server_ts, event_id))

            sql = "UPDATE events SET origin_server_ts = ? WHERE event_id = ?"

            txn.execute_batch(sql, rows_to_update)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows_to_update),
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME, progress
            )

            return len(rows_to_update)

        result = await self.db_pool.runInteraction(
            _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME, reindex_search_txn
        )

        if not result:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME
            )

        return result

    async def _cleanup_extremities_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Background update to clean out extremities that should have been
        deleted previously.

        Mainly used to deal with the aftermath of https://github.com/matrix-org/synapse/issues/5269.
        """

        # This works by first copying all existing forward extremities into the
        # `_extremities_to_check` table at start up, and then checking each
        # event in that table whether we have any descendants that are not
        # soft-failed/rejected. If that is the case then we delete that event
        # from the forward extremities table.
        #
        # For efficiency, we do this in batches by recursively pulling out all
        # descendants of a batch until we find the non soft-failed/rejected
        # events, i.e. the set of descendants whose chain of prev events back
        # to the batch of extremities are all soft-failed or rejected.
        # Typically, we won't find any such events as extremities will rarely
        # have any descendants, but if they do then we should delete those
        # extremities.

        def _cleanup_extremities_bg_update_txn(txn: LoggingTransaction) -> int:
            # The set of extremity event IDs that we're checking this round
            original_set = set()

            # A dict[str, set[str]] of event ID to their prev events.
            graph: dict[str, set[str]] = {}

            # The set of descendants of the original set that are not rejected
            # nor soft-failed. Ancestors of these events should be removed
            # from the forward extremities table.
            non_rejected_leaves = set()

            # Set of event IDs that have been soft failed, and for which we
            # should check if they have descendants which haven't been soft
            # failed.
            soft_failed_events_to_lookup = set()

            # First, we get `batch_size` events from the table, pulling out
            # their successor events, if any, and the successor events'
            # rejection status.
            txn.execute(
                """SELECT prev_event_id, event_id, internal_metadata,
                    rejections.event_id IS NOT NULL, events.outlier
                FROM (
                    SELECT event_id AS prev_event_id
                    FROM _extremities_to_check
                    LIMIT ?
                ) AS f
                LEFT JOIN event_edges USING (prev_event_id)
                LEFT JOIN events USING (event_id)
                LEFT JOIN event_json USING (event_id)
                LEFT JOIN rejections USING (event_id)
                """,
                (batch_size,),
            )

            for prev_event_id, event_id, metadata, rejected, outlier in txn:
                original_set.add(prev_event_id)

                if not event_id or outlier:
                    # Common case where the forward extremity doesn't have any
                    # descendants.
                    continue

                graph.setdefault(event_id, set()).add(prev_event_id)

                soft_failed = False
                if metadata:
                    soft_failed = db_to_json(metadata).get("soft_failed")

                if soft_failed or rejected:
                    soft_failed_events_to_lookup.add(event_id)
                else:
                    non_rejected_leaves.add(event_id)

            # Now we recursively check all the soft-failed descendants we
            # found above in the same way, until we have nothing left to
            # check.
            while soft_failed_events_to_lookup:
                # We only want to do 100 at a time, so we split given list
                # into two.
                batch = list(soft_failed_events_to_lookup)
                to_check, to_defer = batch[:100], batch[100:]
                soft_failed_events_to_lookup = set(to_defer)

                sql = """SELECT prev_event_id, event_id, internal_metadata,
                    rejections.event_id IS NOT NULL
                    FROM event_edges
                    INNER JOIN events USING (event_id)
                    INNER JOIN event_json USING (event_id)
                    LEFT JOIN rejections USING (event_id)
                    WHERE
                        NOT events.outlier
                        AND
                """
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "prev_event_id", to_check
                )
                txn.execute(sql + clause, list(args))

                for prev_event_id, event_id, metadata, rejected in txn:
                    if event_id in graph:
                        # Already handled this event previously, but we still
                        # want to record the edge.
                        graph[event_id].add(prev_event_id)
                        continue

                    graph[event_id] = {prev_event_id}

                    soft_failed = db_to_json(metadata).get("soft_failed")
                    if soft_failed or rejected:
                        soft_failed_events_to_lookup.add(event_id)
                    else:
                        non_rejected_leaves.add(event_id)

            # We have a set of non-soft-failed descendants, so we recurse up
            # the graph to find all ancestors and add them to the set of event
            # IDs that we can delete from forward extremities table.
            to_delete = set()
            while non_rejected_leaves:
                event_id = non_rejected_leaves.pop()
                prev_event_ids = graph.get(event_id, set())
                non_rejected_leaves.update(prev_event_ids)
                to_delete.update(prev_event_ids)

            to_delete.intersection_update(original_set)

            deleted = self.db_pool.simple_delete_many_txn(
                txn=txn,
                table="event_forward_extremities",
                column="event_id",
                values=to_delete,
                keyvalues={},
            )

            logger.info(
                "Deleted %d forward extremities of %d checked, to clean up matrix-org/synapse#5269",
                deleted,
                len(original_set),
            )

            if deleted:
                # We now need to invalidate the caches of these rooms
                rows = cast(
                    list[tuple[str]],
                    self.db_pool.simple_select_many_txn(
                        txn,
                        table="events",
                        column="event_id",
                        iterable=to_delete,
                        keyvalues={},
                        retcols=("room_id",),
                    ),
                )
                room_ids = {row[0] for row in rows}
                for room_id in room_ids:
                    txn.call_after(
                        self.get_latest_event_ids_in_room.invalidate,  # type: ignore[attr-defined]
                        (room_id,),
                    )

            self.db_pool.simple_delete_many_txn(
                txn=txn,
                table="_extremities_to_check",
                column="event_id",
                values=original_set,
                keyvalues={},
            )

            return len(original_set)

        num_handled = await self.db_pool.runInteraction(
            "_cleanup_extremities_bg_update", _cleanup_extremities_bg_update_txn
        )

        if not num_handled:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.DELETE_SOFT_FAILED_EXTREMITIES
            )

            def _drop_table_txn(txn: LoggingTransaction) -> None:
                txn.execute("DROP TABLE _extremities_to_check")

            await self.db_pool.runInteraction(
                "_cleanup_extremities_bg_update_drop_table", _drop_table_txn
            )

        return num_handled

    async def _redactions_received_ts(self, progress: JsonDict, batch_size: int) -> int:
        """Handles filling out the `received_ts` column in redactions."""
        last_event_id = progress.get("last_event_id", "")

        def _redactions_received_ts_txn(txn: LoggingTransaction) -> int:
            # Fetch the set of event IDs that we want to update
            sql = """
                SELECT event_id FROM redactions
                WHERE event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
            """

            txn.execute(sql, (last_event_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            (upper_event_id,) = rows[-1]

            # Update the redactions with the received_ts.
            #
            # Note: Not all events have an associated received_ts, so we
            # fallback to using origin_server_ts. If we for some reason don't
            # have an origin_server_ts, lets just use the current timestamp.
            #
            # We don't want to leave it null, as then we'll never try and
            # censor those redactions.
            sql = """
                UPDATE redactions
                SET received_ts = (
                    SELECT COALESCE(received_ts, origin_server_ts, ?) FROM events
                    WHERE events.event_id = redactions.event_id
                )
                WHERE ? <= event_id AND event_id <= ?
            """

            txn.execute(sql, (self.clock.time_msec(), last_event_id, upper_event_id))

            self.db_pool.updates._background_update_progress_txn(
                txn, "redactions_received_ts", {"last_event_id": upper_event_id}
            )

            return len(rows)

        count = await self.db_pool.runInteraction(
            "_redactions_received_ts", _redactions_received_ts_txn
        )

        if not count:
            await self.db_pool.updates._end_background_update("redactions_received_ts")

        return count

    async def _event_fix_redactions_bytes(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Undoes hex encoded censored redacted event JSON."""

        def _event_fix_redactions_bytes_txn(txn: LoggingTransaction) -> None:
            # This update is quite fast due to new index.
            txn.execute(
                """
                UPDATE event_json
                SET
                    json = convert_from(json::bytea, 'utf8')
                FROM redactions
                WHERE
                    redactions.have_censored
                    AND event_json.event_id = redactions.redacts
                    AND json NOT LIKE '{%';
                """
            )

            txn.execute("DROP INDEX redactions_censored_redacts")

        await self.db_pool.runInteraction(
            "_event_fix_redactions_bytes", _event_fix_redactions_bytes_txn
        )

        await self.db_pool.updates._end_background_update("event_fix_redactions_bytes")

        return 1

    async def _event_store_labels(self, progress: JsonDict, batch_size: int) -> int:
        """Background update handler which will store labels for existing events."""
        last_event_id = progress.get("last_event_id", "")

        def _event_store_labels_txn(txn: LoggingTransaction) -> int:
            txn.execute(
                """
                SELECT event_id, json FROM event_json
                LEFT JOIN event_labels USING (event_id)
                WHERE event_id > ? AND label IS NULL
                ORDER BY event_id LIMIT ?
                """,
                (last_event_id, batch_size),
            )

            results = list(txn)

            nbrows = 0
            last_row_event_id = ""
            for event_id, event_json_raw in results:
                try:
                    event_json = db_to_json(event_json_raw)

                    self.db_pool.simple_insert_many_txn(
                        txn=txn,
                        table="event_labels",
                        keys=("event_id", "label", "room_id", "topological_ordering"),
                        values=[
                            (
                                event_id,
                                label,
                                event_json["room_id"],
                                event_json["depth"],
                            )
                            for label in event_json["content"].get(
                                EventContentFields.LABELS, []
                            )
                            if isinstance(label, str)
                        ],
                    )
                except Exception as e:
                    logger.warning(
                        "Unable to load event %s (no labels will be imported): %s",
                        event_id,
                        e,
                    )

                nbrows += 1
                last_row_event_id = event_id

            self.db_pool.updates._background_update_progress_txn(
                txn, "event_store_labels", {"last_event_id": last_row_event_id}
            )

            return nbrows

        num_rows = await self.db_pool.runInteraction(
            desc="event_store_labels", func=_event_store_labels_txn
        )

        if not num_rows:
            await self.db_pool.updates._end_background_update("event_store_labels")

        return num_rows

    async def _rejected_events_metadata(self, progress: dict, batch_size: int) -> int:
        """Adds rejected events to the `state_events` and `event_auth` metadata
        tables.
        """

        last_event_id = progress.get("last_event_id", "")

        def get_rejected_events(
            txn: Cursor,
        ) -> list[tuple[str, str, JsonDict, bool, bool]]:
            # Fetch rejected event json, their room version and whether we have
            # inserted them into the state_events or auth_events tables.
            #
            # Note we can assume that events that don't have a corresponding
            # room version are V1 rooms.
            sql = """
                SELECT DISTINCT
                    event_id,
                    COALESCE(room_version, '1'),
                    json,
                    state_events.event_id IS NOT NULL,
                    event_auth.event_id IS NOT NULL
                FROM rejections
                INNER JOIN event_json USING (event_id)
                LEFT JOIN rooms USING (room_id)
                LEFT JOIN state_events USING (event_id)
                LEFT JOIN event_auth USING (event_id)
                WHERE event_id > ?
                ORDER BY event_id
                LIMIT ?
            """

            txn.execute(
                sql,
                (
                    last_event_id,
                    batch_size,
                ),
            )

            return cast(
                list[tuple[str, str, JsonDict, bool, bool]],
                [(row[0], row[1], db_to_json(row[2]), row[3], row[4]) for row in txn],
            )

        results = await self.db_pool.runInteraction(
            desc="_rejected_events_metadata_get", func=get_rejected_events
        )

        if not results:
            await self.db_pool.updates._end_background_update(
                "rejected_events_metadata"
            )
            return 0

        state_events = []
        auth_events = []
        for event_id, room_version, event_json, has_state, has_event_auth in results:
            last_event_id = event_id

            if has_state and has_event_auth:
                continue

            room_version_obj = KNOWN_ROOM_VERSIONS.get(room_version)
            if not room_version_obj:
                # We no longer support this room version, so we just ignore the
                # events entirely.
                logger.info(
                    "Ignoring event with unknown room version %r: %r",
                    room_version,
                    event_id,
                )
                continue

            event = make_event_from_dict(event_json, room_version_obj)

            if not event.is_state():
                continue

            if not has_state:
                state_events.append(
                    (event.event_id, event.room_id, event.type, event.state_key)
                )

            if not has_event_auth:
                # Old, dodgy, events may have duplicate auth events, which we
                # need to deduplicate as we have a unique constraint.
                for auth_id in set(event.auth_event_ids()):
                    auth_events.append((event.event_id, event.room_id, auth_id))

        if state_events:
            await self.db_pool.simple_insert_many(
                table="state_events",
                keys=("event_id", "room_id", "type", "state_key"),
                values=state_events,
                desc="_rejected_events_metadata_state_events",
            )

        if auth_events:
            await self.db_pool.simple_insert_many(
                table="event_auth",
                keys=("event_id", "room_id", "auth_id"),
                values=auth_events,
                desc="_rejected_events_metadata_event_auth",
            )

        await self.db_pool.updates._background_update_progress(
            "rejected_events_metadata", {"last_event_id": last_event_id}
        )

        if len(results) < batch_size:
            await self.db_pool.updates._end_background_update(
                "rejected_events_metadata"
            )

        return len(results)

    async def _chain_cover_index(self, progress: dict, batch_size: int) -> int:
        """A background updates that iterates over all rooms and generates the
        chain cover index for them.
        """

        current_room_id = progress.get("current_room_id", "")

        # Where we've processed up to in the room, defaults to the start of the
        # room.
        last_depth = progress.get("last_depth", -1)
        last_stream = progress.get("last_stream", -1)

        result = await self.db_pool.runInteraction(
            "_chain_cover_index",
            self._calculate_chain_cover_txn,
            current_room_id,
            last_depth,
            last_stream,
            batch_size,
            single_room=False,
        )

        finished = result.processed_count == 0

        total_rows_processed = result.processed_count
        current_room_id = result.room_id
        last_depth = result.depth
        last_stream = result.stream

        for room_id, (depth, stream) in result.finished_room_map.items():
            # If we've done all the events in the room we flip the
            # `has_auth_chain_index` in the DB. Note that its possible for
            # further events to be persisted between the above and setting the
            # flag without having the chain cover calculated for them. This is
            # fine as a) the code gracefully handles these cases and b) we'll
            # calculate them below.

            await self.db_pool.simple_update(
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"has_auth_chain_index": True},
                desc="_chain_cover_index",
            )

            # Handle any events that might have raced with us flipping the
            # bit above.
            result = await self.db_pool.runInteraction(
                "_chain_cover_index",
                self._calculate_chain_cover_txn,
                room_id,
                depth,
                stream,
                batch_size=None,
                single_room=True,
            )

            total_rows_processed += result.processed_count

        if finished:
            await self.db_pool.updates._end_background_update("chain_cover")
            return total_rows_processed

        await self.db_pool.updates._background_update_progress(
            "chain_cover",
            {
                "current_room_id": current_room_id,
                "last_depth": last_depth,
                "last_stream": last_stream,
            },
        )

        return total_rows_processed

    def _calculate_chain_cover_txn(
        self,
        txn: LoggingTransaction,
        last_room_id: str,
        last_depth: int,
        last_stream: int,
        batch_size: int | None,
        single_room: bool,
    ) -> _CalculateChainCover:
        """Calculate the chain cover for `batch_size` events, ordered by
        `(room_id, depth, stream)`.

        Args:
            txn,
            last_room_id, last_depth, last_stream: The `(room_id, depth, stream)`
                tuple to fetch results after.
            batch_size: The maximum number of events to process. If None then
                no limit.
            single_room: Whether to calculate the index for just the given
                room.
        """

        # Get the next set of events in the room (that we haven't already
        # computed chain cover for). We do this in topological order.

        # We want to do a `(topological_ordering, stream_ordering) > (?,?)`
        # comparison, but that is not supported on older SQLite versions
        tuple_clause, tuple_args = make_tuple_comparison_clause(
            [
                ("events.room_id", last_room_id),
                ("topological_ordering", last_depth),
                ("stream_ordering", last_stream),
            ],
        )

        extra_clause = ""
        if single_room:
            extra_clause = "AND events.room_id = ?"
            tuple_args.append(last_room_id)

        sql = """
            SELECT
                event_id, state_events.type, state_events.state_key,
                topological_ordering, stream_ordering,
                events.room_id
            FROM events
            INNER JOIN state_events USING (event_id)
            LEFT JOIN event_auth_chains USING (event_id)
            LEFT JOIN event_auth_chain_to_calculate USING (event_id)
            WHERE event_auth_chains.event_id IS NULL
                AND event_auth_chain_to_calculate.event_id IS NULL
                AND %(tuple_cmp)s
                %(extra)s
            ORDER BY events.room_id, topological_ordering, stream_ordering
            %(limit)s
        """ % {
            "tuple_cmp": tuple_clause,
            "limit": "LIMIT ?" if batch_size is not None else "",
            "extra": extra_clause,
        }

        if batch_size is not None:
            tuple_args.append(batch_size)

        txn.execute(sql, tuple_args)
        rows = txn.fetchall()

        # Put the results in the necessary format for
        # `_add_chain_cover_index`
        event_to_room_id = {row[0]: row[5] for row in rows}
        event_to_types = {row[0]: (row[1], row[2]) for row in rows}

        # Calculate the new last position we've processed up to.
        new_last_depth: int = rows[-1][3] if rows else last_depth
        new_last_stream: int = rows[-1][4] if rows else last_stream
        new_last_room_id: str = rows[-1][5] if rows else ""

        # Map from room_id to last depth/stream_ordering processed for the room,
        # excluding the last room (which we're likely still processing). We also
        # need to include the room passed in if it's not included in the result
        # set (as we then know we've processed all events in said room).
        #
        # This is the set of rooms that we can now safely flip the
        # `has_auth_chain_index` bit for.
        finished_rooms = {
            row[5]: (row[3], row[4]) for row in rows if row[5] != new_last_room_id
        }
        if last_room_id not in finished_rooms and last_room_id != new_last_room_id:
            finished_rooms[last_room_id] = (last_depth, last_stream)

        count = len(rows)

        # We also need to fetch the auth events for them.
        auth_events = cast(
            list[tuple[str, str]],
            self.db_pool.simple_select_many_txn(
                txn,
                table="event_auth",
                column="event_id",
                iterable=event_to_room_id,
                keyvalues={},
                retcols=("event_id", "auth_id"),
            ),
        )

        event_to_auth_chain: dict[str, list[str]] = {}
        for event_id, auth_id in auth_events:
            event_to_auth_chain.setdefault(event_id, []).append(auth_id)

        # Calculate and persist the chain cover index for this set of events.
        #
        # Annoyingly we need to gut wrench into the persit event store so that
        # we can reuse the function to calculate the chain cover for rooms.
        PersistEventsStore._add_chain_cover_index(
            txn,
            self.db_pool,
            self.event_chain_id_gen,
            event_to_room_id,
            event_to_types,
            cast(dict[str, StrCollection], event_to_auth_chain),
        )

        return _CalculateChainCover(
            room_id=new_last_room_id,
            depth=new_last_depth,
            stream=new_last_stream,
            processed_count=count,
            finished_room_map=finished_rooms,
        )

    async def _purged_chain_cover_index(self, progress: dict, batch_size: int) -> int:
        """
        A background updates that iterates over the chain cover and deletes the
        chain cover for events that have been purged.

        This may be due to fully purging a room or via setting a retention policy.
        """
        current_event_id = progress.get("current_event_id", "")

        def purged_chain_cover_txn(txn: LoggingTransaction) -> int:
            # The event ID from events will be null if the chain ID / sequence
            # number points to a purged event.
            sql = """
                SELECT event_id, chain_id, sequence_number, e.event_id IS NOT NULL
                FROM event_auth_chains
                LEFT JOIN events AS e USING (event_id)
                WHERE event_id > ? ORDER BY event_auth_chains.event_id ASC LIMIT ?
            """
            txn.execute(sql, (current_event_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            # The event IDs and chain IDs / sequence numbers where the event has
            # been purged.
            unreferenced_event_ids = []
            unreferenced_chain_id_tuples = []
            event_id = ""
            for event_id, chain_id, sequence_number, has_event in rows:
                if not has_event:
                    unreferenced_event_ids.append((event_id,))
                    unreferenced_chain_id_tuples.append((chain_id, sequence_number))

            # Delete the unreferenced auth chains from event_auth_chain_links and
            # event_auth_chains.
            txn.executemany(
                """
                DELETE FROM event_auth_chains WHERE event_id = ?
                """,
                unreferenced_event_ids,
            )
            # We should also delete matching target_*, but there is no index on
            # target_chain_id. Hopefully any purged events are due to a room
            # being fully purged and they will be removed from the origin_*
            # searches.
            txn.executemany(
                """
                DELETE FROM event_auth_chain_links WHERE
                origin_chain_id = ? AND origin_sequence_number = ?
                """,
                unreferenced_chain_id_tuples,
            )

            progress = {
                "current_event_id": event_id,
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, "purged_chain_cover", progress
            )

            return len(rows)

        result = await self.db_pool.runInteraction(
            "_purged_chain_cover_index",
            purged_chain_cover_txn,
        )

        if not result:
            await self.db_pool.updates._end_background_update("purged_chain_cover")

        return result

    async def _event_arbitrary_relations(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Background update handler which will store previously unknown relations for existing events."""
        last_event_id = progress.get("last_event_id", "")

        def _event_arbitrary_relations_txn(txn: LoggingTransaction) -> int:
            # Fetch events and then filter based on whether the event has a
            # relation or not.
            txn.execute(
                """
                SELECT event_id, json FROM event_json
                WHERE event_id > ?
                ORDER BY event_id LIMIT ?
                """,
                (last_event_id, batch_size),
            )

            results = list(txn)
            # (event_id, parent_id, rel_type) for each relation
            relations_to_insert: list[tuple[str, str, str, str]] = []
            for event_id, event_json_raw in results:
                try:
                    event_json = db_to_json(event_json_raw)
                except Exception as e:
                    logger.warning(
                        "Unable to load event %s (no relations will be updated): %s",
                        event_id,
                        e,
                    )
                    continue

                # If there's no relation, skip!
                relates_to = event_json["content"].get("m.relates_to")
                if not relates_to or not isinstance(relates_to, dict):
                    continue

                # If the relation type or parent event ID is not a string, skip it.
                #
                # Do not consider relation types that have existed for a long time,
                # since they will already be listed in the `event_relations` table.
                rel_type = relates_to.get("rel_type")
                if not isinstance(rel_type, str) or rel_type in (
                    RelationTypes.ANNOTATION,
                    RelationTypes.REFERENCE,
                    RelationTypes.REPLACE,
                ):
                    continue

                parent_id = relates_to.get("event_id")
                if not isinstance(parent_id, str):
                    continue

                room_id = event_json["room_id"]
                relations_to_insert.append((room_id, event_id, parent_id, rel_type))

            # Insert the missing data, note that we upsert here in case the event
            # has already been processed.
            if relations_to_insert:
                self.db_pool.simple_upsert_many_txn(
                    txn=txn,
                    table="event_relations",
                    key_names=("event_id",),
                    key_values=[(r[1],) for r in relations_to_insert],
                    value_names=("relates_to_id", "relation_type"),
                    value_values=[r[2:] for r in relations_to_insert],
                )

                # Iterate the parent IDs and invalidate caches.
                self._invalidate_cache_and_stream_bulk(  # type: ignore[attr-defined]
                    txn,
                    self.get_relations_for_event,  # type: ignore[attr-defined]
                    {
                        (
                            r[0],  # room_id
                            r[2],  # parent_id
                        )
                        for r in relations_to_insert
                    },
                )
                self._invalidate_cache_and_stream_bulk(  # type: ignore[attr-defined]
                    txn,
                    self.get_thread_summary,  # type: ignore[attr-defined]
                    {(r[1],) for r in relations_to_insert},
                )

            if results:
                latest_event_id = results[-1][0]
                self.db_pool.updates._background_update_progress_txn(
                    txn, "event_arbitrary_relations", {"last_event_id": latest_event_id}
                )

            return len(results)

        num_rows = await self.db_pool.runInteraction(
            desc="event_arbitrary_relations", func=_event_arbitrary_relations_txn
        )

        if not num_rows:
            await self.db_pool.updates._end_background_update(
                "event_arbitrary_relations"
            )

        return num_rows

    async def _background_populate_stream_ordering2(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Populate events.stream_ordering2, then replace stream_ordering

        This is to deal with the fact that stream_ordering was initially created as a
        32-bit integer field.
        """
        batch_size = max(batch_size, 1)

        def process(txn: LoggingTransaction) -> int:
            last_stream = progress.get("last_stream", -(1 << 31))
            txn.execute(
                """
                UPDATE events SET stream_ordering2=stream_ordering
                WHERE stream_ordering IN (
                   SELECT stream_ordering FROM events WHERE stream_ordering > ?
                   ORDER BY stream_ordering LIMIT ?
                )
                RETURNING stream_ordering;
                """,
                (last_stream, batch_size),
            )
            row_count = txn.rowcount
            if row_count == 0:
                return 0
            last_stream = max(row[0] for row in txn)
            logger.info("populated stream_ordering2 up to %i", last_stream)

            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.POPULATE_STREAM_ORDERING2,
                {"last_stream": last_stream},
            )
            return row_count

        result = await self.db_pool.runInteraction(
            "_background_populate_stream_ordering2", process
        )

        if result != 0:
            return result

        await self.db_pool.updates._end_background_update(
            _BackgroundUpdates.POPULATE_STREAM_ORDERING2
        )
        return 0

    async def _background_replace_stream_ordering_column(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Drop the old 'stream_ordering' column and rename 'stream_ordering2' into its place."""

        def process(txn: Cursor) -> None:
            for sql in _REPLACE_STREAM_ORDERING_SQL_COMMANDS:
                logger.info("completing stream_ordering migration: %s", sql)
                txn.execute(sql)

        # ANALYZE the new column to build stats on it, to encourage PostgreSQL to use the
        # indexes on it.
        await self.db_pool.runInteraction(
            "background_analyze_new_stream_ordering_column",
            lambda txn: txn.execute("ANALYZE events(stream_ordering2)"),
        )

        await self.db_pool.runInteraction(
            "_background_replace_stream_ordering_column", process
        )

        await self.db_pool.updates._end_background_update(
            _BackgroundUpdates.REPLACE_STREAM_ORDERING_COLUMN
        )

        return 0

    async def _background_drop_invalid_event_edges_rows(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Drop invalid rows from event_edges

        This only runs for postgres. For SQLite, it all happens synchronously.

        Firstly, drop any rows with is_state=True. These may have been added a long time
        ago, but they are no longer used.

        We also drop rows that do not correspond to entries in `events`, and add a
        foreign key.
        """

        last_event_id = progress.get("last_event_id", "")

        def drop_invalid_event_edges_txn(txn: LoggingTransaction) -> bool:
            """Returns True if we're done."""

            # first we need to find an endpoint.
            txn.execute(
                """
                SELECT event_id FROM event_edges
                WHERE event_id > ?
                ORDER BY event_id
                LIMIT 1 OFFSET ?
                """,
                (last_event_id, batch_size),
            )

            endpoint = None
            row = txn.fetchone()

            if row:
                endpoint = row[0]

            where_clause = "ee.event_id > ?"
            args = [last_event_id]
            if endpoint:
                where_clause += " AND ee.event_id <= ?"
                args.append(endpoint)

            # now delete any that:
            #   - have is_state=TRUE, or
            #   - do not correspond to a row in `events`
            txn.execute(
                f"""
                DELETE FROM event_edges
                WHERE event_id IN (
                   SELECT ee.event_id
                   FROM event_edges ee
                     LEFT JOIN events ev USING (event_id)
                   WHERE ({where_clause}) AND
                     (is_state OR ev.event_id IS NULL)
                )""",
                args,
            )

            logger.info(
                "cleaned up event_edges up to %s: removed %i/%i rows",
                endpoint,
                txn.rowcount,
                batch_size,
            )

            if endpoint is not None:
                self.db_pool.updates._background_update_progress_txn(
                    txn,
                    _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS,
                    {"last_event_id": endpoint},
                )
                return False

            # if that was the final batch, we validate the foreign key.
            #
            # The constraint should have been in place and enforced for new rows since
            # before we started deleting invalid rows, so there's no chance for any
            # invalid rows to have snuck in the meantime. In other words, this really
            # ought to succeed.
            logger.info("cleaned up event_edges; enabling foreign key")
            txn.execute(
                "ALTER TABLE event_edges VALIDATE CONSTRAINT event_edges_event_id_fkey"
            )
            return True

        done = await self.db_pool.runInteraction(
            desc="drop_invalid_event_edges", func=drop_invalid_event_edges_txn
        )

        if done:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS
            )

        return batch_size

    async def _background_events_populate_state_key_rejections(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Back-populate `events.state_key` and `events.rejection_reason"""

        min_stream_ordering_exclusive = progress["min_stream_ordering_exclusive"]
        max_stream_ordering_inclusive = progress["max_stream_ordering_inclusive"]

        def _populate_txn(txn: LoggingTransaction) -> bool:
            """Returns True if we're done."""

            # first we need to find an endpoint.
            # we need to find the final row in the batch of batch_size, which means
            # we need to skip over (batch_size-1) rows and get the next row.
            txn.execute(
                """
                SELECT stream_ordering FROM events
                WHERE stream_ordering > ? AND stream_ordering <= ?
                ORDER BY stream_ordering
                LIMIT 1 OFFSET ?
                """,
                (
                    min_stream_ordering_exclusive,
                    max_stream_ordering_inclusive,
                    batch_size - 1,
                ),
            )

            row = txn.fetchone()
            if row:
                endpoint = row[0]
            else:
                # if the query didn't return a row, we must be almost done. We just
                # need to go up to the recorded max_stream_ordering.
                endpoint = max_stream_ordering_inclusive

            where_clause = "stream_ordering > ? AND stream_ordering <= ?"
            args = [min_stream_ordering_exclusive, endpoint]

            # now do the updates.
            txn.execute(
                f"""
                UPDATE events
                SET state_key = (SELECT state_key FROM state_events se WHERE se.event_id = events.event_id),
                    rejection_reason = (SELECT reason FROM rejections rej WHERE rej.event_id = events.event_id)
                WHERE ({where_clause})
                """,
                args,
            )

            logger.info(
                "populated new `events` columns up to %i/%i: updated %i rows",
                endpoint,
                max_stream_ordering_inclusive,
                txn.rowcount,
            )

            if endpoint >= max_stream_ordering_inclusive:
                # we're done
                return True

            progress["min_stream_ordering_exclusive"] = endpoint
            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS,
                progress,
            )
            return False

        done = await self.db_pool.runInteraction(
            desc="events_populate_state_key_rejections", func=_populate_txn
        )

        if done:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS
            )

        return batch_size

    async def _sliding_sync_prefill_joined_rooms_to_recalculate_table_bg_update(
        self, progress: JsonDict, _batch_size: int
    ) -> int:
        """
        Prefill `sliding_sync_joined_rooms_to_recalculate` table with all rooms we know about already.
        """

        def _txn(txn: LoggingTransaction) -> None:
            # We do this as one big bulk insert. This has been tested on a bigger
            # homeserver with ~10M rooms and took 60s. There is potential for this to
            # starve disk usage while this goes on.
            #
            # We upsert in case we have to run this multiple times.
            txn.execute(
                """
                INSERT INTO sliding_sync_joined_rooms_to_recalculate
                    (room_id)
                SELECT DISTINCT room_id FROM local_current_membership
                WHERE membership = 'join'
                ON CONFLICT (room_id)
                DO NOTHING;
                """,
            )

        await self.db_pool.runInteraction(
            "_sliding_sync_prefill_joined_rooms_to_recalculate_table_bg_update",
            _txn,
        )

        # Background update is done.
        await self.db_pool.updates._end_background_update(
            _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE
        )
        return 0

    async def _sliding_sync_joined_rooms_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Background update to populate the `sliding_sync_joined_rooms` table.
        """
        # We don't need to fetch any progress state because we just grab the next N
        # events in `sliding_sync_joined_rooms_to_recalculate`

        def _get_rooms_to_update_txn(txn: LoggingTransaction) -> list[tuple[str]]:
            """
            Returns:
                A list of room ID's to update along with the progress value
                (event_stream_ordering) indicating the continuation point in the
                `current_state_events` table for the next batch.
            """
            # Fetch the set of room IDs that we want to update
            #
            # We use `current_state_events` table as the barometer for whether the
            # server is still participating in the room because if we're
            # `no_longer_in_room`, this table would be cleared out for the given
            # `room_id`.
            txn.execute(
                """
                SELECT room_id
                FROM sliding_sync_joined_rooms_to_recalculate
                LIMIT ?
                """,
                (batch_size,),
            )

            rooms_to_update_rows = cast(list[tuple[str]], txn.fetchall())

            return rooms_to_update_rows

        rooms_to_update = await self.db_pool.runInteraction(
            "_sliding_sync_joined_rooms_bg_update._get_rooms_to_update_txn",
            _get_rooms_to_update_txn,
        )

        if not rooms_to_update:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE
            )
            return 0

        # Map from room_id to insert/update state values in the `sliding_sync_joined_rooms` table.
        joined_room_updates: dict[str, SlidingSyncStateInsertValues] = {}
        # Map from room_id to stream_ordering/bump_stamp, etc values
        joined_room_stream_ordering_updates: dict[
            str, _JoinedRoomStreamOrderingUpdate
        ] = {}
        # As long as we get this value before we fetch the current state, we can use it
        # to check if something has changed since that point.
        most_recent_current_state_delta_stream_id = (
            await self.get_max_stream_id_in_current_state_deltas()
        )
        for (room_id,) in rooms_to_update:
            current_state_ids_map = await self.db_pool.runInteraction(
                "_sliding_sync_joined_rooms_bg_update._get_relevant_sliding_sync_current_state_event_ids_txn",
                PersistEventsStore._get_relevant_sliding_sync_current_state_event_ids_txn,
                room_id,
            )

            # If we're not joined to the room a) it doesn't belong in the
            # `sliding_sync_joined_rooms` table so we should skip and b) we won't have
            # any `current_state_events` for the room.
            if not current_state_ids_map:
                continue

            try:
                fetched_events = await self.get_events(current_state_ids_map.values())
            except (DatabaseCorruptionError, InvalidEventError) as e:
                logger.warning(
                    "Failed to fetch state for room '%s' due to corrupted events. Ignoring. Error: %s",
                    room_id,
                    e,
                )
                continue

            current_state_map: StateMap[EventBase] = {
                state_key: fetched_events[event_id]
                for state_key, event_id in current_state_ids_map.items()
                # `get_events(...)` will filter out events for unknown room versions
                if event_id in fetched_events
            }

            # Even if we are joined to the room, this can happen for unknown room
            # versions (old room versions that aren't known anymore) since
            # `get_events(...)` will filter out events for unknown room versions
            if not current_state_map:
                continue

            state_insert_values = (
                PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                    current_state_map
                )
            )
            # We should have some insert values for each room, even if they are `None`
            assert state_insert_values
            joined_room_updates[room_id] = state_insert_values

            # Figure out the stream_ordering of the latest event in the room
            most_recent_event_pos_results = await self.get_last_event_pos_in_room(
                room_id, event_types=None
            )
            assert most_recent_event_pos_results is not None, (
                f"We should not be seeing `None` here because the room ({room_id}) should at-least have a create event "
                + "given we pulled the room out of `current_state_events`"
            )
            most_recent_event_stream_ordering = most_recent_event_pos_results[1].stream

            # The `most_recent_event_stream_ordering` should be positive,
            # however there are (very rare) rooms where that is not the case in
            # the matrix.org database. It's not clear how they got into that
            # state, but does mean that we cannot assert that the stream
            # ordering is indeed positive.

            # Figure out the latest `bump_stamp` in the room. This could be `None` for a
            # federated room you just joined where all of events are still `outliers` or
            # backfilled history. In the Sliding Sync API, we default to the user's
            # membership event `stream_ordering` if we don't have a `bump_stamp` so
            # having it as `None` in this table is fine.
            bump_stamp_event_pos_results = await self.get_last_event_pos_in_room(
                room_id, event_types=SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
            )
            most_recent_bump_stamp = None
            if (
                bump_stamp_event_pos_results is not None
                and bump_stamp_event_pos_results[1].stream > 0
            ):
                most_recent_bump_stamp = bump_stamp_event_pos_results[1].stream

            joined_room_stream_ordering_updates[room_id] = (
                _JoinedRoomStreamOrderingUpdate(
                    most_recent_event_stream_ordering=most_recent_event_stream_ordering,
                    most_recent_bump_stamp=most_recent_bump_stamp,
                )
            )

        def _fill_table_txn(txn: LoggingTransaction) -> None:
            # Handle updating the `sliding_sync_joined_rooms` table
            #
            for (
                room_id,
                update_map,
            ) in joined_room_updates.items():
                joined_room_stream_ordering_update = (
                    joined_room_stream_ordering_updates[room_id]
                )
                event_stream_ordering = (
                    joined_room_stream_ordering_update.most_recent_event_stream_ordering
                )
                bump_stamp = joined_room_stream_ordering_update.most_recent_bump_stamp

                # Check if the current state has been updated since we gathered it.
                # We're being careful not to insert/overwrite with stale data.
                state_deltas_since_we_gathered_current_state = (
                    self.get_current_state_deltas_for_room_txn(
                        txn,
                        room_id,
                        from_token=RoomStreamToken(
                            stream=most_recent_current_state_delta_stream_id
                        ),
                        to_token=None,
                    )
                )
                for state_delta in state_deltas_since_we_gathered_current_state:
                    # We only need to check for the state is relevant to the
                    # `sliding_sync_joined_rooms` table.
                    if (
                        state_delta.event_type,
                        state_delta.state_key,
                    ) in SLIDING_SYNC_RELEVANT_STATE_SET:
                        # Raising exception so we can just exit and try again. It would
                        # be hard to resolve this within the transaction because we need
                        # to get full events out that take redactions into account. We
                        # could add some retry logic here, but it's easier to just let
                        # the background update try again.
                        raise Exception(
                            "Current state was updated after we gathered it to update "
                            + "`sliding_sync_joined_rooms` in the background update. "
                            + "Raising exception so we can just try again."
                        )

                # Since we fully insert rows into `sliding_sync_joined_rooms`, we can
                # just do everything on insert and `ON CONFLICT DO NOTHING`.
                #
                self.db_pool.simple_upsert_txn(
                    txn,
                    table="sliding_sync_joined_rooms",
                    keyvalues={"room_id": room_id},
                    values={},
                    insertion_values={
                        **update_map,
                        # The reason we're only *inserting* (not *updating*) `event_stream_ordering`
                        # and `bump_stamp` is because if they are present, that means they are already
                        # up-to-date.
                        "event_stream_ordering": event_stream_ordering,
                        "bump_stamp": bump_stamp,
                    },
                )

            # Now that we've processed all the room, we can remove them from the
            # queue.
            #
            # Note: we need to remove all the rooms from the queue we pulled out
            # from the DB, not just the ones we've processed above. Otherwise
            # we'll simply keep pulling out the same rooms over and over again.
            self.db_pool.simple_delete_many_batch_txn(
                txn,
                table="sliding_sync_joined_rooms_to_recalculate",
                keys=("room_id",),
                values=rooms_to_update,
            )

        await self.db_pool.runInteraction(
            "sliding_sync_joined_rooms_bg_update", _fill_table_txn
        )

        return len(rooms_to_update)

    async def _sliding_sync_membership_snapshots_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Background update to populate the `sliding_sync_membership_snapshots` table.
        """
        # We do this in two phases: a) the initial phase where we go through all
        # room memberships, and then b) a second phase where we look at new
        # memberships (this is to handle the case where we downgrade and then
        # upgrade again).
        #
        # We have to do this as two phases (rather than just the second phase
        # where we iterate on event_stream_ordering), as the
        # `event_stream_ordering` column may have null values for old rows.
        # Therefore we first do the set of historic rooms and *then* look at any
        # new rows (which will have a non-null `event_stream_ordering`).
        initial_phase = progress.get("initial_phase")
        if initial_phase is None:
            # If this is the first run, store the current max stream position.
            # We know we will go through all memberships less than the current
            # max in the initial phase.
            progress = {
                "initial_phase": True,
                "last_event_stream_ordering": self.get_room_max_stream_ordering(),
            }
            await self.db_pool.updates._background_update_progress(
                _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                progress,
            )
            initial_phase = True

        last_room_id = progress.get("last_room_id", "")
        last_user_id = progress.get("last_user_id", "")
        last_event_stream_ordering = progress["last_event_stream_ordering"]

        def _find_memberships_to_update_txn(
            txn: LoggingTransaction,
        ) -> list[
            tuple[
                str,
                str | None,
                str | None,
                str,
                str,
                str,
                str,
                int,
                str | None,
                bool,
            ]
        ]:
            # Fetch the set of event IDs that we want to update
            #
            # We skip over rows which we've already handled, i.e. have a
            # matching row in `sliding_sync_membership_snapshots` with the same
            # room, user and event ID.
            #
            # We also ignore rooms that the user has left themselves (i.e. not
            # kicked). This is to avoid having to port lots of old rooms that we
            # will never send down sliding sync (as we exclude such rooms from
            # initial syncs).

            if initial_phase:
                # There are some old out-of-band memberships (before
                # https://github.com/matrix-org/synapse/issues/6983) where we don't have
                # the corresponding room stored in the `rooms` table`. We use `LEFT JOIN
                # rooms AS r USING (room_id)` to find the rooms missing from `rooms` and
                # insert a row for them below.
                txn.execute(
                    """
                    SELECT
                        c.room_id,
                        r.room_id,
                        r.room_version,
                        c.user_id,
                        e.sender,
                        c.event_id,
                        c.membership,
                        e.stream_ordering,
                        e.instance_name,
                        e.outlier
                    FROM local_current_membership AS c
                    LEFT JOIN sliding_sync_membership_snapshots AS m USING (room_id, user_id)
                    INNER JOIN events AS e USING (event_id)
                    LEFT JOIN rooms AS r ON (c.room_id = r.room_id)
                    WHERE (c.room_id, c.user_id) > (?, ?)
                        AND (m.user_id IS NULL OR c.event_id != m.membership_event_id)
                    ORDER BY c.room_id ASC, c.user_id ASC
                    LIMIT ?
                    """,
                    (last_room_id, last_user_id, batch_size),
                )
            elif last_event_stream_ordering is not None:
                # It's important to sort by `event_stream_ordering` *ascending* (oldest to
                # newest) so that if we see that this background update in progress and want
                # to start the catch-up process, we can safely assume that it will
                # eventually get to the rooms we want to catch-up on anyway (see
                # `_resolve_stale_data_in_sliding_sync_tables()`).
                #
                # `c.room_id` is duplicated to make it match what we're doing in the
                # `initial_phase`. But we can avoid doing the extra `rooms` table join
                # because we can assume all of these new events won't have this problem.
                txn.execute(
                    """
                    SELECT
                        c.room_id,
                        r.room_id,
                        r.room_version,
                        c.user_id,
                        e.sender,
                        c.event_id,
                        c.membership,
                        c.event_stream_ordering,
                        e.instance_name,
                        e.outlier
                    FROM local_current_membership AS c
                    LEFT JOIN sliding_sync_membership_snapshots AS m USING (room_id, user_id)
                    INNER JOIN events AS e USING (event_id)
                    LEFT JOIN rooms AS r ON (c.room_id = r.room_id)
                    WHERE c.event_stream_ordering > ?
                        AND (m.user_id IS NULL OR c.event_id != m.membership_event_id)
                    ORDER BY c.event_stream_ordering ASC
                    LIMIT ?
                    """,
                    (last_event_stream_ordering, batch_size),
                )
            else:
                raise Exception("last_event_stream_ordering should not be None")

            memberships_to_update_rows = cast(
                list[
                    tuple[
                        str,
                        str | None,
                        str | None,
                        str,
                        str,
                        str,
                        str,
                        int,
                        str | None,
                        bool,
                    ]
                ],
                txn.fetchall(),
            )

            return memberships_to_update_rows

        memberships_to_update_rows = await self.db_pool.runInteraction(
            "sliding_sync_membership_snapshots_bg_update._find_memberships_to_update_txn",
            _find_memberships_to_update_txn,
        )

        if not memberships_to_update_rows:
            if initial_phase:
                # Move onto the next phase.
                await self.db_pool.updates._background_update_progress(
                    _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
                    {
                        "initial_phase": False,
                        "last_event_stream_ordering": last_event_stream_ordering,
                    },
                )
                return 0
            else:
                # We've finished both phases, we're done.
                await self.db_pool.updates._end_background_update(
                    _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE
                )
                return 0

        def _find_previous_invite_or_knock_membership_txn(
            txn: LoggingTransaction, room_id: str, user_id: str, event_id: str
        ) -> tuple[str, str] | None:
            # Find the previous invite/knock event before the leave event
            #
            # Here are some notes on how we landed on this query:
            #
            # We're using `topological_ordering` instead of `stream_ordering` because
            # somehow it's possible to have your `leave` event backfilled with a
            # negative `stream_ordering` and your previous `invite` event with a
            # positive `stream_ordering` so we wouldn't have a chance of finding the
            # previous membership with a naive `event_stream_ordering < ?` comparison.
            #
            # Also be careful because `room_memberships.event_stream_ordering` is
            # nullable and not always filled in. You would need to join on `events` to
            # rely on `events.stream_ordering` instead. Even though the
            # `events.stream_ordering` also doesn't have a `NOT NULL` constraint, it
            # doesn't have any rows where this is the case (checked on `matrix.org`).
            # The fact the `events.stream_ordering` is a nullable column is a holdover
            # from a rename of the column.
            #
            # You might also consider using the `event_auth` table to find the previous
            # membership, but there are cases where somehow a membership event doesn't
            # point back to the previous membership event in the auth events (unknown
            # cause).
            txn.execute(
                """
                SELECT event_id, membership
                FROM room_memberships AS m
                INNER JOIN events AS e USING (room_id, event_id)
                WHERE
                    room_id = ?
                    AND m.user_id = ?
                    AND (m.membership = ? OR m.membership = ?)
                    AND e.event_id != ?
                ORDER BY e.topological_ordering DESC
                LIMIT 1
                """,
                (
                    room_id,
                    user_id,
                    # We look explicitly for `invite` and `knock` events instead of
                    # just their previous membership as someone could have been `invite`
                    # -> `ban` -> unbanned (`leave`) and we want to find the `invite`
                    # event where the stripped state is.
                    Membership.INVITE,
                    Membership.KNOCK,
                    event_id,
                ),
            )
            row = txn.fetchone()

            if row is None:
                # Generally we should have an invite or knock event for leaves
                # that are outliers, however this may not always be the case
                # (e.g. a local user got kicked but the kick event got pulled in
                # as an outlier).
                return None

            event_id, membership = row

            return event_id, membership

        # Map from (room_id, user_id) to ...
        to_insert_membership_snapshots: dict[
            tuple[str, str], SlidingSyncMembershipSnapshotSharedInsertValues
        ] = {}
        to_insert_membership_infos: dict[
            tuple[str, str], SlidingSyncMembershipInfoWithEventPos
        ] = {}
        for (
            room_id,
            room_id_from_rooms_table,
            room_version_id,
            user_id,
            sender,
            membership_event_id,
            membership,
            membership_event_stream_ordering,
            membership_event_instance_name,
            is_outlier,
        ) in memberships_to_update_rows:
            # We don't know how to handle `membership` values other than these. The
            # code below would need to be updated.
            assert membership in (
                Membership.JOIN,
                Membership.INVITE,
                Membership.KNOCK,
                Membership.LEAVE,
                Membership.BAN,
            )

            if (
                room_version_id is not None
                and room_version_id not in KNOWN_ROOM_VERSIONS
            ):
                # Ignore rooms with unknown room versions (these were
                # experimental rooms, that we no longer support).
                continue

            # There are some old out-of-band memberships (before
            # https://github.com/matrix-org/synapse/issues/6983) where we don't have the
            # corresponding room stored in the `rooms` table`. We have a `FOREIGN KEY`
            # constraint on the `sliding_sync_membership_snapshots` table so we have to
            # fix-up these memberships by adding the room to the `rooms` table.
            if room_id_from_rooms_table is None:
                await self.db_pool.simple_insert(
                    table="rooms",
                    values={
                        "room_id": room_id,
                        # Only out-of-band memberships are missing from the `rooms`
                        # table so that is the only type of membership we're dealing
                        # with here. Since we don't calculate the "chain cover" for
                        # out-of-band memberships, we can just set this to `True` as if
                        # the user ever joins the room, we will end up calculating the
                        # "chain cover" anyway.
                        "has_auth_chain_index": True,
                    },
                )

            # Map of values to insert/update in the `sliding_sync_membership_snapshots` table
            sliding_sync_membership_snapshots_insert_map: SlidingSyncMembershipSnapshotSharedInsertValues = {}
            if membership == Membership.JOIN:
                # If we're still joined, we can pull from current state.
                current_state_ids_map: StateMap[
                    str
                ] = await self.hs.get_storage_controllers().state.get_current_state_ids(
                    room_id,
                    state_filter=StateFilter.from_types(
                        SLIDING_SYNC_RELEVANT_STATE_SET
                    ),
                    # Partially-stated rooms should have all state events except for
                    # remote membership events so we don't need to wait at all because
                    # we only want some non-membership state
                    await_full_state=False,
                )
                # We're iterating over rooms that we are joined to so they should
                # have `current_state_events` and we should have some current state
                # for each room
                if current_state_ids_map:
                    try:
                        fetched_events = await self.get_events(
                            current_state_ids_map.values()
                        )
                    except (DatabaseCorruptionError, InvalidEventError) as e:
                        logger.warning(
                            "Failed to fetch state for room '%s' due to corrupted events. Ignoring. Error: %s",
                            room_id,
                            e,
                        )
                        continue

                    current_state_map: StateMap[EventBase] = {
                        state_key: fetched_events[event_id]
                        for state_key, event_id in current_state_ids_map.items()
                        # `get_events(...)` will filter out events for unknown room versions
                        if event_id in fetched_events
                    }

                    # Can happen for unknown room versions (old room versions that aren't known
                    # anymore) since `get_events(...)` will filter out events for unknown room
                    # versions
                    if not current_state_map:
                        continue

                    state_insert_values = PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                        current_state_map
                    )
                    sliding_sync_membership_snapshots_insert_map.update(
                        state_insert_values
                    )
                    # We should have some insert values for each room, even if they are `None`
                    assert sliding_sync_membership_snapshots_insert_map

                    # We have current state to work from
                    sliding_sync_membership_snapshots_insert_map["has_known_state"] = (
                        True
                    )
                else:
                    # Although we expect every room to have a create event (even
                    # past unknown room versions since we haven't supported one
                    # without it), there seem to be some corrupted rooms in
                    # practice that don't have the create event in the
                    # `current_state_events` table. The create event does exist
                    # in the events table though. We'll just say that we don't
                    # know the state for these rooms and continue on with our
                    # day.
                    sliding_sync_membership_snapshots_insert_map = {
                        "has_known_state": False,
                        "room_type": None,
                        "room_name": None,
                        "is_encrypted": False,
                    }
            elif membership in (Membership.INVITE, Membership.KNOCK) or (
                membership in (Membership.LEAVE, Membership.BAN) and is_outlier
            ):
                invite_or_knock_event_id = None
                invite_or_knock_membership = None

                # If the event is an `out_of_band_membership` (special case of
                # `outlier`), we never had historical state so we have to pull from
                # the stripped state on the previous invite/knock event. This gives
                # us a consistent view of the room state regardless of your
                # membership (i.e. the room shouldn't disappear if your using the
                # `is_encrypted` filter and you leave).
                if membership in (Membership.LEAVE, Membership.BAN) and is_outlier:
                    previous_membership = await self.db_pool.runInteraction(
                        "sliding_sync_membership_snapshots_bg_update._find_previous_invite_or_knock_membership_txn",
                        _find_previous_invite_or_knock_membership_txn,
                        room_id,
                        user_id,
                        membership_event_id,
                    )
                    if previous_membership is not None:
                        (
                            invite_or_knock_event_id,
                            invite_or_knock_membership,
                        ) = previous_membership
                else:
                    invite_or_knock_event_id = membership_event_id
                    invite_or_knock_membership = membership

                if (
                    invite_or_knock_event_id is not None
                    and invite_or_knock_membership is not None
                ):
                    # Pull from the stripped state on the invite/knock event
                    invite_or_knock_event = await self.get_event(
                        invite_or_knock_event_id
                    )

                    raw_stripped_state_events = None
                    if invite_or_knock_membership == Membership.INVITE:
                        invite_room_state = invite_or_knock_event.unsigned.get(
                            "invite_room_state"
                        )
                        raw_stripped_state_events = invite_room_state
                    elif invite_or_knock_membership == Membership.KNOCK:
                        knock_room_state = invite_or_knock_event.unsigned.get(
                            "knock_room_state"
                        )
                        raw_stripped_state_events = knock_room_state

                    sliding_sync_membership_snapshots_insert_map = PersistEventsStore._get_sliding_sync_insert_values_from_stripped_state(
                        raw_stripped_state_events
                    )
                else:
                    # We couldn't find any state for the membership, so we just have to
                    # leave it as empty.
                    sliding_sync_membership_snapshots_insert_map = {
                        "has_known_state": False,
                        "room_type": None,
                        "room_name": None,
                        "is_encrypted": False,
                    }

                # We should have some insert values for each room, even if no
                # stripped state is on the event because we still want to record
                # that we have no known state
                assert sliding_sync_membership_snapshots_insert_map
            elif membership in (Membership.LEAVE, Membership.BAN):
                # Pull from historical state
                state_ids_map = await self.hs.get_storage_controllers().state.get_state_ids_for_event(
                    membership_event_id,
                    state_filter=StateFilter.from_types(
                        SLIDING_SYNC_RELEVANT_STATE_SET
                    ),
                    # Partially-stated rooms should have all state events except for
                    # remote membership events so we don't need to wait at all because
                    # we only want some non-membership state
                    await_full_state=False,
                )

                try:
                    fetched_events = await self.get_events(state_ids_map.values())
                except (DatabaseCorruptionError, InvalidEventError) as e:
                    logger.warning(
                        "Failed to fetch state for room '%s' due to corrupted events. Ignoring. Error: %s",
                        room_id,
                        e,
                    )
                    continue

                state_map: StateMap[EventBase] = {
                    state_key: fetched_events[event_id]
                    for state_key, event_id in state_ids_map.items()
                    # `get_events(...)` will filter out events for unknown room versions
                    if event_id in fetched_events
                }

                # Can happen for unknown room versions (old room versions that aren't known
                # anymore) since `get_events(...)` will filter out events for unknown room
                # versions
                if not state_map:
                    continue

                state_insert_values = (
                    PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                        state_map
                    )
                )
                sliding_sync_membership_snapshots_insert_map.update(state_insert_values)
                # We should have some insert values for each room, even if they are `None`
                assert sliding_sync_membership_snapshots_insert_map

                # We have historical state to work from
                sliding_sync_membership_snapshots_insert_map["has_known_state"] = True
            else:
                # We don't know how to handle this type of membership yet
                #
                # FIXME: We should use `assert_never` here but for some reason
                # the exhaustive matching doesn't recognize the `Never` here.
                # assert_never(membership)
                raise AssertionError(
                    f"Unexpected membership {membership} ({membership_event_id}) that we don't know how to handle yet"
                )

            to_insert_membership_snapshots[(room_id, user_id)] = (
                sliding_sync_membership_snapshots_insert_map
            )
            to_insert_membership_infos[(room_id, user_id)] = (
                SlidingSyncMembershipInfoWithEventPos(
                    user_id=user_id,
                    sender=sender,
                    membership_event_id=membership_event_id,
                    membership=membership,
                    membership_event_stream_ordering=membership_event_stream_ordering,
                    # If instance_name is null we default to "master"
                    membership_event_instance_name=membership_event_instance_name
                    or "master",
                )
            )

        def _fill_table_txn(txn: LoggingTransaction) -> None:
            # Handle updating the `sliding_sync_membership_snapshots` table
            #
            for key, insert_map in to_insert_membership_snapshots.items():
                room_id, user_id = key
                membership_info = to_insert_membership_infos[key]
                sender = membership_info.sender
                membership_event_id = membership_info.membership_event_id
                membership = membership_info.membership
                membership_event_stream_ordering = (
                    membership_info.membership_event_stream_ordering
                )
                membership_event_instance_name = (
                    membership_info.membership_event_instance_name
                )

                # We don't need to upsert the state because we never partially
                # insert/update the snapshots and anything already there is up-to-date
                # EXCEPT for the `forgotten` field since that is updated out-of-band
                # from the membership changes.
                #
                # Even though we're only doing insertions, we're using
                # `simple_upsert_txn()` here to avoid unique violation errors that would
                # happen from `simple_insert_txn()`
                self.db_pool.simple_upsert_txn(
                    txn,
                    table="sliding_sync_membership_snapshots",
                    keyvalues={"room_id": room_id, "user_id": user_id},
                    values={},
                    insertion_values={
                        **insert_map,
                        "sender": sender,
                        "membership_event_id": membership_event_id,
                        "membership": membership,
                        "event_stream_ordering": membership_event_stream_ordering,
                        "event_instance_name": membership_event_instance_name,
                    },
                )
                # We need to find the `forgotten` value during the transaction because
                # we can't risk inserting stale data.
                if isinstance(txn.database_engine, PostgresEngine):
                    txn.execute(
                        """
                        UPDATE sliding_sync_membership_snapshots
                        SET
                            forgotten = m.forgotten
                        FROM room_memberships AS m
                        WHERE sliding_sync_membership_snapshots.room_id = ?
                            AND sliding_sync_membership_snapshots.user_id = ?
                            AND membership_event_id = ?
                            AND membership_event_id = m.event_id
                            AND m.event_id IS NOT NULL
                        """,
                        (
                            room_id,
                            user_id,
                            membership_event_id,
                        ),
                    )
                else:
                    # SQLite doesn't support UPDATE FROM before 3.33.0, so we do
                    # this via sub-selects.
                    txn.execute(
                        """
                        UPDATE sliding_sync_membership_snapshots
                        SET
                            forgotten = (SELECT forgotten FROM room_memberships WHERE event_id = ?)
                        WHERE room_id = ? and user_id = ? AND membership_event_id = ?
                        """,
                        (
                            membership_event_id,
                            room_id,
                            user_id,
                            membership_event_id,
                        ),
                    )

        await self.db_pool.runInteraction(
            "sliding_sync_membership_snapshots_bg_update", _fill_table_txn
        )

        # Update the progress
        (
            room_id,
            _room_id_from_rooms_table,
            _room_version_id,
            user_id,
            _sender,
            _membership_event_id,
            _membership,
            membership_event_stream_ordering,
            _membership_event_instance_name,
            _is_outlier,
        ) = memberships_to_update_rows[-1]

        progress = {
            "initial_phase": initial_phase,
            "last_room_id": room_id,
            "last_user_id": user_id,
            "last_event_stream_ordering": last_event_stream_ordering,
        }
        if not initial_phase:
            progress["last_event_stream_ordering"] = membership_event_stream_ordering

        await self.db_pool.updates._background_update_progress(
            _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE,
            progress,
        )

        return len(memberships_to_update_rows)

    async def _sliding_sync_membership_snapshots_fix_forgotten_column_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Background update to update the `sliding_sync_membership_snapshots` ->
        `forgotten` column to be in sync with the `room_memberships` table.

        Because of previously flawed code (now fixed); any room that someone has
        forgotten and subsequently re-joined or had any new membership on, we need to go
        and update the column to match the `room_memberships` table as it has fallen out
        of sync.
        """
        last_event_stream_ordering = progress.get(
            "last_event_stream_ordering", -(1 << 31)
        )

        def _txn(
            txn: LoggingTransaction,
        ) -> int:
            """
            Returns:
                The number of rows updated.
            """

            # To simplify things, we can just recheck any row in
            # `sliding_sync_membership_snapshots` with `forgotten=1`
            txn.execute(
                """
                SELECT
                    s.room_id,
                    s.user_id,
                    s.membership_event_id,
                    s.event_stream_ordering,
                    m.forgotten
                FROM sliding_sync_membership_snapshots AS s
                INNER JOIN room_memberships AS m ON (s.membership_event_id = m.event_id)
                WHERE s.event_stream_ordering > ?
                    AND s.forgotten = 1
                ORDER BY s.event_stream_ordering ASC
                LIMIT ?
                """,
                (last_event_stream_ordering, batch_size),
            )

            memberships_to_update_rows = cast(
                list[tuple[str, str, str, int, int]],
                txn.fetchall(),
            )
            if not memberships_to_update_rows:
                return 0

            # Assemble the values to update
            #
            # (room_id, user_id)
            key_values: list[tuple[str, str]] = []
            # (forgotten,)
            value_values: list[tuple[int]] = []
            for (
                room_id,
                user_id,
                _membership_event_id,
                _event_stream_ordering,
                forgotten,
            ) in memberships_to_update_rows:
                key_values.append(
                    (
                        room_id,
                        user_id,
                    )
                )
                value_values.append((forgotten,))

            # Update all of the rows in one go
            self.db_pool.simple_update_many_txn(
                txn,
                table="sliding_sync_membership_snapshots",
                key_names=("room_id", "user_id"),
                key_values=key_values,
                value_names=("forgotten",),
                value_values=value_values,
            )

            # Update the progress
            (
                _room_id,
                _user_id,
                _membership_event_id,
                event_stream_ordering,
                _forgotten,
            ) = memberships_to_update_rows[-1]
            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_FIX_FORGOTTEN_COLUMN_BG_UPDATE,
                {
                    "last_event_stream_ordering": event_stream_ordering,
                },
            )

            return len(memberships_to_update_rows)

        num_rows = await self.db_pool.runInteraction(
            "_sliding_sync_membership_snapshots_fix_forgotten_column_bg_update",
            _txn,
        )

        if not num_rows:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_FIX_FORGOTTEN_COLUMN_BG_UPDATE
            )

        return num_rows

    async def fixup_max_depth_cap_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Fixes the topological ordering for events that have a depth greater
        than MAX_DEPTH. This should fix /messages ordering oddities."""

        room_id_bound = progress.get("room_id", "")

        def redo_max_depth_bg_update_txn(txn: LoggingTransaction) -> tuple[bool, int]:
            txn.execute(
                """
                SELECT room_id, room_version FROM rooms
                WHERE room_id > ?
                ORDER BY room_id
                LIMIT ?
                """,
                (room_id_bound, batch_size),
            )

            # Find the next room ID to process, with a relevant room version.
            room_ids: list[str] = []
            max_room_id: str | None = None
            for room_id, room_version_str in txn:
                max_room_id = room_id

                # We only want to process rooms with a known room version that
                # has strict canonical json validation enabled.
                room_version = KNOWN_ROOM_VERSIONS.get(room_version_str)
                if room_version and room_version.strict_canonicaljson:
                    room_ids.append(room_id)

            if max_room_id is None:
                # The query did not return any rooms, so we are done.
                return True, 0

            # Update the progress to the last room ID we pulled from the DB,
            # this ensures we always make progress.
            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.FIXUP_MAX_DEPTH_CAP,
                progress={"room_id": max_room_id},
            )

            if not room_ids:
                # There were no rooms in this batch that required the fix.
                return False, 0

            clause, list_args = make_in_list_sql_clause(
                self.database_engine, "room_id", room_ids
            )
            sql = f"""
                UPDATE events SET topological_ordering = ?
                WHERE topological_ordering > ? AND {clause}
            """
            args = [MAX_DEPTH, MAX_DEPTH]
            args.extend(list_args)
            txn.execute(sql, args)

            return False, len(room_ids)

        done, num_rooms = await self.db_pool.runInteraction(
            "redo_max_depth_bg_update", redo_max_depth_bg_update_txn
        )

        if done:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.FIXUP_MAX_DEPTH_CAP
            )

        return num_rooms


def _resolve_stale_data_in_sliding_sync_tables(
    txn: LoggingTransaction,
) -> None:
    """
    Clears stale/out-of-date entries from the
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables.

    This accounts for when someone downgrades their Synapse version and then upgrades it
    again. This will ensure that we don't have any stale/out-of-date data in the
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` tables since any new
    events sent in rooms would have also needed to be written to the sliding sync
    tables. For example a new event needs to bump `event_stream_ordering` in
    `sliding_sync_joined_rooms` table or some state in the room changing (like the room
    name). Or another example of someone's membership changing in a room affecting
    `sliding_sync_membership_snapshots`.

    This way, if a row exists in the sliding sync tables, we are able to rely on it
    (accurate data). And if a row doesn't exist, we use a fallback to get the same info
    until the background updates fill in the rows or a new event comes in triggering it
    to be fully inserted.

    FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
    foreground update for
    `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
    https://github.com/element-hq/synapse/issues/17623)
    """

    _resolve_stale_data_in_sliding_sync_joined_rooms_table(txn)
    _resolve_stale_data_in_sliding_sync_membership_snapshots_table(txn)


def _resolve_stale_data_in_sliding_sync_joined_rooms_table(
    txn: LoggingTransaction,
) -> None:
    """
    Clears stale/out-of-date entries from the `sliding_sync_joined_rooms` table and
    kicks-off the background update to catch-up with what we missed while Synapse was
    downgraded.

    See `_resolve_stale_data_in_sliding_sync_tables()` description above for more
    context.
    """

    # Find the point when we stopped writing to the `sliding_sync_joined_rooms` table
    txn.execute(
        """
        SELECT event_stream_ordering
        FROM sliding_sync_joined_rooms
        ORDER BY event_stream_ordering DESC
        LIMIT 1
        """,
    )

    # If we have nothing written to the `sliding_sync_joined_rooms` table, there is
    # nothing to clean up
    row = cast(tuple[int] | None, txn.fetchone())
    max_stream_ordering_sliding_sync_joined_rooms_table = None
    depends_on = None
    if row is not None:
        (max_stream_ordering_sliding_sync_joined_rooms_table,) = row

        txn.execute(
            """
            SELECT room_id
            FROM events
            WHERE stream_ordering > ?
            GROUP BY room_id
            ORDER BY MAX(stream_ordering) ASC
            """,
            (max_stream_ordering_sliding_sync_joined_rooms_table,),
        )

        room_rows = txn.fetchall()
        # No new events have been written to the `events` table since the last time we wrote
        # to the `sliding_sync_joined_rooms` table so there is nothing to clean up. This is
        # the expected normal scenario for people who have not downgraded their Synapse
        # version.
        if not room_rows:
            return

        # 1000 is an arbitrary batch size with no testing
        for chunk in batch_iter(room_rows, 1000):
            # Handle updating the `sliding_sync_joined_rooms` table
            #
            # Clear out the stale data
            DatabasePool.simple_delete_many_batch_txn(
                txn,
                table="sliding_sync_joined_rooms",
                keys=("room_id",),
                values=chunk,
            )

            # Update the `sliding_sync_joined_rooms_to_recalculate` table with the rooms
            # that went stale and now need to be recalculated.
            DatabasePool.simple_upsert_many_txn_native_upsert(
                txn,
                table="sliding_sync_joined_rooms_to_recalculate",
                key_names=("room_id",),
                key_values=chunk,
                value_names=(),
                # No value columns, therefore make a blank list so that the following
                # zip() works correctly.
                value_values=[() for x in range(len(chunk))],
            )
    else:
        # Avoid adding the background updates when there is no data to run them on (if
        # the homeserver has no rooms). The portdb script refuses to run with pending
        # background updates and since we potentially add them every time the server
        # starts, we add this check for to allow the script to breath.
        txn.execute("SELECT 1 FROM local_current_membership LIMIT 1")
        row = txn.fetchone()
        if row is None:
            # There are no rooms, so don't schedule the bg update.
            return

        # Re-run the `sliding_sync_joined_rooms_to_recalculate` prefill if there is
        # nothing in the `sliding_sync_joined_rooms` table
        DatabasePool.simple_upsert_txn_native_upsert(
            txn,
            table="background_updates",
            keyvalues={
                "update_name": _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE
            },
            values={},
            # Only insert the row if it doesn't already exist. If it already exists,
            # we're already working on it
            insertion_values={
                "progress_json": "{}",
            },
        )
        depends_on = _BackgroundUpdates.SLIDING_SYNC_PREFILL_JOINED_ROOMS_TO_RECALCULATE_TABLE_BG_UPDATE

    # Now kick-off the background update to catch-up with what we missed while Synapse
    # was downgraded.
    #
    # We may need to catch-up on everything if we have nothing written to the
    # `sliding_sync_joined_rooms` table yet. This could happen if someone had zero rooms
    # on their server (so the normal background update completes), downgrade Synapse
    # versions, join and create some new rooms, and upgrade again.
    DatabasePool.simple_upsert_txn_native_upsert(
        txn,
        table="background_updates",
        keyvalues={
            "update_name": _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BG_UPDATE
        },
        values={},
        # Only insert the row if it doesn't already exist. If it already exists, we will
        # eventually fill in the rows we're trying to populate.
        insertion_values={
            # Empty progress is expected since it's not used for this background update.
            "progress_json": "{}",
            # Wait for the prefill to finish
            "depends_on": depends_on,
        },
    )


def _resolve_stale_data_in_sliding_sync_membership_snapshots_table(
    txn: LoggingTransaction,
) -> None:
    """
    Clears stale/out-of-date entries from the `sliding_sync_membership_snapshots` table
    and kicks-off the background update to catch-up with what we missed while Synapse
    was downgraded.

    See `_resolve_stale_data_in_sliding_sync_tables()` description above for more
    context.
    """

    # Find the point when we stopped writing to the `sliding_sync_membership_snapshots` table
    txn.execute(
        """
        SELECT event_stream_ordering
        FROM sliding_sync_membership_snapshots
        ORDER BY event_stream_ordering DESC
        LIMIT 1
        """,
    )

    # If we have nothing written to the `sliding_sync_membership_snapshots` table,
    # there is nothing to clean up
    row = cast(tuple[int] | None, txn.fetchone())
    max_stream_ordering_sliding_sync_membership_snapshots_table = None
    if row is not None:
        (max_stream_ordering_sliding_sync_membership_snapshots_table,) = row

        # XXX: Since `forgotten` is simply a flag on the `room_memberships` table that is
        # set out-of-band, there is no way to tell whether it was set while Synapse was
        # downgraded. The only thing the user can do is `/forget` again if they run into
        # this.
        #
        # This only picks up changes to memberships.
        txn.execute(
            """
            SELECT user_id, room_id
            FROM local_current_membership
            WHERE event_stream_ordering > ?
            ORDER BY event_stream_ordering ASC
            """,
            (max_stream_ordering_sliding_sync_membership_snapshots_table,),
        )

        membership_rows = txn.fetchall()
        # No new events have been written to the `events` table since the last time we wrote
        # to the `sliding_sync_membership_snapshots` table so there is nothing to clean up.
        # This is the expected normal scenario for people who have not downgraded their
        # Synapse version.
        if not membership_rows:
            return

        # 1000 is an arbitrary batch size with no testing
        for chunk in batch_iter(membership_rows, 1000):
            # Handle updating the `sliding_sync_membership_snapshots` table
            #
            DatabasePool.simple_delete_many_batch_txn(
                txn,
                table="sliding_sync_membership_snapshots",
                keys=("user_id", "room_id"),
                values=chunk,
            )
    else:
        # Avoid adding the background updates when there is no data to run them on (if
        # the homeserver has no rooms). The portdb script refuses to run with pending
        # background updates and since we potentially add them every time the server
        # starts, we add this check for to allow the script to breath.
        txn.execute("SELECT 1 FROM local_current_membership LIMIT 1")
        row = txn.fetchone()
        if row is None:
            # There are no rooms, so don't schedule the bg update.
            return

    # Now kick-off the background update to catch-up with what we missed while Synapse
    # was downgraded.
    #
    # We may need to catch-up on everything if we have nothing written to the
    # `sliding_sync_membership_snapshots` table yet. This could happen if someone had
    # zero rooms on their server (so the normal background update completes), downgrade
    # Synapse versions, join and create some new rooms, and upgrade again.
    #
    progress_json: JsonDict = {}
    if max_stream_ordering_sliding_sync_membership_snapshots_table is not None:
        progress_json["initial_phase"] = False
        progress_json["last_event_stream_ordering"] = (
            max_stream_ordering_sliding_sync_membership_snapshots_table
        )

    DatabasePool.simple_upsert_txn_native_upsert(
        txn,
        table="background_updates",
        keyvalues={
            "update_name": _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BG_UPDATE
        },
        values={},
        # Only insert the row if it doesn't already exist. If it already exists, we will
        # eventually fill in the rows we're trying to populate.
        insertion_values={
            "progress_json": json_encoder.encode(progress_json),
        },
    )
