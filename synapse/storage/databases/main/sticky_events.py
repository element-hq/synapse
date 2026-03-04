#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Collection, cast

from twisted.internet.defer import Deferred

from synapse.events import EventBase
from synapse.replication.tcp.streams._base import StickyEventsStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.storage.engines import PostgresEngine, Sqlite3Engine
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

DELETE_EXPIRED_STICKY_EVENTS_INTERVAL = Duration(hours=1)
"""
Remove entries from the sticky_events table at this frequency.
Note: don't be misled, we still honour shorter expiration timeouts,
because readers of the sticky_events table filter out expired sticky events
themselves, even if they aren't deleted from the table yet.

Currently just an arbitrary choice.
Frequent enough to clean up expired sticky events promptly,
especially given the short cap on the lifetime of sticky events.
"""


@dataclass(frozen=True)
class StickyEventUpdate:
    stream_id: int
    room_id: str
    event_id: str
    soft_failed: bool


class StickyEventsWorkerStore(StateGroupWorkerStore, CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._can_write_to_sticky_events = (
            self._instance_name in hs.config.worker.writers.events
        )

        # Technically this means we will cleanup N times, once per event persister, maybe put on master?
        if self._can_write_to_sticky_events:
            # Start a looping call to clean up the `sticky_events` table
            #
            # Because this will run once per event persister (for now),
            # randomly stagger the initial time so that they don't all
            # coincide with each other if the workers are deployed at the
            # same time. This allows each cleanup to be somewhat more effective
            # than if they all started at the same time, as they would all be
            # cleaning up the same thing whereas each worker gets to clean up a little
            # throughout the hour when they're staggered.
            #
            # Concurrent execution of the same deletions could also lead to
            # repeatable serialisation violations in the database transaction,
            # meaning we'd have to retry the transaction several times.
            #
            # This staggering is not critical, it's just best-effort.
            self.clock.call_later(
                # random() is 0.0 to 1.0
                DELETE_EXPIRED_STICKY_EVENTS_INTERVAL * random.random(),
                self.clock.looping_call,
                self._run_background_cleanup,
                DELETE_EXPIRED_STICKY_EVENTS_INTERVAL,
            )

        self._sticky_events_id_gen: MultiWriterIdGenerator = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="sticky_events",
            server_name=self.server_name,
            instance_name=self._instance_name,
            tables=[
                ("sticky_events", "instance_name", "stream_id"),
            ],
            sequence_name="sticky_events_sequence",
            writers=hs.config.worker.writers.events,
        )

        if hs.config.experimental.msc4354_enabled and isinstance(
            self.database_engine, Sqlite3Engine
        ):
            import sqlite3

            if sqlite3.sqlite_version_info < (3, 40, 0):
                raise RuntimeError(
                    f"Experimental MSC4354 Sticky Events enabled but SQLite3 version is too old: {sqlite3.sqlite_version_info}, must be at least 3.40. Disable MSC4354 Sticky Events, switch to Postgres, or upgrade SQLite. See https://github.com/element-hq/synapse/issues/19428"
                )

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == StickyEventsStream.NAME:
            self._sticky_events_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def get_max_sticky_events_stream_id(self) -> int:
        """Get the current maximum stream_id for thread subscriptions.

        Returns:
            The maximum stream_id
        """
        return self._sticky_events_id_gen.get_current_token()

    def get_sticky_events_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._sticky_events_id_gen

    async def get_sticky_events_in_rooms(
        self,
        room_ids: Collection[str],
        *,
        from_id: int,
        to_id: int,
        now: int,
        limit: int | None,
    ) -> tuple[int, dict[str, list[str]]]:
        """
        Fetch all the sticky events' IDs in the given rooms, with sticky stream IDs satisfying
        from_id < sticky stream ID <= to_id.

        The events are returned ordered by the sticky events stream.

        Args:
            room_ids: The room IDs to return sticky events in.
            from_id: The sticky stream ID that sticky events should be returned from (exclusive).
            to_id: The sticky stream ID that sticky events should end at (inclusive).
            now: The current time in unix millis, used for skipping expired events.
            limit: Max sticky events to return, or None to apply no limit.
        Returns:
            to_id, dict[room_id, list[event_ids]]
        """
        sticky_events_rows = await self.db_pool.runInteraction(
            "get_sticky_events_in_rooms",
            self._get_sticky_events_in_rooms_txn,
            room_ids,
            from_id,
            to_id,
            now,
            limit,
        )

        if not sticky_events_rows:
            return to_id, {}

        # Get stream_id of the last row, which is the highest
        new_to_id, _, _ = sticky_events_rows[-1]

        # room ID -> event IDs
        room_id_to_event_ids: dict[str, list[str]] = {}
        for _, room_id, event_id in sticky_events_rows:
            events = room_id_to_event_ids.setdefault(room_id, [])
            events.append(event_id)

        return (new_to_id, room_id_to_event_ids)

    def _get_sticky_events_in_rooms_txn(
        self,
        txn: LoggingTransaction,
        room_ids: Collection[str],
        from_id: int,
        to_id: int,
        now: int,
        limit: int | None,
    ) -> list[tuple[int, str, str]]:
        if len(room_ids) == 0:
            return []
        room_id_in_list_clause, room_id_in_list_values = make_in_list_sql_clause(
            txn.database_engine, "se.room_id", room_ids
        )
        limit_clause = ""
        limit_params: tuple[int, ...] = ()
        if limit is not None:
            limit_clause = "LIMIT ?"
            limit_params = (limit,)

        if isinstance(self.database_engine, PostgresEngine):
            expr_soft_failed = "COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)"
        else:
            expr_soft_failed = "COALESCE(ej.internal_metadata->>'soft_failed', FALSE)"

        txn.execute(
            f"""
            SELECT se.stream_id, se.room_id, event_id
            FROM sticky_events se
            INNER JOIN event_json ej USING (event_id)
            WHERE
                NOT {expr_soft_failed}
                AND ? < expires_at
                AND ? < stream_id
                AND stream_id <= ?
                AND {room_id_in_list_clause}
            ORDER BY stream_id ASC
            {limit_clause}
            """,
            (now, from_id, to_id, *room_id_in_list_values, *limit_params),
        )
        return cast(list[tuple[int, str, str]], txn.fetchall())

    async def get_updated_sticky_events(
        self, *, from_id: int, to_id: int, limit: int
    ) -> list[StickyEventUpdate]:
        """Get updates to sticky events between two stream IDs.

        Bounds: from_id < ... <= to_id

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of StickyEventUpdate update rows
        """

        if not self.hs.config.experimental.msc4354_enabled:
            # We need to prevent `_get_updated_sticky_events_txn`
            # from running when MSC4354 is turned off, because the query used
            # for SQLite is not compatible with Ubuntu 22.04 (as used in our CI olddeps run).
            # It's technically out of support.
            # See: https://github.com/element-hq/synapse/issues/19428
            return []

        return await self.db_pool.runInteraction(
            "get_updated_sticky_events",
            self._get_updated_sticky_events_txn,
            from_id,
            to_id,
            limit,
        )

    def _get_updated_sticky_events_txn(
        self, txn: LoggingTransaction, from_id: int, to_id: int, limit: int
    ) -> list[StickyEventUpdate]:
        if isinstance(self.database_engine, PostgresEngine):
            expr_soft_failed = "COALESCE(((ej.internal_metadata::jsonb)->>'soft_failed')::boolean, FALSE)"
        else:
            expr_soft_failed = "COALESCE(ej.internal_metadata->>'soft_failed', FALSE)"

        txn.execute(
            f"""
            SELECT se.stream_id, se.room_id, se.event_id,
            {expr_soft_failed} AS "soft_failed"
            FROM sticky_events se
            INNER JOIN event_json ej USING (event_id)
            WHERE ? < stream_id AND stream_id <= ?
            LIMIT ?
            """,
            (from_id, to_id, limit),
        )

        return [
            StickyEventUpdate(
                stream_id=stream_id,
                room_id=room_id,
                event_id=event_id,
                soft_failed=bool(soft_failed),
            )
            for stream_id, room_id, event_id, soft_failed in txn
        ]

    def insert_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        events: list[EventBase],
    ) -> None:
        """
        Insert events into the sticky_events table.

        Skips inserting events:
            - if they are considered spammy by the policy server;
              (unsure if correct, track: https://github.com/matrix-org/matrix-spec-proposals/pull/4354#discussion_r2727593350)
            - if they are rejected;
            - if they are outliers (they should be reconsidered for insertion when de-outliered); or
            - if they are not sticky (e.g. if the stickiness expired).

        Skipping the insertion of these types of 'invalid' events is useful for performance reasons because
        they would fill up the table yet we wouldn't show them to clients anyway.

        Since syncing clients can't (easily?) 'skip over' sticky events (due to being in-order, reliably delivered),
        tracking loads of invalid events in the table could make it expensive for servers to retrieve the sticky events that are actually valid.

        For instance, someone spamming 1000s of rejected or 'policy_server_spammy' events could clog up this table in a way that means we either
        have to deliver empty payloads to syncing clients, or consider substantially more than 100 events in order to gather a 100-sized batch to send down.
        """

        now_ms = self.clock.time_msec()
        # event, expires_at
        sticky_events: list[tuple[EventBase, int]] = []
        for ev in events:
            # MSC: Note: policy servers and other similar antispam techniques still apply to these events.
            if ev.internal_metadata.policy_server_spammy:
                continue
            # We shouldn't be passed rejected events, but if we do, we filter them out too.
            if ev.rejected_reason is not None:
                continue
            # We can't persist outlier sticky events as we don't know the room state at that event
            if ev.internal_metadata.is_outlier():
                continue
            sticky_duration = ev.sticky_duration()
            if sticky_duration is None:
                continue
            # Calculate the end time as start_time + effecitve sticky duration
            expires_at = min(ev.origin_server_ts, now_ms) + sticky_duration.as_millis()
            # Filter out already expired sticky events
            if expires_at <= now_ms:
                continue

            sticky_events.append((ev, expires_at))

        if len(sticky_events) == 0:
            return

        logger.info(
            "inserting %d sticky events in room %s",
            len(sticky_events),
            sticky_events[0][0].room_id,
        )

        # Generate stream_ids in one go
        sticky_events_with_ids = zip(
            sticky_events,
            self._sticky_events_id_gen.get_next_mult_txn(txn, len(sticky_events)),
            strict=True,
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            "sticky_events",
            keys=(
                "instance_name",
                "stream_id",
                "room_id",
                "event_id",
                "event_stream_ordering",
                "sender",
                "expires_at",
            ),
            values=[
                (
                    self._instance_name,
                    stream_id,
                    ev.room_id,
                    ev.event_id,
                    ev.internal_metadata.stream_ordering,
                    ev.sender,
                    expires_at,
                )
                for (ev, expires_at), stream_id in sticky_events_with_ids
            ],
        )

    async def _delete_expired_sticky_events(self) -> None:
        await self.db_pool.runInteraction(
            "_delete_expired_sticky_events",
            self._delete_expired_sticky_events_txn,
            self.clock.time_msec(),
        )

    def _delete_expired_sticky_events_txn(
        self, txn: LoggingTransaction, now: int
    ) -> None:
        """
        From the `sticky_events` table, deletes all entries whose expiry is in the past
        (older than `now`).

        This is fine because we don't consider the events as sticky anymore when that's
        happened.
        """
        txn.execute(
            """
            DELETE FROM sticky_events WHERE expires_at < ?
            """,
            (now,),
        )

    def _run_background_cleanup(self) -> Deferred:
        return self.hs.run_as_background_process(
            "delete_expired_sticky_events",
            self._delete_expired_sticky_events,
        )
