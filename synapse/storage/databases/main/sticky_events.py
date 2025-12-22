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
from typing import (
    TYPE_CHECKING,
    cast,
)

from twisted.internet.defer import Deferred

from synapse.events import EventBase
from synapse.replication.tcp.streams._base import StickyEventsStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.databases.main.state import StateGroupWorkerStore
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# Remove entries from the sticky_events table at this frequency.
# Note: this does NOT mean we don't honour shorter expiration timeouts.
# Consumers call 'get_sticky_events_in_rooms' which has `WHERE expires_at > ?`
# to filter out expired sticky events that have yet to be deleted.
DELETE_EXPIRED_STICKY_EVENTS_INTERVAL = Duration(hours=1)


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
            self.clock.looping_call(
                self._run_background_cleanup, DELETE_EXPIRED_STICKY_EVENTS_INTERVAL
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

    async def get_updated_sticky_events(
        self, from_id: int, to_id: int, limit: int
    ) -> list[tuple[int, str, str, bool]]:
        """Get updates to sticky events between two stream IDs.

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of (stream_id, room_id, event_id, soft_failed) tuples
        """
        return await self.db_pool.runInteraction(
            "get_updated_sticky_events",
            self._get_updated_sticky_events_txn,
            from_id,
            to_id,
            limit,
        )

    def _get_updated_sticky_events_txn(
        self, txn: LoggingTransaction, from_id: int, to_id: int, limit: int
    ) -> list[tuple[int, str, str, bool]]:
        txn.execute(
            """
            SELECT stream_id, room_id, event_id, soft_failed
            FROM sticky_events
            WHERE ? < stream_id AND stream_id <= ?
            LIMIT ?
            """,
            (from_id, to_id, limit),
        )
        return cast(list[tuple[int, str, str, bool]], txn.fetchall())

    def insert_sticky_events_txn(
        self,
        txn: LoggingTransaction,
        events: list[EventBase],
    ) -> None:
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
            expires_at = min(ev.origin_server_ts, now_ms) + sticky_duration
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
                "soft_failed",
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
                    ev.internal_metadata.is_soft_failed(),
                )
                for (ev, expires_at), stream_id in sticky_events_with_ids
            ],
        )

    async def _delete_expired_sticky_events(self) -> None:
        logger.info("delete_expired_sticky_events")
        await self.db_pool.runInteraction(
            "_delete_expired_sticky_events",
            self._delete_expired_sticky_events_txn,
            self.clock.time_msec(),
        )

    def _delete_expired_sticky_events_txn(
        self, txn: LoggingTransaction, now: int
    ) -> None:
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
