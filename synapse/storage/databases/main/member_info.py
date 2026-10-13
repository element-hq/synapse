#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""MSC4428: the stream of `member_info` changes with no event anchor.

See `synapse.handlers.member_info` for what the map is. Most changes to it are
delivered alongside the event that caused them; the ones that are not (erasure
today, link verification once MSC4430 lands) are recorded here, on a stream of
their own, so that a syncing client is woken and told.

Any worker may write to the stream: it is modelled on the cache invalidation
stream rather than on a stream with configured writers.
"""

import logging
from typing import TYPE_CHECKING, Collection, Mapping, cast

import attr

from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.replication.tcp.streams._base import MemberInfoChangesStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.util.id_generators import (
    MultiWriterIdGenerator,
    make_multiwriter_sharded_token_bounds_sql,
)
from synapse.types import MultiWriterStreamToken
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

MEMBER_INFO_CHANGES_RETENTION = Duration(days=30)
"""How long a change row is kept. A client syncing with a token older than this
is sent the full `member_info` map instead of the deltas."""

MEMBER_INFO_CHANGES_PRUNE_INTERVAL = Duration(hours=1)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class MemberInfoChange:
    stream_id: int
    room_id: str
    stable_id: str


class MemberInfoWorkerStore(CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._member_info_changes_id_gen: MultiWriterIdGenerator = (
            MultiWriterIdGenerator(
                db_conn=db_conn,
                db=database,
                notifier=hs.get_replication_notifier(),
                stream_name="member_info_changes",
                server_name=self.server_name,
                instance_name=self._instance_name,
                tables=[
                    ("msc4428_member_info_changes", "instance_name", "stream_id"),
                ],
                sequence_name="msc4428_member_info_changes_sequence",
                # Any instance may write to this stream; positions are learnt
                # from the table (see the module docstring).
                writers=[],
            )
        )

        if (
            hs.config.worker.run_background_tasks
            and hs.config.experimental.msc4428_enabled
        ):
            self.hs.get_clock().looping_call(
                self._prune_old_member_info_changes,
                MEMBER_INFO_CHANGES_PRUNE_INTERVAL,
            )

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == MemberInfoChangesStream.NAME:
            self._member_info_changes_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    def get_member_info_changes_stream_token(self) -> MultiWriterStreamToken:
        """The current position of the member info changes stream."""
        return MultiWriterStreamToken.from_generator(self._member_info_changes_id_gen)

    def get_member_info_changes_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._member_info_changes_id_gen

    async def get_rooms_with_membership_for_local_user(self, user_id: str) -> list[str]:
        """Every room the local user has any membership state in, including
        rooms they have left, been banned from, or forgotten."""
        return await self.db_pool.simple_select_onecol(
            table="local_current_membership",
            keyvalues={"user_id": user_id},
            retcol="room_id",
            desc="get_rooms_with_membership_for_local_user",
        )

    async def add_member_info_changes(
        self, changes: Mapping[str, Collection[str]]
    ) -> MultiWriterStreamToken:
        """Record that the `member_info` entries of the given stable IDs changed.

        Args:
            changes: room ID -> the stable IDs whose entry changed in that room.

        Returns:
            The stream position after the change, to wake syncing clients with.
        """
        rows = [
            (room_id, stable_id)
            for room_id, stable_ids in changes.items()
            for stable_id in stable_ids
        ]
        if not rows:
            return self.get_member_info_changes_stream_token()

        now = self.hs.get_clock().time_msec()
        async with self._member_info_changes_id_gen.get_next_mult(
            len(rows)
        ) as stream_ids:
            await self.db_pool.simple_insert_many(
                table="msc4428_member_info_changes",
                keys=(
                    "stream_id",
                    "instance_name",
                    "room_id",
                    "stable_id",
                    "changed_ts",
                ),
                values=[
                    (stream_id, self._instance_name, room_id, stable_id, now)
                    for stream_id, (room_id, stable_id) in zip(stream_ids, rows)
                ],
                desc="add_member_info_changes",
            )

        return self.get_member_info_changes_stream_token()

    async def get_member_info_changes_in_rooms(
        self,
        room_ids: Collection[str],
        from_token: MultiWriterStreamToken,
        to_token: MultiWriterStreamToken,
    ) -> dict[str, set[str]] | None:
        """The stable IDs whose `member_info` entry changed in the given rooms,
        with stream positions after `from_token` and at or before `to_token`.

        Returns:
            room ID -> changed stable IDs (only rooms with changes), or None if
            `from_token` is older than the retained rows, in which case the
            caller cannot be given deltas and must send the full map.
        """
        if not room_ids:
            return {}

        def _get_member_info_changes_in_rooms_txn(
            txn: LoggingTransaction,
        ) -> dict[str, set[str]] | None:
            max_pruned_stream_id = self._get_max_pruned_member_info_changes_txn(txn)
            if max_pruned_stream_id > from_token.stream:
                return None

            # The client may hand us a token ahead of our own position if this
            # worker is lagging.
            if to_token.is_before_or_eq(from_token):
                return {}

            stream_clause, stream_values = make_multiwriter_sharded_token_bounds_sql(
                self.database_engine,
                stream_id_column="stream_id",
                instance_name_column="instance_name",
                from_token_exclusive=from_token,
                to_token_inclusive=to_token,
            )
            # Rows in the range are few (one per member per anchorless change),
            # so filter by room here rather than with a long `IN` list.
            txn.execute(
                f"""
                SELECT room_id, stable_id FROM msc4428_member_info_changes
                WHERE {stream_clause}
                """,
                stream_values,
            )

            changes: dict[str, set[str]] = {}
            wanted = (
                room_ids if isinstance(room_ids, (set, frozenset)) else set(room_ids)
            )
            for room_id, stable_id in txn:
                if room_id in wanted:
                    changes.setdefault(room_id, set()).add(stable_id)
            return changes

        return await self.db_pool.runInteraction(
            "get_member_info_changes_in_rooms", _get_member_info_changes_in_rooms_txn
        )

    def _get_max_pruned_member_info_changes_txn(self, txn: LoggingTransaction) -> int:
        """The highest stream ID pruned from the change table. Ranges starting
        at or before it cannot be answered completely."""
        return self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="msc4428_member_info_changes_max_pruned_stream_id",
            keyvalues={},
            retcol="stream_id",
        )

    async def get_updated_member_info_changes(
        self, from_id: int, to_id: int, limit: int
    ) -> list[MemberInfoChange]:
        """Rows with `from_id < stream_id <= to_id`, for replication. Prefer
        `get_member_info_changes_in_rooms`, which understands sharded tokens."""
        rows = cast(
            list[tuple[int, str, str]],
            await self.db_pool.execute(
                "get_updated_member_info_changes",
                """
                SELECT stream_id, room_id, stable_id FROM msc4428_member_info_changes
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
                """,
                from_id,
                to_id,
                limit,
            ),
        )
        return [MemberInfoChange(*row) for row in rows]

    @wrap_as_background_process("prune_old_member_info_changes")
    async def _prune_old_member_info_changes(self) -> None:
        await self.prune_member_info_changes_before(
            self.hs.get_clock().time_msec() - MEMBER_INFO_CHANGES_RETENTION.as_millis()
        )

    async def prune_member_info_changes_before(self, cutoff_ts: int) -> None:
        """Delete change rows recorded before `cutoff_ts` and record the prune
        point, so that syncs from older tokens fall back to the full map."""

        def _prune_txn(txn: LoggingTransaction) -> None:
            txn.execute(
                "SELECT MAX(stream_id) FROM msc4428_member_info_changes WHERE changed_ts < ?",
                (cutoff_ts,),
            )
            row = txn.fetchone()
            if row is None or row[0] is None:
                return
            max_pruned_stream_id: int = row[0]

            txn.execute(
                "DELETE FROM msc4428_member_info_changes WHERE changed_ts < ?",
                (cutoff_ts,),
            )
            # Positions only ever advance, whichever worker's clock was ahead.
            txn.execute(
                """
                UPDATE msc4428_member_info_changes_max_pruned_stream_id
                SET stream_id = ? WHERE stream_id < ?
                """,
                (max_pruned_stream_id, max_pruned_stream_id),
            )

        await self.db_pool.runInteraction("prune_member_info_changes", _prune_txn)
